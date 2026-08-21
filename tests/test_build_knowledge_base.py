import json

import pytest


@pytest.mark.asyncio
async def test_build_command_writes_report_graph_and_is_idempotent(tmp_path):
    from src.build_knowledge_base import build_knowledge_base

    job = {
        "record_id": "BUILD-JD-0001",
        "collector_id": "T",
        "job_family_id": "DATA_ENGINEER",
        "job_title_raw": "数据工程师",
        "company_name": "示例科技",
        "industry": "互联网",
        "region": "北京",
        "source_name": "企业官网",
        "source_id": "ncss_public_jobs",
        "source_type": "university_recruitment",
        "source_url": "https://cnu.ncss.cn/student/jobs/BUILD-JD-0001",
        "source_record_id": "BUILD-JD-0001",
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
            "负责数据平台设计与建设，要求熟悉Python、Flink和Kafka实时计算，"
            "持续完成数据治理、任务调度、性能优化、线上运维和质量保障工作，"
            "并负责需求分析、技术方案评审、监控告警建设和跨团队交付协作。"
        ),
    }
    input_path = tmp_path / "jobs.json"
    input_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    data_dir = tmp_path / "data"

    first = await build_knowledge_base(input_path, data_dir=data_dir)
    original_report = first.report_path.read_text(encoding="utf-8")
    second = await build_knowledge_base(input_path, data_dir=data_dir)

    assert first.report_path.exists()
    assert first.graph_path.exists()
    graph = json.loads(first.graph_path.read_text(encoding="utf-8"))
    assert graph["nodes"] == []
    assert first.summary["review"] == 1
    assert first.summary["analysis"]["profiles_created"] == 0
    assert second.summary["idempotent"] is True
    assert second.summary["analysis"]["profiles_created"] == 0
    assert first.report_path.read_text(encoding="utf-8") == original_report
    assert json.loads(original_report)["idempotent"] is False
