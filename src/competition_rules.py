from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Mapping
from urllib.parse import urlparse


GATE_RULE_VERSION = "competition-gate-v1"
LEVEL_RULE_VERSION = "competition-level-v1"
VALID_LEVELS = ("junior", "mid", "senior", "expert", "unspecified")
HIGH_CONFIDENCE_THRESHOLD = 0.70
SINGLE_SOURCE_CONFIDENCE_CAP = HIGH_CONFIDENCE_THRESHOLD - 0.0001
SKILL_EVIDENCE_RULE_VERSION = "cross-source-skill-v1"
OFFICIAL_SOURCE_TYPES = {
    "occupation_standard",
    "technical_standard",
    "policy_document",
    "official_document",
}


@dataclass(frozen=True)
class GateDecision:
    status: str
    issue_codes: tuple[str, ...]


@dataclass(frozen=True)
class SeniorityDecision:
    level: str
    confidence: float
    rule_version: str
    evidence: dict[str, object]


@dataclass(frozen=True)
class ChangeDecision:
    change_type: str | None
    delta: float


def _dimension(value: object) -> str | None:
    normalized = str(value or "").strip().casefold()
    return normalized or None


def _bounded_score(value: object) -> float:
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return 0.0


def aggregate_skill_evidence(
    rows: Iterable[Mapping[str, object]], *, total_postings: int
) -> list[dict[str, object]]:
    """Aggregate posting-level skill evidence without counting display-name aliases."""
    grouped: dict[object, dict[str, object]] = {}
    for row in rows:
        skill_id = row.get("skill_id")
        group = grouped.setdefault(
            skill_id,
            {
                "names": set(),
                "categories": set(),
                "postings": {},
            },
        )
        name = str(row.get("name") or "").strip()
        category = str(row.get("category") or "").strip()
        if name:
            group["names"].add(name)
        if category:
            group["categories"].add(category)

        posting_id = row.get("posting_id")
        posting = group["postings"].setdefault(
            posting_id,
            {
                "requirements": set(),
                "confidence": 0.0,
                "source_score": 0.0,
                "source_types": set(),
                "source_domains": set(),
                "companies": set(),
                "published_dates": set(),
            },
        )
        requirement = _dimension(row.get("requirement_type"))
        if requirement:
            posting["requirements"].add(requirement)
        posting["confidence"] = max(
            posting["confidence"], _bounded_score(row.get("link_confidence"))
        )
        posting["source_score"] = max(
            posting["source_score"], _bounded_score(row.get("source_score"))
        )
        dimensions = (("companies", "company_name"),)
        if row.get("provenance_status") == "approved":
            dimensions += (
                ("source_types", "source_type"),
                ("source_domains", "source_domain"),
            )
        for key, field in dimensions:
            value = _dimension(row.get(field))
            if value and value not in {"unknown", "未知"}:
                posting[key].add(value)
        published_at = row.get("published_at")
        if (
            isinstance(published_at, datetime)
            and row.get("published_at_trusted") is True
            and row.get("provenance_status") == "approved"
        ):
            posting["published_dates"].add(published_at)

    payloads: list[dict[str, object]] = []
    denominator = max(int(total_postings), 1)
    for skill_id, group in grouped.items():
        postings = list(group["postings"].values())
        if not postings:
            continue
        required_count = sum(
            "required" in posting["requirements"] for posting in postings
        )
        preferred_count = sum(
            "required" not in posting["requirements"]
            and "preferred" in posting["requirements"]
            for posting in postings
        )
        known_requirement_count = required_count + preferred_count
        source_types = set().union(
            *(posting["source_types"] for posting in postings)
        )
        source_domains = set().union(
            *(posting["source_domains"] for posting in postings)
        )
        companies = set().union(*(posting["companies"] for posting in postings))
        published_dates = set().union(
            *(posting["published_dates"] for posting in postings)
        )
        source_type_count = len(source_types)
        source_domain_count = len(source_domains)
        cross_source_status = (
            "confirmed"
            if source_type_count >= 2 or source_domain_count >= 3
            else "single_source"
        )
        base_confidence = sum(
            posting["confidence"] * posting["source_score"] for posting in postings
        ) / len(postings)
        confidence = _bounded_score(base_confidence)
        if cross_source_status == "single_source":
            confidence = min(confidence, SINGLE_SOURCE_CONFIDENCE_CAP)
        evidence_count = len(postings)
        names = sorted(group["names"], key=str.casefold)
        categories = sorted(group["categories"], key=str.casefold)
        payloads.append(
            {
                "id": skill_id,
                "name": names[0] if names else str(skill_id),
                "category": categories[0] if categories else "general",
                "requirement_type": (
                    "required"
                    if known_requirement_count == 0
                    or required_count >= known_requirement_count / 2
                    else "preferred"
                ),
                "confidence": round(confidence, 4),
                "evidence_count": evidence_count,
                "prevalence": round(evidence_count / denominator, 4),
                "source_type_count": source_type_count,
                "source_domain_count": source_domain_count,
                "company_count": len(companies),
                "required_ratio": round(
                    required_count / max(known_requirement_count, 1), 4
                ),
                "preferred_ratio": round(
                    preferred_count / max(known_requirement_count, 1), 4
                ),
                "ratio_evidence_status": (
                    "observed"
                    if known_requirement_count == evidence_count
                    else "partial"
                    if known_requirement_count
                    else "unknown"
                ),
                "first_published_at": min(published_dates) if published_dates else None,
                "last_published_at": max(published_dates) if published_dates else None,
                "cross_source_status": cross_source_status,
            }
        )
    return sorted(
        payloads,
        key=lambda item: (str(item["name"]).casefold(), str(item["id"])),
    )


