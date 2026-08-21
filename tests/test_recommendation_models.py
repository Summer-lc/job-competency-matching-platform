import json

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from model_class.base import Base
from model_class.job_competency import JobProfile, ResumeRecord


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as value:
        yield value
    await engine.dispose()


async def _parents(session):
    resume = ResumeRecord(
        filename="resume.txt",
        content_hash="a" * 64,
        raw_text="Java项目",
        parsed_json=json.dumps({"skills": ["Java"]}),
    )
    profile = JobProfile(
        family_code="JAVA_DEVELOPER",
        name="Java开发工程师",
        description="Java岗位",
        version=1,
    )
    session.add_all([resume, profile])
    await session.flush()
    return resume, profile


@pytest.mark.asyncio
async def test_recommendation_tables_persist_ranked_results(session):
    from model_class.job_competency import RecommendationResult, RecommendationRun

    resume, profile = await _parents(session)
    run = RecommendationRun(
        run_id="rec-1",
        resume_id=resume.id,
        scoring_version="evidence-match-v2",
        input_signature="b" * 64,
        filters_json="{}",
        status="completed",
        result_signature="c" * 64,
    )
    session.add(run)
    await session.flush()
    session.add(
        RecommendationResult(
            recommendation_run_id=run.id,
            job_profile_id=profile.id,
            rank=1,
            total_score=88.0,
            confidence="high",
            result_json='{"match_band":"high"}',
        )
    )
    await session.commit()

    assert await session.scalar(select(func.count(RecommendationRun.id))) == 1
    assert await session.scalar(select(func.count(RecommendationResult.id))) == 1


@pytest.mark.asyncio
async def test_recommendation_run_signature_is_unique(session):
    from model_class.job_competency import RecommendationRun

    resume, _ = await _parents(session)
    values = {
        "resume_id": resume.id,
        "scoring_version": "evidence-match-v2",
        "input_signature": "d" * 64,
        "filters_json": "{}",
        "status": "completed",
    }
    session.add_all(
        [RecommendationRun(run_id="rec-1", **values), RecommendationRun(run_id="rec-2", **values)]
    )

    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.asyncio
async def test_same_profile_cannot_appear_twice_in_one_run(session):
    from model_class.job_competency import RecommendationResult, RecommendationRun

    resume, profile = await _parents(session)
    run = RecommendationRun(
        run_id="rec-1",
        resume_id=resume.id,
        scoring_version="evidence-match-v2",
        input_signature="e" * 64,
        filters_json="{}",
        status="completed",
    )
    session.add(run)
    await session.flush()
    session.add_all(
        [
            RecommendationResult(
                recommendation_run_id=run.id,
                job_profile_id=profile.id,
                rank=1,
                total_score=80,
                confidence="medium",
                result_json="{}",
            ),
            RecommendationResult(
                recommendation_run_id=run.id,
                job_profile_id=profile.id,
                rank=2,
                total_score=79,
                confidence="medium",
                result_json="{}",
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        await session.commit()
