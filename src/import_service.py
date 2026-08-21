from __future__ import annotations

import csv
import hashlib
import io
import json
import posixpath
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Collection, Mapping
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import uuid4

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from model_class.job_competency import JobPosting
from model_class.knowledge_base import (
    ImportBatch,
    EvidenceSnippet,
    JobSource,
    JobPostingRevision,
    QualityIssue,
    RawJobRecord,
)
from src.bounded_json import JSONResourceLimitError, decode_json_array_incrementally
from src.job_data_service import (
    SOURCE_SCORES,
    QualityFinding,
    assess_job_quality,
    persist_prepared_job_record,
    prepare_job_record,
)
from src.job_collection.source_registry import SourceRegistry, SourceRegistryError
from src.observation import (
    canonical_observation_payload,
    observation_identity,
    observation_time,
)
from src.hard_metrics_pipeline import rebuild_duplicate_groups, reclassify_postings


@dataclass(frozen=True)
class ParsedImportLine:
    line_number: int
    raw_text: str
    value: dict | None
    error_code: str | None = None
    error_message: str | None = None


class ImportLimitError(ValueError):
    """An import exceeds a configured resource or parser-complexity bound."""


@dataclass(frozen=True)
class _SourceAuthority:
    source_id: str
    source_name: str
    source_type: str
    base_url: str
    allowed_paths: tuple[str, ...]
    collection_method: str
    compliance_status: str
    parser_name: str
    parser_version: str
    enabled: bool


_COLLECTION_CAPABILITY_MARKER = object()


@dataclass(frozen=True)
class _ImportAuthorizationCapability:
    marker: object
    source_ids: frozenset[str]
    manual_external_url_source_ids: frozenset[str]
    manual_file_import_source_ids: frozenset[str]


def _verified_collection_import_capability(
    source_ids: Collection[str],
    *,
    manual_external_url_source_ids: Collection[str] = (),
    manual_file_import_source_ids: Collection[str] = (),
) -> _ImportAuthorizationCapability:
    return _ImportAuthorizationCapability(
        marker=_COLLECTION_CAPABILITY_MARKER,
        source_ids=frozenset(source_ids),
        manual_external_url_source_ids=frozenset(
            manual_external_url_source_ids
        ),
        manual_file_import_source_ids=frozenset(manual_file_import_source_ids),
    )


def _authorized_collection_sources(
    capability: object | None,
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    if not isinstance(capability, _ImportAuthorizationCapability):
        return frozenset(), frozenset(), frozenset()
    if capability.marker is not _COLLECTION_CAPABILITY_MARKER:
        return frozenset(), frozenset(), frozenset()
    return (
        capability.source_ids,
        capability.manual_external_url_source_ids,
        capability.manual_file_import_source_ids,
    )


MAX_IMPORT_BYTES = 25 * 1024 * 1024
MAX_IMPORT_RECORDS = 10_000
MAX_IMPORT_LINE_BYTES = 1024 * 1024
MAX_IMPORT_JSON_DEPTH = 50
_CURRENT_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "config" / "job_sources.json"


_STAGED_STATUS_RANK = {
    "valid": 0,
    "duplicate": 0,
    "review": 1,
    "quarantine": 2,
    "quarantined": 2,
}

_REVISION_IGNORED_FIELDS = {
    "adapter_extra",
    "collected_at",
    "first_seen_at",
    "last_seen_at",
    "normalization_findings",
    "normalization_status",
    "run_id",
    "snapshot_hash",
}


def _authority_from_row(source: JobSource) -> _SourceAuthority:
    try:
        paths = tuple(json.loads(source.allowed_paths_json or "[]"))
    except (json.JSONDecodeError, TypeError):
        paths = ()
    return _SourceAuthority(
        source_id=source.source_id,
        source_name=source.source_name,
        source_type=source.source_type,
        base_url=source.base_url,
        allowed_paths=paths,
        collection_method=source.collection_mode,
        compliance_status=source.compliance_status,
        parser_name=source.parser_name,
        parser_version=source.parser_version,
        enabled=source.enabled,
    )


@lru_cache(maxsize=1)
def _registry_authorities() -> dict[str, _SourceAuthority]:
    try:
        definitions = SourceRegistry.load(_CURRENT_REGISTRY_PATH).definitions
    except SourceRegistryError:
        return {}
    return {
        item.source_id: _SourceAuthority(
            source_id=item.source_id,
            source_name=item.source_name,
            source_type=item.source_type,
            base_url=item.base_url,
            allowed_paths=tuple(item.allowed_paths),
            collection_method=item.collection_mode,
            compliance_status=item.compliance_status,
            parser_name=item.parser_name,
            parser_version=item.parser_version,
            enabled=item.enabled,
        )
        for item in definitions
    }


def _normalized_source_url(value: object) -> tuple[str, str | None]:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").rstrip(".").lower()
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not host
            or not host.isascii()
            or parsed.username is not None
            or parsed.password is not None
        ):
            return raw, None
        port = parsed.port
    except ValueError:
        return raw, None
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    netloc = host if port in (None, default_port) else f"{host}:{port}"
    path = posixpath.normpath(unquote(parsed.path or "/"))
    if not path.startswith("/"):
        path = f"/{path}"
    normalized = urlunsplit(
        (parsed.scheme.lower(), netloc, path, parsed.query, "")
    )
    return normalized, host


