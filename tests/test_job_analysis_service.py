from datetime import datetime
import hashlib

import json

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
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


async def _analysis_posting(
    session,
    record_id,
    *,
    gate_status="valid",
    published_at=None,
    published_at_trusted=False,
    first_seen_at=None,
    duplicate_of_id=None,
    source_type="company_official",
    source_domain="jobs.example.com",
    source_name="Example Careers",
    company_name="Example Co",
    requirement_type="required",
    include_skill=True,
    responsibility=None,
    industry="Software",
):
    from model_class.job_competency import JobPosting, JobPostingSkill, Skill
    from model_class.knowledge_base import EvidenceSnippet

    skill = await session.scalar(select(Skill).where(Skill.name == "Python"))
    if skill is None:
        skill = Skill(name="Python", category="language", aliases_json="[]")
        session.add(skill)
        await session.flush()
    posting = JobPosting(
        record_id=record_id,
        job_family_id="DATA_ENGINEER",
        job_title_raw="Data Engineer",
        job_title_normalized="Data Engineer",
        company_name=company_name,
        industry=industry,
        source_name=source_name,
        source_type=source_type,
        source_domain=source_domain,
        source_url=f"https://{source_domain or 'example.com'}/{record_id}",
        provenance_status="approved",
        published_at=published_at,
        published_at_trusted=published_at_trusted,
        collected_at=datetime(2026, 4, 1),
        first_seen_at=first_seen_at,
        job_description_raw="Build and maintain production data pipelines with Python and SQL.",
        content_hash=f"hash-{record_id}",
        simhash=f"{len(record_id):016x}",
        source_score=0.95,
        quality_score=0.9,
        status="valid",
        gate_status=gate_status,
        duplicate_of_id=duplicate_of_id,
    )
    session.add(posting)
    await session.flush()
    if include_skill:
        session.add(
            JobPostingSkill(
                job_posting_id=posting.id,
                skill_id=skill.id,
                requirement_type=requirement_type,
                confidence=0.95,
                evidence_text="Python",
            )
        )
    if responsibility:
        session.add(
            EvidenceSnippet(
                evidence_key=hashlib.sha256(
                    f"{record_id}|{responsibility}".encode()
                ).hexdigest(),
                job_posting_id=posting.id,
                entity_type="responsibility",
                entity_key=responsibility,
                evidence_text=responsibility,
                text_hash=hashlib.sha256(responsibility.encode()).hexdigest(),
                confidence=0.82,
                review_status="approved",
            )
        )
    await session.flush()
    return posting


def test_emerging_score_rewards_growth_sources_and_novelty():
    from src.job_analysis_service import emerging_job_score

    strong = emerging_job_score(
        current_count=120,
        previous_count=20,
        source_count=8,
        novelty=0.9,
        persistence=1.0,
    )
    weak = emerging_job_score(
        current_count=20,
        previous_count=18,
        source_count=1,
        novelty=0.1,
        persistence=0.3,
    )
    assert strong >= 0.8
    assert weak < 0.5


def test_compare_skill_windows_reports_added_removed_and_changed():
    from src.job_analysis_service import compare_skill_windows

    baseline = {"Java": 0.9, "MySQL": 0.7, "JSP": 0.5, "Docker": 0.1}
    current = {"Java": 0.88, "MySQL": 0.45, "JSP": 0.05, "Docker": 0.62, "Kubernetes": 0.4}
    result = compare_skill_windows(baseline, current)

    assert {item["skill"] for item in result["added"]} == {"Docker", "Kubernetes"}
    assert {item["skill"] for item in result["removed"]} == {"JSP"}
    assert {item["skill"] for item in result["changed"]} == {"MySQL"}


def test_aggregate_skill_prevalence_respects_time_window():
    from src.job_analysis_service import aggregate_skill_prevalence

    rows = [
        {"published_at": datetime(2024, 1, 1), "published_at_trusted": True, "skills": ["Java", "JSP"]},
        {"published_at": datetime(2024, 5, 1), "published_at_trusted": True, "skills": ["Java"]},
        {"published_at": datetime(2026, 1, 1), "published_at_trusted": True, "skills": ["Java", "Docker"]},
        {"published_at": datetime(2026, 2, 1), "published_at_trusted": True, "skills": ["Java", "Docker", "Kubernetes"]},
        {"published_at": datetime(2026, 3, 1), "published_at_trusted": False, "skills": ["Spoofed"]},
    ]
    old = aggregate_skill_prevalence(rows, start_year=2024, end_year=2025)
    current = aggregate_skill_prevalence(rows, start_year=2026, end_year=2026)
    assert old == {"Java": 1.0, "JSP": 0.5}
    assert current == {"Java": 1.0, "Docker": 1.0, "Kubernetes": 0.5}


