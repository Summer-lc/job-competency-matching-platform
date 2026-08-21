import json
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from config.DB_config import get_db
from model_class.base import Base
import model_class.job_competency  # noqa: F401
import model_class.knowledge_base  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]


def _job(index, family, title, year, skills, source):
    return {
        "record_id": f"T-JD-{index:04d}",
        "collector_id": "T",
        "job_family_id": family,
        "job_title_raw": title,
        "company_name": f"企业{index}",
        "industry": "软件和信息技术服务业",
        "region": "北京",
        "source_name": source,
        "source_id": "ncss_public_jobs",
        "source_type": "university_recruitment",
        "source_url": f"https://cnu.ncss.cn/student/jobs/{index}",
        "source_record_id": f"T-JD-{index:04d}",
        "parser_name": "ncss",
        "parser_version": "v1",
        "collection_method": "public_json",
        "published_at": f"{year}-06-01",
        "published_at_evidence": "structured published_at field",
        "published_at_confidence": 0.95,
        "collected_at": "2026-07-01T10:00:00+08:00",
        "experience_requirement": "3年",
        "education_requirement": "本科",
        "job_description_raw": (
            f"负责{title}相关平台建设，要求熟悉{skills}，"
            "持续完成架构设计、数据治理、性能优化、线上运维和质量保障工作，"
            "参与需求分析、技术方案评审、监控告警建设和跨团队交付协作。"
        ),
    }


@pytest_asyncio.fixture
async def competition_client():
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
        yield client
    await engine.dispose()


@pytest.mark.asyncio
async def test_health_and_frontend_are_product_focused(competition_client):
    health = await competition_client.get("/health")
    assert health.status_code == 200
    assert health.json()["service"] == "job-competency-platform"
    home = await competition_client.get("/")
    assert home.status_code == 200
    assert "岗位能力图谱" in home.text
    assert "学生作业" not in home.text
    assert "多源岗位智能分析" in home.text
    assert "核心业务闭环" in home.text
    assert "模型质量评测" in home.text
    assert "质量评测状态" in home.text

    prohibited_copy = (
        "XH-202621",
        "比赛核心闭环",
        "赛题要求",
        "参赛就绪",
        "最终参赛就绪",
        "至少100条JD",
        "均不低于90%",
        "目标 ≥",
        "单测覆盖门槛",
    )
    assert not [text for text in prohibited_copy if text in home.text]


@pytest.mark.asyncio
async def test_frontend_exposes_batch_quarantine_and_graph_filters(competition_client):
    html = (await competition_client.get("/")).text

    assert "导入批次" in html
    assert "异常隔离" in html
    assert "节点类型" in html
    assert "画像版本" in html
    assert "/api/data/import-batches" in html
    assert "/api/data/quarantine" in html


@pytest.mark.asyncio
async def test_frontend_exposes_knowledge_qa_center(competition_client):
    html = (await competition_client.get("/")).text

    assert "知识问答" in html
    assert 'id="knowledge-question"' in html
    assert 'id="knowledge-family"' in html
    assert 'id="knowledge-evidence"' in html
    assert "生成证据回答" in html
    assert "/api/knowledge/answer" in html
    assert "[K1]" in html


@pytest.mark.asyncio
async def test_openapi_exposes_all_competition_capabilities(competition_client):
    paths = (await competition_client.get("/openapi.json")).json()["paths"]
    assert {
        "/api/data/import",
        "/api/data/import-batches",
        "/api/data/quarantine",
        "/api/data/evidence/import",
        "/api/knowledge/search",
        "/api/knowledge/answer",
        "/api/extraction/jobs/{posting_id}",
        "/api/analysis/rebuild",
        "/api/analysis/emerging",
        "/api/analysis/evolution/{family_code}",
        "/api/graph",
        "/api/graph/versions/{family_code}",
        "/api/graph/sync",
        "/api/resumes/parse",
        "/api/matches",
        "/api/reviews",
        "/api/evaluation/run",
        "/api/evaluation/summary",
        "/api/hard-metrics/rebuild",
        "/api/hard-metrics/runs",
        "/api/hard-metrics/quality",
        "/api/hard-metrics/levels/{posting_id}",
        "/api/analysis/quarterly-profiles",
        "/api/acceptance/summary",
    }.issubset(paths)
    assert "/api/evaluation/runs" not in paths


