import json
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from model_class.base import Base
from model_class.job_competency import EvidenceRecord
from model_class.knowledge_base import KnowledgeChunk


ROOT = Path(__file__).resolve().parents[1]


@pytest_asyncio.fixture
async def evidence_session():
    import model_class.job_competency  # noqa: F401
    import model_class.knowledge_base  # noqa: F401

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        session.add_all(
            [
                KnowledgeChunk(
                    chunk_id="data-engineer-flink",
                    source_type="job_posting",
                    source_entity_id="101",
                    family_code="DATA_ENGINEER",
                    text=(
                        "数据工程师\n"
                        + "负责通用数据平台建设与维护。" * 80
                        + "重点负责Flink实时计算平台建设，要求掌握Kafka和流处理。"
                    ),
                    text_hash="data-hash",
                    source_url="https://example.com/jobs/101",
                    metadata_json=json.dumps(
                        {
                            "record_id": "JD-101",
                            "company_name": "流式计算科技",
                            "source_name": "企业官网",
                            "quality_score": 0.91,
                            "review_status": "valid",
                        },
                        ensure_ascii=False,
                    ),
                ),
                KnowledgeChunk(
                    chunk_id="security-zero-trust",
                    source_type="job_posting",
                    source_entity_id="202",
                    family_code="CYBERSECURITY_ENGINEER",
                    text="安全工程师\n负责零信任安全平台建设。",
                    text_hash="security-hash",
                    source_url="https://example.com/jobs/202",
                    metadata_json=json.dumps(
                        {
                            "record_id": "JD-202",
                            "company_name": "安全科技",
                            "source_name": "企业官网",
                            "quality_score": 0.9,
                            "review_status": "valid",
                        },
                        ensure_ascii=False,
                    ),
                ),
                EvidenceRecord(
                    evidence_id="STD-FLINK-001",
                    job_family_id="DATA_ENGINEER",
                    evidence_type="official_standard",
                    title="实时计算职业能力标准",
                    publisher="行业标准组织",
                    source_url="https://example.com/standards/flink",
                    related_skill="Flink",
                    evidence_summary="数据工程师应掌握Flink实时计算、状态管理和流式数据处理能力。",
                    source_score=0.98,
                ),
            ]
        )
        await session.commit()
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_gather_answer_evidence_merges_sources_and_filters_family(evidence_session):
    from src.evidence_qa_service import gather_answer_evidence

    items = await gather_answer_evidence(
        evidence_session,
        "Flink 实时计算能力",
        family_code="DATA_ENGINEER",
        limit=6,
    )

    assert [item["citation_id"] for item in items] == ["K1", "K2"]
    assert {item["source_kind"] for item in items} == {"jd", "external"}
    assert all(item["family_code"] == "DATA_ENGINEER" for item in items)
    assert all(item["source_url"].startswith("https://") for item in items)
    assert all(len(item["text"]) <= 800 for item in items)
    assert any("Flink" in item["text"] for item in items)
    assert not any("零信任" in item["text"] for item in items)


@pytest.mark.asyncio
async def test_all_job_families_retrieve_their_official_evidence():
    from src.evidence_dataset_service import load_jsonl
    from src.evidence_qa_service import gather_answer_evidence
    from src.job_data_service import SOURCE_SCORES

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    records = load_jsonl(
        ROOT / "data" / "evidence" / "official-standards-2026.jsonl"
    )

    async with Session() as session:
        session.add_all(
            [
                EvidenceRecord(
                    evidence_id=item["evidence_id"],
                    job_family_id=item["job_family_id"],
                    evidence_type=item["evidence_type"],
                    title=item["title"],
                    publisher=item["publisher"],
                    source_url=item["source_url"],
                    related_skill=item.get("related_skill"),
                    evidence_summary=item["evidence_summary"],
                    source_score=SOURCE_SCORES[item["evidence_type"]],
                )
                for item in records
            ]
        )
        await session.commit()

        for item in records[::3]:
            evidence = await gather_answer_evidence(
                session,
                item["related_skill"],
                family_code=item["job_family_id"],
                limit=3,
            )
            assert evidence
            assert evidence[0]["source_kind"] == "external"
            assert evidence[0]["family_code"] == item["job_family_id"]
            assert evidence[0]["source_url"].startswith("https://")

    await engine.dispose()


def test_validate_citations_rejects_missing_and_unknown_ids():
    from src.evidence_qa_service import validate_citations

    evidence = [{"citation_id": "K1"}, {"citation_id": "K2"}]

    assert validate_citations("Flink用于实时计算。[K1]", evidence) is True
    assert validate_citations("Flink用于实时计算。", evidence) is False
    assert validate_citations("Flink用于实时计算。[K99]", evidence) is False


@pytest.mark.asyncio
async def test_answer_uses_grounded_model_when_citations_are_valid(evidence_session):
    from src.evidence_qa_service import answer_knowledge_question

    async def invoke_model(_prompt, _model):
        return "数据工程师需要掌握Flink实时计算能力。[K1]"

    result = await answer_knowledge_question(
        evidence_session,
        "数据工程师需要哪些实时计算能力？",
        family_code="DATA_ENGINEER",
        model_invoker=invoke_model,
    )

    assert result["mode"] == "grounded_llm"
    assert result["citations_valid"] is True
    assert result["warning"] is None
    assert "[K1]" in result["answer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("model_behavior", ["error", "invalid_citation"])
async def test_answer_falls_back_when_model_is_unavailable_or_ungrounded(
    evidence_session, model_behavior
):
    from src.evidence_qa_service import answer_knowledge_question

    async def invoke_model(_prompt, _model):
        if model_behavior == "error":
            raise RuntimeError("model offline")
        return "模型生成了没有依据的回答。[K99]"

    result = await answer_knowledge_question(
        evidence_session,
        "数据工程师需要哪些实时计算能力？",
        family_code="DATA_ENGINEER",
        model_invoker=invoke_model,
    )

    assert result["mode"] == "extractive_fallback"
    assert result["citations_valid"] is True
    assert result["warning"]
    assert "[K1]" in result["answer"]


@pytest.mark.asyncio
async def test_answer_refuses_when_no_evidence_matches(evidence_session):
    from src.evidence_qa_service import NoEvidenceError, answer_knowledge_question

    with pytest.raises(NoEvidenceError):
        await answer_knowledge_question(
            evidence_session,
            "量子芯片光刻工艺",
            family_code="DATA_ENGINEER",
            model_invoker=lambda *_: None,
        )
