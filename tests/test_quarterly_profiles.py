from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import event, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from model_class.base import Base


@pytest_asyncio.fixture
async def session():
    import model_class.job_competency  # noqa: F401
    import model_class.knowledge_base  # noqa: F401

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as value:
        yield value
    await engine.dispose()


async def _pipeline_run(session):
    from model_class.knowledge_base import PipelineRun

    run = PipelineRun(
        run_id=f"run-{id(session)}-{datetime.now().timestamp()}",
        mode="full",
        rule_version="competition-gate-v1",
        status="running",
    )
    session.add(run)
    await session.flush()
    return run


async def _postings(
    session,
    count,
    *,
    quarter="2026-Q1",
    level="mid",
    gate_status="valid",
    duplicate=False,
    published_at_trusted=True,
):
    from model_class.job_competency import JobPosting, JobPostingSkill, Skill

    year, quarter_number = quarter.split("-Q")
    month = (int(quarter_number) - 1) * 3 + 1
    skill = await session.scalar(select(Skill).where(Skill.name == "Python"))
    if skill is None:
        skill = Skill(name="Python", category="language", aliases_json="[]")
        session.add(skill)
        await session.flush()
    start_index = await session.scalar(select(func.count()).select_from(JobPosting)) or 0
    created = []
    for index in range(count):
        unique_index = start_index + index
        posting = JobPosting(
            record_id=f"{quarter}-{level}-{gate_status}-{unique_index}-{id(session)}",
            job_family_id="DATA_ENGINEER",
            job_title_raw="数据工程师",
            job_title_normalized="数据工程师",
            company_name=f"企业{index}",
            industry="软件和信息技术服务业",
            region="北京",
            source_name=f"企业官网{index % 3}",
            source_type="company_official",
            source_url=f"https://example.com/jobs/{quarter}/{unique_index}",
            provenance_status="approved",
            published_at=datetime(int(year), month, min(index + 1, 28)),
            published_at_trusted=published_at_trusted,
            collected_at=datetime(2026, 7, 23),
            first_seen_at=datetime(2026, 7, 1),
            experience_requirement="3-5年",
            education_requirement="本科",
            salary_range="20-30K",
            job_description_raw=(
                "负责数据平台开发、部署和维护，要求熟悉Python与实时计算，"
                f"持续优化第{index}类数据任务的质量、性能和交付流程。"
            ),
            content_hash=f"hash-{quarter}-{level}-{unique_index}-{id(session)}",
            simhash=f"{unique_index + 1000:016x}",
            source_score=0.95,
            quality_score=0.9,
            status="valid",
            gate_status=gate_status,
            gate_rule_version="competition-gate-v1",
            machine_level=level,
            machine_level_confidence=0.85,
        )
        session.add(posting)
        await session.flush()
        if duplicate and index == count - 1:
            posting.duplicate_of_id = created[0].id
        session.add(
            JobPostingSkill(
                job_posting_id=posting.id,
                skill_id=skill.id,
                requirement_type="required",
                confidence=0.9,
                evidence_text="熟悉Python",
            )
        )
        created.append(posting)
    await session.flush()
    return created


@pytest.mark.asyncio
async def test_nine_records_do_not_create_quarterly_profile(session):
    from src.quarterly_profile_service import rebuild_quarterly_profiles

    await _postings(session, 9)
    run = await _pipeline_run(session)

    result = await rebuild_quarterly_profiles(session, pipeline_run_id=run.id)

    assert result["profiles_created"] == 0
    assert result["insufficient"] == 1


@pytest.mark.asyncio
async def test_ten_records_create_low_sample_profile(session):
    from model_class.job_competency import JobProfile
    from src.quarterly_profile_service import rebuild_quarterly_profiles

    await _postings(session, 10)
    run = await _pipeline_run(session)
    result = await rebuild_quarterly_profiles(session, pipeline_run_id=run.id)
    profile = await session.scalar(
        select(JobProfile).where(JobProfile.profile_kind == "quarterly")
    )

    assert result["profiles_created"] == 1
    assert profile is not None
    assert profile.level == "mid"
    assert profile.period_key == "2026-Q1"
    assert profile.sample_count == 10
    assert profile.sample_status == "low_sample"


