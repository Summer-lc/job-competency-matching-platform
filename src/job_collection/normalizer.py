from __future__ import annotations

import hashlib
import html
import json
import math
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from src.job_collection.models import SourceDefinition, UnifiedJobRecord
from src.job_collection.source_registry import SourceRegistry, URLScopeError
from src.job_data_service import content_hash, normalize_text, simhash64


class NormalizationError(ValueError):
    """Raised when an adapter record cannot safely become a unified record."""


@dataclass(frozen=True)
class ReviewFinding:
    code: str
    severity: str
    field_name: str | None
    reason: str
    original_value: Any = None

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["field"] = self.field_name
        return value


_SPACE = re.compile(r"[^\S\r\n]+")
_CHINA_TIMEZONE = ZoneInfo("Asia/Shanghai")
_HTML_BLOCK_TAGS = frozenset({"p", "div", "li", "section", "article", "ul", "ol", "pre"})
_HTML_INLINE_TAGS = frozenset({"a", "b", "strong", "i", "em", "span", "code"})
_CONSUMED_ADAPTER_FIELDS = frozenset(
    {
        "collector_id",
        "job_family_id",
        "job_title_raw",
        "job_title",
        "title",
        "company_name",
        "industry",
        "region",
        "source_url",
        "source_record_id",
        "published_at",
        "published_at_evidence",
        "published_at_confidence",
        "first_seen_at",
        "last_seen_at",
        "experience_requirement",
        "experience",
        "education_requirement",
        "education",
        "salary_range",
        "salary",
        "job_description_raw",
        "description",
    }
)
_REQUIREMENT_PATTERN = re.compile(
    r"(?:岗位|职位|任职|工作)(?:要求|职责)|学历|本科|硕士|博士|大专|"
    r"\d+\s*[-~至到]?\s*\d*\s*年(?:以上)?(?:工作)?经验|负责|熟悉"
)
_SCALAR_REQUIREMENT_PATTERN = re.compile(
    r"(?:岗位|职位|任职|工作|学历|经验)(?:要求|职责)|负责|熟悉"
)
_OBVIOUS_SALARY_PATTERN = re.compile(
    r"(?:"
    r"[¥￥$]\s*\d+(?:\.\d+)?\s*(?:[kK千万元])?\s*[-~至到]\s*"
    r"\d+(?:\.\d+)?\s*(?:[kK千万元])?(?:\s*/\s*(?:月|年|天|小时))?"
    r"|\d+(?:\.\d+)?\s*(?:"
    r"[kK千万元]\s*[-~至到]\s*\d+(?:\.\d+)?\s*(?:[kK千万元])?"
    r"|[-~至到]\s*\d+(?:\.\d+)?\s*[kK千万元]"
    r")(?:\s*/\s*(?:月|年|天|小时))?"
    r"|\d+(?:\.\d+)?\s*[-~至到]\s*\d+(?:\.\d+)?\s*/\s*(?:月|年|天|小时)"
    r")",
    re.IGNORECASE,
)
_EDUCATION_PATTERN = re.compile(r"学历|本科|硕士|博士|大专|高中|中专")
_EXPERIENCE_PATTERN = re.compile(
    r"(?:\d+\s*(?:[-~至到]\s*\d+\s*)?年(?:以上|以下)?(?:工作)?经验|经验不限|应届)"
)
_REGION_PATTERN = re.compile(
    r"(?:北京|上海|天津|重庆)(?:市)?(?:[·,/]|$)|"
    r"[\u4e00-\u9fff]{2,}(?:省|市|自治区)(?:[·,/]|$)"
)


