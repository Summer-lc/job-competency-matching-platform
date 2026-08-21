import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from model_class.base import Base
from model_class.job_competency import (
    EvidenceRecord,
    JobProfile,
    JobProfileSkill,
    RecommendationResult,
    RecommendationRun,
    ResumeRecord,
    Skill,
)


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as value:
        yield value
    await engine.dispose()


async def _add_profile(
    session,
    family,
    name,
    skills,
    *,
    level="mid",
    kind="legacy",
    version=1,
    review_status="approved",
    sample_status="ready",
):
    profile = JobProfile(
        family_code=family,
        name=name,
        description=f"{name}画像",
        level=level,
        version=version,
        review_status=review_status,
        profile_kind=kind,
        period_key="2026Q2" if kind == "quarterly" else None,
        sample_count=20 if sample_status == "ready" else 10,
        sample_status=sample_status,
        derivation_status="active",
        responsibilities_json=json.dumps(["平台开发"], ensure_ascii=False),
        industry_scenarios_json=json.dumps(["互联网"], ensure_ascii=False),
        confidence=0.9 if sample_status == "ready" else 0.65,
    )
    session.add(profile)
    await session.flush()
    for index, skill_name in enumerate(skills):
        skill = await session.scalar(select(Skill).where(Skill.name == skill_name))
        if skill is None:
            skill = Skill(name=skill_name, category="general")
            session.add(skill)
            await session.flush()
        session.add(
            JobProfileSkill(
                job_profile_id=profile.id,
                skill_id=skill.id,
                requirement_type="required" if index < 2 else "preferred",
                proficiency_level="working",
                confidence=0.9,
                evidence_count=10,
                prevalence=0.8 if index < 2 else 0.4,
            )
        )
    return profile


@pytest_asyncio.fixture
async def recommendation_data(session):
    resume = ResumeRecord(
        filename="resume.txt",
        content_hash="a" * 64,
        raw_text="4年Java和Docker项目经验",
        parsed_json=json.dumps(
            {
                "schema_version": "resume-profile-v2",
                "skills": [
                    {"name": "Java", "proficiency": "advanced", "evidence_sources": ["project"]},
                    {"name": "Docker", "proficiency": "working", "evidence_sources": ["project"]},
                ],
                "recent_skills": ["Java", "Docker"],
                "experience_years": 4,
                "project_experiences": [
                    {"name": "平台", "skills": ["Java", "Docker"], "responsibilities": ["平台开发"]}
                ],
                "projects": ["平台"],
            },
            ensure_ascii=False,
        ),
    )
    session.add(resume)
    await session.flush()
    legacy_java = await _add_profile(
        session, "JAVA_DEVELOPER", "Java开发工程师", ["Java", "MySQL"], kind="legacy", version=1
    )
    quarterly_java = await _add_profile(
        session,
        "JAVA_DEVELOPER",
        "Java开发工程师",
        ["Java", "Docker"],
        kind="quarterly",
        version=2,
        sample_status="low_sample",
    )
    data_profile = await _add_profile(
        session, "DATA_ENGINEER", "数据工程师", ["Python", "Spark", "Docker"], version=1
    )
    await _add_profile(
        session,
        "REJECTED_FAMILY",
        "已拒绝岗位",
        ["Java"],
        review_status="rejected",
    )
    await _add_profile(session, "EMPTY_FAMILY", "空技能岗位", [], version=1)
    await session.commit()
    return {
        "resume": resume,
        "legacy_java": legacy_java,
        "quarterly_java": quarterly_java,
        "data": data_profile,
    }


@pytest.mark.asyncio
async def test_recommendations_are_stable_deduplicated_and_idempotent(session, recommendation_data):
    from src.job_recommendation_service import recommend_jobs

    first = await recommend_jobs(session, resume_id=recommendation_data["resume"].id, limit=5)
    second = await recommend_jobs(session, resume_id=recommendation_data["resume"].id, limit=5)

    assert [item["family_code"] for item in first["items"]] == ["JAVA_DEVELOPER", "DATA_ENGINEER"]
    assert len({item["family_code"] for item in first["items"]}) == len(first["items"])
    assert [item["profile_id"] for item in first["items"]] == [item["profile_id"] for item in second["items"]]
    assert first["result_signature"] == second["result_signature"]
    assert first["recommendation_run_id"] == second["recommendation_run_id"]
    assert await session.scalar(select(func.count(RecommendationRun.id))) == 1
    assert await session.scalar(select(func.count(RecommendationResult.id))) == 2
    run = await session.scalar(select(RecommendationRun))
    assert sorted(json.loads(run.filters_json)["candidate_profile_ids"]) == sorted(
        [item["profile_id"] for item in first["items"]]
    )


