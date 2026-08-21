import json
import hashlib
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from model_class.base import Base


NOW = datetime(2026, 7, 23, 10, 0, 0)
DESCRIPTION = (
    "负责数据平台架构设计、开发和日常维护，要求熟悉Python与Flink实时计算，"
    "持续优化数据质量、处理性能和工程交付流程。"
)


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


async def _posting(session, record_id, **changes):
    from model_class.job_competency import JobPosting, JobPostingSkill, Skill

    values = {
        "record_id": record_id,
        "job_family_id": "DATA_ENGINEER",
        "job_title_raw": "高级数据工程师",
        "job_title_normalized": "数据工程师",
        "company_name": "示例企业",
        "industry": "软件和信息技术服务业",
        "region": "北京",
        "source_name": "企业官网",
        "source_type": "company_official",
        "source_url": f"https://example.com/jobs/{record_id}",
        "provenance_status": "approved",
        "published_at": NOW - timedelta(days=10),
        "published_at_trusted": True,
        "collected_at": NOW,
        "experience_requirement": "6年以上",
        "education_requirement": "本科",
        "salary_range": "20-30K",
        "job_description_raw": DESCRIPTION,
        "content_hash": f"hash-{record_id}",
        "simhash": hashlib.sha256(record_id.encode("utf-8")).hexdigest()[:16],
        "source_score": 0.95,
        "quality_score": 0.9,
        "status": "valid",
        "raw_payload": "{}",
    }
    values.update(changes)
    posting = JobPosting(**values)
    session.add(posting)
    await session.flush()
    skill = await session.scalar(select(Skill).where(Skill.name == "Python"))
    if skill is None:
        skill = Skill(name="Python", category="language", aliases_json="[]")
        session.add(skill)
        await session.flush()
    session.add(
        JobPostingSkill(
            job_posting_id=posting.id,
            skill_id=skill.id,
            requirement_type="required",
            confidence=0.9,
            evidence_text="熟悉Python",
        )
    )
    await session.flush()
    return posting


@pytest.mark.asyncio
async def test_reclassify_postings_persists_gate_and_level(session):
    from src.hard_metrics_pipeline import reclassify_postings

    valid = await _posting(session, "VALID")
    future = await _posting(
        session,
        "FUTURE",
        published_at=NOW + timedelta(days=2),
        job_title_raw="数据工程师",
        experience_requirement="3-5年",
    )

    summary = await reclassify_postings(session, now=NOW)

    await session.refresh(valid)
    await session.refresh(future)
    assert summary == {
        "processed": 2,
        "valid": 1,
        "review": 1,
        "quarantined": 0,
        "duplicate": 0,
    }
    assert valid.machine_level == "senior"
    assert valid.gate_status == "valid"
    assert future.machine_level == "mid"
    assert future.gate_status == "review"
    assert json.loads(future.gate_issue_codes_json) == [
        "future_published_at",
        "published_after_collection",
    ]


@pytest.mark.asyncio
async def test_reclassification_is_idempotent(session):
    from src.hard_metrics_pipeline import reclassify_postings

    posting = await _posting(session, "STABLE")
    await reclassify_postings(session, now=NOW)
    first = (
        posting.gate_status,
        posting.gate_issue_codes_json,
        posting.machine_level,
        posting.machine_level_evidence_json,
    )
    await reclassify_postings(session, now=NOW)

    assert (
        posting.gate_status,
        posting.gate_issue_codes_json,
        posting.machine_level,
        posting.machine_level_evidence_json,
    ) == first


@pytest.mark.asyncio
async def test_reclassification_routes_unverified_source_to_review(session):
    from src.hard_metrics_pipeline import reclassify_postings

    posting = await _posting(
        session,
        "UNVERIFIED",
        provenance_status="unverified",
        published_at_trusted=True,
    )

    await reclassify_postings(session, now=NOW)

    assert posting.gate_status == "review"
    assert json.loads(posting.gate_issue_codes_json) == ["unverified_provenance"]


@pytest.mark.asyncio
async def test_duplicate_rebalance_chooses_highest_quality_source(session):
    from src.hard_metrics_pipeline import rebuild_duplicate_groups

    low = await _posting(
        session,
        "LOW",
        source_score=0.7,
        source_type="public_recruitment",
        content_hash="same-content",
        simhash="1111111111111111",
    )
    high = await _posting(
        session,
        "HIGH",
        source_score=0.95,
        content_hash="same-content",
        simhash="1111111111111111",
    )

    summary = await rebuild_duplicate_groups(session)

    assert summary["groups"] == 1
    assert summary["duplicates"] == 1
    assert high.duplicate_of_id is None
    assert low.duplicate_of_id == high.id


