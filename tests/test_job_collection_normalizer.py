from datetime import datetime, timezone

import pytest

from src.job_collection.models import SourceDefinition, UnifiedJobRecord
from src.job_collection.normalizer import (
    NormalizationError,
    ReviewFinding,
    normalize_job_record,
)
from src.job_data_service import content_hash, simhash64


def make_source(**overrides) -> SourceDefinition:
    payload = {
        "source_id": "example_jobs",
        "source_name": "示例招聘",
        "source_type": "company_official",
        "market_scope": "china",
        "base_url": "https://jobs.example.com",
        "allowed_paths": ["/careers/"],
        "collection_mode": "public_html",
        "compliance_status": "approved",
        "compliance_note": "仅采集公开且无需登录的招聘页面",
        "rate_limit_seconds": 2.0,
        "max_pages": 10,
        "max_records": 100,
        "parser_name": "example_parser",
        "parser_version": "v2",
        "enabled": True,
    }
    payload.update(overrides)
    return SourceDefinition.model_validate(payload)


def make_raw(**overrides) -> dict:
    payload = {
        "job_family_id": "PYTHON_BACKEND",
        "job_title": "  Python　后端工程师  ",
        "company_name": " 示例科技有限公司 ",
        "industry": "互联网 / 软件",
        "region": " 上海市 · 浦东新区 ",
        "source_url": "https://jobs.example.com/careers/42?lang=zh",
        "source_record_id": "job-42",
        "education": " 本科及以上 ",
        "experience": "3 ～ 5 年",
        "salary": "￥20Ｋ－30Ｋ／月",
        "description": (
            "<p>岗位职责：</p>\n"
            "负责 Python 服务设计、开发与维护，使用 FastAPI 和 PostgreSQL。\n\n"
            "&nbsp;岗位要求：熟悉容器化部署，具备良好的工程实践能力。"
        ),
    }
    payload.update(overrides)
    return payload


def normalize(raw=None, **kwargs) -> UnifiedJobRecord:
    return normalize_job_record(
        raw or make_raw(),
        source=kwargs.pop("source", make_source()),
        run_id="run-20260806-001",
        snapshot_metadata={
            "snapshot_hash": "a" * 64,
            "response_status": 200,
            "page_title": "Python 后端工程师 | 示例招聘",
            **kwargs.pop("snapshot_metadata", {}),
        },
        collected_at=kwargs.pop(
            "collected_at", datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        ),
        **kwargs,
    )


def finding_codes(record: UnifiedJobRecord) -> set[str]:
    return {item["code"] for item in record.normalization_findings}


def test_review_finding_has_a_json_safe_audit_shape():
    finding = ReviewFinding(
        code="example",
        severity="review",
        field_name="industry",
        reason="test_reason",
        original_value="raw value",
    )

    assert finding.as_dict() == {
        "code": "example",
        "severity": "review",
        "field_name": "industry",
        "field": "industry",
        "reason": "test_reason",
        "original_value": "raw value",
    }


def test_normalizes_text_without_destroying_readable_description():
    record = normalize()

    assert record.normalization_status == "valid"
    assert record.job_title_raw == "Python 后端工程师"
    assert record.company_name == "示例科技有限公司"
    assert "岗位职责:" in record.job_description_raw
    assert "\n\n岗位要求:" in record.job_description_raw
    assert "FastAPI 和 PostgreSQL" in record.job_description_raw
    assert record.education_requirement == "本科及以上"
    assert record.experience_requirement == "3-5年"
    assert record.salary_range == "20K-30K/月"
    assert record.content_hash == content_hash(record.job_description_raw)
    assert record.simhash == simhash64(record.job_description_raw)


@pytest.mark.parametrize(
    ("industry", "reason_fragment"),
    [
        ("20K-30K/月", "salary"),
        ("本科以上，3-5年经验，负责平台开发", "requirement"),
        ("岗位要求：熟悉 Java，负责系统设计与开发", "requirement"),
        ("这是一个异常冗长的字段" * 20, "too_long"),
    ],
)
def test_quarantines_polluted_industry_but_preserves_audit_value(
    industry, reason_fragment
):
    record = normalize(make_raw(industry=industry))

    assert record.industry == "unknown"
    finding = next(
        item for item in record.normalization_findings if item["code"] == "invalid_industry"
    )
    assert finding["original_value"] == industry
    assert reason_fragment in finding["reason"]
    assert record.normalization_status == "review"


def test_keeps_a_valid_industry():
    record = normalize(make_raw(industry="电子信息制造业"))

    assert record.industry == "电子信息制造业"
    assert "invalid_industry" not in finding_codes(record)


