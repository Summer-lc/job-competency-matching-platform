from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.job_collection.models import SourceDefinition
from src.job_collection.source_registry import SourceRegistry


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "authorized_exports"


def _source(source_id: str, base_url: str, parser_name: str) -> SourceDefinition:
    return SourceDefinition.model_validate(
        {
            "source_id": source_id,
            "source_name": f"{source_id}授权岗位导出",
            "source_type": "authorized_platform",
            "market_scope": "china",
            "base_url": base_url,
            "allowed_paths": ["/"],
            "collection_mode": "file_import",
            "compliance_status": "manual_only",
            "compliance_note": "人工审核日期 2026-08-12：仅处理有效授权覆盖的导出文件。",
            "rate_limit_seconds": 5.0,
            "max_pages": 1,
            "max_records": 10_000,
            "parser_name": parser_name,
            "parser_version": "v1",
            "enabled": True,
        }
    )


def _adapter(source: SourceDefinition):
    from src.job_collection.adapters.authorized_export import AuthorizedExportAdapter

    return AuthorizedExportAdapter(
        source=source,
        registry=SourceRegistry([source]),
    )


def test_boss_jsonl_normalizes_provenance_and_removes_contact_pii():
    source = _source(
        "boss_zhipin_authorized",
        "https://www.zhipin.com",
        "boss_authorized_export",
    )
    path = FIXTURES / "boss_jobs.jsonl"
    adapter = _adapter(source)

    record = adapter.load_file(
        path,
        run_id="authorized-export-fixture",
        authorization_reference="AUTH-BOSS-2026-001",
        authorization_scope="全国公开招聘岗位批量导出，仅限比赛研究。",
        max_records=20,
    )[0]

    assert record.source_id == "boss_zhipin_authorized"
    assert record.source_domain == "www.zhipin.com"
    assert record.collection_method == "file_import"
    assert record.adapter_extra["authorization_reference"] == "AUTH-BOSS-2026-001"
    assert record.adapter_extra["input_file_sha256"] == hashlib.sha256(
        path.read_bytes()
    ).hexdigest()
    assert record.job_title_raw == "Java开发工程师"
    assert record.company_name == "示例科技有限公司"
    assert record.job_family_id == "JAVA_DEVELOPER"
    assert record.published_at_trusted is True
    assert "13800138000" not in record.job_description_raw
    assert "fixture@example.test" not in record.model_dump_json()
    assert record.adapter_extra["pii_removed"] is True


@pytest.mark.parametrize(
    ("filename", "source_id", "base_url", "parser_name"),
    [
        (
            "job51_jobs.csv",
            "job51_authorized",
            "https://we.51job.com",
            "job51_authorized_export",
        ),
        (
            "liepin_jobs.json",
            "liepin_authorized",
            "https://www.liepin.com",
            "liepin_authorized_export",
        ),
        (
            "lagou_jobs.jsonl",
            "lagou_authorized",
            "https://www.lagou.com",
            "lagou_authorized_export",
        ),
    ],
)
def test_json_array_csv_and_jsonl_use_the_same_canonical_contract(
    filename, source_id, base_url, parser_name
):
    source = _source(source_id, base_url, parser_name)
    adapter = _adapter(source)

    records = adapter.load_file(
        FIXTURES / filename,
        run_id=f"{source_id}-fixture",
        authorization_reference=f"AUTH-{source_id}-001",
        authorization_scope="全国公开招聘岗位批量导出，仅限比赛研究。",
        max_records=20,
    )

    assert len(records) == 1
    assert records[0].source_id == source_id
    assert records[0].job_family_id == "JAVA_DEVELOPER"
    assert records[0].adapter_extra["input_row_number"] >= 1
    assert adapter.errors == ()


def test_external_source_url_is_rejected_without_losing_valid_rows(tmp_path):
    source = _source(
        "boss_zhipin_authorized",
        "https://www.zhipin.com",
        "boss_authorized_export",
    )
    rows = [
        json.loads((FIXTURES / "boss_jobs.jsonl").read_text(encoding="utf-8")),
        json.loads((FIXTURES / "boss_jobs.jsonl").read_text(encoding="utf-8")),
    ]
    rows[1]["岗位ID"] = "BOSS-002"
    rows[1]["职位链接"] = "https://example.com/jobs/BOSS-002"
    path = tmp_path / "jobs.jsonl"
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    adapter = _adapter(source)

    records = adapter.load_file(
        path,
        run_id="wrong-domain-fixture",
        authorization_reference="AUTH-BOSS-2026-001",
        authorization_scope="全国公开招聘岗位批量导出，仅限比赛研究。",
        max_records=20,
    )

    assert len(records) == 1
    assert adapter.errors[0]["code"] == "record_validation_error"
    assert "source_url" in str(adapter.errors[0]["message"])


def test_authorized_export_record_offset_selects_deterministic_batch(tmp_path):
    source = _source(
        "boss_zhipin_authorized",
        "https://www.zhipin.com",
        "boss_authorized_export",
    )
    template = json.loads(
        (FIXTURES / "boss_jobs.jsonl").read_text(encoding="utf-8")
    )
    rows = []
    for index in range(1, 4):
        row = dict(template)
        row["岗位ID"] = f"BOSS-{index:03d}"
        row["职位链接"] = f"https://www.zhipin.com/job_detail/BOSS-{index:03d}.html"
        rows.append(row)
    path = tmp_path / "jobs.jsonl"
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )

    record = _adapter(source).load_file(
        path,
        run_id="offset-fixture",
        authorization_reference="AUTH-BOSS-2026-001",
        authorization_scope="全国公开招聘岗位批量导出，仅限比赛研究。",
        max_records=1,
        record_offset=1,
    )[0]

    assert record.source_record_id == "BOSS-002"
    assert record.adapter_extra["input_row_number"] == 2


@pytest.mark.parametrize("record_offset", [-1, True])
def test_authorized_export_rejects_invalid_record_offset(record_offset):
    source = _source(
        "boss_zhipin_authorized",
        "https://www.zhipin.com",
        "boss_authorized_export",
    )

    with pytest.raises(ValueError, match="record_offset"):
        _adapter(source).load_file(
            FIXTURES / "boss_jobs.jsonl",
            run_id="invalid-offset",
            authorization_reference="AUTH-BOSS-2026-001",
            authorization_scope="全国公开招聘岗位批量导出，仅限比赛研究。",
            max_records=1,
            record_offset=record_offset,
        )


def test_csv_formula_prefixed_cell_is_rejected(tmp_path):
    path = tmp_path / "jobs.csv"
    path.write_text(
        "职位ID,职位名称,企业名称,城市,职位描述,更新日期,原始链接\n"
        "JOB51-002,Java开发工程师,=HYPERLINK(\"https://bad.test\"),北京,"
        "负责Java开发并使用Spring Boot和MyBatis完成接口数据库测试发布监控故障排查及技术文档维护,"
        "2026-08-10T09:00:00+08:00,https://we.51job.com/job/JOB51-002.html\n",
        encoding="utf-8",
    )
    source = _source(
        "job51_authorized",
        "https://we.51job.com",
        "job51_authorized_export",
    )

    from src.job_collection.adapters.authorized_export import (
        AuthorizedExportAdapterError,
    )

    with pytest.raises(AuthorizedExportAdapterError, match="formula"):
        _adapter(source).load_file(
            path,
            run_id="csv-formula-fixture",
            authorization_reference="AUTH-JOB51-001",
            authorization_scope="全国公开招聘岗位批量导出，仅限比赛研究。",
            max_records=20,
        )
