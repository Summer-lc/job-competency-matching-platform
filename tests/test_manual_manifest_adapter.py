from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.job_collection.models import SourceDefinition, UnifiedJobRecord
from src.job_collection.source_registry import CollectionBlocked, SourceRegistry
from src.job_data_service import assess_job_quality, prepare_job_record


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "manual" / "company-job.html"
EXAMPLE_MANIFEST = (
    PROJECT_ROOT / "data" / "collection_manifests" / "company-official.example.jsonl"
)
MODULE_NAME = "src.job_collection.adapters.manual_manifest"


def make_source(**overrides: object) -> SourceDefinition:
    payload = {
        "source_id": "company_official_manifest",
        "source_name": "企业官方岗位人工清单",
        "source_type": "company_official",
        "market_scope": "china",
        "base_url": "https://company-official.invalid",
        "allowed_paths": ["/"],
        "collection_mode": "manual_url_manifest",
        "compliance_status": "manual_only",
        "compliance_note": "仅处理人工审核并在本地提供的官方岗位内容；禁止联网。",
        "rate_limit_seconds": 5.0,
        "max_pages": 1,
        "max_records": 500,
        "parser_name": "company_manifest",
        "parser_version": "v1",
        "enabled": True,
    }
    payload.update(overrides)
    return SourceDefinition.model_validate(payload)


def base_record(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "source_name": "清洗测试企业官方招聘",
        "source_url": "https://careers.example.test/jobs/python-42?lang=zh",
        "company_name": "清洗测试企业有限公司",
        "collection_authorization_note": "2026-08-05 人工确认：公开页面导出，仅限本地研究处理。",
        "exported_html_path": "company-job.html",
    }
    payload.update(overrides)
    return payload


def write_manifest(
    tmp_path: Path,
    records: list[dict[str, object]],
    *,
    copy_fixture: bool = True,
) -> Path:
    if copy_fixture:
        (tmp_path / "company-job.html").write_bytes(FIXTURE_PATH.read_bytes())
    manifest_path = tmp_path / "manifest.jsonl"
    manifest_path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def make_adapter(adapter_module, **source_overrides: object):
    source = make_source(**source_overrides)
    return adapter_module.ManualManifestAdapter(
        source=source,
        registry=SourceRegistry([source]),
    )


@pytest.fixture
def adapter_module():
    if importlib.util.find_spec(MODULE_NAME) is None:
        pytest.skip("covered by the adapter module existence test")
    return importlib.import_module(MODULE_NAME)


def test_manual_manifest_adapter_module_exists():
    assert importlib.util.find_spec(MODULE_NAME) is not None


@pytest.mark.parametrize(
    "missing_field",
    [
        "source_name",
        "source_url",
        "company_name",
        "collection_authorization_note",
    ],
)
def test_manifest_requires_reviewed_provenance_fields(
    adapter_module, tmp_path, missing_field
):
    record = base_record()
    record.pop(missing_field)
    manifest = write_manifest(tmp_path, [record])

    with pytest.raises(adapter_module.ManualManifestError, match=missing_field):
        make_adapter(adapter_module).load_manifest(
            manifest,
            run_id="manual-run-001",
            collected_at=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
        )