def test_industry_numeric_classification_range_is_not_mistaken_for_salary():
    record = normalize(make_raw(industry="制造业分类31-40"))

    assert record.industry == "制造业分类31-40"
    assert "invalid_industry" not in finding_codes(record)


@pytest.mark.parametrize("industry", ["20-30K/月", "$20-30", "20-30/月"])
def test_industry_range_with_salary_marker_is_still_pollution(industry):
    record = normalize(make_raw(industry=industry))

    assert record.industry == "unknown"
    finding = next(
        item for item in record.normalization_findings if item["code"] == "invalid_industry"
    )
    assert finding["reason"] == "salary_pattern"


def test_publication_is_absent_without_an_explicit_field_and_not_inferred_from_body():
    record = normalize(
        make_raw(
            description="本岗位正文提到 2026-08-05，但这是项目日期，不是发布时间。" * 3
        )
    )

    assert record.published_at is None
    assert record.published_at_trusted is False
    assert record.published_at_confidence == 0.0


def test_publication_confidence_is_zero_without_timestamp_and_evidence_is_reviewed():
    record = normalize(
        make_raw(
            published_at=None,
            published_at_evidence="列表显示日期，但解析失败",
            published_at_confidence=0.95,
        )
    )

    assert record.published_at is None
    assert record.published_at_confidence == 0.0
    assert record.published_at_trusted is False
    assert record.published_at_evidence == "列表显示日期,但解析失败"
    assert record.normalization_status == "review"
    finding = next(
        item
        for item in record.normalization_findings
        if item["code"] == "publication_without_timestamp"
    )
    assert finding["field"] == "published_at"


def test_publication_is_trusted_only_with_evidence_and_sufficient_confidence():
    record = normalize(
        make_raw(
            published_at="2026-08-05T20:00:00+08:00",
            published_at_evidence="页面明确标注：发布时间 2026-08-05 20:00",
            published_at_confidence=0.9,
        )
    )

    assert record.published_at == datetime(2026, 8, 5, 12, 0)
    assert record.published_at.tzinfo is None
    assert record.published_at_trusted is True


def test_publication_one_hour_in_the_future_is_untrusted_after_utc_normalization():
    record = normalize(
        make_raw(
            published_at="2026-08-06T21:00:00+08:00",
            published_at_evidence="页面明确标注发布时间",
            published_at_confidence=0.99,
        )
    )

    assert record.published_at == datetime(2026, 8, 6, 13, 0)
    assert record.collected_at == datetime(2026, 8, 6, 12, 0)
    assert record.published_at_trusted is False
    assert record.normalization_status == "review"
    finding = next(
        item
        for item in record.normalization_findings
        if item["code"] == "untrusted_publication"
    )
    assert finding["field_name"] == "published_at"
    assert "future_date" in finding["reason"]


@pytest.mark.parametrize("confidence", ["nan", "inf", "-inf", -0.01, 1.01])
def test_invalid_publication_confidence_is_rejected_without_clamping(confidence):
    with pytest.raises(NormalizationError, match="published_at_confidence"):
        normalize(
            make_raw(
                published_at="2026-08-05T12:00:00Z",
                published_at_evidence="页面明确标注发布时间",
                published_at_confidence=confidence,
            )
        )


def test_naive_public_source_times_are_interpreted_as_asia_shanghai():
    record = normalize(
        make_raw(
            published_at="2026-08-06 19:30:00",
            published_at_evidence="页面明确标注发布时间",
            published_at_confidence=0.9,
            first_seen_at="2026-08-06 20:00:00",
            last_seen_at="2026-08-06 20:30:00",
        )
    )

    assert record.published_at == datetime(2026, 8, 6, 11, 30)
    assert record.first_seen_at == datetime(2026, 8, 6, 12, 0)
    assert record.last_seen_at == datetime(2026, 8, 6, 12, 30)
    assert record.published_at_trusted is True


def test_twenty_year_publication_boundary_uses_calendar_years_across_leap_day():
    record = normalize(
        make_raw(
            published_at="2004-02-29T12:00:00Z",
            published_at_evidence="页面明确标注发布时间",
            published_at_confidence=0.9,
        ),
        collected_at=datetime(2024, 2, 29, 12, 0, tzinfo=timezone.utc),
    )

    assert record.published_at_trusted is True
    assert "untrusted_publication" not in finding_codes(record)


