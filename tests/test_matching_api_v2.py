import json

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from config.DB_config import get_db
from model_class.base import Base
from model_class.job_competency import JobProfile, JobProfileSkill, Skill


@pytest_asyncio.fixture
async def matching_client():
    from src.api import create_app

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = create_app()

    async def override_db():
        async with Session() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, Session
    await engine.dispose()


async def _seed_profile(Session):
    async with Session() as session:
        profile = JobProfile(
            family_code="JAVA_DEVELOPER",
            name="高级Java工程师",
            description="Java平台岗位",
            level="senior",
            version=1,
            review_status="approved",
            profile_kind="legacy",
            derivation_status="active",
            sample_status="ready",
            sample_count=20,
            confidence=0.9,
            responsibilities_json=json.dumps(["平台开发"], ensure_ascii=False),
            industry_scenarios_json=json.dumps(["互联网"], ensure_ascii=False),
        )
        session.add(profile)
        await session.flush()
        for name, kind in (("Java", "required"), ("Kubernetes", "required"), ("Docker", "preferred")):
            skill = await session.scalar(select(Skill).where(Skill.name == name))
            if skill is None:
                skill = Skill(name=name, category="general")
                session.add(skill)
                await session.flush()
            session.add(
                JobProfileSkill(
                    job_profile_id=profile.id,
                    skill_id=skill.id,
                    requirement_type=kind,
                    proficiency_level="working",
                    confidence=0.9,
                    evidence_count=12,
                    prevalence=0.8 if kind == "required" else 0.4,
                )
            )
        await session.commit()
        return profile.id


@pytest.mark.asyncio
async def test_parse_recommend_match_and_read_detail(matching_client):
    client, Session = matching_client
    profile_id = await _seed_profile(Session)
    parsed_response = await client.post(
        "/api/resumes/parse?enrich=false",
        files={
            "file": (
                "resume.txt",
                "专业技能：Java、Docker\n工作经历：4年Java开发经验\n项目经历：使用Java和Docker建设平台。",
                "text/plain",
            )
        },
    )
    assert parsed_response.status_code == 200
    parsed = parsed_response.json()
    assert parsed["schema_version"] == "resume-profile-v2"
    assert parsed["parser_mode"] == "rules"
    assert all(item["evidence"] for item in parsed["skills"])

    recommendation = await client.post(
        "/api/matches/recommend",
        json={"resume_id": parsed["resume_id"], "limit": 5},
    )
    assert recommendation.status_code == 200
    assert recommendation.json()["items"][0]["profile_id"] == profile_id

    match = await client.post(
        "/api/matches",
        json={"resume_id": parsed["resume_id"], "job_profile_id": profile_id},
    )
    assert match.status_code == 200
    body = match.json()
    assert body["scoring_version"] == "evidence-match-v2"
    assert len(body["dimensions"]) == 7
    assert body["learning_plan"]["version"] == "learning-path-v2"

    detail = await client.get(f"/api/matches/{body['match_id']}")
    assert detail.status_code == 200
    assert detail.json()["match_id"] == body["match_id"]
    assert detail.json()["dimensions"] == body["dimensions"]
    assert detail.json()["learning_plan"] == body["learning_plan"]


@pytest.mark.asyncio
async def test_recommendation_api_returns_404_for_missing_resume(matching_client):
    client, _ = matching_client

    response = await client.post(
        "/api/matches/recommend", json={"resume_id": 999, "limit": 5}
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "简历不存在"


@pytest.mark.asyncio
async def test_resume_model_enrichment_requires_explicit_opt_in(
    matching_client, monkeypatch
):
    import src.api as api_module

    client, _ = matching_client
    monkeypatch.setattr(api_module, "DEEPSEEK_API_KEY", "configured")

    def unexpected_call(*args, **kwargs):
        raise AssertionError("model enrichment must be opt-in")

    monkeypatch.setattr(api_module, "enrich_resume_profile", unexpected_call)
    response = await client.post(
        "/api/resumes/parse",
        files={"file": ("resume.txt", "专业技能：Java", "text/plain")},
    )

    assert response.status_code == 200
    assert response.json()["parser_mode"] == "rules"


@pytest.mark.asyncio
async def test_cross_origin_requests_are_not_open_to_arbitrary_sites(matching_client):
    client, _ = matching_client

    response = await client.options(
        "/api/jobs",
        headers={
            "Origin": "https://untrusted.example",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.headers.get("access-control-allow-origin") is None