@pytest.mark.asyncio
async def test_manual_level_review_preserves_machine_decision(session):
    from src.hard_metrics_pipeline import (
        persist_manual_level_review,
        reclassify_postings,
    )

    posting = await _posting(session, "MANUAL")
    await reclassify_postings(session, now=NOW)

    payload = await persist_manual_level_review(
        session,
        posting.id,
        level="expert",
        reviewer="team-member",
        note="岗位承担跨部门技术规划职责",
        reviewed_at=NOW,
    )

    assert posting.machine_level == "senior"
    assert posting.manual_level == "expert"
    assert payload["effective_level"] == "expert"
    assert json.loads(posting.manual_level_review_json)["reviewer"] == "team-member"


@pytest.mark.asyncio
async def test_reclassification_does_not_create_review_rows(session):
    from model_class.job_competency import ReviewItem
    from src.hard_metrics_pipeline import reclassify_postings

    await _posting(session, "FUTURE", published_at=NOW + timedelta(days=2))
    await reclassify_postings(session, now=NOW)
    await reclassify_postings(session, now=NOW)

    assert await session.scalar(select(func.count()).select_from(ReviewItem)) == 0


@pytest.mark.asyncio
async def test_pipeline_state_signature_includes_profile_skill_evidence(session):
    from model_class.job_competency import JobProfile, JobProfileSkill, Skill
    from src.hard_metrics_pipeline import _state_signature

    skill = Skill(name="Python", category="language", aliases_json="[]")
    profile = JobProfile(
        family_code="DATA_ENGINEER",
        name="Data Engineer",
        status="existing",
        level="mid",
        tech_stack="big_data",
        version=1,
        profile_kind="quarterly",
        period_key="2026-Q1",
        sample_status="ready",
        input_signature="input",
        generation_key="generation",
        derivation_status="active",
    )
    session.add_all([skill, profile])
    await session.flush()
    link = JobProfileSkill(
        job_profile_id=profile.id,
        skill_id=skill.id,
        requirement_type="required",
        confidence=0.9,
        evidence_count=20,
        prevalence=1.0,
        source_type_count=1,
        source_domain_count=1,
        company_count=20,
        required_ratio=1.0,
        preferred_ratio=0.0,
        cross_source_status="single_source",
    )
    session.add(link)
    await session.flush()

    before = await _state_signature(session, {})
    link.source_domain_count = 3
    link.cross_source_status = "confirmed"
    await session.flush()
    after = await _state_signature(session, {})

    assert after != before


@pytest.mark.asyncio
async def test_full_pipeline_is_idempotent(session):
    from model_class.job_competency import JobProfile
    from model_class.knowledge_base import AcceptanceSnapshot, EvolutionEvent, PipelineRun
    from src.hard_metrics_pipeline import run_hard_metrics_pipeline

    for quarter, month in (("Q1", 1), ("Q2", 4)):
        for index in range(20):
            await _posting(
                session,
                f"PIPE-{quarter}-{index}",
                published_at=datetime(2026, month, min(index + 1, 28)),
            )

    first = await run_hard_metrics_pipeline(session, mode="full", now=NOW)
    counts_after_first = {
        "profiles": await session.scalar(
            select(func.count())
            .select_from(JobProfile)
            .where(JobProfile.profile_kind == "quarterly")
        ),
        "events": await session.scalar(select(func.count()).select_from(EvolutionEvent)),
        "snapshots": await session.scalar(
            select(func.count()).select_from(AcceptanceSnapshot)
        ),
    }
    second = await run_hard_metrics_pipeline(session, mode="full", now=NOW)

    assert first["status"] == second["status"] == "completed"
    assert second["result_signature"] == first["result_signature"]
    assert {
        "profiles": await session.scalar(
            select(func.count())
            .select_from(JobProfile)
            .where(JobProfile.profile_kind == "quarterly")
        ),
        "events": await session.scalar(select(func.count()).select_from(EvolutionEvent)),
        "snapshots": await session.scalar(
            select(func.count()).select_from(AcceptanceSnapshot)
        ),
    } == counts_after_first
    assert await session.scalar(select(func.count()).select_from(PipelineRun)) == 2