@pytest.mark.asyncio
async def test_acceptance_endpoint_keeps_missing_metrics_unmeasured(competition_client):
    response = await competition_client.get("/api/acceptance/summary")

    assert response.status_code == 200
    payload = response.json()
    assert payload["minimum"]["overall"] == "not_measured"
    assert payload["minimum"]["metrics"]["jd_parsing_accuracy"]["status"] == (
        "not_measured"
    )
    assert payload["data_quality"]["metrics"]["usable_unique_job_postings"] == 0
    assert payload["data_quality"]["denominators"]["valid_rate"] == {
        "metric": "raw_job_postings",
        "count": 0,
    }
    assert payload["data_quality"]["latest_collection"] == {
        "batch": None,
        "run": None,
    }
    expected_internal = {
        "raw_job_postings",
        "usable_unique_job_postings",
        "job_families",
        "unknown_family_job_postings",
        "minimum_usable_samples_per_covered_family",
        "source_types",
        "source_domains",
        "source_domain_coverage",
        "maximum_single_domain_share",
        "trusted_publication_or_first_seen_coverage",
        "all_high_confidence_core_skills_confirmed",
        "sample_family_level_period_coverage",
        "jd_parsing_accuracy",
        "resume_extraction_accuracy",
        "matching_accuracy",
    }
    assert set(payload["internal"]["metrics"]) == expected_internal
    assert all(
        {"current", "target", "gap", "status"} <= set(metric)
        for metric in payload["internal"]["metrics"].values()
    )
    assert payload["internal"]["metrics"][
        "all_high_confidence_core_skills_confirmed"
    ]["status"] == "not_measured"
    assert payload["internal"]["metrics"]["source_domain_coverage"]["status"] == (
        "not_measured"
    )


@pytest.mark.asyncio
async def test_full_hard_metric_rebuild_requires_confirmation(competition_client):
    response = await competition_client.post(
        "/api/hard-metrics/rebuild", json={"mode": "full"}
    )

    assert response.status_code == 400
    assert "确认" in response.json()["detail"]


@pytest.mark.asyncio
async def test_hard_metric_quality_and_profile_filters_have_stable_empty_payloads(
    competition_client,
):
    quality = await competition_client.get("/api/hard-metrics/quality")
    profiles = await competition_client.get(
        "/api/analysis/quarterly-profiles",
        params={"level": "mid", "period_key": "2026-Q1"},
    )

    assert quality.status_code == 200
    assert quality.json()["total"] == 0
    assert profiles.status_code == 200
    assert profiles.json() == {"items": [], "total": 0}


@pytest.mark.asyncio
async def test_official_evidence_import_is_idempotent(competition_client):
    dataset = ROOT / "data" / "evidence" / "official-standards-2026.jsonl"
    payload = dataset.read_bytes()

    first = await competition_client.post(
        "/api/data/evidence/import",
        files={"file": (dataset.name, payload, "application/x-ndjson")},
    )
    second = await competition_client.post(
        "/api/data/evidence/import",
        files={"file": (dataset.name, payload, "application/x-ndjson")},
    )
    stats = await competition_client.get("/api/data/stats")

    assert first.status_code == 200
    assert first.json() == {
        "received": 24,
        "imported": 24,
        "skipped": 0,
        "errors": [],
    }
    assert second.status_code == 200
    assert second.json() == {
        "received": 24,
        "imported": 0,
        "skipped": 24,
        "errors": [],
    }
    assert stats.json()["evidence_records"] == 24


@pytest.mark.asyncio
async def test_incremental_import_api_exposes_batch_quarantine_and_search(competition_client):
    valid = _job(101, "DATA_ENGINEER", "数据工程师", 2026, "Python、Flink、Kafka", "企业官网")
    payload = json.dumps(valid, ensure_ascii=False).encode("utf-8") + b'\n{"broken":'

    response = await competition_client.post(
        "/api/data/import",
        files={"file": ("jobs.json", payload, "application/json")},
    )

    assert response.status_code == 200
    assert response.json()["batch_id"]
    assert response.json()["quarantined"] == 1
    batches = (await competition_client.get("/api/data/import-batches")).json()
    quarantine = (await competition_client.get("/api/data/quarantine")).json()
    search = (
        await competition_client.get(
            "/api/knowledge/search", params={"q": "Flink", "family_code": "DATA_ENGINEER"}
        )
    ).json()
    versions = (await competition_client.get("/api/graph/versions/DATA_ENGINEER")).json()

    assert batches["total"] == 1
    assert quarantine["total"] == 1
    assert search["items"][0]["record_id"] == "T-JD-0101"
    assert versions["total"] == 0