def test_manifest_requires_exactly_one_local_content_input(adapter_module, tmp_path):
    no_input = base_record()
    no_input.pop("exported_html_path")
    both_inputs = base_record(
        job_title_raw="Python 后端工程师",
        job_description_raw="负责 Python 和 FastAPI 服务开发、测试与维护，持续改进接口可靠性和工程质量。",
    )

    for record in (no_input, both_inputs):
        manifest = write_manifest(tmp_path, [record])
        with pytest.raises(
            adapter_module.ManualManifestError,
            match="exactly one.*exported_html_path.*job_description_raw",
        ):
            make_adapter(adapter_module).load_manifest(
                manifest,
                run_id="manual-run-001",
                collected_at=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
            )


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../outside.html",
        "sub/../../outside.html",
        "/absolute/company-job.html",
        "C:\\outside\\company-job.html",
        "C:outside\\company-job.html",
        "\\\\server\\share\\company-job.html",
        "//server/share/company-job.html",
        "https://careers.example.test/jobs/42",
        "file:///tmp/company-job.html",
        "%2e%2e/outside.html",
        "%252e%252e/outside.html",
        "sub%2f..%2foutside.html",
        "bad%ZZpath.html",
    ],
)
def test_exported_html_path_rejects_remote_or_out_of_scope_values(
    adapter_module, tmp_path, unsafe_path
):
    manifest = write_manifest(
        tmp_path,
        [base_record(exported_html_path=unsafe_path)],
        copy_fixture=False,
    )

    with pytest.raises(adapter_module.ManualManifestError, match="exported_html_path"):
        make_adapter(adapter_module).load_manifest(
            manifest,
            run_id="manual-run-001",
            collected_at=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
        )


def test_manual_only_adapter_never_builds_network_or_redirect_requests(adapter_module):
    adapter = make_adapter(adapter_module)

    with pytest.raises(CollectionBlocked, match="manual_only.*network"):
        adapter.build_list_request("Python", offset=0, limit=1)
    with pytest.raises(CollectionBlocked, match="manual_only.*network"):
        adapter.build_detail_url("https://careers.example.test/jobs/42")
    with pytest.raises(CollectionBlocked, match="manual_only.*redirect"):
        adapter.validate_redirect(
            "https://careers.example.test/jobs/42",
            "/jobs/43",
        )


def test_local_html_is_parsed_normalized_classified_and_quality_checked(
    adapter_module, tmp_path
):
    manifest = write_manifest(tmp_path, [base_record()])
    content = (tmp_path / "company-job.html").read_bytes()

    records = make_adapter(adapter_module).load_manifest(
        manifest,
        run_id="manual-run-001",
        collected_at=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
    )

    assert len(records) == 1
    record = records[0]
    assert isinstance(record, UnifiedJobRecord)
    assert record.source_id == "company_official_manifest"
    assert record.source_name == "清洗测试企业官方招聘"
    assert record.source_url == "https://careers.example.test/jobs/python-42?lang=zh"
    assert record.source_domain == "careers.example.test"
    assert record.company_name == "清洗测试企业有限公司"
    assert record.job_title_raw == "Python 后端工程师"
    assert record.job_family_id == "PYTHON_BACKEND"
    assert "FastAPI" in record.job_description_raw
    assert record.snapshot_hash == hashlib.sha256(content).hexdigest()
    assert record.compliance_note == base_record()["collection_authorization_note"]
    assert (
        record.adapter_extra["collection_authorization_note"] == record.compliance_note
    )
    assert record.adapter_extra["content_input_type"] == "exported_html_path"
    assert record.adapter_extra["manifest_line_number"] == 1
    assert record.adapter_extra["family_classification"]["status"] == "auto"
    assert record.adapter_extra["quality_gate"]["status"] == "valid"

    prepared = prepare_job_record(record.model_dump(mode="json"))
    assert assess_job_quality(prepared) == []


def test_pre_extracted_description_uses_local_bytes_without_file_access(
    adapter_module, tmp_path
):
    description = (
        "负责 Python 服务和 FastAPI 接口的设计、开发、自动化测试与维护，"
        "使用 PostgreSQL 完成可靠的数据存储，并持续改进部署流程。"
    )
    record = base_record(
        job_title_raw="Python 后端工程师",
        job_description_raw=description,
    )
    record.pop("exported_html_path")
    manifest = write_manifest(tmp_path, [record], copy_fixture=False)

    result = make_adapter(adapter_module).load_manifest(
        manifest,
        run_id="manual-run-002",
        collected_at=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
    )[0]

    assert result.job_description_raw == description.replace("，", ",")
    assert (
        result.snapshot_hash == hashlib.sha256(description.encode("utf-8")).hexdigest()
    )
    assert result.adapter_extra["original_content_hash"] == result.snapshot_hash
    assert result.adapter_extra["content_input_type"] == "job_description_raw"


