from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.job_data_service import JOB_FAMILY_NAMES


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "job_family_queries.json"
GENERIC_TERMS = ("ai", "数据", "开发", "运维", "平台", "工程师")


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower()
    return re.sub(r"\s+", " ", value).strip()


class FamilyQuota(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target: int = Field(gt=0, strict=True)
    batch_size: int = Field(gt=0, strict=True)


class FamilyDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    queries: tuple[str, ...] = Field(min_length=1)
    title_aliases: tuple[str, ...] = Field(min_length=1)
    skill_indicators: tuple[str, ...] = Field(min_length=1)
    exclusions: tuple[str, ...] = ()
    minimum_title_evidence: int = Field(gt=0, strict=True)
    minimum_skill_evidence: int = Field(gt=0, strict=True)
    confidence: float = Field(ge=0.0, le=1.0, strict=True)
    quota: FamilyQuota

    @field_validator(
        "queries", "title_aliases", "skill_indicators", "exclusions", mode="before"
    )
    @classmethod
    def validate_term_list(cls, value: Any) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("term list must be an array")
        stripped: list[str] = []
        normalized: set[str] = set()
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("terms must be non-empty strings")
            term = item.strip()
            canonical = _normalize(term)
            if canonical in normalized:
                raise ValueError("terms must be unique after normalization")
            normalized.add(canonical)
            stripped.append(term)
        return tuple(stripped)


@dataclass(frozen=True)
class ClassificationResult:
    status: str
    family_code: str | None
    candidates: tuple[str, ...]
    matched_title_terms: tuple[str, ...]
    matched_skill_terms: tuple[str, ...]
    excluded_terms: tuple[str, ...]
    confidence: float
    reason: str


@dataclass(frozen=True)
class DeficitScheduleItem:
    family_code: str
    valid_count: int
    deficit: int
    batch_count: int
    requested: int


_NEGATION_PATTERN = re.compile(
    r"(?:不使用|无需|不涉及|仅了解|非核心|不负责|未使用|不要求|不采用|没有)"
    r"[\s、/和与及的]*$"
)


def _is_negated(text: str, start: int) -> bool:
    prefix = text[max(0, start - 16) : start]
    clause = re.split(r"[，,。；;！!？?\n]", prefix)[-1]
    return bool(_NEGATION_PATTERN.search(clause))


def _terms_found(
    terms: tuple[str, ...], text: str, *, ignore_negated: bool = False
) -> tuple[str, ...]:
    normalized = _normalize(text)
    found = []
    for term in terms:
        needle = _normalize(term)
        leading = r"(?<![a-z0-9])" if needle[0].isascii() and needle[0].isalnum() else ""
        trailing = r"(?![a-z0-9])" if needle[-1].isascii() and needle[-1].isalnum() else ""
        pattern = re.compile(f"{leading}{re.escape(needle)}{trailing}")
        matches = tuple(pattern.finditer(normalized))
        if any(not ignore_negated or not _is_negated(normalized, match.start()) for match in matches):
            found.append(term)
    return tuple(found)


def load_family_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, FamilyDefinition]:
    config_path = Path(path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    expected = set(JOB_FAMILY_NAMES)
    if not isinstance(raw, dict) or set(raw) != expected:
        missing = sorted(expected - set(raw if isinstance(raw, dict) else ()))
        extra = sorted(set(raw if isinstance(raw, dict) else ()) - expected)
        raise ValueError(f"family config must exactly match JOB_FAMILY_NAMES; missing={missing}, extra={extra}")

    definitions: dict[str, FamilyDefinition] = {}
    for code, value in raw.items():
        try:
            definition = FamilyDefinition.model_validate(value)
        except ValidationError as exc:
            raise ValueError(f"invalid family config for {code}: {exc}") from exc
        definitions[code] = definition
    return definitions


def classify_job_family(
    title: str,
    description: str,
    config: Mapping[str, FamilyDefinition] | None = None,
) -> ClassificationResult:
    """Classify from title plus capability evidence; retrieval queries are never evidence."""

    definitions = dict(config or load_family_config())
    scored: list[dict[str, object]] = []
    for code, definition in definitions.items():
        title_hits = _terms_found(definition.title_aliases, title)
        skill_hits = _terms_found(
            definition.skill_indicators, description, ignore_negated=True
        )
        excluded_hits = _terms_found(
            definition.exclusions, f"{title}\n{description}", ignore_negated=True
        )
        if not title_hits and not skill_hits:
            continue
        confidence = min(0.98, 0.62 + min(len(title_hits), 2) * 0.12 + min(len(skill_hits), 2) * 0.12)
        capability_only = not title_hits and len(skill_hits) >= 3
        if capability_only:
            confidence = min(0.98, confidence + 0.08)
        confidence = max(0.0, confidence - min(len(excluded_hits), 2) * 0.25)
        title_and_capability = (
            len(title_hits) >= definition.minimum_title_evidence
            and len(skill_hits) >= definition.minimum_skill_evidence
        )
        evidence_supported = title_and_capability or capability_only
        eligible = (
            evidence_supported
            and confidence >= definition.confidence
            and not excluded_hits
        )
        scored.append(
            {
                "code": code,
                "title": title_hits,
                "skills": skill_hits,
                "excluded": excluded_hits,
                "confidence": confidence,
                "capability_only": capability_only,
                "evidence_supported": evidence_supported,
                "eligible": eligible,
            }
        )

    scored.sort(key=lambda item: (-float(item["confidence"]), str(item["code"])))
    candidates = tuple(str(item["code"]) for item in scored)
    eligible = [item for item in scored if item["eligible"]]

    evidence_ready = [
        item
        for item in scored
        if item["evidence_supported"]
    ]

    if len(evidence_ready) == 1 and len(eligible) == 1:
        winner = eligible[0]
        return ClassificationResult(
            status="auto",
            family_code=str(winner["code"]),
            candidates=candidates,
            matched_title_terms=tuple(winner["title"]),
            matched_skill_terms=tuple(winner["skills"]),
            excluded_terms=tuple(winner["excluded"]),
            confidence=round(float(winner["confidence"]), 3),
            reason=(
                "strong_capability_only_evidence"
                if winner["capability_only"]
                else "strong_title_and_capability_evidence"
            ),
        )

    title_terms = tuple(dict.fromkeys(term for item in scored for term in item["title"]))
    skill_terms = tuple(dict.fromkeys(term for item in scored for term in item["skills"]))
    excluded_terms = tuple(dict.fromkeys(term for item in scored for term in item["excluded"]))
    if len(evidence_ready) > 1:
        reason = "multi_family_conflict"
    elif excluded_terms:
        reason = "exclusion_conflict"
    elif any(term in _normalize(f"{title} {description}") for term in GENERIC_TERMS):
        reason = "generic_or_insufficient_evidence"
    else:
        reason = "insufficient_evidence"
    confidence = min(0.79, max((float(item["confidence"]) for item in scored), default=0.0))
    return ClassificationResult(
        status="review",
        family_code=None,
        candidates=candidates,
        matched_title_terms=title_terms,
        matched_skill_terms=skill_terms,
        excluded_terms=excluded_terms,
        confidence=round(confidence, 3),
        reason=reason,
    )


def classify_job_family_with_hint(
    title: str,
    description: str,
    supplied_family: str,
    config: Mapping[str, FamilyDefinition] | None = None,
) -> ClassificationResult:
    """Use a reviewed family label only when capability evidence supports it."""

    definitions = dict(config or load_family_config())
    automatic = classify_job_family(title, description, definitions)
    if automatic.status == "auto" or supplied_family not in definitions:
        return automatic

    definition = definitions[supplied_family]
    title_hits = _terms_found(definition.title_aliases, title)
    skill_hits = _terms_found(
        definition.skill_indicators, description, ignore_negated=True
    )
    excluded_hits = _terms_found(
        definition.exclusions, f"{title}\n{description}", ignore_negated=True
    )
    supported = bool(title_hits and skill_hits) or len(skill_hits) >= 2
    if excluded_hits or not supported:
        return automatic

    candidates = tuple(
        dict.fromkeys((supplied_family, *automatic.candidates))
    )
    confidence = min(0.95, 0.80 + 0.04 * min(len(skill_hits), 3))
    return ClassificationResult(
        status="annotated",
        family_code=supplied_family,
        candidates=candidates,
        matched_title_terms=title_hits,
        matched_skill_terms=skill_hits,
        excluded_terms=excluded_hits,
        confidence=round(confidence, 3),
        reason="reviewed_family_hint_with_capability_evidence",
    )


def schedule_family_deficits(
    valid_unique_counts: Mapping[str, int],
    batch_counts: Mapping[str, int],
    config: Mapping[str, FamilyDefinition] | None = None,
) -> tuple[DeficitScheduleItem, ...]:
    """Return deterministic work ordered by the lowest valid unique count first."""

    definitions = dict(config or load_family_config())
    scheduled: list[tuple[int, str, DeficitScheduleItem]] = []
    for code, definition in definitions.items():
        valid_count = max(0, int(valid_unique_counts.get(code, 0)))
        batch_count = max(0, int(batch_counts.get(code, 0)))
        deficit = max(0, definition.quota.target - valid_count)
        batch_remaining = max(0, definition.quota.batch_size - batch_count)
        requested = min(deficit, batch_remaining)
        if requested:
            item = DeficitScheduleItem(code, valid_count, deficit, batch_count, requested)
            scheduled.append((valid_count, code, item))
    scheduled.sort(key=lambda entry: (entry[0], entry[1]))
    return tuple(entry[2] for entry in scheduled)
