from datetime import datetime, timedelta

import pytest


NOW = datetime(2026, 7, 23, 10, 0, 0)
BASE_TEXT = (
    "负责数据平台开发、部署和日常维护，能够独立完成数据任务并保障系统稳定运行，"
    "持续优化数据质量、处理性能和交付流程。"
)


def _record(**changes):
    value = {
        "record_id": "JD-001",
        "job_family_id": "DATA_ENGINEER",
        "job_title_raw": "数据工程师",
        "source_name": "企业官网",
        "source_type": "company_official",
        "source_url": "https://example.com/jobs/1",
        "provenance_status": "approved",
        "published_at": NOW - timedelta(days=10),
        "collected_at": NOW,
        "job_description_raw": BASE_TEXT,
        "quality_score": 0.9,
        "duplicate_of_id": None,
        "has_capability_evidence": True,
    }
    value.update(changes)
    return value


def test_gate_status_uses_strict_precedence():
    from src.competition_rules import assess_gate

    decision = assess_gate(
        _record(job_description_raw="", duplicate_of_id=9), now=NOW
    )

    assert decision.status == "quarantined"
    assert "missing_job_description" in decision.issue_codes


def test_future_publication_requires_review():
    from src.competition_rules import assess_gate

    decision = assess_gate(
        _record(published_at=NOW + timedelta(days=2), collected_at=NOW), now=NOW
    )

    assert decision.status == "review"
    assert "future_published_at" in decision.issue_codes


def test_invalid_source_and_long_collection_gap_require_review():
    from src.competition_rules import assess_gate

    decision = assess_gate(
        _record(
            source_url="ftp://example.com/job",
            published_at=NOW - timedelta(days=3654),
        ),
        now=NOW,
    )

    assert decision.status == "review"
    assert decision.issue_codes == (
        "invalid_source_url",
        "published_collection_gap_too_large",
    )


def test_traceable_official_document_without_url_can_pass():
    from src.competition_rules import assess_gate

    decision = assess_gate(
        _record(source_type="official_document", source_url=""), now=NOW
    )

    assert decision.status == "valid"


def test_valid_record_passes_gate():
    from src.competition_rules import GateDecision, assess_gate

    assert assess_gate(_record(), now=NOW) == GateDecision("valid", ())


def test_unverified_provenance_requires_review():
    from src.competition_rules import assess_gate

    decision = assess_gate(_record(provenance_status="unverified"), now=NOW)

    assert decision.status == "review"
    assert decision.issue_codes == ("unverified_provenance",)


def _skill_evidence(
    posting_id,
    *,
    source_type="company_official",
    source_domain="jobs.example.com",
    source_name="Example Careers",
    company_name="Example Co",
    requirement_type="required",
    published_at=None,
    published_at_trusted=True,
    provenance_status="approved",
):
    return {
        "posting_id": posting_id,
        "skill_id": 1,
        "name": "Python",
        "category": "language",
        "requirement_type": requirement_type,
        "link_confidence": 0.95,
        "source_score": 1.0,
        "source_type": source_type,
        "source_domain": source_domain,
        "source_name": source_name,
        "company_name": company_name,
        "published_at": published_at,
        "published_at_trusted": published_at_trusted,
        "provenance_status": provenance_status,
    }


def test_single_source_skill_confidence_ignores_display_names_and_is_capped():
    from src import competition_rules

    rows = [
        _skill_evidence(
            1,
            source_name="Careers display A",
            company_name="Company A",
            requirement_type="required",
            published_at=datetime(2026, 1, 2),
        ),
        _skill_evidence(
            2,
            source_name="Careers display B",
            company_name="Company B",
            requirement_type="preferred",
            published_at=datetime(2026, 2, 3),
        ),
    ]

    skill = competition_rules.aggregate_skill_evidence(rows, total_postings=2)[0]

    assert skill["evidence_count"] == 2
    assert skill["source_type_count"] == 1
    assert skill["source_domain_count"] == 1
    assert skill["company_count"] == 2
    assert skill["required_ratio"] == 0.5
    assert skill["preferred_ratio"] == 0.5
    assert skill["cross_source_status"] == "single_source"
    assert skill["confidence"] < competition_rules.HIGH_CONFIDENCE_THRESHOLD
    assert skill["first_published_at"] == datetime(2026, 1, 2)
    assert skill["last_published_at"] == datetime(2026, 2, 3)


@pytest.mark.parametrize(
    ("rows", "source_types", "source_domains"),
    [
        (
            [
                _skill_evidence(1, source_type="company_official", source_domain="a.example"),
                _skill_evidence(2, source_type="public_service", source_domain="b.example"),
            ],
            2,
            2,
        ),
        (
            [
                _skill_evidence(1, source_domain="a.example"),
                _skill_evidence(2, source_domain="b.example"),
                _skill_evidence(3, source_domain="c.example"),
            ],
            1,
            3,
        ),
    ],
)
def test_cross_source_skill_is_confirmed_by_types_or_domains(
    rows, source_types, source_domains
):
    from src import competition_rules

    skill = competition_rules.aggregate_skill_evidence(
        rows, total_postings=len(rows)
    )[0]

    assert skill["source_type_count"] == source_types
    assert skill["source_domain_count"] == source_domains
    assert skill["cross_source_status"] == "confirmed"
    assert skill["confidence"] >= competition_rules.HIGH_CONFIDENCE_THRESHOLD