def test_inline_hash_uses_exact_submitted_text_before_pydantic_stripping(
    adapter_module, tmp_path
):
    description = (
        "\n  负责 Python 服务和 FastAPI 接口设计、开发、自动化测试与维护，"
        "使用 PostgreSQL 建设稳定的数据处理流程。\t "
    )
    record = base_record(
        job_title_raw="Python 后端工程师",
        job_description_raw=description,
        source_record_id="inline-42",
    )
    record.pop("exported_html_path")
    manifest = write_manifest(tmp_path, [record], copy_fixture=False)

    result = make_adapter(adapter_module).load_manifest(
        manifest,
        run_id="manual-run-inline-hash",
        collected_at=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
    )[0]

    expected = hashlib.sha256(description.encode("utf-8")).hexdigest()
    assert result.snapshot_hash == expected
    assert result.adapter_extra["original_content_hash"] == expected


def test_manifest_line_hash_is_canonical_across_key_order_and_whitespace(
    adapter_module, tmp_path
):
    record = base_record(source_record_id="canonical-42")
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = write_manifest(first_dir, [record])
    (second_dir / "company-job.html").write_bytes(FIXTURE_PATH.read_bytes())
    second = second_dir / "manifest.jsonl"
    reversed_record = dict(reversed(list(record.items())))
    second.write_text(
        json.dumps(reversed_record, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    adapter = make_adapter(adapter_module)
    kwargs = {
        "run_id": "manual-run-canonical-hash",
        "collected_at": datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
    }
    first_result = adapter.load_manifest(first, **kwargs)[0]
    second_result = adapter.load_manifest(second, **kwargs)[0]

    assert (
        first_result.adapter_extra["manifest_line_hash"]
        == second_result.adapter_extra["manifest_line_hash"]
    )


def test_same_original_source_id_is_scoped_by_company_origin(adapter_module, tmp_path):
    description = (
        "负责 Python 服务和 FastAPI 接口设计、开发、自动化测试与维护，"
        "使用 PostgreSQL 建设稳定可靠的数据处理和部署流程。"
    )
    records = []
    for domain in ("careers.alpha.test", "jobs.beta.test"):
        record = base_record(
            source_url=f"https://{domain}/jobs/42",
            source_record_id="job-42",
            job_title_raw="Python 后端工程师",
            job_description_raw=description,
        )
        record.pop("exported_html_path")
        records.append(record)
    manifest = write_manifest(tmp_path, records, copy_fixture=False)

    results = make_adapter(adapter_module).load_manifest(
        manifest,
        run_id="manual-run-scoped-id",
        collected_at=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
    )

    assert results[0].source_record_id != results[1].source_record_id
    assert results[0].record_id != results[1].record_id
    assert {
        result.adapter_extra["original_source_record_id"] for result in results
    } == {"job-42"}
    assert {result.adapter_extra["source_identity_origin"] for result in results} == {
        "https://careers.alpha.test",
        "https://jobs.beta.test",
    }


def test_adapter_combines_normalizer_review_into_quality_gate(adapter_module, tmp_path):
    description = (
        "负责 Python 服务和 FastAPI 接口设计、开发、自动化测试与维护，"
        "使用 PostgreSQL 建设稳定可靠的数据处理和部署流程。"
    )
    record = base_record(
        job_title_raw="Python 后端工程师",
        job_description_raw=description,
    )
    record.pop("exported_html_path")
    manifest = write_manifest(tmp_path, [record], copy_fixture=False)

    result = make_adapter(adapter_module).load_manifest(
        manifest,
        run_id="manual-run-normalizer-review",
        collected_at=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
    )[0]

    assert result.normalization_status == "review"
    assert result.adapter_extra["quality_gate"]["status"] == "review"
    assert "record_id_from_url" in result.adapter_extra["quality_gate"]["issue_codes"]


@pytest.mark.parametrize("job_family_id", ["NOT_A_FAMILY", "PYTHON_BACKEND_V2"])
def test_explicit_family_must_be_one_of_the_canonical_22(
    adapter_module, tmp_path, job_family_id
):
    manifest = write_manifest(tmp_path, [base_record(job_family_id=job_family_id)])

    with pytest.raises(
        adapter_module.ManualManifestError, match="canonical job_family_id"
    ):
        make_adapter(adapter_module).load_manifest(
            manifest,
            run_id="manual-run-invalid-family",
            collected_at=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
        )


def test_explicit_family_conflicting_with_automatic_classifier_is_rejected(
    adapter_module, tmp_path
):
    manifest = write_manifest(
        tmp_path,
        [base_record(job_family_id="DATA_ENGINEER")],
    )

    with pytest.raises(
        adapter_module.ManualManifestError, match="conflicts.*PYTHON_BACKEND"
    ):
        make_adapter(adapter_module).load_manifest(
            manifest,
            run_id="manual-run-family-conflict",
            collected_at=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
        )


def test_manifest_total_bytes_are_bounded(adapter_module, tmp_path, monkeypatch):
    monkeypatch.setattr(adapter_module, "MAX_MANIFEST_BYTES", 128)
    manifest = write_manifest(tmp_path, [base_record()])

    with pytest.raises(adapter_module.ManualManifestError, match="manifest.*bytes"):
        make_adapter(adapter_module).load_manifest(
            manifest,
            run_id="manual-run-manifest-bytes",
            collected_at=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
        )


def test_manifest_line_bytes_are_bounded(adapter_module, tmp_path, monkeypatch):
    monkeypatch.setattr(adapter_module, "MAX_MANIFEST_BYTES", 4096)
    monkeypatch.setattr(adapter_module, "MAX_MANIFEST_LINE_BYTES", 128)
    manifest = write_manifest(tmp_path, [base_record()])

    with pytest.raises(adapter_module.ManualManifestError, match="line 1.*bytes"):
        make_adapter(adapter_module).load_manifest(
            manifest,
            run_id="manual-run-line-bytes",
            collected_at=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
        )


def test_json_nesting_depth_is_bounded_before_parsing(adapter_module, tmp_path):
    nested: object = "value"
    for _ in range(adapter_module.MAX_JSON_DEPTH + 1):
        nested = {"child": nested}
    manifest = write_manifest(tmp_path, [base_record(metadata=nested)])

    with pytest.raises(adapter_module.ManualManifestError, match="JSON nesting"):
        make_adapter(adapter_module).load_manifest(
            manifest,
            run_id="manual-run-json-depth",
            collected_at=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
        )


def test_html_bytes_are_bounded_before_parsing(adapter_module, tmp_path, monkeypatch):
    monkeypatch.setattr(adapter_module, "MAX_HTML_BYTES", 128)
    manifest = write_manifest(tmp_path, [base_record()])

    with pytest.raises(adapter_module.ManualManifestError, match="HTML.*bytes"):
        make_adapter(adapter_module).load_manifest(
            manifest,
            run_id="manual-run-html-bytes",
            collected_at=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
        )


def test_record_limit_stops_before_reading_or_parsing_record_n_plus_one(
    adapter_module, tmp_path
):
    (tmp_path / "company-job.html").write_bytes(FIXTURE_PATH.read_bytes())
    valid = json.dumps(base_record(), ensure_ascii=False)
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(f"{valid}\n{{}}\nnot-json\n", encoding="utf-8")

    records = make_adapter(adapter_module).load_manifest(
        manifest,
        run_id="manual-run-record-limit",
        collected_at=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
        max_records=1,
    )

    assert len(records) == 1
    assert records[0].adapter_extra["manifest_line_number"] == 1


@pytest.mark.parametrize("nested", [False, True], ids=["top-level", "nested"])
def test_duplicate_json_keys_are_rejected_recursively(adapter_module, tmp_path, nested):
    serialized = json.dumps(base_record(), ensure_ascii=False)
    if nested:
        raw_line = serialized[:-1] + ',"metadata":{"reviewed":true,"reviewed":false}}'
    else:
        raw_line = serialized.replace(
            '"source_name":',
            '"source_name":"duplicate","source_name":',
            1,
        )
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(raw_line, encoding="utf-8")

    with pytest.raises(adapter_module.ManualManifestError, match="duplicate JSON key"):
        make_adapter(adapter_module).load_manifest(
            manifest,
            run_id="manual-run-duplicate-key",
            collected_at=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
        )


def test_html_without_supported_deterministic_encoding_is_rejected(
    adapter_module, tmp_path
):
    html = FIXTURE_PATH.read_text(encoding="utf-8").replace(
        '<meta charset="utf-8">', '<meta charset="windows-1252">'
    )
    (tmp_path / "company-job.html").write_bytes(html.encode("utf-8"))
    manifest = write_manifest(tmp_path, [base_record()], copy_fixture=False)

    with pytest.raises(
        adapter_module.ManualManifestError, match="unsupported HTML charset"
    ):
        make_adapter(adapter_module).load_manifest(
            manifest,
            run_id="manual-run-unsupported-charset",
            collected_at=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
        )


def test_explicit_gb18030_html_is_decoded_without_charset_guessing(
    adapter_module, tmp_path
):
    html = FIXTURE_PATH.read_text(encoding="utf-8").replace("utf-8", "gb18030", 1)
    (tmp_path / "company-job.html").write_bytes(html.encode("gb18030"))
    manifest = write_manifest(tmp_path, [base_record()], copy_fixture=False)

    result = make_adapter(adapter_module).load_manifest(
        manifest,
        run_id="manual-run-gb18030",
        collected_at=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
    )[0]

    assert result.job_title_raw == "Python 后端工程师"
    assert "自动化测试" in result.job_description_raw


@pytest.mark.parametrize(
    ("marker", "fragment"),
    [
        ("</body>", "<p>charset=gbk</p></body>"),
        ("</head>", "<!-- charset=gbk --></head>"),
        ("</head>", '<script>const note = "charset=gbk";</script></head>'),
    ],
    ids=["body-text", "comment", "script-text"],
)
def test_charset_like_text_outside_head_meta_is_ignored(
    adapter_module, tmp_path, marker, fragment
):
    html = FIXTURE_PATH.read_text(encoding="utf-8").replace(marker, fragment, 1)
    (tmp_path / "company-job.html").write_bytes(html.encode("utf-8"))
    manifest = write_manifest(tmp_path, [base_record()], copy_fixture=False)

    result = make_adapter(adapter_module).load_manifest(
        manifest,
        run_id="manual-run-charset-text",
        collected_at=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
    )[0]

    assert result.job_title_raw


def test_http_equiv_content_type_charset_in_head_is_honored(adapter_module, tmp_path):
    html = FIXTURE_PATH.read_text(encoding="utf-8").replace(
        '<meta charset="utf-8">',
        '<meta http-equiv="content-type" content="text/html; charset=gb18030">',
    )
    (tmp_path / "company-job.html").write_bytes(html.encode("gb18030"))
    manifest = write_manifest(tmp_path, [base_record()], copy_fixture=False)

    result = make_adapter(adapter_module).load_manifest(
        manifest,
        run_id="manual-run-http-equiv-gb18030",
        collected_at=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
    )[0]

    assert result.job_title_raw


def test_gb18030_only_bytes_are_rejected_under_gbk_declaration(
    adapter_module, tmp_path
):
    gb18030_only_character = "\U00020000"
    with pytest.raises(UnicodeEncodeError):
        gb18030_only_character.encode("gbk")

    html = FIXTURE_PATH.read_text(encoding="utf-8")
    html = html.replace('<meta charset="utf-8">', '<meta charset="gbk">')
    html = html.replace("</section>", f"<p>{gb18030_only_character}</p></section>")
    (tmp_path / "company-job.html").write_bytes(html.encode("gb18030"))
    manifest = write_manifest(tmp_path, [base_record()], copy_fixture=False)

    with pytest.raises(
        adapter_module.ManualManifestError, match="invalid for declared charset"
    ):
        make_adapter(adapter_module).load_manifest(
            manifest,
            run_id="manual-run-gbk-strict",
            collected_at=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
        )


def test_html_parser_failures_are_wrapped_as_manifest_errors(
    adapter_module, tmp_path, monkeypatch
):
    manifest = write_manifest(tmp_path, [base_record()])

    def fail_parser(*_args, **_kwargs):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(adapter_module, "BeautifulSoup", fail_parser)

    with pytest.raises(adapter_module.ManualManifestError, match="HTML parser failed"):
        make_adapter(adapter_module).load_manifest(
            manifest,
            run_id="manual-run-parser-failure",
            collected_at=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
        )


def test_exported_html_symlink_is_rejected(adapter_module, tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.html"
    outside.write_bytes(FIXTURE_PATH.read_bytes())
    link = tmp_path / "company-job.html"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    manifest = write_manifest(tmp_path, [base_record()], copy_fixture=False)

    with pytest.raises(adapter_module.ManualManifestError, match="symlink|reparse"):
        make_adapter(adapter_module).load_manifest(
            manifest,
            run_id="manual-run-symlink",
            collected_at=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
        )


def test_exported_html_replacement_race_is_detected(
    adapter_module, tmp_path, monkeypatch
):
    manifest = write_manifest(tmp_path, [base_record()])
    target = tmp_path / "company-job.html"
    replacement = tmp_path / "replacement.html"
    displaced = tmp_path / "displaced.html"
    replacement.write_bytes(FIXTURE_PATH.read_bytes() + b"\n<!-- replacement -->")
    real_open = os.open
    replaced = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal replaced
        open_path = path
        if Path(path) == target and not replaced:
            replaced = True
            os.replace(target, displaced)
            os.replace(replacement, target)
            open_path = displaced
        if dir_fd is None:
            descriptor = real_open(open_path, flags, mode)
        else:
            descriptor = real_open(open_path, flags, mode, dir_fd=dir_fd)
        return descriptor

    monkeypatch.setattr(adapter_module.os, "open", racing_open)

    with pytest.raises(
        adapter_module.ManualManifestError, match="changed while opening"
    ):
        make_adapter(adapter_module).load_manifest(
            manifest,
            run_id="manual-run-race",
            collected_at=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
        )


def test_example_manifest_is_schema_only_and_inert(adapter_module):
    lines = [
        json.loads(line)
        for line in EXAMPLE_MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert len(lines) == 1
    example = lines[0]
    assert example["documentation_only"] is True
    assert example["source_url"].startswith("https://example.invalid/")
    assert {
        "source_name",
        "source_url",
        "company_name",
        "collection_authorization_note",
    } <= example.keys()
    assert (
        sum(
            bool(example.get(field))
            for field in ("exported_html_path", "job_description_raw")
        )
        == 1
    )

    assert (
        make_adapter(adapter_module).load_manifest(
            EXAMPLE_MANIFEST,
            run_id="documentation-check",
            collected_at=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
        )
        == ()
    )


def test_adapter_rejects_non_manual_source_definitions(adapter_module):
    source = make_source(compliance_status="approved", collection_mode="public_html")

    with pytest.raises(ValueError, match="manual_only"):
        adapter_module.ManualManifestAdapter(
            source=source,
            registry=SourceRegistry([source]),
        )