def test_html_parser_extracts_whitelisted_markup_but_preserves_technical_angles():
    record = normalize(
        make_raw(
            description=(
                "<p>负责 C++ List<String> 容器开发，判断 a < b，"
                "并处理 URL https://example.test/?a=1&b=2。</p>"
                "<p>熟悉 AT&amp;T API 与 <custom>tag</custom> 语法。</p>"
            )
        )
    )

    assert "<p>" not in record.job_description_raw
    assert "List<String>" in record.job_description_raw
    assert "a < b" in record.job_description_raw
    assert "https://example.test/?a=1&b=2" in record.job_description_raw
    assert "AT&T API" in record.job_description_raw
    assert "<custom>tag</custom>" in record.job_description_raw


def test_plain_technical_angle_expression_is_not_treated_as_html():
    record = normalize(
        make_raw(
            description=(
                "负责 Java List<String> 泛型容器与比较逻辑 a < b 的实现。"
                "维护查询地址 https://example.test/?a=1&b=2，并编写完整测试。"
            )
        )
    )

    assert "List<String>" in record.job_description_raw
    assert "a < b" in record.job_description_raw
    assert "a=1&b=2" in record.job_description_raw


def test_html_anchor_keeps_url_evidence_and_unpaired_span_generic_is_preserved():
    record = normalize(
        make_raw(
            description=(
                '<p><span>负责平台开发</span>，查看'
                '<a href="https://docs.example.com/jobs?a=1&amp;b=2">岗位说明</a>。</p>'
                "负责 std::vector<span> 与 List<String> 泛型容器的接口设计和测试。"
            )
        )
    )

    assert "负责平台开发" in record.job_description_raw
    assert "<span>负责平台开发</span>" not in record.job_description_raw
    assert "岗位说明 [https://docs.example.com/jobs?a=1&b=2]" in record.job_description_raw
    assert "std::vector<span>" in record.job_description_raw
    assert "List<String>" in record.job_description_raw


@pytest.mark.parametrize("break_tag", ["<br>", "<br/>", "<br />"])
def test_html_break_variants_create_readable_line_breaks(break_tag):
    record = normalize(
        make_raw(
            description=(
                f"第一段负责平台接口设计和开发{break_tag}"
                "第二段负责测试、维护和技术文档整理。"
            )
        )
    )

    assert record.job_description_raw == (
        "第一段负责平台接口设计和开发\n第二段负责测试、维护和技术文档整理。"
    )
    assert "<br" not in record.job_description_raw.lower()


@pytest.mark.parametrize(
    ("published_at", "evidence", "confidence"),
    [
        ("2026-08-05", None, 0.95),
        ("2026-08-05", "页面日期", 0.79),
        ("2026-08-08", "页面发布时间", 0.99),
        ("1990-01-01", "页面发布时间", 0.99),
    ],
)
def test_untrusted_or_implausible_publication_is_reviewed(
    published_at, evidence, confidence
):
    record = normalize(
        make_raw(
            published_at=published_at,
            published_at_evidence=evidence,
            published_at_confidence=confidence,
        )
    )

    assert record.published_at is not None
    assert record.published_at_trusted is False
    assert record.normalization_status == "review"
    assert "untrusted_publication" in finding_codes(record)


@pytest.mark.parametrize(
    ("input_field", "value", "output_field", "reason"),
    [
        ("region", "20K-30K/月", "region", "salary_pattern"),
        ("region", "上海" * 50, "region", "too_long"),
        (
            "education_requirement",
            "岗位要求：本科及以上\n负责系统开发",
            "education_requirement",
            "multiline_text",
        ),
        (
            "education_requirement",
            "20K-30K/月",
            "education_requirement",
            "salary_pattern",
        ),
        (
            "experience_requirement",
            "3-5年经验，岗位职责：负责平台开发",
            "experience_requirement",
            "requirement_text",
        ),
        (
            "experience_requirement",
            "15K-25K/月",
            "experience_requirement",
            "salary_pattern",
        ),
        (
            "salary_range",
            "本科及以上",
            "salary_range",
            "education_pattern",
        ),
    ],
)
def test_polluted_scalar_fields_are_cleared_and_audited(
    input_field, value, output_field, reason
):
    record = normalize(make_raw(**{input_field: value}))

    assert getattr(record, output_field) is None
    assert record.normalization_status == "review"
    finding = next(
        item
        for item in record.normalization_findings
        if item["field_name"] == output_field
    )
    assert finding["code"] == "invalid_scalar_field"
    assert finding["field"] == output_field
    assert finding["original_value"] == value
    assert finding["reason"] == reason