def _source_url_matches(url: str, authority: _SourceAuthority) -> bool:
    normalized_base, base_host = _normalized_source_url(authority.base_url)
    normalized_url, host = _normalized_source_url(url)
    if base_host is None or host is None:
        return False
    base = urlsplit(normalized_base)
    target = urlsplit(normalized_url)
    if (base.scheme, base.netloc) != (target.scheme, target.netloc):
        return False
    target_path = posixpath.normpath(target.path or "/")
    return any(
        target_path == (prefix.rstrip("/") or "/")
        or target_path.startswith(f"{prefix.rstrip('/')}/")
        for prefix in authority.allowed_paths
    )


def _publication_is_structurally_trusted(prepared: Mapping[str, object]) -> bool:
    published = prepared.get("published_at")
    collected = prepared.get("collected_at")
    evidence = str(prepared.get("published_at_evidence") or "").strip()
    confidence = float(prepared.get("published_at_confidence") or 0.0)
    if not isinstance(published, datetime) or not evidence or confidence < 0.8:
        return False
    if isinstance(collected, datetime):
        if published > collected:
            return False
        if published.year < collected.year - 20:
            return False
    return True


async def _canonicalize_import_provenance(
    db: AsyncSession,
    prepared: dict[str, object],
    *,
    authorization: object | None = None,
) -> list[QualityFinding]:
    normalized_url, canonical_domain = _normalized_source_url(
        prepared.get("source_url")
    )
    prepared["source_url"] = normalized_url
    prepared["source_domain"] = canonical_domain
    source_id = str(prepared.get("source_id") or "").strip()
    row = (
        await db.scalar(select(JobSource).where(JobSource.source_id == source_id))
        if source_id
        else None
    )
    authority = _authority_from_row(row) if row is not None else _registry_authorities().get(source_id)
    if authority is None:
        prepared.update(
            source_type="unknown",
            provenance_status="unverified",
            published_at_trusted=False,
            source_score=0.0,
        )
        return [
            QualityFinding(
                code="unknown_source_provenance",
                severity="review",
                field_name="source_id",
                message="source provenance is not present in the reviewed registry",
            )
        ]

    claims = {
        "source_type": authority.source_type,
        "parser_name": authority.parser_name,
        "parser_version": authority.parser_version,
        "collection_method": authority.collection_method,
    }
    mismatched = [
        field
        for field, expected in claims.items()
        if str(prepared.get(field) or "").strip() != expected
    ]
    claimed_domain = str(prepared.get("source_domain") or "").strip().casefold()
    if claimed_domain and canonical_domain and claimed_domain != canonical_domain:
        mismatched.append("source_domain")
    if not _source_url_matches(normalized_url, authority):
        mismatched.append("source_url")
    (
        authorized_source_ids,
        manual_external_url_source_ids,
        manual_file_import_source_ids,
    ) = (
        _authorized_collection_sources(authorization)
    )
    manually_authorized_manifest = bool(
        source_id in authorized_source_ids
        and source_id in manual_external_url_source_ids
        and authority.compliance_status == "manual_only"
        and authority.collection_method == "manual_url_manifest"
    )
    manually_authorized_file = bool(
        source_id in authorized_source_ids
        and source_id in manual_file_import_source_ids
        and authority.compliance_status == "manual_only"
        and authority.collection_method == "file_import"
    )
    manually_authorized = manually_authorized_manifest or manually_authorized_file
    blocking_mismatches = (
        [item for item in mismatched if item != "source_url"]
        if manually_authorized
        else mismatched
    )
    approved = bool(
        authority.enabled
        and source_id in authorized_source_ids
        and (
            authority.compliance_status == "approved" or manually_authorized
        )
        and not blocking_mismatches
    )
    prepared.update(
        source_name=authority.source_name,
        source_type=authority.source_type,
        parser_name=authority.parser_name,
        parser_version=authority.parser_version,
        collection_method=authority.collection_method,
        provenance_status="approved" if approved else "unverified",
        source_score=(SOURCE_SCORES.get(authority.source_type, 0.65) if approved else 0.0),
    )
    prepared["published_at_trusted"] = bool(
        approved and _publication_is_structurally_trusted(prepared)
    )
    findings: list[QualityFinding] = []
    if not approved:
        findings.append(
            QualityFinding(
                code="provenance_mismatch",
                severity="review",
                field_name=",".join(sorted(set(mismatched))) or "source_id",
                message="uploaded provenance does not match an approved source definition",
            )
        )
    if prepared.get("published_at") and not prepared["published_at_trusted"]:
        findings.append(
            QualityFinding(
                code="untrusted_publication_provenance",
                severity="review",
                field_name="published_at",
                message="publication date lacks approved provenance and structured evidence",
            )
        )
    return findings


