from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from model_class.job_competency import JobPosting
from model_class.knowledge_base import DataRepairAudit
from src.hard_metrics_pipeline import rebuild_duplicate_groups
from src.job_data_service import content_hash, hamming_distance, simhash64
from src.job_collection.security import (
    provision_secure_directory,
    secure_atomic_write,
    secure_read_file,
)


REPAIR_RULE_VERSION = "historical-job-repair-v1"
DEFAULT_REPAIRS_ROOT = Path(__file__).resolve().parents[1] / "data" / "repairs"
MAX_REPAIR_REPORT_BYTES = 64 * 1024 * 1024

_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
_EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
_SALARY_PATTERN = re.compile(
    r"(?:[¥￥$]\s*\d|\d+(?:\.\d+)?\s*(?:k|千|万|元)\s*"
    r"[-~至到]|\d+\s*[-~至到]\s*\d+\s*/\s*(?:月|年|day|month))",
    re.I,
)
_REQUIREMENT_PATTERN = re.compile(
    r"(?:岗位|职位|任职|工作)(?:要求|职责)|学历|本科|硕士|博士|大专|"
    r"负责|熟悉|bachelor|master|phd|degree|years?\s+(?:of\s+)?experience|"
    r"responsible\s+for|requirements?",
    re.I,
)


class RepairStorageError(ValueError):
    """A repair run identifier or report path is unsafe."""


@dataclass(frozen=True)
class RepairChange:
    field_name: str
    before: object
    after: object
    reason_code: str


@dataclass(frozen=True)
class LegacySourceAuthorization:
    source_id: str
    source_name: str
    source_type: str
    source_domain: str
    collection_method: str
    parser_name: str
    parser_version: str
    authorization_note: str
    domain_scope: str = "zhaopin.com"