@pytest.mark.asyncio
async def test_import_api_rejects_registry_impersonation_capability_fields(
    competition_client,
):
    crafted = _job(
        777,
        "DATA_ENGINEER",
        "数据工程师",
        2026,
        "Python、Flink、Kafka",
        "Forged NCSS display",
    )
    crafted.update(
        {
            "import_authorization": "verified_collection",
            "authorized_source_ids": ["ncss_public_jobs"],
            "authorized_manual_source_ids": ["ncss_public_jobs"],
            "published_at_trusted": True,
        }
    )

    response = await competition_client.post(
        "/api/data/import",
        files={
            "file": (
                "crafted.jsonl",
                json.dumps(crafted, ensure_ascii=False).encode("utf-8"),
                "application/jsonl",
            )
        },
    )

    assert response.status_code == 200
    assert response.json()["review"] == 1
    assert response.json()["analysis"]["profiles_created"] == 0
    stats = (await competition_client.get("/api/data/stats")).json()
    assert stats["valid_postings"] == 0
    assert stats["job_profiles"] == 0


@pytest.mark.asyncio
async def test_import_api_returns_413_before_unbounded_upload_read(
    competition_client, monkeypatch
):
    import src.api as api_module

    monkeypatch.setattr(api_module, "MAX_IMPORT_BYTES", 32)

    response = await competition_client.post(
        "/api/data/import",
        files={"file": ("jobs.jsonl", b"x" * 33, "application/jsonl")},
    )

    assert response.status_code == 413


@pytest.mark.asyncio
async def test_evidence_import_api_returns_413_before_unbounded_upload_read(
    competition_client, monkeypatch
):
    import src.api as api_module

    monkeypatch.setattr(api_module, "MAX_EVIDENCE_IMPORT_BYTES", 32)

    response = await competition_client.post(
        "/api/data/evidence/import",
        files={"file": ("evidence.jsonl", b"x" * 33, "application/jsonl")},
    )

    assert response.status_code == 413


@pytest.mark.asyncio
async def test_evidence_import_api_maps_extreme_json_depth_to_413(
    competition_client,
):
    payload = ("[" * 1_100 + "0" + "]" * 1_100).encode()
    response = await competition_client.post(
        "/api/data/evidence/import",
        files={"file": ("evidence.json", payload, "application/json")},
    )

    assert response.status_code == 413