def _revision_payload_hash(value: str | Mapping[str, object]) -> str:
    if isinstance(value, str):
        try:
            payload: object = json.loads(value)
        except json.JSONDecodeError:
            payload = value
    else:
        payload = dict(value)
    if isinstance(payload, dict):
        payload = {
            key: item
            for key, item in payload.items()
            if key not in _REVISION_IGNORED_FIELDS
        }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _payload_mapping(value: str) -> Mapping[str, object]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _observation_time(values: Mapping[str, object]) -> datetime:
    return observation_time(values)


def _observation_identity(
    values: Mapping[str, object], observation_at: datetime
) -> str:
    return observation_identity(values, observation_at)


def _set_current_observation(
    posting: JobPosting,
    prepared: Mapping[str, object],
    raw: Mapping[str, object],
) -> None:
    posting.raw_payload = canonical_observation_payload(raw)
    for field in (
        "source_name",
        "source_type",
        "source_url",
        "source_id",
        "source_domain",
        "provenance_status",
        "source_record_id",
        "published_at_evidence",
        "published_at_confidence",
        "published_at_trusted",
        "snapshot_hash",
        "parser_name",
        "parser_version",
        "collection_method",
        "source_score",
    ):
        if prepared.get(field) is not None:
            setattr(posting, field, prepared[field])


def _earliest(first: datetime | None, second: datetime | None) -> datetime | None:
    values = [value for value in (first, second) if value is not None]
    return min(values) if values else None


def _latest(first: datetime | None, second: datetime | None) -> datetime | None:
    values = [value for value in (first, second) if value is not None]
    return max(values) if values else None


async def _reserve_import_writer(db: AsyncSession, *, commit: bool) -> None:
    if commit and not db.in_transaction() and db.get_bind().dialect.name == "sqlite":
        await db.execute(text("BEGIN IMMEDIATE"))


