from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import func, select
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


async def _run(session):
    from model_class.knowledge_base import PipelineRun

    run = PipelineRun(
        run_id=f"evolution-{id(session)}",
        mode="full",
        rule_version="competition-profile-v1",
        status="running",
    )
    session.add(run)
    await session.flush()
    return run


async def _profile(session, period, version, skills, *, sample_status="ready"):
    from model_class.job_competency import JobProfile, JobProfileSkill, Skill

    profile = JobProfile(
        family_code="DATA_ENGINEER",
        name="数据工程师",
        description=f"{period}画像",
        status="existing",
        level="mid",
        tech_stack="big_data",
        version=version,
        confidence=0.9,
        review_status="approved",
        profile_kind="quarterly",
        period_key=period,
        sample_count=20,
        sample_status=sample_status,
        generation_key=f"profile-{period}-{id(session)}",
        derivation_status="active",
    )
    session.add(profile)
    await session.flush()
    for name, rate, requirement, evidence_count in skills:
        skill = await session.scalar(select(Skill).where(Skill.name == name))
        if skill is None:
            skill = Skill(name=name, category="big_data", aliases_json="[]")
            session.add(skill)
            await session.flush()
        session.add(
            JobProfileSkill(
                job_profile_id=profile.id,
                skill_id=skill.id,
                requirement_type=requirement,
                proficiency_level="working",
                confidence=0.9,
                evidence_count=evidence_count,
                prevalence=rate,
            )
        )
    await session.flush()
    return profile


async def _evidence_postings(
    session, period, skill_counts, *, published_at_trusted=True
):
    from model_class.job_competency import JobPosting, JobPostingSkill, Skill

    year, quarter = period.split("-Q")
    month = (int(quarter) - 1) * 3 + 1
    maximum = max(skill_counts.values())
    for index in range(maximum):
        posting = JobPosting(
            record_id=f"{period}-{index}-{id(session)}",
            job_family_id="DATA_ENGINEER",
            job_title_raw="数据工程师",
            job_title_normalized="数据工程师",
            company_name=f"企业{index}",
            industry="软件和信息技术服务业",
            region="北京",
            source_name=f"企业官网{index}",
            source_type="company_official",
            source_url=f"https://example.com/{period}/{index}",
            source_domain="example.com",
            provenance_status="approved",
            published_at=datetime(int(year), month, index + 1),
            published_at_trusted=published_at_trusted,
            collected_at=datetime(2026, 7, 23),
            experience_requirement="3-5年",
            education_requirement="本科",
            job_description_raw=(
                "负责数据平台开发和维护，要求掌握相关数据处理技术，"
                f"持续优化季度任务{index}的质量、性能和交付流程。"
            ),
            content_hash=f"hash-{period}-{index}-{id(session)}",
            simhash=f"{index + (100 if period.endswith('Q2') else 0):016x}",
            source_score=0.95,
            quality_score=0.9,
            status="valid",
            gate_status="valid",
            machine_level="mid",
            machine_level_confidence=0.85,
        )
        session.add(posting)
        await session.flush()
        for name, count in skill_counts.items():
            if index >= count:
                continue
            skill = await session.scalar(select(Skill).where(Skill.name == name))
            session.add(
                JobPostingSkill(
                    job_posting_id=posting.id,
                    skill_id=skill.id,
                    requirement_type=(
                        "required" if period == "2026-Q2" or name != "Flink" else "preferred"
                    ),
                    confidence=0.9,
                    evidence_text=f"掌握{name}",
                )
            )
    await session.flush()


@pytest.mark.asyncio
async def test_adjacent_ready_profiles_create_three_change_types_with_evidence(session):
    from model_class.knowledge_base import EvolutionEvidence
    from src.evolution_service import rebuild_evolution

    previous = await _profile(
        session,
        "2026-Q1",
        1,
        [("Spark", 0.20, "required", 4), ("Flink", 0.20, "preferred", 4)],
    )
    current = await _profile(
        session,
        "2026-Q2",
        2,
        [("Kafka", 0.20, "required", 4), ("Flink", 0.35, "required", 7)],
    )
    await _evidence_postings(session, "2026-Q1", {"Spark": 4, "Flink": 4})
    await _evidence_postings(session, "2026-Q2", {"Kafka": 4, "Flink": 7})
    run = await _run(session)

    events = await rebuild_evolution(
        session, previous.id, current.id, pipeline_run_id=run.id
    )

    assert {(event.entity_key, event.change_type) for event in events} == {
        ("Kafka", "added"),
        ("Spark", "removed"),
        ("Flink", "modified"),
    }
    for event in events:
        count = await session.scalar(
            select(func.count())
            .select_from(EvolutionEvidence)
            .where(EvolutionEvidence.evolution_event_id == event.id)
        )
        assert count >= 3


