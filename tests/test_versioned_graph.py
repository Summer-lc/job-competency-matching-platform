import json

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from model_class.base import Base


def _job(record_id: str, description: str) -> dict:
    return {
        "record_id": record_id,
        "collector_id": "T",
        "job_family_id": "DATA_ENGINEER",
        "job_title_raw": "数据工程师",
        "company_name": "示例科技",
        "industry": "互联网",
        "region": "北京",
        "source_name": "企业官网",
        "source_id": "ncss_public_jobs",
        "source_type": "university_recruitment",
        "source_url": f"https://cnu.ncss.cn/student/jobs/{record_id}",
        "source_record_id": record_id,
        "parser_name": "ncss",
        "parser_version": "v1",
        "collection_method": "public_json",
        "published_at": "2026-07-01",
        "published_at_evidence": "structured published_at field",
        "published_at_confidence": 0.95,
        "collected_at": "2026-07-22T10:00:00+08:00",
        "experience_requirement": "3-5年",
        "education_requirement": "本科",
        "salary_range": "20-30K",
        "job_description_raw": (
            description
            + "，并持续负责架构设计、任务调度、性能优化、线上运维和数据质量保障工作，"
            "参与需求分析、技术方案评审、监控告警建设和跨团队交付协作。"
        ),
    }


def _bytes(record: dict) -> bytes:
    return json.dumps(record, ensure_ascii=False).encode("utf-8")


@pytest_asyncio.fixture
async def populated_session():
    from model_class.job_competency import JobPosting
    import model_class.knowledge_base  # noqa: F401
    from src.import_service import import_job_file

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        await import_job_file(
            session,
            _bytes(
                _job(
                    "VG-JD-0001",
                    "负责实时数据平台设计与建设，要求熟悉Python和Flink实时计算。",
                )
            ),
            "initial.jsonl",
        )
        posting = await session.scalar(
            select(JobPosting).where(JobPosting.record_id == "VG-JD-0001")
        )
        posting.status = "valid"
        posting.gate_status = "valid"
        posting.provenance_status = "approved"
        posting.published_at_trusted = True
        posting.duplicate_of_id = None
        await session.flush()
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_unchanged_rebuild_does_not_create_empty_version(populated_session):
    from src.job_analysis_service import rebuild_analysis

    first = await rebuild_analysis(populated_session, family_codes={"DATA_ENGINEER"})
    second = await rebuild_analysis(populated_session, family_codes={"DATA_ENGINEER"})

    assert first["profiles_created"] == 1
    assert second["profiles_created"] == 0
    assert second["unchanged_families"] == ["DATA_ENGINEER"]


@pytest.mark.asyncio
async def test_changed_family_creates_evolution_event(populated_session):
    from model_class.job_competency import JobPosting
    from model_class.knowledge_base import EvolutionEvent
    from src.import_service import import_job_file
    from src.job_analysis_service import rebuild_analysis

    await rebuild_analysis(populated_session, family_codes={"DATA_ENGINEER"})
    await import_job_file(
        populated_session,
        _bytes(
            _job(
                "VG-JD-0002",
                "负责离线数据仓库开发与维护，要求熟悉Python、Spark和Kafka消息处理。",
            )
        ),
        "changed.jsonl",
    )
    posting = await populated_session.scalar(
        select(JobPosting).where(JobPosting.record_id == "VG-JD-0002")
    )
    posting.status = "valid"
    posting.gate_status = "valid"
    posting.provenance_status = "approved"
    posting.published_at_trusted = True
    posting.duplicate_of_id = None
    await populated_session.flush()
    result = await rebuild_analysis(populated_session, family_codes={"DATA_ENGINEER"})
    events = (await populated_session.execute(select(EvolutionEvent))).scalars().all()

    assert result["profiles_created"] == 1
    assert any(
        item.change_type == "added" and item.entity_type == "skill" and item.entity_key == "Kafka"
        for item in events
    )