def test_aggregate_skill_prevalence_excludes_untrusted_dates():
    from src.job_analysis_service import aggregate_skill_prevalence

    rows = [
        {
            "published_at": datetime(2026, 1, 1),
            "published_at_trusted": True,
            "skills": ["Python"],
        },
        {
            "published_at": datetime(2026, 2, 1),
            "published_at_trusted": False,
            "skills": ["InventedSkill"],
        },
    ]

    assert aggregate_skill_prevalence(rows, start_year=2026, end_year=2026) == {
        "Python": 1.0
    }


@pytest.mark.asyncio
async def test_overall_profile_includes_valid_undated_and_excludes_review_and_duplicate(
    session,
):
    from model_class.job_competency import JobProfile, JobProfileSkill
    from model_class.knowledge_base import JobProfileSnapshot
    from src.job_analysis_service import rebuild_analysis

    valid = await _analysis_posting(session, "VALID-UNDATED")
    await _analysis_posting(
        session,
        "REVIEW-DATED",
        gate_status="review",
        published_at=datetime(2026, 1, 1),
        published_at_trusted=True,
    )
    await _analysis_posting(session, "DUPLICATE", duplicate_of_id=valid.id)

    result = await rebuild_analysis(session)
    profile = await session.scalar(select(JobProfile).order_by(JobProfile.id.desc()))
    link = await session.scalar(
        select(JobProfileSkill).where(JobProfileSkill.job_profile_id == profile.id)
    )
    snapshot = await session.scalar(
        select(JobProfileSnapshot).where(JobProfileSnapshot.job_profile_id == profile.id)
    )

    assert result["profiles_created"] == 1
    assert profile.valid_from is None
    assert profile.valid_to is None
    assert link.evidence_count == 1
    assert json.loads(snapshot.payload_json)["posting_count"] == 1


@pytest.mark.asyncio
async def test_overall_profile_persists_cross_source_and_temporal_evidence(session):
    from model_class.job_competency import JobProfile, JobProfileSkill
    from model_class.knowledge_base import JobProfileSnapshot
    from src.job_analysis_service import rebuild_analysis

    for index, requirement in enumerate(("required", "required", "preferred"), start=1):
        await _analysis_posting(
            session,
            f"VALID-{index}",
            source_name=f"Display name {index}",
            source_domain=f"source{index}.example",
            company_name=f"Company {index}",
            requirement_type=requirement,
            published_at=datetime(2026, index, 1),
            published_at_trusted=index != 2,
            first_seen_at=datetime(2026, index, 2),
        )

    await rebuild_analysis(session)
    profile = await session.scalar(select(JobProfile).order_by(JobProfile.id.desc()))
    link = await session.scalar(
        select(JobProfileSkill).where(JobProfileSkill.job_profile_id == profile.id)
    )
    snapshot = await session.scalar(
        select(JobProfileSnapshot).where(JobProfileSnapshot.job_profile_id == profile.id)
    )
    payload = json.loads(snapshot.payload_json)

    assert link.evidence_count == 3
    assert link.source_type_count == 1
    assert link.source_domain_count == 3
    assert link.company_count == 3
    assert link.required_ratio == pytest.approx(2 / 3, abs=0.0001)
    assert link.preferred_ratio == pytest.approx(1 / 3, abs=0.0001)
    assert link.first_published_at == datetime(2026, 1, 1)
    assert link.last_published_at == datetime(2026, 3, 1)
    assert link.cross_source_status == "confirmed"
    assert payload["source_type_count"] == 1
    assert payload["source_domain_count"] == 3
    assert payload["company_count"] == 3
    assert payload["skill_evidence_rule_version"] == "cross-source-skill-v1"
    assert payload["temporal_basis"]["publication_time_field"] == "published_at"
    assert payload["temporal_basis"]["observation_time_field"] == "first_seen_at"
    assert payload["temporal_basis"]["quarter_assignment"] is None
    assert snapshot.data_cutoff == datetime(2026, 3, 1)