@pytest.mark.asyncio
async def test_twenty_records_create_ready_profile_with_canonical_children(session):
    from model_class.job_competency import JobProfile, JobProfileSkill
    from model_class.knowledge_base import JobProfileScenario, JobProfileSnapshot
    from src.quarterly_profile_service import rebuild_quarterly_profiles

    await _postings(session, 20)
    run = await _pipeline_run(session)
    await rebuild_quarterly_profiles(session, pipeline_run_id=run.id)
    profile = await session.scalar(
        select(JobProfile).where(JobProfile.profile_kind == "quarterly")
    )

    assert profile is not None and profile.sample_status == "ready"
    assert await session.scalar(
        select(func.count())
        .select_from(JobProfileSkill)
        .where(JobProfileSkill.job_profile_id == profile.id)
    ) == 1
    assert await session.scalar(
        select(func.count())
        .select_from(JobProfileScenario)
        .where(JobProfileScenario.job_profile_id == profile.id)
    ) == 1
    assert await session.scalar(
        select(func.count())
        .select_from(JobProfileSnapshot)
        .where(JobProfileSnapshot.job_profile_id == profile.id)
    ) == 1

    link = await session.scalar(
        select(JobProfileSkill).where(JobProfileSkill.job_profile_id == profile.id)
    )
    snapshot = await session.scalar(
        select(JobProfileSnapshot).where(JobProfileSnapshot.job_profile_id == profile.id)
    )
    payload = __import__("json").loads(snapshot.payload_json)
    assert link.evidence_count == 20
    assert link.source_type_count == 1
    assert link.source_domain_count == 0
    assert link.company_count == 20
    assert link.required_ratio == 1.0
    assert link.preferred_ratio == 0.0
    assert link.cross_source_status == "single_source"
    assert link.confidence < 0.7
    assert link.first_published_at == datetime(2026, 1, 1)
    assert link.last_published_at == datetime(2026, 1, 20)
    assert payload["profile_rule_version"] == "competition-profile-v3"
    assert payload["skill_evidence_rule_version"] == "cross-source-skill-v1"
    assert payload["temporal_basis"]["quarter_assignment"] == "published_at"
    assert payload["temporal_basis"]["publication_trust_required"] is True
    assert payload["temporal_basis"]["observation_affects_profile"] is False


@pytest.mark.asyncio
async def test_quarterly_rebuild_is_idempotent_and_preserves_legacy_profile(session):
    from model_class.job_competency import JobProfile, JobProfileSkill
    from src.quarterly_profile_service import rebuild_quarterly_profiles

    legacy = JobProfile(
        family_code="DATA_ENGINEER",
        name="数据工程师",
        description="旧画像",
        status="existing",
        level="all",
        tech_stack="big_data",
        version=1,
        confidence=0.5,
        review_status="pending",
    )
    session.add(legacy)
    await _postings(session, 20)
    run = await _pipeline_run(session)

    first = await rebuild_quarterly_profiles(session, pipeline_run_id=run.id)
    profile = await session.scalar(
        select(JobProfile).where(JobProfile.profile_kind == "quarterly")
    )
    first_id = profile.id
    first_version = profile.version
    second = await rebuild_quarterly_profiles(session, pipeline_run_id=run.id)

    assert first["profiles_created"] == 1
    assert second["profiles_created"] == 0
    assert second["profiles_updated"] == 1
    assert profile.id == first_id
    assert profile.version == first_version == 2
    assert legacy.profile_kind == "legacy"
    assert await session.scalar(select(func.count()).select_from(JobProfile)) == 2
    assert await session.scalar(select(func.count()).select_from(JobProfileSkill)) == 1


@pytest.mark.asyncio
async def test_nonvalid_duplicate_and_undated_postings_are_excluded(session):
    from src.quarterly_profile_service import rebuild_quarterly_profiles

    await _postings(session, 9)
    await _postings(session, 2, gate_status="review")
    duplicates = await _postings(session, 2, duplicate=True)
    duplicates[0].gate_status = "review"
    undated = await _postings(session, 1)
    undated[0].published_at = None
    run = await _pipeline_run(session)

    result = await rebuild_quarterly_profiles(session, pipeline_run_id=run.id)

    assert result["profiles_created"] == 0
    assert result["insufficient"] == 1


@pytest.mark.asyncio
async def test_untrusted_publication_date_is_excluded_from_quarterly_profile(session):
    from src.quarterly_profile_service import rebuild_quarterly_profiles

    await _postings(session, 9)
    await _postings(session, 3, published_at_trusted=False)
    run = await _pipeline_run(session)

    result = await rebuild_quarterly_profiles(session, pipeline_run_id=run.id)

    assert result["profiles_created"] == 0
    assert result["insufficient"] == 1