def _safe_url_hostname(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urlsplit(value.strip())
        host = parsed.hostname
        parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not host
        or not host.isascii()
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return host.rstrip(".").lower()


def is_legacy_source_candidate(
    posting: object, authorization: LegacySourceAuthorization
) -> bool:
    if getattr(posting, "source_id", None) not in (None, ""):
        return False
    if getattr(posting, "provenance_status", None) != "unverified":
        return False
    if getattr(posting, "source_name", None) != "智联招聘":
        return False
    host = _safe_url_hostname(getattr(posting, "source_url", None))
    scope = authorization.domain_scope.rstrip(".").lower()
    return bool(host and (host == scope or host.endswith(f".{scope}")))


def _authorization_repairs(
    posting: object, authorization: LegacySourceAuthorization
) -> tuple[RepairChange, ...]:
    if not is_legacy_source_candidate(posting, authorization):
        return ()
    desired: list[tuple[str, object]] = [
        ("source_id", authorization.source_id),
        ("source_name", authorization.source_name),
        ("source_type", authorization.source_type),
        ("source_domain", authorization.source_domain),
    ]
    if getattr(posting, "source_record_id", None) in (None, ""):
        desired.append(("source_record_id", getattr(posting, "record_id", None)))
    collected_at = _datetime_value(getattr(posting, "collected_at", None))
    if collected_at is not None:
        if getattr(posting, "first_seen_at", None) is None:
            desired.append(("first_seen_at", collected_at))
        if getattr(posting, "last_seen_at", None) is None:
            desired.append(("last_seen_at", collected_at))
    desired.extend(
        [
            ("parser_name", authorization.parser_name),
            ("parser_version", authorization.parser_version),
            ("collection_method", authorization.collection_method),
            ("provenance_status", "approved"),
        ]
    )
    return tuple(
        RepairChange(
            field_name=field_name,
            before=getattr(posting, field_name, None),
            after=after,
            reason_code="authorized_legacy_zhaopin_source",
        )
        for field_name, after in desired
        if after not in (None, "") and getattr(posting, field_name, None) != after
    )


def _json_value(value: object) -> object:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _redact_pii(value: object) -> object:
    if isinstance(value, str):
        value = _EMAIL_PATTERN.sub("[redacted-email]", value)
        return _PHONE_PATTERN.sub("[redacted-phone]", value)
    if isinstance(value, list):
        return [_redact_pii(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_pii(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_pii(item) for key, item in value.items()}
    return value


def _payload_mapping(posting: object) -> Mapping[str, object]:
    value = getattr(posting, "raw_payload", None)
    if not isinstance(value, str):
        return value if isinstance(value, Mapping) else {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _datetime_value(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _publication_supported(posting: object) -> bool:
    published_at = _datetime_value(getattr(posting, "published_at", None))
    if published_at is None:
        return False
    payload = _payload_mapping(posting)
    structured = _datetime_value(payload.get("published_at"))
    if structured == published_at:
        return True
    adapter_extra = payload.get("adapter_extra")
    if isinstance(adapter_extra, Mapping):
        structured_fields = adapter_extra.get("structured_fields")
        if isinstance(structured_fields, Mapping):
            return _datetime_value(structured_fields.get("published_at")) == published_at
    return False


def _suspicious_publication(posting: object) -> bool:
    published_at = _datetime_value(getattr(posting, "published_at", None))
    collected_at = _datetime_value(getattr(posting, "collected_at", None))
    if published_at is None:
        return False
    if collected_at is not None:
        if published_at > collected_at + timedelta(days=1):
            return True
        if abs((collected_at - published_at).days) > 3653:
            return True
    return published_at > datetime.now() + timedelta(days=1)


def _industry_reason(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip() or value.strip() == "unknown":
        return None
    cleaned = value.strip()
    if _REQUIREMENT_PATTERN.search(cleaned):
        return "requirement_contaminated_industry"
    if _SALARY_PATTERN.search(cleaned):
        return "salary_contaminated_industry"
    return None


def plan_repairs(
    posting: object,
    *,
    authorization: LegacySourceAuthorization | None = None,
) -> tuple[RepairChange, ...]:
    """Return deterministic proposed changes without mutating ``posting``."""

    changes: list[RepairChange] = []
    published_at = getattr(posting, "published_at", None)
    if (
        published_at is not None
        and _suspicious_publication(posting)
        and not _publication_supported(posting)
    ):
        changes.append(
            RepairChange(
                field_name="published_at",
                before=published_at,
                after=None,
                reason_code="unsupported_suspicious_publication",
            )
        )
        if bool(getattr(posting, "published_at_trusted", False)):
            changes.append(
                RepairChange(
                    field_name="published_at_trusted",
                    before=True,
                    after=False,
                    reason_code="unsupported_suspicious_publication",
                )
            )

    industry = getattr(posting, "industry", None)
    reason = _industry_reason(industry)
    if reason is not None:
        changes.append(
            RepairChange(
                field_name="industry",
                before=industry,
                after="unknown",
                reason_code=reason,
            )
        )
    if authorization is not None:
        changes.extend(_authorization_repairs(posting, authorization))
    return tuple(changes)


def repair_report_path(repairs_root: str | Path, repair_run_id: str) -> Path:
    device = repair_run_id.split(".", 1)[0].upper()
    if (
        not _RUN_ID_PATTERN.fullmatch(repair_run_id)
        or repair_run_id in {".", ".."}
        or repair_run_id.endswith((".", " "))
        or device in _WINDOWS_RESERVED_NAMES
    ):
        raise RepairStorageError("invalid repair run id")
    root = Path(os.path.abspath(repairs_root))
    run_root = Path(os.path.abspath(root / repair_run_id))
    if run_root.parent != root:
        raise RepairStorageError("repair run path escapes repair root")
    report = Path(os.path.abspath(run_root / "report.json"))
    if report.parent != run_root:
        raise RepairStorageError("repair report path escapes repair run")
    return report


def write_repair_report(
    repairs_root: str | Path, repair_run_id: str, report: Mapping[str, object]
) -> Path:
    path = repair_report_path(repairs_root, repair_run_id)
    trusted_root = provision_secure_directory(repairs_root)
    payload = json.dumps(
        _redact_pii(dict(report)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    secure_atomic_write(path, payload, root=trusted_root)
    return path


def read_repair_report(
    repairs_root: str | Path, repair_run_id: str
) -> dict[str, object] | None:
    path = repair_report_path(repairs_root, repair_run_id)
    if not path.exists():
        return None
    raw = secure_read_file(
        path,
        root=Path(repairs_root),
        max_bytes=MAX_REPAIR_REPORT_BYTES,
    )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RepairStorageError("repair report is invalid JSON") from exc
    if not isinstance(value, dict) or value.get("repair_run_id") != repair_run_id:
        raise RepairStorageError("repair report identity is invalid")
    if value.get("rule_version") != REPAIR_RULE_VERSION:
        raise RepairStorageError("repair report rule version is invalid")
    return value


def _master_rank(posting: JobPosting, overrides: Mapping[str, object]) -> tuple:
    published = overrides.get("published_at", posting.published_at)
    complete = sum(
        bool(overrides.get(field, getattr(posting, field)))
        for field in (
            "company_name",
            "industry",
            "region",
            "published_at",
            "experience_requirement",
            "education_requirement",
            "salary_range",
            "source_url",
        )
    )
    timestamp = published.timestamp() if isinstance(published, datetime) else 0.0
    return (float(posting.source_score or 0.0), complete, timestamp, -posting.id)


def _predicted_duplicates(
    postings: Sequence[JobPosting], planned: Mapping[int, Mapping[str, object]]
) -> tuple[list[dict[str, object]], dict[int, int | None]]:
    families: dict[str, list[JobPosting]] = defaultdict(list)
    for posting in postings:
        families[posting.job_family_id].append(posting)
    groups: list[dict[str, object]] = []
    duplicate_of = {posting.id: None for posting in postings}
    for family in sorted(families):
        members = families[family]
        parent = list(range(len(members)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(first: int, second: int) -> None:
            first_root = find(first)
            second_root = find(second)
            if first_root != second_root:
                parent[second_root] = first_root

        fingerprints = {
            posting.id: (
                content_hash(posting.job_description_raw),
                simhash64(posting.job_description_raw),
            )
            for posting in members
        }
        for first_index, first in enumerate(members):
            first_hash, first_simhash = fingerprints[first.id]
            for second_index in range(first_index + 1, len(members)):
                second = members[second_index]
                second_hash, second_simhash = fingerprints[second.id]
                if first_hash == second_hash or hamming_distance(
                    first_simhash, second_simhash
                ) <= 8:
                    union(first_index, second_index)
        clusters: dict[int, list[JobPosting]] = defaultdict(list)
        for index, posting in enumerate(members):
            clusters[find(index)].append(posting)
        for cluster in clusters.values():
            if len(cluster) < 2:
                continue
            master = max(
                cluster,
                key=lambda item: _master_rank(item, planned.get(item.id, {})),
            )
            posting_ids = sorted(item.id for item in cluster)
            groups.append(
                {
                    "master_posting_id": master.id,
                    "posting_ids": posting_ids,
                }
            )
            for posting in cluster:
                if posting.id != master.id:
                    duplicate_of[posting.id] = master.id
    groups.sort(key=lambda item: tuple(item["posting_ids"]))
    return groups, duplicate_of


def _change_payload(posting_id: int, change: RepairChange) -> dict[str, object]:
    return {
        "posting_id": posting_id,
        "field_name": change.field_name,
        "before": _redact_pii(_json_value(change.before)),
        "after": _redact_pii(_json_value(change.after)),
        "reason_code": change.reason_code,
    }


def _authorization_payload(
    authorization: LegacySourceAuthorization,
) -> dict[str, str]:
    return {
        "source_id": authorization.source_id,
        "source_name": authorization.source_name,
        "source_type": authorization.source_type,
        "source_domain": authorization.source_domain,
        "collection_method": authorization.collection_method,
        "parser_name": authorization.parser_name,
        "parser_version": authorization.parser_version,
        "domain_scope": authorization.domain_scope,
        "authorization_note": authorization.authorization_note,
    }


async def audit_job_data(
    db: AsyncSession,
    *,
    repair_run_id: str,
    authorization: LegacySourceAuthorization | None = None,
) -> dict[str, object]:
    repair_report_path(DEFAULT_REPAIRS_ROOT, repair_run_id)
    postings = list(
        (await db.execute(select(JobPosting).order_by(JobPosting.id))).scalars()
    )
    changes: list[dict[str, object]] = []
    planned: dict[int, dict[str, object]] = defaultdict(dict)
    fingerprint_changes: list[dict[str, object]] = []
    for posting in postings:
        for change in plan_repairs(posting, authorization=authorization):
            changes.append(_change_payload(posting.id, change))
            planned[posting.id][change.field_name] = change.after
        expected_hash = content_hash(posting.job_description_raw)
        expected_simhash = simhash64(posting.job_description_raw)
        for field_name, before, after in (
            ("content_hash", posting.content_hash, expected_hash),
            ("simhash", posting.simhash, expected_simhash),
        ):
            if before != after:
                fingerprint_changes.append(
                    _change_payload(
                        posting.id,
                        RepairChange(
                            field_name, before, after, "recomputed_description_fingerprint"
                        ),
                    )
                )
    groups, predicted = _predicted_duplicates(postings, planned)
    duplicate_changes = [
        _change_payload(
            posting.id,
            RepairChange(
                "duplicate_of_id",
                posting.duplicate_of_id,
                predicted[posting.id],
                "recomputed_duplicate_group",
            ),
        )
        for posting in postings
        if posting.duplicate_of_id != predicted[posting.id]
    ]
    report: dict[str, object] = {
        "repair_run_id": repair_run_id,
        "rule_version": REPAIR_RULE_VERSION,
        "mode": "dry-run",
        "status": "planned",
        "row_count_before": len(postings),
        "row_count_after": len(postings),
        "changes": changes,
        "fingerprint_changes": fingerprint_changes,
        "duplicate_changes": duplicate_changes,
        "duplicate_groups": groups,
        "duplicate_summary": {
            "groups": len(groups),
            "duplicates": sum(len(item["posting_ids"]) - 1 for item in groups),
        },
    }
    if authorization is not None:
        report["authorization"] = _authorization_payload(authorization)
    return report


def _audit_row(
    *, repair_run_id: str, posting_id: int, change: RepairChange
) -> DataRepairAudit:
    return DataRepairAudit(
        repair_run_id=repair_run_id,
        job_posting_id=posting_id,
        field_name=change.field_name,
        before_json=json.dumps(_json_value(change.before), ensure_ascii=False),
        after_json=json.dumps(_json_value(change.after), ensure_ascii=False),
        reason_code=change.reason_code,
        rule_version=REPAIR_RULE_VERSION,
        applied=True,
    )


def _audit_payload(audit: DataRepairAudit) -> dict[str, object]:
    return {
        "posting_id": audit.job_posting_id,
        "field_name": audit.field_name,
        "before": _redact_pii(json.loads(audit.before_json)),
        "after": _redact_pii(json.loads(audit.after_json)),
        "reason_code": audit.reason_code,
    }


async def apply_job_data_repairs(
    db: AsyncSession,
    *,
    repair_run_id: str,
    authorization: LegacySourceAuthorization | None = None,
) -> dict[str, object]:
    existing = int(
        await db.scalar(
            select(func.count())
            .select_from(DataRepairAudit)
            .where(DataRepairAudit.repair_run_id == repair_run_id)
        )
        or 0
    )
    if existing:
        raise ValueError(f"repair run already has audit rows: {repair_run_id}")

    report = await audit_job_data(
        db,
        repair_run_id=repair_run_id,
        authorization=authorization,
    )
    postings = list(
        (await db.execute(select(JobPosting).order_by(JobPosting.id))).scalars()
    )
    audits: list[DataRepairAudit] = []
    duplicate_before = {posting.id: posting.duplicate_of_id for posting in postings}
    status_before = {posting.id: posting.status for posting in postings}
    for posting in postings:
        for change in plan_repairs(posting, authorization=authorization):
            setattr(posting, change.field_name, change.after)
            audits.append(
                _audit_row(
                    repair_run_id=repair_run_id,
                    posting_id=posting.id,
                    change=change,
                )
            )
        for field_name, after in (
            ("content_hash", content_hash(posting.job_description_raw)),
            ("simhash", simhash64(posting.job_description_raw)),
        ):
            before = getattr(posting, field_name)
            if before == after:
                continue
            setattr(posting, field_name, after)
            audits.append(
                _audit_row(
                    repair_run_id=repair_run_id,
                    posting_id=posting.id,
                    change=RepairChange(
                        field_name,
                        before,
                        after,
                        "recomputed_description_fingerprint",
                    ),
                )
            )
    await db.flush()
    duplicate_summary = await rebuild_duplicate_groups(db)
    for posting in postings:
        if duplicate_before[posting.id] != posting.duplicate_of_id:
            audits.append(
                _audit_row(
                    repair_run_id=repair_run_id,
                    posting_id=posting.id,
                    change=RepairChange(
                        "duplicate_of_id",
                        duplicate_before[posting.id],
                        posting.duplicate_of_id,
                        "recomputed_duplicate_group",
                    ),
                )
            )
        repaired_status = "duplicate" if posting.duplicate_of_id is not None else (
            "valid" if status_before[posting.id] == "duplicate" else status_before[posting.id]
        )
        if repaired_status != posting.status:
            before = posting.status
            posting.status = repaired_status
            audits.append(
                _audit_row(
                    repair_run_id=repair_run_id,
                    posting_id=posting.id,
                    change=RepairChange(
                        "status", before, repaired_status, "recomputed_duplicate_group"
                    ),
                )
            )
    db.add_all(audits)
    await db.flush()
    row_count_after = int(
        await db.scalar(select(func.count()).select_from(JobPosting)) or 0
    )
    if row_count_after != report["row_count_before"]:
        raise RuntimeError("job posting row count changed during repair")
    report.update(
        {
            "mode": "apply",
            "status": "applied",
            "applied_change_count": len(audits),
            "applied_changes": [_audit_payload(audit) for audit in audits],
            "row_count_after": row_count_after,
            "duplicate_summary": duplicate_summary,
        }
    )
    return report


__all__ = [
    "DEFAULT_REPAIRS_ROOT",
    "LegacySourceAuthorization",
    "REPAIR_RULE_VERSION",
    "RepairChange",
    "RepairStorageError",
    "apply_job_data_repairs",
    "audit_job_data",
    "is_legacy_source_candidate",
    "plan_repairs",
    "read_repair_report",
    "repair_report_path",
    "write_repair_report",
]