@pytest.mark.asyncio
async def test_overall_profile_includes_valid_responsibility_only_posting(session):
    from model_class.job_competency import JobProfile, JobProfileSkill
    from model_class.knowledge_base import JobProfileResponsibility, JobProfileSnapshot
    from src.job_analysis_service import rebuild_analysis

    await _analysis_posting(session, "SKILLED", responsibility="Build pipelines")
    await _analysis_posting(
        session,
        "RESPONSIBILITY-ONLY",
        include_skill=False,
        responsibility="Operate data quality controls",
        company_name="Second Co",
        industry="Finance",
    )

    await rebuild_analysis(session)
    profile = await session.scalar(select(JobProfile).order_by(JobProfile.id.desc()))
    snapshot = await session.scalar(
        select(JobProfileSnapshot).where(JobProfileSnapshot.job_profile_id == profile.id)
    )
    skill = await session.scalar(
        select(JobProfileSkill).where(JobProfileSkill.job_profile_id == profile.id)
    )
    responsibilities = list(
        (
            await session.execute(
                select(JobProfileResponsibility).where(
                    JobProfileResponsibility.job_profile_id == profile.id
                )
            )
        ).scalars()
    )

    assert json.loads(snapshot.payload_json)["posting_count"] == 2
    assert skill.evidence_count == 1
    assert skill.prevalence == 0.5
    assert sorted(item.evidence_count for item in responsibilities) == [1, 1]
    assert all(item.prevalence == 0.5 for item in responsibilities)


@pytest.mark.asyncio
async def test_overall_observation_change_does_not_create_new_profile(session):
    from model_class.job_competency import JobProfile
    from src.job_analysis_service import rebuild_analysis

    posting = await _analysis_posting(
        session,
        "OBSERVATION-ONLY",
        first_seen_at=datetime(2026, 1, 1),
    )
    first = await rebuild_analysis(session)
    profile_id = await session.scalar(select(JobProfile.id))

    posting.first_seen_at = datetime(2025, 12, 1)
    posting.last_seen_at = datetime(2026, 5, 1)
    second = await rebuild_analysis(session)

    assert first["profiles_created"] == 1
    assert second["profiles_created"] == 0
    assert second["unchanged_families"] == ["DATA_ENGINEER"]
    assert await session.scalar(select(func.count()).select_from(JobProfile)) == 1
    assert await session.scalar(select(JobProfile.id)) == profile_id


async def _snapshot_signature_for_insertion_order(records):
    import model_class.job_competency  # noqa: F401
    import model_class.knowledge_base  # noqa: F401
    from model_class.knowledge_base import JobProfileSnapshot
    from src.job_analysis_service import rebuild_analysis

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as value:
            for record in records:
                await _analysis_posting(value, **record)
            await rebuild_analysis(value)
            snapshot = await value.scalar(select(JobProfileSnapshot))
            return snapshot.content_signature
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_overall_snapshot_hash_is_deterministic_for_reverse_insertion():
    records = [
        {
            "record_id": "CASE-UPPER",
            "industry": "Finance",
            "responsibility": "Build Pipelines",
        },
        {
            "record_id": "CASE-LOWER",
            "industry": "finance",
            "responsibility": "build pipelines",
        },
    ]

    forward = await _snapshot_signature_for_insertion_order(records)
    reverse = await _snapshot_signature_for_insertion_order(reversed(records))

    assert forward == reverse


@pytest.mark.asyncio
async def test_overall_rebuild_uses_bounded_queries_for_5000_postings(session):
    from model_class.job_competency import JobPosting
    from src.job_analysis_service import rebuild_analysis

    rows = [
        {
            "record_id": f"SCALE-{index}",
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
            "collected_at": datetime(2026, 4, 1),
            "job_description_raw": "x" * 500,
            "content_hash": f"scale-hash-{index}",
            "simhash": f"{index:016x}",
            "source_score": 0.95,
            "quality_score": 0.9,
            "status": "valid",
            "gate_status": "valid",
        }
        for index in range(5000)
    ]
    await session.execute(insert(JobPosting), rows)
    await session.flush()
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        await rebuild_analysis(session)
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert len(statements) <= 10
    assert all("job_description_raw" not in statement for statement in statements)