@pytest.mark.asyncio
async def test_active_quarterly_profile_replaces_legacy_and_marks_low_sample(session, recommendation_data):
    from src.job_recommendation_service import recommend_jobs

    result = await recommend_jobs(session, resume_id=recommendation_data["resume"].id)
    java = next(item for item in result["items"] if item["family_code"] == "JAVA_DEVELOPER")

    assert java["profile_id"] == recommendation_data["quarterly_java"].id
    assert java["profile_kind"] == "quarterly"
    assert java["sample_status"] == "low_sample"
    assert "岗位画像样本尚未达到稳定状态" in java["confidence_notes"]


@pytest.mark.asyncio
async def test_recommendation_filters_family_and_level(session, recommendation_data):
    from src.job_recommendation_service import recommend_jobs

    result = await recommend_jobs(
        session,
        resume_id=recommendation_data["resume"].id,
        family_codes=["DATA_ENGINEER"],
        levels=["mid"],
    )

    assert [item["family_code"] for item in result["items"]] == ["DATA_ENGINEER"]


@pytest.mark.asyncio
async def test_no_eligible_profiles_returns_reason_without_fake_run(session):
    from src.job_recommendation_service import recommend_jobs

    resume = ResumeRecord(
        filename="empty.txt",
        content_hash="f" * 64,
        raw_text="无技能",
        parsed_json='{"skills": []}',
    )
    session.add(resume)
    await session.commit()
    result = await recommend_jobs(session, resume_id=resume.id, family_codes=["MISSING"])

    assert result == {
        "items": [],
        "reason": "no_eligible_profiles",
        "result_signature": None,
        "recommendation_run_id": None,
    }
    assert await session.scalar(select(func.count(RecommendationRun.id))) == 0


@pytest.mark.asyncio
async def test_missing_resume_is_rejected(session):
    from src.job_recommendation_service import recommend_jobs

    with pytest.raises(ValueError, match="简历不存在"):
        await recommend_jobs(session, resume_id=999)


def test_recommendation_signature_changes_when_parsed_profile_changes():
    from src.job_recommendation_service import recommendation_input_signature

    profile = SimpleNamespace(
        id=1,
        family_code="JAVA_DEVELOPER",
        version=1,
        input_signature="profile-v1",
    )
    candidates = [(profile, {"id": 1, "required_skills": ["Java"]})]
    first = SimpleNamespace(
        id=10,
        content_hash="a" * 64,
        parsed_json='{"schema_version":"resume-profile-v2","skills":[]}',
    )
    second = SimpleNamespace(
        id=10,
        content_hash="a" * 64,
        parsed_json='{"schema_version":"resume-profile-v2","skills":["Java"]}',
    )

    assert recommendation_input_signature(first, candidates, {}) != recommendation_input_signature(
        second, candidates, {}
    )


def test_payload_ranking_uses_profile_evidence_before_stable_name():
    from src.job_recommendation_service import rank_job_payloads

    candidates = [
        {
            "id": 1,
            "family_code": "A_FAMILY",
            "name": "A岗位",
            "required_skills": ["Java"],
            "skills": [{"name": "Java", "requirement_type": "required", "evidence_count": 1}],
        },
        {
            "id": 2,
            "family_code": "Z_FAMILY",
            "name": "Z岗位",
            "required_skills": ["Java"],
            "skills": [{"name": "Java", "requirement_type": "required", "evidence_count": 12}],
        },
    ]

    result = rank_job_payloads({"skills": ["Java"]}, candidates)

    assert [item["profile_id"] for item in result] == [2, 1]
    assert result[0]["evidence_count"] == 12


@pytest.mark.asyncio
async def test_profile_payload_includes_traceable_external_learning_evidence(session):
    from src.job_recommendation_service import profile_matching_payload

    profile = await _add_profile(
        session, "JAVA_DEVELOPER", "Java开发工程师", ["Java"]
    )
    session.add(
        EvidenceRecord(
            evidence_id="E-JAVA-1",
            job_family_id="JAVA_DEVELOPER",
            evidence_type="official_standard",
            title="Java官方学习资料",
            publisher="OpenJDK",
            source_url="https://openjdk.org/",
            related_skill="Java",
            evidence_summary="Java平台标准资料",
            source_score=0.95,
        )
    )
    await session.commit()

    payload = await profile_matching_payload(session, profile)

    assert payload["evidence_records"] == [
        {
            "title": "Java官方学习资料",
            "publisher": "OpenJDK",
            "source_url": "https://openjdk.org/",
            "related_skill": "Java",
        }
    ]