@pytest.mark.asyncio
async def test_stale_quarterly_profile_is_superseded_when_trusted_inputs_shrink(session):
    from model_class.job_competency import JobProfile
    from src.quarterly_profile_service import rebuild_quarterly_profiles

    postings = await _postings(session, 10)
    run = await _pipeline_run(session)
    await rebuild_quarterly_profiles(session, pipeline_run_id=run.id)
    old_profile = await session.scalar(
        select(JobProfile).where(JobProfile.derivation_status == "active")
    )

    postings[0].published_at_trusted = False
    result = await rebuild_quarterly_profiles(session, pipeline_run_id=run.id)
    active = await session.scalar(
        select(JobProfile).where(JobProfile.derivation_status == "active")
    )

    assert result["profiles_superseded"] == 1
    assert old_profile.derivation_status == "superseded"
    assert active is None


@pytest.mark.asyncio
async def test_profile_rebuild_reconciles_formal_events_after_threshold_drop(session):
    from model_class.job_competency import JobProfile
    from model_class.knowledge_base import EvolutionEvent
    from src.quarterly_profile_service import rebuild_quarterly_profiles

    postings = await _postings(session, 20)
    run = await _pipeline_run(session)
    await rebuild_quarterly_profiles(session, pipeline_run_id=run.id)
    profile = await session.scalar(
        select(JobProfile).where(JobProfile.derivation_status == "active")
    )
    evolution = EvolutionEvent(
        family_code=profile.family_code,
        previous_profile_id=profile.id,
        current_profile_id=profile.id,
        entity_type="skill",
        entity_key="Python",
        change_type="modified",
        event_status="formal",
        generation_key=f"threshold-drop-{id(session)}",
    )
    session.add(evolution)
    for posting in postings[:11]:
        posting.published_at_trusted = False

    await rebuild_quarterly_profiles(session, pipeline_run_id=run.id)

    assert profile.derivation_status == "superseded"
    assert evolution.event_status == "superseded"


@pytest.mark.asyncio
async def test_quarterly_input_signature_covers_material_inputs_not_observations(session):
    from model_class.job_competency import JobPostingSkill
    from model_class.knowledge_base import EvidenceSnippet
    from src.quarterly_profile_service import input_signature

    postings = await _postings(session, 1)
    posting = postings[0]
    link = await session.scalar(
        select(JobPostingSkill).where(JobPostingSkill.job_posting_id == posting.id)
    )
    snippet = EvidenceSnippet(
        evidence_key="responsibility-signature",
        job_posting_id=posting.id,
        entity_type="responsibility",
        entity_key="Build pipelines",
        evidence_text="Build pipelines",
        text_hash="responsibility-signature",
        confidence=0.8,
        review_status="approved",
    )
    session.add(snippet)
    await session.flush()
    before = await input_signature(session, postings)

    posting.first_seen_at = datetime(2025, 1, 1)
    posting.last_seen_at = datetime(2026, 8, 1)
    assert await input_signature(session, postings) == before

    posting.parser_version = "parser-v2"
    assert await input_signature(session, postings) != before
    posting.parser_version = None
    posting.industry = "Finance"
    assert await input_signature(session, postings) != before
    posting.industry = "杞欢鍜屼俊鎭妧鏈湇鍔′笟"
    posting.source_score = 0.8
    assert await input_signature(session, postings) != before
    posting.source_score = 0.95
    posting.quality_score = 0.8
    assert await input_signature(session, postings) != before
    posting.quality_score = 0.9
    posting.published_at_evidence = "Published date"
    assert await input_signature(session, postings) != before
    posting.published_at_evidence = None
    posting.published_at_confidence = 0.9
    assert await input_signature(session, postings) != before
    posting.published_at_confidence = 0.0
    link.requirement_type = "preferred"
    assert await input_signature(session, postings) != before
    link.requirement_type = "required"
    link.confidence = 0.7
    assert await input_signature(session, postings) != before
    link.confidence = 0.9
    snippet.entity_key = "Operate pipelines"
    assert await input_signature(session, postings) != before


@pytest.mark.asyncio
async def test_material_skill_change_supersedes_and_preserves_old_snapshot(session):
    from model_class.job_competency import JobPostingSkill, JobProfile
    from model_class.knowledge_base import JobProfileSnapshot
    from src.quarterly_profile_service import rebuild_quarterly_profiles

    postings = await _postings(session, 10)
    run = await _pipeline_run(session)
    await rebuild_quarterly_profiles(session, pipeline_run_id=run.id)
    old_profile = await session.scalar(
        select(JobProfile).where(JobProfile.derivation_status == "active")
    )
    old_snapshot_id = await session.scalar(
        select(JobProfileSnapshot.id).where(
            JobProfileSnapshot.job_profile_id == old_profile.id
        )
    )
    link = await session.scalar(
        select(JobPostingSkill).where(JobPostingSkill.job_posting_id == postings[0].id)
    )
    link.confidence = 0.5

    result = await rebuild_quarterly_profiles(session, pipeline_run_id=run.id)
    active = await session.scalar(
        select(JobProfile).where(JobProfile.derivation_status == "active")
    )

    assert result["profiles_superseded"] == 1
    assert old_profile.derivation_status == "superseded"
    assert active.id != old_profile.id
    assert await session.get(JobProfileSnapshot, old_snapshot_id) is not None
    assert await session.scalar(select(func.count()).select_from(JobProfileSnapshot)) == 2


