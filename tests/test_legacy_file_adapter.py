from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.job_collection.models import SourceDefinition
from src.job_collection.source_registry import SourceRegistry


def _source() -> SourceDefinition:
    return SourceDefinition.model_validate(
        {
            "source_id": "zhaopin_legacy_import",
            "source_name": "智联招聘授权历史文件导入",
            "source_type": "authorized_platform",
            "market_scope": "china",
            "base_url": "https://www.zhaopin.com",
            "allowed_paths": ["/"],
            "collection_mode": "file_import",
            "compliance_status": "manual_only",
            "compliance_note": "仅处理团队确认授权的本地历史文件，禁止联网。",
            "rate_limit_seconds": 5.0,
            "max_pages": 1,
            "max_records": 10_000,
            "parser_name": "zhaopin_legacy",
            "parser_version": "v1",
            "enabled": True,
        }
    )


def _row(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "record_id": "A-JD-0001",
        "collector_id": "A",
        "job_family_id": "PYTHON_BACKEND",
        "job_title_raw": "Java后端开发工程师",
        "company_name": "测试科技有限公司",
        "industry": "软件和信息技术服务业",
        "region": "北京",
        "source_name": "智联招聘",
        "source_url": "http://www.zhaopin.com/jobdetail/CC1J1.htm?refcode=1",
        "published_at": "2026-08-06",
        "collected_at": "2026-08-07T21:03:09+08:00",
        "experience_requirement": "3-5年",
        "education_requirement": "本科",
        "salary_range": "20-30K",
        "job_description_raw": (
            "负责Java后端服务设计与开发，使用Spring Boot、MySQL和Redis完成接口、"
            "性能优化、自动化测试及生产维护，参与需求评审并编写技术文档；负责服务拆分、"
            "数据库设计、代码评审、故障排查和持续交付，能够独立完成需求分析、方案设计、"
            "开发联调、上线验证与运行监控，并持续改进系统稳定性和可维护性。"
        ),
        "search_keyword": "Python后端工程师",
    }
    value.update(overrides)
    return value


def _write(path: Path, *rows: dict[str, object]) -> bytes:
    payload = b"\n".join(
        json.dumps(row, ensure_ascii=False).encode("utf-8") for row in rows
    ) + b"\n"
    path.write_bytes(payload)
    return payload


def test_authorized_file_normalizes_url_corrects_family_and_records_hashes(tmp_path):
    from src.job_collection.adapters.legacy_file import LegacyFileAdapter

    path = tmp_path / "jobs.jsonl"
    payload = _write(path, _row())
    source = _source()
    adapter = LegacyFileAdapter(source=source, registry=SourceRegistry([source]))

    records = adapter.load_file(
        path,
        run_id="legacy-file-run",
        authorization_note="团队确认该本地导出仅用于比赛研究。",
        max_records=10,
    )

    assert len(records) == 1
    record = records[0]
    assert record.source_url.startswith("https://www.zhaopin.com/")
    assert record.job_family_id == "JAVA_DEVELOPER"
    assert record.published_at_trusted is True
    assert record.adapter_extra["supplied_job_family_id"] == "PYTHON_BACKEND"
    assert record.adapter_extra["input_file_sha256"] == hashlib.sha256(payload).hexdigest()
    assert record.adapter_extra["input_line_number"] == 1
    assert record.adapter_extra["quality_gate"]["status"] == "valid"


def test_implausible_publication_date_is_discarded_without_rejecting_job(tmp_path):
    from src.job_collection.adapters.legacy_file import LegacyFileAdapter

    path = tmp_path / "jobs.jsonl"
    row = _row(published_at="1994-09-28")
    row["job_description_raw"] = str(row["job_description_raw"]) * 2
    _write(path, row)
    source = _source()
    adapter = LegacyFileAdapter(source=source, registry=SourceRegistry([source]))

    record = adapter.load_file(
        path,
        run_id="legacy-old-date",
        authorization_note="团队确认授权。",
        max_records=10,
    )[0]

    assert record.published_at is None
    assert record.published_at_trusted is False
    assert record.adapter_extra["quality_gate"]["status"] == "valid"
    assert record.adapter_extra["legacy_publication_issue"] == (
        "implausible_legacy_publication"
    )
    assert "untrusted_publication" not in record.adapter_extra["quality_gate"]["issue_codes"]