@pytest.mark.asyncio
async def test_knowledge_answer_endpoint_returns_grounded_evidence(
    competition_client, monkeypatch
):
    async def fake_answer(
        _db, question, *, family_code=None, limit=6, model=None
    ):
        assert question == "数据工程师需要哪些实时计算能力？"
        assert family_code == "DATA_ENGINEER"
        assert limit == 5
        assert model is None
        return {
            "answer": "需要掌握Flink实时计算。[K1]",
            "mode": "grounded_llm",
            "family_code": family_code,
            "citations_valid": True,
            "evidence": [
                {
                    "citation_id": "K1",
                    "source_kind": "jd",
                    "evidence_type": "job_description",
                    "family_code": family_code,
                    "title": "数据工程师",
                    "organization": "示例企业",
                    "record_id": "JD-001",
                    "review_status": "valid",
                    "text": "负责Flink实时计算平台建设。",
                    "source_url": "https://example.com/jobs/1",
                    "score": 2.1,
                }
            ],
            "warning": None,
        }

    monkeypatch.setattr("src.api.answer_knowledge_question", fake_answer)
    response = await competition_client.post(
        "/api/knowledge/answer",
        json={
            "question": "数据工程师需要哪些实时计算能力？",
            "family_code": "DATA_ENGINEER",
            "limit": 5,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "grounded_llm"
    assert payload["citations_valid"] is True
    assert payload["evidence"][0]["citation_id"] == "K1"


@pytest.mark.asyncio
async def test_knowledge_answer_endpoint_rejects_question_without_evidence(
    competition_client,
):
    response = await competition_client.post(
        "/api/knowledge/answer",
        json={"question": "量子芯片光刻工艺", "family_code": "DATA_ENGINEER"},
    )

    assert response.status_code == 422
    assert "没有足够证据" in response.json()["detail"]


@pytest.mark.asyncio
async def test_automatic_evaluation_run_persists_measured_results(competition_client):
    benchmark = [
        {
            "case_id": "JD-001",
            "task": "jd_parsing",
            "input": {"text": "要求掌握Python。"},
            "expected": {"required_skills": ["Python"], "preferred_skills": []},
        },
        {
            "case_id": "CV-001",
            "task": "resume_extraction",
            "input": {"text": "3年Java开发经验，本科学历。"},
            "expected": {
                "skills": ["Java"],
                "experience_years": 3,
                "education": ["本科"],
            },
        },
        {
            "case_id": "MATCH-001",
            "task": "matching",
            "input": {
                "resume": {
                    "skills": ["Java"],
                    "recent_skills": ["Java"],
                    "experience_years": 3,
                    "projects": ["后端项目"],
                },
                "job_profile": {
                    "name": "Java开发工程师",
                    "required_skills": ["Java"],
                    "preferred_skills": [],
                    "required_years": 3,
                },
            },
            "expected": {"band": "high"},
        },
    ]
    payload = "\n".join(json.dumps(item, ensure_ascii=False) for item in benchmark).encode()
    response = await competition_client.post(
        "/api/evaluation/run",
        files={"file": ("team-benchmark.jsonl", payload, "application/jsonl")},
    )

    assert response.status_code == 200
    report = response.json()
    assert len(report["results"]) == 3
    assert report["readiness"]["jd_case_count"] == 1
    assert report["readiness"]["meets_jd_case_requirement"] is False
    assert report["readiness"]["competition_ready"] is False

    summary = (await competition_client.get("/api/evaluation/summary")).json()
    assert set(summary["latest"]) == {"jd_parsing", "resume_extraction", "matching"}
    assert summary["latest"]["jd_parsing"]["dataset_name"] == "team-benchmark.jsonl"
    assert summary["readiness"]["jd_case_count"] == 1
    assert len(summary["runs"]) == 3

    partial = json.dumps(benchmark[0], ensure_ascii=False).encode("utf-8")
    partial_response = await competition_client.post(
        "/api/evaluation/run",
        files={"file": ("partial.json", partial, "application/json")},
    )
    assert partial_response.status_code == 200

    latest_batch = (await competition_client.get("/api/evaluation/summary")).json()
    assert set(latest_batch["latest"]) == {"jd_parsing"}
    assert latest_batch["readiness"]["all_metrics_present"] is False
    assert len(latest_batch["runs"]) == 4


@pytest.mark.asyncio
async def test_direct_api_batch_cannot_enter_formal_closed_loop(competition_client):
    jobs = [
        _job(1, "AI_AGENT_ENGINEER", "AI智能体应用工程师", 2025, "Python、LangChain、RAG", "来源A"),
        _job(2, "AI_AGENT_ENGINEER", "AI智能体应用工程师", 2026, "Python、LangChain、RAG、AI Agent", "来源B"),
        _job(3, "AI_AGENT_ENGINEER", "智能体平台工程师", 2026, "Python、RAG、MCP、向量数据库", "来源C"),
        _job(4, "AI_AGENT_ENGINEER", "Agent开发工程师", 2026, "Python、AI Agent、MCP", "来源D"),
        _job(5, "JAVA_DEVELOPER", "Java开发工程师", 2024, "Java、Spring Boot、MySQL", "来源E"),
        _job(6, "JAVA_DEVELOPER", "Java开发工程师", 2026, "Java、Spring Boot、Docker、Kubernetes", "来源F"),
    ]
    payload = "\n".join(json.dumps(row, ensure_ascii=False) for row in jobs).encode("utf-8")
    imported = await competition_client.post(
        "/api/data/import",
        files={"file": ("jobs.jsonl", payload, "application/jsonl")},
    )
    assert imported.status_code == 200
    assert imported.json()["imported"] == 6
    assert imported.json()["review"] + imported.json()["duplicates"] == 6
    assert imported.json()["analysis"]["profiles_created"] == 0

    stats = (await competition_client.get("/api/data/stats")).json()
    assert stats["job_postings"] == 6
    assert stats["valid_postings"] == 0
    assert stats["job_profiles"] == 0
    assert stats["sources"] == 1
    assert stats["skills"] >= 8

    rebuilt = await competition_client.post("/api/analysis/rebuild")
    assert rebuilt.status_code == 200
    assert rebuilt.json()["profiles_created"] == 0
    assert rebuilt.json()["unchanged_families"] == []

    graph = (await competition_client.get("/api/graph")).json()
    assert graph["nodes"] == []