@pytest.mark.asyncio
async def test_graph_contains_version_responsibility_scenario_and_evidence(populated_session):
    from src.job_analysis_service import graph_data, rebuild_analysis

    await rebuild_analysis(populated_session, family_codes={"DATA_ENGINEER"})
    graph = await graph_data(
        populated_session, family_code="DATA_ENGINEER", include_evidence=True
    )

    assert {node["type"] for node in graph["nodes"]} >= {
        "family",
        "job",
        "skill",
        "responsibility",
        "scenario",
        "evidence",
    }


@pytest.mark.asyncio
async def test_graph_contains_external_standard_evidence(populated_session):
    from model_class.job_competency import EvidenceRecord
    from src.job_analysis_service import graph_data, rebuild_analysis

    populated_session.add(
        EvidenceRecord(
            evidence_id="OFF-DATA-TEST",
            job_family_id="DATA_ENGINEER",
            evidence_type="technical_standard",
            title="Data engineering reference architecture",
            publisher="Official publisher",
            source_url="https://www.nist.gov/data-engineering-test",
            related_skill="data architecture",
            evidence_summary="Official evidence summary for graph verification.",
            source_score=0.95,
        )
    )
    await populated_session.flush()
    await rebuild_analysis(populated_session, family_codes={"DATA_ENGINEER"})

    graph = await graph_data(
        populated_session, family_code="DATA_ENGINEER", include_evidence=True
    )

    assert any(
        node["id"] == "evidence:external:OFF-DATA-TEST"
        and node["evidence_kind"] == "external_standard"
        for node in graph["nodes"]
    )
    assert {
        "source": "family:DATA_ENGINEER",
        "target": "evidence:external:OFF-DATA-TEST",
        "type": "supported_by",
    } in graph["edges"]


@pytest.mark.asyncio
async def test_graph_contains_external_evidence_without_a_job_profile(
    populated_session,
):
    from model_class.job_competency import EvidenceRecord
    from src.job_analysis_service import graph_data

    populated_session.add(
        EvidenceRecord(
            evidence_id="OFF-PROMPT-TEST",
            job_family_id="PROMPT_ENGINEER",
            evidence_type="official_document",
            title="Prompt engineering guide",
            publisher="Official publisher",
            source_url="https://www.nist.gov/prompt-test",
            related_skill="prompt engineering",
            evidence_summary="Official evidence summary for a family without a profile.",
            source_score=0.9,
        )
    )
    await populated_session.flush()

    graph = await graph_data(
        populated_session, family_code="PROMPT_ENGINEER", include_evidence=True
    )

    assert any(
        node["id"] == "family:PROMPT_ENGINEER" for node in graph["nodes"]
    )
    assert any(
        node["id"] == "evidence:external:OFF-PROMPT-TEST"
        for node in graph["nodes"]
    )


def test_neo4j_partition_supports_all_graph_node_and_edge_types():
    from src.job_graph_sync import partition_graph

    graph = {
        "nodes": [
            {"id": "family:F", "type": "family", "label": "岗位族"},
            {"id": "job:1", "type": "job", "label": "画像"},
            {"id": "skill:1", "type": "skill", "label": "Python"},
            {"id": "responsibility:1", "type": "responsibility", "label": "开发平台"},
            {"id": "scenario:1", "type": "scenario", "label": "互联网"},
            {"id": "evidence:1", "type": "evidence", "label": "原文"},
        ],
        "edges": [
            {"source": "family:F", "target": "job:1", "type": "has_version"},
            {"source": "job:1", "target": "skill:1", "type": "required"},
            {"source": "skill:1", "target": "evidence:1", "type": "supported_by"},
        ],
    }

    groups = partition_graph(graph)

    assert set(groups["nodes"]) == {
        "family",
        "job",
        "skill",
        "responsibility",
        "scenario",
        "evidence",
    }
    assert set(groups["edges"]) == {"HAS_VERSION", "REQUIRES_SKILL", "SUPPORTED_BY"}