def test_supported_reviewed_family_hint_does_not_force_manual_review(tmp_path):
    from src.job_collection.adapters.legacy_file import LegacyFileAdapter

    path = tmp_path / "jobs.jsonl"
    description = (
        "负责建设后端服务，使用 Spring Boot、Spring Cloud 和 MyBatis 完成接口开发、"
        "数据库设计、自动化测试、性能优化、故障排查、发布监控和技术文档维护。"
    ) * 5
    _write(
        path,
        _row(
            job_family_id="JAVA_DEVELOPER",
            job_title_raw="后端平台工程师",
            job_description_raw=description,
        ),
    )
    source = _source()
    adapter = LegacyFileAdapter(source=source, registry=SourceRegistry([source]))

    record = adapter.load_file(
        path,
        run_id="legacy-reviewed-hint",
        authorization_note="团队确认授权并复核岗位族。",
        max_records=10,
    )[0]

    classification = record.adapter_extra["family_classification"]
    assert classification["status"] == "auto"
    assert classification["reason"] == "strong_capability_only_evidence"
    assert record.job_family_id == "JAVA_DEVELOPER"
    assert record.adapter_extra["quality_gate"]["status"] == "valid"


def test_non_zhaopin_url_is_rejected_without_network_access(tmp_path):
    from src.job_collection.adapters.legacy_file import LegacyFileAdapter

    path = tmp_path / "jobs.jsonl"
    _write(path, _row(source_url="https://example.com/jobs/1"))
    source = _source()
    adapter = LegacyFileAdapter(source=source, registry=SourceRegistry([source]))

    records = adapter.load_file(
        path,
        run_id="legacy-wrong-host",
        authorization_note="团队确认授权。",
        max_records=10,
    )

    assert records == ()
    assert "source_url" in adapter.errors[0]["message"]


def test_duplicate_json_keys_are_rejected(tmp_path):
    from src.job_collection.adapters.legacy_file import (
        LegacyFileAdapter,
        LegacyFileAdapterError,
    )

    path = tmp_path / "jobs.jsonl"
    path.write_text(
        '{"record_id":"one","record_id":"two"}\n', encoding="utf-8"
    )
    source = _source()
    adapter = LegacyFileAdapter(source=source, registry=SourceRegistry([source]))

    with pytest.raises(LegacyFileAdapterError, match="duplicate JSON key"):
        adapter.load_file(
            path,
            run_id="legacy-duplicate-key",
            authorization_note="团队确认授权。",
            max_records=10,
        )


def test_max_records_stops_before_parsing_later_invalid_lines(tmp_path):
    from src.job_collection.adapters.legacy_file import LegacyFileAdapter

    path = tmp_path / "jobs.jsonl"
    payload = json.dumps(_row(), ensure_ascii=False).encode("utf-8") + b"\nnot-json\n"
    path.write_bytes(payload)
    source = _source()
    adapter = LegacyFileAdapter(source=source, registry=SourceRegistry([source]))

    records = adapter.load_file(
        path,
        run_id="legacy-bounded",
        authorization_note="团队确认授权。",
        max_records=1,
    )

    assert len(records) == 1


def test_schema_invalid_rows_are_reported_without_losing_valid_rows(tmp_path):
    from src.job_collection.adapters.legacy_file import LegacyFileAdapter

    path = tmp_path / "jobs.jsonl"
    _write(path, _row(), _row(record_id="bad", job_title_raw="", job_description_raw=""))
    source = _source()
    adapter = LegacyFileAdapter(source=source, registry=SourceRegistry([source]))

    records = adapter.load_file(
        path,
        run_id="legacy-partial-quality",
        authorization_note="团队确认授权。",
        max_records=10,
    )

    assert len(records) == 1
    assert adapter.errors == (
        {
            "line": 2,
            "code": "record_validation_error",
            "message": adapter.errors[0]["message"],
        },
    )
    assert "normalization" in adapter.errors[0]["message"]