async def _add_revision(
    db: AsyncSession,
    *,
    posting_id: int,
    import_batch_id: int,
    payload_hash: str,
    raw_payload: str,
    observation_at: datetime,
    observation_identity: str,
) -> bool:
    existing = await db.scalar(
        select(JobPostingRevision.id).where(
            JobPostingRevision.job_posting_id == posting_id,
            JobPostingRevision.payload_hash == payload_hash,
            JobPostingRevision.observation_identity == observation_identity,
        )
    )
    if existing is not None:
        return False
    revision_no = (
        await db.scalar(
            select(func.max(JobPostingRevision.revision_no)).where(
                JobPostingRevision.job_posting_id == posting_id
            )
        )
        or 0
    ) + 1
    db.add(
        JobPostingRevision(
            job_posting_id=posting_id,
            import_batch_id=import_batch_id,
            revision_no=revision_no,
            payload_hash=payload_hash,
            observation_at=observation_at,
            observation_identity=observation_identity,
            raw_payload=raw_payload,
        )
    )
    return True


def _staged_quality(record: Mapping[str, object]) -> tuple[str, list[QualityFinding]]:
    statuses: list[str] = []
    findings: dict[str, QualityFinding] = {}

    def add_status(value: object, field_name: str) -> None:
        if value in (None, ""):
            return
        status = str(value).strip().lower()
        if status not in _STAGED_STATUS_RANK:
            statuses.append("quarantine")
            add_finding(
                {
                    "code": "invalid_staged_quality_status",
                    "severity": "quarantine",
                    "field_name": field_name,
                    "message": f"unsupported staged status: {status}",
                },
                "quarantine",
            )
            return
        statuses.append(status)

    def add_finding(value: object, default_severity: str) -> None:
        if not isinstance(value, Mapping):
            return
        code = str(value.get("code") or "").strip()
        if not code:
            return
        severity = str(value.get("severity") or default_severity).strip().lower()
        if severity not in {"review", "quarantine"}:
            severity = "quarantine"
        field = value.get("field_name", value.get("field"))
        message = str(
            value.get("message")
            or value.get("reason")
            or "staged collection quality finding"
        )
        finding = QualityFinding(
            code=code,
            severity=severity,
            field_name=str(field) if field not in (None, "") else None,
            message=message,
        )
        previous = findings.get(code)
        if (
            previous is None
            or _STAGED_STATUS_RANK[severity] > _STAGED_STATUS_RANK[previous.severity]
        ):
            findings[code] = finding
        statuses.append(severity)

    add_status(record.get("normalization_status"), "normalization_status")
    normalization_findings = record.get("normalization_findings", [])
    if isinstance(normalization_findings, list):
        for finding in normalization_findings:
            add_finding(finding, "review")

    adapter_extra = record.get("adapter_extra")
    if isinstance(adapter_extra, Mapping):
        quality_findings = adapter_extra.get("quality_findings", [])
        if isinstance(quality_findings, list):
            for finding in quality_findings:
                add_finding(finding, "review")
        quality_gate = adapter_extra.get("quality_gate")
        if isinstance(quality_gate, Mapping):
            gate_status = quality_gate.get("status")
            add_status(gate_status, "adapter_extra.quality_gate.status")
            default_severity = (
                "quarantine"
                if _STAGED_STATUS_RANK.get(str(gate_status).lower(), 1) >= 2
                else "review"
            )
            issue_codes = quality_gate.get("issue_codes", [])
            if isinstance(issue_codes, list):
                for code in issue_codes:
                    add_finding(
                        {
                            "code": code,
                            "severity": default_severity,
                            "message": "staged adapter quality gate finding",
                        },
                        default_severity,
                    )

    highest = max(
        (_STAGED_STATUS_RANK.get(status, 1) for status in statuses), default=0
    )
    return ("valid", "review", "quarantine")[highest], list(findings.values())