@pytest.mark.asyncio
async def test_observation_only_change_does_not_supersede_quarterly_profile(session):
    from model_class.job_competency import JobProfile
    from model_class.knowledge_base import JobProfileSnapshot
    from src.quarterly_profile_service import rebuild_quarterly_profiles

    postings = await _postings(session, 10)
    run = await _pipeline_run(session)
    await rebuild_quarterly_profiles(session, pipeline_run_id=run.id)
    profile = await session.scalar(
        select(JobProfile).where(JobProfile.derivation_status == "active")
    )
    signature = profile.input_signature

    postings[0].first_seen_at = datetime(2025, 1, 1)
    postings[0].last_seen_at = datetime(2026, 8, 1)
    result = await rebuild_quarterly_profiles(session, pipeline_run_id=run.id)
    active = await session.scalar(
        select(JobProfile).where(JobProfile.derivation_status == "active")
    )

    assert result["profiles_superseded"] == 0
    assert active.id == profile.id
    assert active.input_signature == signature
    assert await session.scalar(select(func.count()).select_from(JobProfileSnapshot)) == 1


@pytest.mark.asyncio
async def test_quarterly_rebuild_uses_bounded_queries_for_5000_posting_slice(session):
    from model_class.job_competency import JobPosting, JobPostingSkill, Skill
    from model_class.knowledge_base import EvidenceSnippet
    from src.quarterly_profile_service import rebuild_quarterly_profiles

    skill = Skill(name="Python", category="language", aliases_json="[]")
    session.add(skill)
    await session.flush()
    posting_rows = [
        {
            "record_id": f"QUARTER-SCALE-{index}",
            "job_family_id": "DATA_ENGINEER",
            "job_title_raw": "Data Engineer",
            "job_title_normalized": "Data Engineer",
            "company_name": f"Company {index % 100}",
            "industry": f"Industry {index % 5}",
            "source_name": "Approved source",
            "source_type": "company_official",
            "source_url": f"https://scale.example/jobs/{index}",
            "source_domain": "scale.example",
            "provenance_status": "approved",
            "published_at": datetime(2026, 1, index % 28 + 1),
            "published_at_evidence": "structured published_at field",
            "published_at_confidence": 0.95,
            "published_at_trusted": True,
            "collected_at": datetime(2026, 4, 1),
            "job_description_raw": "x" * 500,
            "content_hash": f"quarter-scale-hash-{index}",
            "simhash": f"{index:016x}",
            "source_score": 0.95,
            "quality_score": 0.9,
            "status": "valid",
            "gate_status": "valid",
            "machine_level": "mid",
        }
        for index in range(5000)
    ]
    await session.execute(insert(JobPosting), posting_rows)
    posting_ids = list(
        (
            await session.execute(
                select(JobPosting.id).order_by(JobPosting.id)
            )
        ).scalars()
    )
    await session.execute(
        insert(JobPostingSkill),
        [
            {
                "job_posting_id": posting_id,
                "skill_id": skill.id,
                "requirement_type": "required",
                "confidence": 0.9,
                "evidence_text": "Python",
            }
            for posting_id in posting_ids
        ],
    )
    await session.execute(
        insert(EvidenceSnippet),
        [
            {
                "evidence_key": f"quarter-scale-responsibility-{posting_id}",
                "job_posting_id": posting_id,
                "entity_type": "responsibility",
                "entity_key": f"Responsibility {posting_id % 5}",
                "evidence_text": "Build and operate data pipelines",
                "text_hash": f"quarter-scale-text-{posting_id}",
                "confidence": 0.9,
                "review_status": "approved",
            }
            for posting_id in posting_ids
        ],
    )
    run = await _pipeline_run(session)
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        result = await rebuild_quarterly_profiles(
            session, pipeline_run_id=run.id
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert result["profiles_created"] == 1
    assert len(statements) <= 10
    assert all("job_description_raw" not in statement for statement in statements)