def _datetime_value(value: object) -> tuple[datetime | None, bool]:
    if value in (None, ""):
        return None, True
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value, True
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None, False
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed, True


def _normalized_text_length(value: object) -> int:
    return len(re.sub(r"[\W_]+", "", str(value or ""), flags=re.UNICODE))


def _valid_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def assess_gate(record: Mapping[str, object], *, now: datetime) -> GateDecision:
    quarantine: set[str] = set()
    review: set[str] = set()

    if (
        "provenance_status" in record
        and record.get("provenance_status") != "approved"
    ):
        review.add("unverified_provenance")

    required = {
        "record_id": "missing_record_id",
        "job_family_id": "missing_job_family",
        "job_title_raw": "missing_job_title",
        "source_name": "missing_source_name",
        "collected_at": "missing_collected_at",
        "job_description_raw": "missing_job_description",
    }
    for field, code in required.items():
        if record.get(field) in (None, ""):
            quarantine.add(code)

    description = record.get("job_description_raw")
    if description not in (None, "") and _normalized_text_length(description) < 40:
        quarantine.add("job_description_too_short")

    published_at, published_valid = _datetime_value(record.get("published_at"))
    collected_at, collected_valid = _datetime_value(record.get("collected_at"))
    if not published_valid:
        quarantine.add("invalid_published_at")
    if not collected_valid:
        quarantine.add("invalid_collected_at")

    if published_at and collected_at:
        if published_at > collected_at + timedelta(days=1):
            review.add("published_after_collection")
        if abs((collected_at - published_at).days) > 3653:
            review.add("published_collection_gap_too_large")
    if published_at and published_at > now + timedelta(days=1):
        review.add("future_published_at")

    source_url = str(record.get("source_url") or "").strip()
    source_type = str(record.get("source_type") or "").strip()
    if source_url and not _valid_http_url(source_url):
        review.add("invalid_source_url")
    elif not source_url and source_type not in OFFICIAL_SOURCE_TYPES:
        review.add("missing_source_url")

    if record.get("has_capability_evidence") is False:
        review.add("no_capability_evidence")
    try:
        quality_score = float(record.get("quality_score") or 0.0)
    except (TypeError, ValueError):
        quality_score = 0.0
    if quality_score < 0.70:
        review.add("low_quality_score")

    duplicate = record.get("duplicate_of_id") not in (None, "")
    issue_codes = tuple(sorted(quarantine | review))
    if quarantine:
        status = "quarantined"
    elif duplicate:
        status = "duplicate"
    elif review:
        status = "review"
    else:
        status = "valid"
    return GateDecision(status=status, issue_codes=issue_codes)