def _clean_inline(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", html.unescape(str(value)))
    text = text.translate(str.maketrans({"～": "-", "—": "-", "–": "-", "·": "·"}))
    return _SPACE.sub(" ", text).strip()


class _ReadableHTMLParser(HTMLParser):
    """Extract known HTML while preserving unknown angle-bracket expressions."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.events: list[tuple[str, Any, Any, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.events.append(("self", tag, self.get_starttag_text(), tuple(attrs)))
            return
        self.events.append(("start", tag, self.get_starttag_text(), tuple(attrs)))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.events.append(("self", tag, self.get_starttag_text(), tuple(attrs)))

    def handle_endtag(self, tag: str) -> None:
        self.events.append(("end", tag, f"</{tag}>", ()))

    def handle_data(self, data: str) -> None:
        self.events.append(("text", None, data, ()))

    def handle_entityref(self, name: str) -> None:
        encoded = f"&{name};"
        decoded = html.unescape(encoded)
        self.events.append(("text", None, f"&{name}" if decoded == encoded else decoded, ()))

    def handle_charref(self, name: str) -> None:
        self.events.append(("text", None, html.unescape(f"&#{name};"), ()))

    @staticmethod
    def _append_newline(parts: list[str]) -> None:
        if parts and parts[-1] != "\n":
            parts.append("\n")

    @staticmethod
    def _safe_href(attrs: tuple[tuple[str, str | None], ...]) -> str | None:
        href = next((value for name, value in attrs if name.lower() == "href"), None)
        if not href or any(ord(character) < 32 for character in href):
            return None
        parsed = urlsplit(href)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        return href

    def render(self) -> str:
        paired: dict[int, int] = {}
        stack: list[tuple[str, int]] = []
        recognized = _HTML_BLOCK_TAGS | _HTML_INLINE_TAGS
        for index, (kind, tag, _, _) in enumerate(self.events):
            if kind == "start" and tag in recognized:
                stack.append((tag, index))
            elif kind == "end" and tag in recognized:
                match_position = next(
                    (position for position in range(len(stack) - 1, -1, -1) if stack[position][0] == tag),
                    None,
                )
                if match_position is not None:
                    _, start_index = stack.pop(match_position)
                    paired[start_index] = index
                    paired[index] = start_index

        parts: list[str] = []
        for index, (kind, tag, raw, attrs) in enumerate(self.events):
            if kind == "text":
                parts.append(raw)
            elif kind == "self":
                if tag == "br" or tag in _HTML_BLOCK_TAGS:
                    self._append_newline(parts)
                elif tag not in _HTML_INLINE_TAGS:
                    parts.append(raw)
            elif kind == "start":
                if index not in paired:
                    parts.append(raw)
                elif tag in _HTML_BLOCK_TAGS:
                    self._append_newline(parts)
            elif kind == "end":
                if index not in paired:
                    parts.append(raw)
                    continue
                start_index = paired[index]
                if tag == "a":
                    href = self._safe_href(self.events[start_index][3])
                    if href:
                        parts.append(f" [{href}]")
                if tag in _HTML_BLOCK_TAGS:
                    self._append_newline(parts)
        return "".join(parts)


def _clean_description(value: Any) -> str:
    parser = _ReadableHTMLParser()
    parser.feed(str(value or ""))
    parser.close()
    text = unicodedata.normalize("NFKC", parser.render())
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_SPACE.sub(" ", line).strip() for line in text.split("\n")]
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _parse_datetime(value: Any, field_name: str) -> datetime | None:
    if value is None or (isinstance(value, str) and value.strip().lower() in {"", "unknown", "none"}):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = _clean_inline(value).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise NormalizationError(f"invalid {field_name}: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_CHINA_TIMEZONE)
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _calendar_years_before(value: datetime, years: int) -> datetime:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def _canonical_url(value: str) -> str:
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        host = f"{host}:{port}"
    path = parsed.path or "/"
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)), doseq=True)
    return urlunsplit((scheme, host, path, query, ""))


def _validate_source_scope(source_url: str, source: SourceDefinition) -> str:
    try:
        resolved = SourceRegistry((source,)).validate_url(source.source_id, source_url)
    except URLScopeError as exc:
        raise NormalizationError(f"source_url is outside source scope: {exc}") from exc
    return urlsplit(resolved).hostname or ""


def _required(raw: dict[str, Any], *names: str) -> str:
    for name in names:
        value = _clean_inline(raw.get(name))
        if value:
            return value
    raise NormalizationError(f"missing required adapter value: {names[0]}")


def _optional(raw: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = _clean_inline(raw.get(name))
        if value:
            return value
    return None


def _opaque_source_record_id(value: Any) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def _collect_adapter_extra(raw: dict[str, Any]) -> dict[str, object]:
    extra: dict[str, object] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise NormalizationError("adapter_extra keys must be strings")
        if key in _CONSUMED_ADAPTER_FIELDS or key.startswith("_"):
            continue
        try:
            json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise NormalizationError(f"adapter_extra field {key!r} is not JSON-safe") from exc
        extra[key] = value
    return extra


def _normalize_industry(value: Any) -> tuple[str | None, dict[str, Any] | None]:
    original = None if value is None else str(value).strip()
    cleaned = _clean_inline(value)
    if not cleaned:
        return None, None
    if len(cleaned) > 80:
        reason = "too_long"
    elif _REQUIREMENT_PATTERN.search(cleaned):
        reason = "requirement_text"
    elif _OBVIOUS_SALARY_PATTERN.search(cleaned):
        reason = "salary_pattern"
    else:
        return cleaned, None
    return "unknown", ReviewFinding(
        code="invalid_industry",
        severity="review",
        field_name="industry",
        reason=reason,
        original_value=original,
    ).as_dict()


def _normalize_region(value: Any) -> str | None:
    cleaned = _clean_inline(value)
    if not cleaned:
        return None
    return re.sub(r"\s*([·,/])\s*", r"\1", cleaned)


def _normalize_education(value: Any) -> str | None:
    cleaned = _clean_inline(value)
    if not cleaned:
        return None
    for level in ("博士", "硕士", "本科", "大专", "高中", "中专"):
        if level in cleaned:
            return level + ("及以上" if any(marker in cleaned for marker in ("及以上", "以上")) else "")
    if cleaned in {"不限", "无要求", "学历不限"}:
        return "不限"
    return cleaned


def _normalize_experience(value: Any) -> str | None:
    cleaned = _clean_inline(value)
    if not cleaned:
        return None
    if cleaned in {"不限", "无要求", "经验不限"}:
        return "不限"
    if any(marker in cleaned for marker in ("应届", "无经验")):
        return "无经验"
    compact = re.sub(r"\s+", "", cleaned).replace("~", "-")
    match = re.fullmatch(r"(\d+)-?(\d+)?年(?:工作)?(?:经验)?", compact)
    if match:
        return f"{match.group(1)}-{match.group(2)}年" if match.group(2) else f"{match.group(1)}年"
    return cleaned


def _normalize_salary(value: Any) -> str | None:
    cleaned = _clean_inline(value)
    if not cleaned:
        return None
    cleaned = cleaned.replace("¥", "").replace("￥", "")
    cleaned = cleaned.replace("~", "-").replace("K", "k")
    cleaned = re.sub(r"\s+", "", cleaned)
    return cleaned.replace("k", "K")


def _normalize_scalar_field(
    value: Any,
    field_name: str,
    normalizer: Any,
) -> tuple[str | None, dict[str, Any] | None]:
    if value is None:
        return None, None
    original = str(value)
    cleaned = _clean_inline(value)
    if not cleaned:
        return None, None

    if "\n" in original or "\r" in original:
        reason = "multiline_text"
    elif len(cleaned) > 80:
        reason = "too_long"
    elif _SCALAR_REQUIREMENT_PATTERN.search(cleaned):
        reason = "requirement_text"
    elif field_name != "salary_range" and _OBVIOUS_SALARY_PATTERN.search(cleaned):
        reason = "salary_pattern"
    elif field_name != "education_requirement" and _EDUCATION_PATTERN.search(cleaned):
        reason = "education_pattern"
    elif field_name != "experience_requirement" and _EXPERIENCE_PATTERN.search(cleaned):
        reason = "experience_pattern"
    elif field_name != "region" and _REGION_PATTERN.search(cleaned):
        reason = "region_pattern"
    else:
        return normalizer(value), None

    return None, ReviewFinding(
        code="invalid_scalar_field",
        severity="review",
        field_name=field_name,
        reason=reason,
        original_value=original,
    ).as_dict()


def _make_record_id(source_id: str, identity: str) -> str:
    digest = hashlib.sha256(f"{source_id}\0{identity}".encode("utf-8")).hexdigest()[:32]
    prefix = source_id[:47]
    return f"{prefix}-{digest}"


def normalize_job_record(
    raw: dict[str, Any],
    *,
    source: SourceDefinition,
    run_id: str,
    snapshot_metadata: dict[str, Any],
    collected_at: datetime,
) -> UnifiedJobRecord:
    """Normalize one structured adapter record without inferring missing facts."""

    if not isinstance(raw, dict):
        raise NormalizationError("adapter record must be a dict")

    collected = _parse_datetime(collected_at, "collected_at")
    if collected is None:
        raise NormalizationError("collected_at is required")

    source_url = _required(raw, "source_url")
    source_domain = _validate_source_scope(source_url, source)
    description = _clean_description(raw.get("job_description_raw", raw.get("description")))
    if not description:
        raise NormalizationError("missing required adapter value: job_description_raw")

    findings: list[dict[str, Any]] = []
    industry, industry_finding = _normalize_industry(raw.get("industry"))
    if industry_finding:
        findings.append(industry_finding)

    region, region_finding = _normalize_scalar_field(
        raw.get("region"), "region", _normalize_region
    )
    education, education_finding = _normalize_scalar_field(
        raw.get("education_requirement", raw.get("education")),
        "education_requirement",
        _normalize_education,
    )
    experience, experience_finding = _normalize_scalar_field(
        raw.get("experience_requirement", raw.get("experience")),
        "experience_requirement",
        _normalize_experience,
    )
    salary, salary_finding = _normalize_scalar_field(
        raw.get("salary_range", raw.get("salary")),
        "salary_range",
        _normalize_salary,
    )
    findings.extend(
        finding
        for finding in (
            region_finding,
            education_finding,
            experience_finding,
            salary_finding,
        )
        if finding is not None
    )

    published = _parse_datetime(raw.get("published_at"), "published_at")
    evidence = _optional(raw, "published_at_evidence")
    raw_confidence = raw.get("published_at_confidence")
    try:
        confidence = 0.0 if raw_confidence in (None, "") else float(raw_confidence)
    except (TypeError, ValueError) as exc:
        raise NormalizationError("published_at_confidence must be numeric") from exc
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise NormalizationError("published_at_confidence must be finite and between 0 and 1")
    if published is None:
        if evidence or confidence:
            findings.append(
                ReviewFinding(
                    code="publication_without_timestamp",
                    severity="review",
                    field_name="published_at",
                    reason="evidence_or_confidence_without_timestamp",
                    original_value=raw.get("published_at"),
                ).as_dict()
            )
        confidence = 0.0
    published_trusted = False
    if published:
        too_old = published < _calendar_years_before(collected, 20)
        future = published > collected
        plausible = not too_old and not future
        published_trusted = bool(evidence and confidence >= 0.8 and plausible)
        if not published_trusted:
            reasons = []
            if not evidence:
                reasons.append("missing_evidence")
            if confidence < 0.8:
                reasons.append("low_confidence")
            if future:
                reasons.append("future_date")
            elif too_old:
                reasons.append("implausible_date")
            findings.append(
                ReviewFinding(
                    code="untrusted_publication",
                    severity="review",
                    field_name="published_at",
                    reason=",".join(reasons),
                    original_value=raw.get("published_at"),
                ).as_dict()
            )

    observed = _parse_datetime(snapshot_metadata.get("observed_at"), "observed_at")
    first_seen = _parse_datetime(raw.get("first_seen_at"), "first_seen_at") or observed or collected
    last_seen = _parse_datetime(raw.get("last_seen_at"), "last_seen_at") or observed or collected
    if last_seen < first_seen:
        last_seen = first_seen

    source_record_id = _opaque_source_record_id(raw.get("source_record_id"))
    if source_record_id:
        identity = source_record_id
    else:
        identity = _canonical_url(source_url)
        findings.append(
            ReviewFinding(
                code="record_id_from_url",
                severity="review",
                field_name="source_record_id",
                reason="missing_source_record_id",
            ).as_dict()
        )

    if len(normalize_text(description)) < 40:
        findings.append(
            ReviewFinding(
                code="short_description",
                severity="quarantine",
                field_name="job_description_raw",
                reason="fewer_than_40_effective_characters",
                original_value=raw.get("job_description_raw", raw.get("description")),
            ).as_dict()
        )

    status = "valid"
    if any(item["severity"] == "quarantine" for item in findings):
        status = "quarantine"
    elif findings:
        status = "review"

    snapshot_hash = _clean_inline(snapshot_metadata.get("snapshot_hash"))
    if not re.fullmatch(r"[0-9a-fA-F]{64}", snapshot_hash):
        raise NormalizationError("snapshot_metadata.snapshot_hash must be 64 hex characters")

    payload = {
        "record_id": _make_record_id(source.source_id, identity),
        "collector_id": _optional(raw, "collector_id"),
        "job_family_id": _required(raw, "job_family_id"),
        "job_title_raw": _required(raw, "job_title_raw", "job_title", "title"),
        "company_name": _required(raw, "company_name"),
        "industry": industry,
        "region": region,
        "source_name": source.source_name,
        "source_type": source.source_type,
        "source_url": source_url,
        "source_id": source.source_id,
        "source_domain": source_domain,
        "source_record_id": source_record_id,
        "published_at": published,
        "published_at_evidence": evidence,
        "published_at_confidence": confidence,
        "published_at_trusted": published_trusted,
        "collected_at": collected,
        "first_seen_at": first_seen,
        "last_seen_at": last_seen,
        "snapshot_hash": snapshot_hash.lower(),
        "parser_name": source.parser_name,
        "parser_version": source.parser_version,
        "collection_method": source.collection_mode,
        "compliance_status": source.compliance_status,
        "compliance_note": source.compliance_note,
        "page_title": _optional(snapshot_metadata, "page_title"),
        "response_status": snapshot_metadata.get("response_status"),
        "run_id": _clean_inline(run_id),
        "experience_requirement": experience,
        "education_requirement": education,
        "salary_range": salary,
        "job_description_raw": description,
        "adapter_extra": _collect_adapter_extra(raw),
        "content_hash": content_hash(description),
        "simhash": simhash64(description),
        "normalization_status": status,
        "normalization_findings": findings,
        "normalization_audit": {
            "industry_original": raw.get("industry"),
            "canonical_source_url": _canonical_url(source_url),
        },
    }
    try:
        return UnifiedJobRecord.model_validate(payload)
    except ValidationError as exc:
        fields = ", ".join(".".join(map(str, error["loc"])) for error in exc.errors())
        raise NormalizationError(f"normalized record failed validation: {fields}") from exc
