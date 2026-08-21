from pathlib import Path

import pytest

from src.evidence_dataset_service import (
    APPROVED_JOB_FAMILIES,
    SUPPLEMENTAL_JOB_FAMILIES,
    SUPPORTED_JOB_FAMILIES,
    load_jsonl,
    validate_official_evidence_records,
)


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "data" / "evidence" / "official-standards-2026.jsonl"
SUPPLEMENT = (
    ROOT / "data" / "evidence" / "official-standards-2026-supplement.jsonl"
)


def _valid_supplemental_record(**overrides):
    record = {
        "evidence_id": "OFF-AIA-TEST",
        "job_family_id": "AI_AGENT_ENGINEER",
        "evidence_type": "technical_standard",
        "title": "AI Agent technical specification",
        "publisher": "Official publisher",
        "source_url": "https://modelcontextprotocol.io/specification/latest",
        "related_skill": "MCP",
        "evidence_summary": (
            "该技术规范说明智能体应用连接外部数据与工具时所使用的协议结构、能力协商、消息传输和授权边界，"
            "可用于核验智能体工程师在工具调用、上下文接入、接口设计、安全控制和异常处理方面的岗位能力要求。"
        ),
    }
    record.update(overrides)
    return record


def test_supported_families_cover_baseline_and_supplement():
    assert SUPPORTED_JOB_FAMILIES == (
        APPROVED_JOB_FAMILIES | SUPPLEMENTAL_JOB_FAMILIES
    )
    assert len(APPROVED_JOB_FAMILIES) == 8
    assert len(SUPPLEMENTAL_JOB_FAMILIES) == 14
    assert len(SUPPORTED_JOB_FAMILIES) == 22


def test_validator_accepts_a_supported_supplemental_family():
    result = validate_official_evidence_records(
        [_valid_supplemental_record()],
        expected_total=None,
        enforce_family_mix=False,
        required_families=SUPPLEMENTAL_JOB_FAMILIES,
    )

    assert result["family_counts"] == {"AI_AGENT_ENGINEER": 1}


def test_validator_rejects_known_incorrect_ai_standard_mapping():
    record = _valid_supplemental_record(
        source_url="https://www.iso.org/standard/78843.html"
    )

    with pytest.raises(ValueError, match="known incorrect source mapping"):
        validate_official_evidence_records(
            [record],
            expected_total=None,
            enforce_family_mix=False,
        )


def test_official_evidence_dataset_meets_competition_contract():
    records = load_jsonl(DATASET)

    result = validate_official_evidence_records(records)

    assert result["total"] == 24
    assert result["family_counts"] == {
        family: 3 for family in sorted(APPROVED_JOB_FAMILIES)
    }


def test_official_evidence_supplement_meets_contract():
    records = load_jsonl(SUPPLEMENT)

    result = validate_official_evidence_records(
        records,
        expected_total=42,
        required_families=SUPPLEMENTAL_JOB_FAMILIES,
    )

    assert result["total"] == 42
    assert result["family_counts"] == {
        family: 3 for family in sorted(SUPPLEMENTAL_JOB_FAMILIES)
    }


def test_validator_rejects_non_official_source_domain():
    record = {
        "evidence_id": "OFF-BIG-001",
        "job_family_id": "BIG_DATA_DEVELOPER",
        "evidence_type": "technical_standard",
        "title": "大数据参考架构标准",
        "publisher": "示例机构",
        "source_url": "https://example.com/standard",
        "related_skill": "大数据架构",
        "evidence_summary": (
            "该标准说明大数据系统的参考架构、核心组件和数据处理关系，可用于识别大数据开发岗位在架构设计、批流处理、数据治理与安全控制方面的能力要求。"
        ),
    }

    with pytest.raises(ValueError, match="非准入官方域名"):
        validate_official_evidence_records([record], expected_total=None)


def test_validator_rejects_duplicate_evidence_id():
    record = {
        "evidence_id": "OFF-IOT-001",
        "job_family_id": "IOT_ENGINEER",
        "evidence_type": "occupation_standard",
        "title": "物联网工程技术人员职业标准",
        "publisher": "人力资源社会保障部",
        "source_url": "https://www.gov.cn/example-one.pdf",
        "related_skill": "物联网工程",
        "evidence_summary": (
            "该职业标准围绕物联网架构设计、设备连接、平台开发、系统实施和运行维护描述工作任务，可用于映射物联网工程岗位的核心职责、知识要求和专业技能。"
        ),
    }
    duplicate = {**record, "title": "另一标准", "source_url": "https://www.gov.cn/example-two.pdf"}

    with pytest.raises(ValueError, match="evidence_id 重复"):
        validate_official_evidence_records(
            [record, duplicate], expected_total=None, enforce_family_mix=False
        )
