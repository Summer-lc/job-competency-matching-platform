import json

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from model_class.base import Base


def _job_bytes() -> bytes:
    record = {
        "record_id": "KB-JD-0001",
        "collector_id": "T",
        "job_family_id": "DATA_ENGINEER",
        "job_title_raw": "实时数据工程师",
        "company_name": "流式计算科技",
        "industry": "软件和信息技术服务业",
        "region": "北京",
        "source_name": "企业官网",
        "source_type": "company_official",
        "source_url": "https://example.com/jobs/KB-JD-0001",
        "published_at": "2026-07-01",
        "collected_at": "2026-07-22T10:00:00+08:00",
        "experience_requirement": "3-5年",
        "education_requirement": "本科",
        "salary_range": "20-30K",
        "job_description_raw": "负责实时数据平台建设和维护，要求熟悉Python、Flink与Kafka流式计算。",
    }
    return json.dumps(record, ensure_ascii=False).encode("utf-8")


@pytest_asyncio.fixture
async def memory_session():
    import model_class.job_competency  # noqa: F401
    import model_class.knowledge_base  # noqa: F401

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_import_creates_traceable_skill_and_responsibility_evidence(memory_session):
    from model_class.job_competency import JobPosting
    from model_class.knowledge_base import EvidenceSnippet
    from src.import_service import import_job_file

    await import_job_file(memory_session, _job_bytes(), "knowledge.json")
    posting = await memory_session.scalar(select(JobPosting))
    snippets = (await memory_session.execute(select(EvidenceSnippet))).scalars().all()

    assert {item.entity_type for item in snippets} >= {"skill", "responsibility"}
    assert all(item.evidence_text in posting.job_description_raw for item in snippets)
    assert all(item.job_posting_id == posting.id for item in snippets)


@pytest.mark.asyncio
async def test_lexical_search_returns_source_metadata(memory_session):
    from src.import_service import import_job_file
    from src.knowledge_service import search_knowledge, update_knowledge_chunks

    await import_job_file(memory_session, _job_bytes(), "knowledge.json")
    await update_knowledge_chunks(memory_session, {"DATA_ENGINEER"})
    result = await search_knowledge(
        memory_session, "Flink 实时计算", family_code="DATA_ENGINEER"
    )

    assert result["mode"] == "lexical"
    assert result["items"][0]["record_id"] == "KB-JD-0001"
    assert result["items"][0]["source_url"].startswith("https://")
    assert "Flink" in result["items"][0]["text"]


def test_cosine_similarity_handles_identical_and_empty_vectors():
    from src.knowledge_service import cosine_similarity

    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine_similarity([], []) == 0.0