def _merge_quality_findings(*groups: list[QualityFinding]) -> list[QualityFinding]:
    merged: dict[str, QualityFinding] = {}
    for group in groups:
        for finding in group:
            previous = merged.get(finding.code)
            if previous is None or _STAGED_STATUS_RANK.get(
                finding.severity, 1
            ) > _STAGED_STATUS_RANK.get(previous.severity, 1):
                merged[finding.code] = finding
    return list(merged.values())


def _parsed_line(line_number: int, raw_text: str, value: object) -> ParsedImportLine:
    if not isinstance(value, dict):
        return ParsedImportLine(
            line_number=line_number,
            raw_text=raw_text,
            value=None,
            error_code="invalid_record_type",
            error_message="记录必须是JSON对象",
        )
    return ParsedImportLine(line_number=line_number, raw_text=raw_text, value=value)


def _validate_json_depth(text: str, *, label: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_IMPORT_JSON_DEPTH:
                raise ImportLimitError(
                    f"{label} exceeds JSON nesting limit {MAX_IMPORT_JSON_DEPTH}"
                )
        elif character in "]}":
            depth = max(0, depth - 1)


def _validate_import_bounds(raw: bytes) -> None:
    if len(raw) > MAX_IMPORT_BYTES:
        raise ImportLimitError(
            f"import exceeds byte limit {MAX_IMPORT_BYTES}: {len(raw)} bytes"
        )
    for line_number, line in enumerate(io.BytesIO(raw), start=1):
        if len(line) > MAX_IMPORT_LINE_BYTES:
            raise ImportLimitError(
                f"import line {line_number} exceeds byte limit {MAX_IMPORT_LINE_BYTES}"
            )


def _validate_record_count(count: int) -> None:
    if count > MAX_IMPORT_RECORDS:
        raise ImportLimitError(
            f"import record count exceeds limit {MAX_IMPORT_RECORDS}: {count}"
        )


def parse_import_lines(raw: bytes, filename: str) -> list[ParsedImportLine]:
    _validate_import_bounds(raw)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return [
            ParsedImportLine(
                line_number=1,
                raw_text="",
                value=None,
                error_code="invalid_encoding",
                error_message=str(exc),
            )
        ]

    if filename.lower().endswith(".csv"):
        reader = csv.DictReader(io.StringIO(text))
        parsed = []
        for index, row in enumerate(reader, start=2):
            if len(parsed) >= MAX_IMPORT_RECORDS:
                _validate_record_count(len(parsed) + 1)
            parsed.append(
                _parsed_line(index, json.dumps(row, ensure_ascii=False), dict(row))
            )
        return parsed

    stripped = text.strip()
    if not stripped:
        return []
    _validate_json_depth(stripped, label="import")
    if stripped.startswith("["):
        try:
            decoded = decode_json_array_incrementally(
                stripped, max_records=MAX_IMPORT_RECORDS
            )
        except JSONResourceLimitError as exc:
            raise ImportLimitError(str(exc)) from exc
        return [
            _parsed_line(index, item.raw_text, item.value)
            for index, item in enumerate(decoded, start=1)
        ]
    try:
        value = json.loads(stripped)
    except RecursionError as exc:
        raise ImportLimitError("import exceeds JSON decoder nesting resources") from exc
    except json.JSONDecodeError:
        lines: list[ParsedImportLine] = []
        for line_number, line in enumerate(io.StringIO(text), start=1):
            if not line.strip():
                continue
            _validate_json_depth(line, label=f"import line {line_number}")
            if len(lines) >= MAX_IMPORT_RECORDS:
                _validate_record_count(len(lines) + 1)
            try:
                item = json.loads(line)
            except RecursionError as exc:
                raise ImportLimitError(
                    f"import line {line_number} exceeds JSON decoder nesting resources"
                ) from exc
            except json.JSONDecodeError as exc:
                lines.append(
                    ParsedImportLine(
                        line_number=line_number,
                        raw_text=line,
                        value=None,
                        error_code="invalid_json",
                        error_message=exc.msg,
                    )
                )
            else:
                lines.append(_parsed_line(line_number, line, item))
        return lines

    _validate_record_count(1)
    return [_parsed_line(1, stripped, value)]


async def import_job_file(
    db: AsyncSession,
    raw: bytes,
    filename: str,
    *,
    commit: bool = True,
    authorization: object | None = None,
) -> dict:
    await _reserve_import_writer(db, commit=commit)
    file_hash = hashlib.sha256(raw).hexdigest()
    existing_batch = await db.scalar(
        select(ImportBatch).where(ImportBatch.file_hash == file_hash)
    )
    if existing_batch is not None and existing_batch.status == "completed":
        summary = json.loads(existing_batch.summary_json)
        summary["idempotent"] = True
        return summary

    lines = parse_import_lines(raw, filename)
    batch = ImportBatch(
        batch_id=str(uuid4()),
        filename=filename,
        file_hash=file_hash,
        file_size=len(raw),
        raw_lines=len(lines),
        parsed_lines=sum(line.value is not None for line in lines),
    )
    db.add(batch)
    await db.flush()

    imported = 0
    quarantined = 0
    review_count = 0
    duplicates = 0
    skipped = 0
    revised = 0
    errors = []
    affected_families = set()
    formal_review_overrides: dict[int, set[str]] = {}
    for line in lines:
        raw_record = RawJobRecord(
            import_batch_id=batch.id,
            line_number=line.line_number,
            raw_text=line.raw_text,
            raw_hash=hashlib.sha256(line.raw_text.encode("utf-8")).hexdigest(),
            parsed_json=(
                json.dumps(line.value, ensure_ascii=False)
                if line.value is not None
                else None
            ),
            status="parsed" if line.value is not None else "quarantined",
            error_code=line.error_code,
            error_message=line.error_message,
        )
        db.add(raw_record)
        await db.flush()
        if line.value is None:
            quarantined += 1
            db.add(
                QualityIssue(
                    raw_record_id=raw_record.id,
                    code=line.error_code or "invalid_record",
                    severity="quarantine",
                    message=line.error_message or "记录无法解析",
                )
            )
            errors.append(
                {"row": line.line_number, "message": raw_record.error_message}
            )
            continue

        staged_status, staged_findings = _staged_quality(line.value)
        if staged_status == "quarantine":
            raw_record.status = "quarantined"
            raw_record.error_code = "staged_quality_quarantine"
            raw_record.error_message = (
                "staged collection quality gate requires quarantine"
            )
            quarantined += 1
            for finding in staged_findings or [
                QualityFinding(
                    code="staged_quality_quarantine",
                    severity="quarantine",
                    field_name=None,
                    message=raw_record.error_message,
                )
            ]:
                db.add(
                    QualityIssue(
                        raw_record_id=raw_record.id,
                        code=finding.code,
                        severity="quarantine",
                        field_name=finding.field_name,
                        message=finding.message,
                    )
                )
            errors.append(
                {"row": line.line_number, "message": raw_record.error_message}
            )
            continue

        try:
            prepared = prepare_job_record(line.value)
        except ValueError as exc:
            description = str(line.value.get("job_description_raw") or "")
            code = (
                "description_too_short"
                if len(description.strip()) < 10
                else "validation_error"
            )
            raw_record.status = "quarantined"
            raw_record.error_code = code
            raw_record.error_message = str(exc)
            quarantined += 1
            db.add(
                QualityIssue(
                    raw_record_id=raw_record.id,
                    code=code,
                    severity="quarantine",
                    field_name="job_description_raw"
                    if code == "description_too_short"
                    else None,
                    message=str(exc),
                )
            )
            errors.append({"row": line.line_number, "message": str(exc)})
            continue

        provenance_findings = await _canonicalize_import_provenance(
            db,
            prepared,
            authorization=authorization,
        )
        findings = _merge_quality_findings(
            assess_job_quality(prepared), staged_findings, provenance_findings
        )
        existing_posting = await db.scalar(
            select(JobPosting)
            .where(JobPosting.record_id == prepared["record_id"])
            .with_for_update()
        )
        incoming_hash = _revision_payload_hash(line.value)
        previous_hash: str | None = None
        previous_payload: str | None = None
        previous_values: Mapping[str, object] = {}
        if existing_posting is not None:
            previous_payload = existing_posting.raw_payload or "{}"
            previous_values = _payload_mapping(previous_payload)
            previous_hash = _revision_payload_hash(previous_payload)
            if previous_hash == incoming_hash:
                incoming_time = _observation_time(prepared)
                current_time = _observation_time(existing_posting.__dict__)
                incoming_key = canonical_observation_payload(line.value)
                current_key = canonical_observation_payload(previous_values)
                async with db.begin_nested():
                    existing_posting.first_seen_at = _earliest(
                        existing_posting.first_seen_at, prepared.get("first_seen_at")
                    )
                    existing_posting.last_seen_at = _latest(
                        existing_posting.last_seen_at, prepared.get("last_seen_at")
                    )
                    if incoming_time > current_time or (
                        incoming_time == current_time and incoming_key < current_key
                    ):
                        _set_current_observation(
                            existing_posting, prepared, line.value
                        )
                    existing_posting.observation_version += 1
                    await db.flush()
                raw_record.status = "unchanged"
                raw_record.job_posting_id = existing_posting.id
                skipped += 1
                continue

            incoming_time = _observation_time(prepared)
            current_time = _observation_time(existing_posting.__dict__)
            stale = incoming_time < current_time
            equal_time_loser = (
                incoming_time == current_time
                and incoming_hash > (previous_hash or "")
            )
            if stale or equal_time_loser:
                try:
                    async with db.begin_nested():
                        revision_added = await _add_revision(
                            db,
                            posting_id=existing_posting.id,
                            import_batch_id=batch.id,
                            payload_hash=incoming_hash,
                            raw_payload=json.dumps(line.value, ensure_ascii=False),
                            observation_at=incoming_time,
                            observation_identity=_observation_identity(
                                line.value, incoming_time
                            ),
                        )
                        existing_posting.first_seen_at = _earliest(
                            existing_posting.first_seen_at,
                            prepared.get("first_seen_at"),
                        )
                        existing_posting.last_seen_at = _latest(
                            existing_posting.last_seen_at,
                            prepared.get("last_seen_at"),
                        )
                        existing_posting.observation_version += 1
                        await db.flush()
                except Exception as exc:
                    raw_record.status = "quarantined"
                    raw_record.error_code = "persistence_error"
                    raw_record.error_message = str(exc)
                    quarantined += 1
                    db.add(
                        QualityIssue(
                            raw_record_id=raw_record.id,
                            code="persistence_error",
                            severity="quarantine",
                            message=str(exc),
                        )
                    )
                    errors.append({"row": line.line_number, "message": str(exc)})
                    continue
                raw_record.status = (
                    "stale_revision" if stale else "equal_time_conflict"
                )
                raw_record.job_posting_id = existing_posting.id
                if revision_added:
                    revised += 1
                else:
                    skipped += 1
                continue

        try:
            async with db.begin_nested():
                if existing_posting is not None:
                    await _add_revision(
                        db,
                        posting_id=existing_posting.id,
                        import_batch_id=batch.id,
                        payload_hash=previous_hash or "",
                        raw_payload=previous_payload or "{}",
                        observation_at=_observation_time(previous_values),
                        observation_identity=_observation_identity(
                            previous_values, _observation_time(previous_values)
                        ),
                    )
                posting, is_duplicate = await persist_prepared_job_record(
                    db, prepared, existing=existing_posting
                )
                if existing_posting is not None:
                    await db.execute(
                        delete(EvidenceSnippet).where(
                            EvidenceSnippet.job_posting_id == posting.id
                        )
                    )
                description = posting.job_description_raw
                for entity_type, items in (
                    ("skill", prepared.get("skills", [])),
                    ("responsibility", prepared.get("responsibilities", [])),
                ):
                    for item in items:
                        evidence_text = str(item.get("evidence_text") or "").strip()
                        if not evidence_text or evidence_text not in description:
                            continue
                        start_offset = item.get("start_offset")
                        if start_offset is None:
                            start_offset = description.find(evidence_text)
                        end_offset = (
                            start_offset + len(evidence_text)
                            if start_offset >= 0
                            else None
                        )
                        entity_key = str(item["name"])
                        evidence_key = hashlib.sha256(
                            f"{posting.id}|{entity_type}|{entity_key}|{evidence_text}".encode(
                                "utf-8"
                            )
                        ).hexdigest()
                        db.add(
                            EvidenceSnippet(
                                evidence_key=evidence_key,
                                job_posting_id=posting.id,
                                entity_type=entity_type,
                                entity_key=entity_key,
                                evidence_text=evidence_text,
                                start_offset=start_offset
                                if start_offset >= 0
                                else None,
                                end_offset=end_offset,
                                text_hash=hashlib.sha256(
                                    evidence_text.encode("utf-8")
                                ).hexdigest(),
                                confidence=float(item.get("confidence") or 0.0),
                                review_status="pending" if findings else "approved",
                            )
                        )
        except Exception as exc:
            raw_record.status = "quarantined"
            raw_record.error_code = "persistence_error"
            raw_record.error_message = str(exc)
            quarantined += 1
            db.add(
                QualityIssue(
                    raw_record_id=raw_record.id,
                    code="persistence_error",
                    severity="quarantine",
                    message=str(exc),
                )
            )
            errors.append({"row": line.line_number, "message": str(exc)})
            continue

        raw_record.job_posting_id = posting.id
        review_codes = {
            finding.code for finding in findings if finding.severity == "review"
        }
        if staged_status == "review" or review_codes:
            formal_review_overrides[posting.id] = review_codes
        if existing_posting is None:
            imported += 1
        else:
            revised += 1
        if is_duplicate:
            duplicates += 1
            raw_record.status = "duplicate"
        elif findings:
            posting.status = "review"
            raw_record.status = "review"
            review_count += 1
        else:
            raw_record.status = "valid"
        for finding in findings:
            db.add(
                QualityIssue(
                    raw_record_id=raw_record.id,
                    job_posting_id=posting.id,
                    code=finding.code,
                    severity=finding.severity,
                    field_name=finding.field_name,
                    message=finding.message,
                )
            )
        affected_families.add(posting.job_family_id)

    if affected_families:
        await rebuild_duplicate_groups(db, family_codes=affected_families)
        await reclassify_postings(db, family_codes=affected_families)
        for posting_id, review_codes in formal_review_overrides.items():
            posting = await db.get(JobPosting, posting_id)
            if posting is None:
                continue
            try:
                current_codes = set(json.loads(posting.gate_issue_codes_json or "[]"))
            except (json.JSONDecodeError, TypeError):
                current_codes = set()
            posting.gate_issue_codes_json = json.dumps(
                sorted(current_codes | review_codes), ensure_ascii=False
            )
            if posting.gate_status == "valid":
                posting.gate_status = "review"
            if posting.status == "valid":
                posting.status = "review"

    batch.imported = imported
    batch.quarantined = quarantined
    batch.review_count = review_count
    batch.duplicates = duplicates
    batch.skipped = skipped
    batch.revised = revised
    batch.affected_families_json = json.dumps(
        sorted(affected_families), ensure_ascii=False
    )
    batch.status = "completed"
    batch.completed_at = datetime.now()
    summary = {
        "batch_id": batch.batch_id,
        "filename": batch.filename,
        "raw_lines": batch.raw_lines,
        "parsed_lines": batch.parsed_lines,
        "imported": imported,
        "revised": revised,
        "review": review_count,
        "quarantined": quarantined,
        "duplicates": duplicates,
        "skipped": skipped,
        "errors": errors,
        "affected_families": sorted(affected_families),
        "idempotent": False,
    }
    batch.summary_json = json.dumps(summary, ensure_ascii=False)
    if commit:
        await db.commit()
    else:
        await db.flush()
    return summary