@pytest.mark.parametrize(
    ("input_field", "value", "output_field", "expected"),
    [
        ("region", " 北京市 / 海淀区 ", "region", "北京市/海淀区"),
        (
            "education_requirement",
            " 硕士及以上 ",
            "education_requirement",
            "硕士及以上",
        ),
        (
            "experience_requirement",
            "3 ～ 5 年",
            "experience_requirement",
            "3-5年",
        ),
        ("salary_range", "￥15Ｋ－25Ｋ／月", "salary_range", "15K-25K/月"),
    ],
)
def test_valid_short_scalar_fields_are_preserved(
    input_field, value, output_field, expected
):
    record = normalize(make_raw(**{input_field: value}))

    assert getattr(record, output_field) == expected
    assert not any(
        item["field_name"] == output_field
        for item in record.normalization_findings
    )


def test_source_metadata_domain_and_observation_times_are_preserved():
    record = normalize(
        make_raw(first_seen_at="2026-08-06T09:00:00+08:00", last_seen_at="2026-08-05"),
        snapshot_metadata={
            "snapshot_hash": "b" * 64,
            "response_status": 203,
            "page_title": "原始页面标题",
            "observed_at": "2026-08-06T10:30:00+08:00",
        },
    )

    assert record.source_domain == "jobs.example.com"
    assert record.source_id == "example_jobs"
    assert record.source_name == "示例招聘"
    assert record.source_type == "company_official"
    assert record.collection_method == "public_html"
    assert record.parser_name == "example_parser"
    assert record.parser_version == "v2"
    assert record.run_id == "run-20260806-001"
    assert record.compliance_status == "approved"
    assert record.compliance_note == "仅采集公开且无需登录的招聘页面"
    assert record.snapshot_hash == "b" * 64
    assert record.response_status == 203
    assert record.page_title == "原始页面标题"
    assert record.first_seen_at == datetime(2026, 8, 6, 1, 0)
    assert record.last_seen_at == record.first_seen_at
    assert record.collected_at.tzinfo is None


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/careers/42",
        "https://jobs.example.com/private/42",
        "http://jobs.example.com/careers/42",
        "https://jobs.example.com/careers/%252e%252e/private/42",
    ],
)
def test_rejects_source_urls_outside_the_source_scope(url):
    with pytest.raises(NormalizationError, match="source scope"):
        normalize(make_raw(source_url=url))


def test_record_id_is_stable_and_prefers_source_record_id():
    first = normalize(make_raw(source_url="https://jobs.example.com/careers/old"))
    second = normalize(make_raw(source_url="https://jobs.example.com/careers/new"))

    assert first.record_id == second.record_id
    assert len(first.record_id) <= 80
    assert "record_id_from_url" not in finding_codes(first)


def test_source_record_id_is_opaque_except_for_edge_whitespace():
    circled = normalize(make_raw(source_record_id=" ① "))
    ascii_one = normalize(make_raw(source_record_id="1"))
    spaced = normalize(make_raw(source_record_id="A  B"))

    assert circled.source_record_id == "①"
    assert ascii_one.source_record_id == "1"
    assert circled.record_id != ascii_one.record_id
    assert spaced.source_record_id == "A  B"


def test_public_unknown_adapter_fields_are_preserved_in_adapter_extra():
    record = normalize(
        make_raw(
            department="研发平台部",
            benefits=["五险一金", "弹性工作"],
            recruiter={"name": "张老师", "verified": True},
            _parser_state={"internal": True},
        )
    )

    assert record.adapter_extra == {
        "department": "研发平台部",
        "benefits": ["五险一金", "弹性工作"],
        "recruiter": {"name": "张老师", "verified": True},
    }


def test_non_json_adapter_extra_is_rejected_clearly():
    with pytest.raises(NormalizationError, match="adapter_extra"):
        normalize(make_raw(adapter_object=object()))


def test_missing_source_record_id_uses_canonical_url_and_requires_review():
    first = normalize(
        make_raw(
            source_record_id=None,
            source_url="HTTPS://JOBS.EXAMPLE.COM/careers/42?b=2&a=1#details",
        )
    )
    second = normalize(
        make_raw(
            source_record_id=None,
            source_url="https://jobs.example.com/careers/42?a=1&b=2",
        )
    )

    assert first.record_id == second.record_id
    assert first.normalization_status == "review"
    assert "record_id_from_url" in finding_codes(first)


def test_short_description_is_quarantined_not_valid():
    record = normalize(make_raw(description="负责 Python 开发。"))

    assert record.normalization_status == "quarantine"
    assert "short_description" in finding_codes(record)


def test_missing_required_adapter_value_raises_clear_error():
    with pytest.raises(NormalizationError, match="company_name"):
        normalize(make_raw(company_name=" "))