@pytest.mark.asyncio
async def test_nonadjacent_or_low_sample_profiles_do_not_create_events(session):
    from src.evolution_service import rebuild_evolution

    previous = await _profile(
        session, "2026-Q1", 1, [("Spark", 0.20, "required", 4)]
    )
    nonadjacent = await _profile(
        session, "2026-Q3", 2, [("Kafka", 0.20, "required", 4)]
    )
    low_sample = await _profile(
        session,
        "2026-Q2",
        3,
        [("Kafka", 0.20, "required", 4)],
        sample_status="low_sample",
    )
    run = await _run(session)

    assert await rebuild_evolution(
        session, previous.id, nonadjacent.id, pipeline_run_id=run.id
    ) == []
    assert await rebuild_evolution(
        session, previous.id, low_sample.id, pipeline_run_id=run.id
    ) == []


@pytest.mark.asyncio
async def test_change_with_fewer_than_three_jd_sources_is_not_formal(session):
    from src.evolution_service import rebuild_evolution

    previous = await _profile(session, "2026-Q1", 1, [])
    current = await _profile(
        session, "2026-Q2", 2, [("Kafka", 0.20, "required", 2)]
    )
    await _evidence_postings(session, "2026-Q2", {"Kafka": 2})
    run = await _run(session)

    assert await rebuild_evolution(
        session, previous.id, current.id, pipeline_run_id=run.id
    ) == []


@pytest.mark.asyncio
async def test_untrusted_postings_do_not_support_formal_evolution(session):
    from src.evolution_service import rebuild_evolution

    previous = await _profile(session, "2026-Q1", 1, [])
    current = await _profile(
        session, "2026-Q2", 2, [("Kafka", 0.20, "required", 4)]
    )
    await _evidence_postings(
        session,
        "2026-Q2",
        {"Kafka": 4},
        published_at_trusted=False,
    )
    run = await _run(session)

    assert await rebuild_evolution(
        session, previous.id, current.id, pipeline_run_id=run.id
    ) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("derivation_status", "superseded"),
        ("sample_status", "low_sample"),
        ("sample_count", 19),
    ],
)
async def test_global_reconciliation_demotes_events_for_ineligible_profiles(
    session, field, value
):
    from model_class.knowledge_base import EvolutionEvent
    from src.evolution_service import rebuild_all_adjacent_evolution

    previous = await _profile(session, "2026-Q1", 1, [])
    current = await _profile(session, "2026-Q2", 2, [])
    event = EvolutionEvent(
        family_code="DATA_ENGINEER",
        previous_profile_id=previous.id,
        current_profile_id=current.id,
        entity_type="skill",
        entity_key="Kafka",
        change_type="added",
        event_status="formal",
        generation_key=f"stale-{field}-{id(session)}",
    )
    session.add(event)
    setattr(current, field, value)
    run = await _run(session)

    await rebuild_all_adjacent_evolution(
        session,
        pipeline_run_id=run.id,
        family_codes={"UNRELATED_FAMILY"},
    )

    assert event.event_status == "superseded"


@pytest.mark.asyncio
async def test_evolution_rebuild_is_idempotent(session):
    from model_class.knowledge_base import EvolutionEvent
    from src.evolution_service import rebuild_evolution

    previous = await _profile(session, "2026-Q1", 1, [])
    current = await _profile(
        session, "2026-Q2", 2, [("Kafka", 0.20, "required", 4)]
    )
    await _evidence_postings(session, "2026-Q2", {"Kafka": 4})
    run = await _run(session)

    first = await rebuild_evolution(
        session, previous.id, current.id, pipeline_run_id=run.id
    )
    second = await rebuild_evolution(
        session, previous.id, current.id, pipeline_run_id=run.id
    )

    assert [event.id for event in first] == [event.id for event in second]
    assert await session.scalar(select(func.count()).select_from(EvolutionEvent)) == 1


@pytest.mark.asyncio
async def test_family_evolution_payload_returns_formal_quarterly_events(session):
    from src.evolution_service import family_evolution_payload, rebuild_evolution

    previous = await _profile(session, "2026-Q1", 1, [])
    current = await _profile(
        session, "2026-Q2", 2, [("Kafka", 0.20, "required", 4)]
    )
    await _evidence_postings(session, "2026-Q2", {"Kafka": 4})
    run = await _run(session)
    await rebuild_evolution(session, previous.id, current.id, pipeline_run_id=run.id)

    payload = await family_evolution_payload(
        session, "DATA_ENGINEER", level="mid", current_period="2026-Q2"
    )

    assert payload["family_code"] == "DATA_ENGINEER"
    assert payload["previous_period"] == "2026-Q1"
    assert payload["current_period"] == "2026-Q2"
    assert payload["added"][0]["skill"] == "Kafka"
    assert payload["added"][0]["evidence_count"] == 4
    assert len(payload["evidence"]) == 4
    assert payload["sources"][0].startswith("https://example.com/")