def test_skill_evidence_aggregation_is_deterministic_and_ignores_null_dimensions():
    from src import competition_rules

    rows = [
        _skill_evidence(
            2,
            source_type=None,
            source_domain=None,
            company_name=None,
            requirement_type="preferred",
            published_at=datetime(2026, 3, 1),
            published_at_trusted=False,
        ),
        _skill_evidence(
            1,
            source_type=" Company_Official ",
            source_domain="JOBS.EXAMPLE.COM ",
            company_name=" Example Co ",
            published_at=datetime(2026, 1, 1),
        ),
    ]
    rows.append(dict(rows[-1]))

    forward = competition_rules.aggregate_skill_evidence(rows, total_postings=2)
    reverse = competition_rules.aggregate_skill_evidence(reversed(rows), total_postings=2)

    assert forward == reverse
    assert forward[0]["evidence_count"] == 2
    assert forward[0]["source_type_count"] == 1
    assert forward[0]["source_domain_count"] == 1
    assert forward[0]["company_count"] == 1
    assert forward[0]["first_published_at"] == datetime(2026, 1, 1)
    assert forward[0]["last_published_at"] == datetime(2026, 1, 1)


def test_unverified_provenance_does_not_confirm_cross_source_skill():
    from src import competition_rules

    rows = [
        _skill_evidence(1, source_domain="approved.example"),
        _skill_evidence(
            2,
            source_type="invented_type_a",
            source_domain="spoof-a.example",
            provenance_status="unverified",
        ),
        _skill_evidence(
            3,
            source_type="invented_type_b",
            source_domain="spoof-b.example",
            provenance_status="unverified",
        ),
    ]

    skill = competition_rules.aggregate_skill_evidence(rows, total_postings=3)[0]

    assert skill["source_type_count"] == 1
    assert skill["source_domain_count"] == 1
    assert skill["cross_source_status"] == "single_source"
    assert skill["confidence"] < competition_rules.HIGH_CONFIDENCE_THRESHOLD


@pytest.mark.parametrize(
    ("title", "experience", "description", "expected"),
    [
        ("初级数据工程师", "1年", BASE_TEXT, "junior"),
        ("数据工程师", "3-5年", BASE_TEXT, "mid"),
        ("高级数据工程师", "3年", BASE_TEXT, "senior"),
        ("技术专家", "8年", "负责技术规划、团队管理和行业标准制定。", "expert"),
        ("数据工程师", None, BASE_TEXT, "mid"),
    ],
)
def test_classify_seniority(title, experience, description, expected):
    from src.competition_rules import classify_seniority

    assert classify_seniority(title, experience, description).level == expected


def test_years_alone_cannot_create_expert_level():
    from src.competition_rules import classify_seniority

    decision = classify_seniority("数据工程师", "10年以上", BASE_TEXT)

    assert decision.level == "senior"


def test_explicit_title_wins_conflict_and_reduces_confidence():
    from src.competition_rules import classify_seniority

    decision = classify_seniority("高级数据工程师", "1年", BASE_TEXT)

    assert decision.level == "senior"
    assert decision.confidence == 0.85
    assert decision.evidence["conflict"] is True


def test_quarter_keys_and_adjacency_cross_year_boundary():
    from src.competition_rules import are_adjacent_quarters, quarter_key

    assert quarter_key(datetime(2026, 3, 31)) == "2026-Q1"
    assert quarter_key(datetime(2026, 4, 1)) == "2026-Q2"
    assert are_adjacent_quarters("2025-Q4", "2026-Q1") is True
    assert are_adjacent_quarters("2025-Q3", "2026-Q1") is False


@pytest.mark.parametrize(
    (
        "before_rate",
        "after_rate",
        "before_requirement",
        "after_requirement",
        "before_evidence",
        "after_evidence",
        "expected",
    ),
    [
        (0.0, 0.2, None, "required", 0, 4, "added"),
        (0.2, 0.0, "required", None, 4, 0, "removed"),
        (0.2, 0.35, "preferred", "preferred", 4, 7, "modified"),
        (0.2, 0.2, "preferred", "required", 4, 4, "modified"),
        (0.0, 0.2, None, "required", 0, 2, None),
    ],
)
def test_classify_skill_change(
    before_rate,
    after_rate,
    before_requirement,
    after_requirement,
    before_evidence,
    after_evidence,
    expected,
):
    from src.competition_rules import classify_skill_change

    decision = classify_skill_change(
        before_rate,
        after_rate,
        before_requirement=before_requirement,
        after_requirement=after_requirement,
        before_evidence=before_evidence,
        after_evidence=after_evidence,
    )

    assert decision.change_type == expected