def _experience_level(value: str | None) -> tuple[str | None, list[int]]:
    years = [int(item) for item in re.findall(r"\d+", value or "")]
    if not years:
        return None, []
    lower_bound = min(years)
    if lower_bound <= 2:
        return "junior", years
    if lower_bound <= 5:
        return "mid", years
    return "senior", years


def classify_seniority(
    title: str, experience: str | None, description: str
) -> SeniorityDecision:
    title = title or ""
    description = description or ""
    experience_level, years = _experience_level(experience)
    leadership_terms = (
        "技术规划",
        "团队管理",
        "跨部门决策",
        "标准制定",
        "行业标准",
    )
    senior_terms = ("架构设计", "技术攻关", "指导成员", "带领团队")
    has_leadership = any(term in description for term in leadership_terms)

    explicit_level: str | None = None
    if any(term in title for term in ("专家", "首席", "技术负责人", "技术总监")):
        explicit_level = "expert" if has_leadership else "senior"
    elif any(term in title for term in ("高级", "资深", "架构师")):
        explicit_level = "senior"
    elif any(term in title for term in ("初级", "助理", "应届", "实习")):
        explicit_level = "junior"

    evidence: dict[str, object] = {
        "title": title,
        "experience": experience,
        "experience_years": years,
        "title_level": explicit_level,
        "experience_level": experience_level,
        "conflict": False,
    }
    if explicit_level:
        confidence = 0.95
        if experience_level and experience_level != explicit_level:
            confidence -= 0.10
            evidence["conflict"] = True
        level = explicit_level
    elif experience_level:
        level = experience_level
        confidence = 0.85
    elif any(term in description for term in senior_terms):
        level = "senior"
        confidence = 0.75
    elif "独立完成" in description or "独立交付" in description:
        level = "mid"
        confidence = 0.75
    else:
        level = "unspecified"
        confidence = 0.0

    if confidence < 0.65:
        level = "unspecified"
    return SeniorityDecision(
        level=level,
        confidence=round(confidence, 2),
        rule_version=LEVEL_RULE_VERSION,
        evidence=evidence,
    )


def quarter_key(value: datetime) -> str:
    return f"{value.year}-Q{((value.month - 1) // 3) + 1}"


def _quarter_ordinal(value: str) -> int:
    match = re.fullmatch(r"(\d{4})-Q([1-4])", value)
    if not match:
        raise ValueError(f"无效季度键: {value}")
    return int(match.group(1)) * 4 + int(match.group(2)) - 1


def are_adjacent_quarters(previous: str, current: str) -> bool:
    return _quarter_ordinal(current) - _quarter_ordinal(previous) == 1


def classify_skill_change(
    before_rate: float,
    after_rate: float,
    *,
    before_requirement: str | None,
    after_requirement: str | None,
    before_evidence: int,
    after_evidence: int,
) -> ChangeDecision:
    delta = round(after_rate - before_rate, 6)
    if (
        before_rate < 0.05
        and after_rate >= 0.15
        and delta >= 0.10
        and after_evidence >= 3
    ):
        return ChangeDecision("added", delta)
    if (
        before_rate >= 0.15
        and after_rate < 0.05
        and delta <= -0.10
        and before_evidence >= 3
    ):
        return ChangeDecision("removed", delta)
    requirement_changed = (
        before_requirement in {"required", "preferred"}
        and after_requirement in {"required", "preferred"}
        and before_requirement != after_requirement
        and after_evidence >= 3
    )
    if (
        before_rate >= 0.05
        and after_rate >= 0.05
        and (abs(delta) >= 0.10 or requirement_changed)
    ):
        return ChangeDecision("modified", delta)
    return ChangeDecision(None, delta)
