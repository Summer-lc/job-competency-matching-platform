from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

import httpx
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from model_class.job_competency import JobPosting
from model_class.knowledge_base import CollectionRun
from src.competition_rules import assess_gate
from src.import_service import (
    _verified_collection_import_capability,
    import_job_file,
)
from src.job_collection.adapters import (
    AdapterStructureError,
    AuthorizedExportAdapter,
    BeisenATSAdapter,
    FeishuATSAdapter,
    LegacyFileAdapter,
    MOHRSSAdapter,
    NCSSAdapter,
)
from src.job_collection.adapters.manual_manifest import ManualManifestAdapter
from src.job_collection.authorization import load_authorized_source_grants
from src.job_collection.family_classifier import (
    FamilyDefinition,
    classify_job_family,
    load_family_config,
    schedule_family_deficits,
)
from src.job_collection.http_client import BoundedHttpClient, RequestBudget, SourceStopped
from src.job_collection.models import SourceDefinition, UnifiedJobRecord
from src.job_collection.normalizer import NormalizationError, normalize_job_record
from src.job_collection.source_registry import CollectionBlocked, SourceRegistry
from src.job_collection.security import (
    ExclusiveRunLock,
    default_control_state_root,
    ensure_secure_directory,
    load_or_create_attestation_key,
    provision_secure_directory,
    secure_atomic_write,
    secure_read_file,
)
from src.job_collection.storage import RunStorage
from src.job_data_service import assess_job_quality, prepare_job_record
from src.schema_migration import (
    DatabaseOperationalError,
    backup_sqlite_database,
    sqlite_database_path,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_COLLECTIONS_ROOT = PROJECT_ROOT / "data" / "collections"
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "config" / "job_sources.json"
DEFAULT_AUTHORIZATION_MANIFEST_PATH = (
    PROJECT_ROOT / "config" / "authorized_job_sources.local.json"
)
MAX_RUN_RECORDS = 10_000
MAX_RUN_PAGES = 100


def _control_state_paths(
    collections_root: str | Path, control_root: str | Path | None
) -> tuple[Path, Path, Path, Path]:
    base = (
        Path(os.path.abspath(control_root))
        if control_root is not None
        else default_control_state_root(collections_root)
    )
    return base, base / "attestations", base / "locks", base / "keys"
MAX_RUN_REQUESTS = 10_000
DEFAULT_RUN_REQUESTS = 100
MAX_STAGED_BYTES = 100 * 1024 * 1024
MAX_JSONL_LINE_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 50
REPORT_SCHEMA_VERSION = 1
ATTESTATION_SCHEMA_VERSION = 1
MIN_ATTESTATION_KEY_BYTES = 32


class CollectionReportError(ValueError):
    """A staging report or one of its declared artifacts is invalid."""


class AttestationError(CollectionReportError):
    """External authenticated staging metadata is absent or invalid."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("collection clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat()


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _jsonl_bytes(records: Sequence[Mapping[str, object]]) -> bytes:
    ordered = sorted(records, key=lambda item: str(item.get("record_id") or ""))
    if not ordered:
        return b""
    return b"\n".join(_json_bytes(dict(record)) for record in ordered) + b"\n"


def _atomic_write(path: Path, payload: bytes, run_root: Path) -> None:
    secure_atomic_write(path, payload, root=run_root)


def _read_json(path: Path, *, max_bytes: int = MAX_JSONL_LINE_BYTES) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size > max_bytes:
        raise CollectionReportError(f"missing or oversized JSON artifact: {path.name}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CollectionReportError(f"invalid JSON artifact {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise CollectionReportError(f"JSON artifact must be an object: {path.name}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise CollectionReportError(f"missing JSONL artifact: {path.name}")
    size = path.stat().st_size
    if size > MAX_STAGED_BYTES:
        raise CollectionReportError(f"JSONL artifact exceeds {MAX_STAGED_BYTES} bytes")
    records: list[dict[str, Any]] = []
    try:
        with path.open("rb") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                if len(raw_line) > MAX_JSONL_LINE_BYTES:
                    raise CollectionReportError(
                        f"JSONL line {line_number} exceeds the byte limit"
                    )
                if not raw_line.strip():
                    continue
                if len(records) >= MAX_RUN_RECORDS:
                    raise CollectionReportError("JSONL record count exceeds the run limit")
                value = json.loads(raw_line.decode("utf-8"))
                if not isinstance(value, dict):
                    raise CollectionReportError(
                        f"JSONL line {line_number} must be an object"
                    )
                records.append(value)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        if isinstance(exc, CollectionReportError):
            raise
        raise CollectionReportError(f"invalid JSONL artifact {path.name}: {exc}") from exc
    return records


def _parse_jsonl_bytes(payload: bytes, *, label: str) -> list[dict[str, Any]]:
    if len(payload) > MAX_STAGED_BYTES:
        raise CollectionReportError(f"{label} exceeds the byte limit")
    records: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(payload.splitlines(), start=1):
        if not raw_line.strip():
            continue
        if len(raw_line) > MAX_JSONL_LINE_BYTES:
            raise CollectionReportError(
                f"{label} line {line_number} exceeds the byte limit"
            )
        if len(records) >= MAX_RUN_RECORDS:
            raise CollectionReportError(f"{label} record count exceeds the run limit")
        try:
            value = json.loads(raw_line.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise CollectionReportError(
                f"invalid {label} JSON at line {line_number}"
            ) from exc
        if not isinstance(value, dict):
            raise CollectionReportError(f"{label} line {line_number} must be an object")
        _validate_json_depth(value, label=f"{label} line {line_number}")
        records.append(value)
    return records


def _validate_json_depth(value: object, *, label: str) -> None:
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            raise CollectionReportError(f"{label} exceeds the JSON nesting limit")
        if isinstance(current, Mapping):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _attestation_key(
    value: bytes | str | None = None, *, key_root: str | Path | None = None
) -> bytes:
    configured = value if value is not None else os.getenv("JOB_COLLECTION_ATTESTATION_KEY")
    if configured is None:
        if key_root is None:
            raise AttestationError("attestation key storage is not configured")
        return load_or_create_attestation_key(root=key_root)
    if isinstance(configured, str):
        configured = configured.encode("utf-8")
    if not isinstance(configured, bytes) or len(configured) < MIN_ATTESTATION_KEY_BYTES:
        raise AttestationError(
            "a protected attestation key of at least 32 bytes is required"
        )
    return configured


def _attestation_payload(
    run_id: str,
    report_bytes: bytes,
    artifacts: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    return {
        "schema_version": ATTESTATION_SCHEMA_VERSION,
        "run_id": run_id,
        "report_sha256": _sha256(report_bytes),
        "artifacts": {
            name: {
                "sha256": descriptor["sha256"],
                "bytes": descriptor["bytes"],
            }
            for name, descriptor in sorted(artifacts.items())
        },
    }


def _write_attestation(
    *,
    path: Path,
    root: Path,
    key: bytes,
    run_id: str,
    report_bytes: bytes,
    artifacts: Mapping[str, Mapping[str, object]],
) -> None:
    payload = _attestation_payload(run_id, report_bytes, artifacts)
    signature = hmac.new(key, _json_bytes(payload), hashlib.sha256).hexdigest()
    _atomic_write(path, _json_bytes({**payload, "hmac_sha256": signature}), root)


def _verify_attestation(
    *,
    attestation_bytes: bytes,
    key: bytes,
    run_id: str,
    report_bytes: bytes,
    artifacts: Mapping[str, Mapping[str, object]],
) -> None:
    try:
        document = json.loads(attestation_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise AttestationError("collection attestation is invalid") from exc
    if not isinstance(document, dict):
        raise AttestationError("collection attestation is invalid")
    signature = document.pop("hmac_sha256", None)
    expected_payload = _attestation_payload(run_id, report_bytes, artifacts)
    expected_signature = hmac.new(
        key, _json_bytes(expected_payload), hashlib.sha256
    ).hexdigest()
    if (
        document != expected_payload
        or not isinstance(signature, str)
        or not hmac.compare_digest(signature, expected_signature)
    ):
        raise AttestationError("collection attestation validation failed")


def _artifact_descriptor(path: Path, run_root: Path, record_count: int) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "path": path.relative_to(run_root).as_posix(),
        "sha256": _sha256(payload),
        "bytes": len(payload),
        "records": record_count,
    }


class _DefaultFetcher:
    def __init__(
        self,
        source: SourceDefinition,
        storage: RunStorage,
        registry: SourceRegistry,
        request_budget: RequestBudget | None = None,
    ) -> None:
        self._client = httpx.AsyncClient()
        self._bounded = BoundedHttpClient(
            source=source,
            registry=registry,
            storage=storage,
            client=self._client,
            request_budget=request_budget,
        )

    async def fetch(self, *args: object, **kwargs: object):
        return await self._bounded.fetch(*args, **kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()


def _default_adapter(source: SourceDefinition, registry: SourceRegistry):
    factories = {
        "ncss": NCSSAdapter,
        "mohrss": MOHRSSAdapter,
        "feishu_company_ats": FeishuATSAdapter,
        "beisen_company_ats": BeisenATSAdapter,
    }
    try:
        factory = factories[source.parser_name]
    except KeyError as exc:
        raise CollectionBlocked(
            f"no reviewed automatic adapter for {source.source_id}"
        ) from exc
    return factory(source=source, registry=registry)


def _default_fetcher(
    source: SourceDefinition,
    storage: RunStorage,
    registry: SourceRegistry,
    request_budget: RequestBudget | None = None,
) -> _DefaultFetcher:
    return _DefaultFetcher(source, storage, registry, request_budget)


class _BudgetedFetcher:
    def __init__(self, delegate: object, budget: RequestBudget) -> None:
        self._delegate = delegate
        self._budget = budget

    async def fetch(self, *args: object, **kwargs: object):
        await self._budget.consume()
        return await self._delegate.fetch(*args, **kwargs)

    async def aclose(self) -> None:
        close = getattr(self._delegate, "aclose", None)
        if close is not None:
            await close()


def _request_url(url: str, params: Mapping[str, object]) -> str:
    if not params:
        return url
    return f"{url}?{urlencode(sorted(params.items()), doseq=False)}"


def _list_cursor_key(
    source_id: str, family_code: str, query: str | Mapping[str, object]
) -> str:
    canonical_query = json.dumps(
        query, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return f"{source_id}|{family_code}|{canonical_query}"


def _persist_fetch_evidence(
    storage: RunStorage, source: SourceDefinition, result: object
) -> None:
    url = str(getattr(result, "url"))
    if storage.load_snapshot(source.source_id, url) is not None:
        return
    status = int(getattr(result, "status_code"))
    write = storage.write_success if 200 <= status < 300 else storage.write_error_metadata
    write(
        source=source,
        url=url,
        final_url=str(getattr(result, "final_url")),
        status=status,
        content_type=str(getattr(result, "content_type")),
        content=bytes(getattr(result, "content")),
    )


def _snapshot_manual_evidence(
    storage: RunStorage,
    source: SourceDefinition,
    manifest_path: str | Path,
    adapter: ManualManifestAdapter,
    records: Sequence[UnifiedJobRecord],
) -> Path:
    manifest = Path(manifest_path).absolute()
    last_line = max(
        (
            int(record.adapter_extra["manifest_line_number"])
            for record in records
        ),
        default=0,
    )
    chunks: list[bytes] = []
    with adapter._open_verified_file(
        manifest, trusted_root=manifest.parent, label="manifest"
    ) as stream:
        for _ in range(last_line):
            raw_line = stream.readline(MAX_JSONL_LINE_BYTES + 1)
            if not raw_line:
                break
            if len(raw_line) > MAX_JSONL_LINE_BYTES:
                raise CollectionReportError("manual manifest line exceeds byte limit")
            chunks.append(raw_line)
    payload = b"".join(chunks)
    if len(payload) > MAX_STAGED_BYTES:
        raise CollectionReportError("manual manifest exceeds byte limit")
    snapshot_root = storage.resolve_path("raw", source.source_id)
    snapshot_manifest = snapshot_root / manifest.name
    _atomic_write(snapshot_manifest, payload, storage.run_root)
    for record in records:
        relative = record.adapter_extra.get("exported_html_path")
        if not isinstance(relative, str):
            continue
        source_path = manifest.parent / relative
        html = secure_read_file(
            source_path, root=manifest.parent, max_bytes=MAX_STAGED_BYTES
        )
        destination = storage.resolve_path("raw", source.source_id, relative)
        _atomic_write(destination, html, storage.run_root)
    return snapshot_manifest


def _snapshot_legacy_file_evidence(
    storage: RunStorage,
    source: SourceDefinition,
    input_path: str | Path,
    adapter: LegacyFileAdapter,
) -> Path:
    path = Path(input_path).absolute()
    payload = adapter._read_file(path)
    destination = storage.resolve_path("raw", source.source_id, path.name)
    _atomic_write(destination, payload, storage.run_root)
    return destination


def _is_authorized_export(source: SourceDefinition) -> bool:
    return (
        source.collection_mode == "file_import"
        and source.parser_name.endswith("_authorized_export")
    )


def _combined_quality(
    record: UnifiedJobRecord,
    *,
    classification_status: str,
    classification: Mapping[str, object],
    now: datetime,
) -> UnifiedJobRecord:
    serialized = record.model_dump(mode="json")
    prepared = prepare_job_record(serialized)
    findings = [asdict(item) for item in assess_job_quality(prepared)]
    has_capability_evidence = bool(
        prepared.get("skills") or prepared.get("responsibilities")
    )
    gate_payload = dict(serialized)
    gate_payload.update(
        quality_score=prepared["quality_score"],
        has_capability_evidence=has_capability_evidence,
    )
    naive_now = now.astimezone(timezone.utc).replace(tzinfo=None)
    hard_gate = assess_gate(gate_payload, now=naive_now)
    statuses = [str(serialized.get("normalization_status") or "valid"), hard_gate.status]
    if classification_status != "auto":
        statuses.append("review")
    statuses.extend(str(item.get("severity") or "review") for item in findings)
    rank = {
        "valid": 0,
        "duplicate": 0,
        "review": 1,
        "quarantine": 2,
        "quarantined": 2,
    }
    highest = max((rank.get(status, 1) for status in statuses), default=0)
    combined = ("valid", "review", "quarantined")[highest]
    issue_codes = {
        str(item.get("code"))
        for item in serialized.get("normalization_findings", [])
        if isinstance(item, Mapping) and item.get("code")
    }
    issue_codes.update(str(item["code"]) for item in findings)
    issue_codes.update(hard_gate.issue_codes)
    if classification_status != "auto":
        issue_codes.add("family_classification_review")
    adapter_extra = {
        **record.adapter_extra,
        "family_classification": dict(classification),
        "quality_findings": findings,
        "quality_gate": {
            "status": combined,
            "issue_codes": sorted(issue_codes),
            "quality_score": prepared["quality_score"],
            "has_capability_evidence": has_capability_evidence,
            "component_statuses": {
                "normalizer": serialized.get("normalization_status", "valid"),
                "classifier": classification_status,
                "quality": "review" if findings else "valid",
                "hard_gate": hard_gate.status,
            },
        },
    }
    return record.model_copy(update={"adapter_extra": adapter_extra})


def _gate_status(record: Mapping[str, object]) -> str:
    extra = record.get("adapter_extra")
    if not isinstance(extra, Mapping):
        return "quarantined"
    gate = extra.get("quality_gate")
    if not isinstance(gate, Mapping):
        return "quarantined"
    return str(gate.get("status") or "quarantined")


def _destination(status: str) -> str:
    if status in {"valid", "duplicate"}:
        return "staged"
    if status == "review":
        return "review"
    return "quarantine"


def _record_counts(
    records: Sequence[Mapping[str, object]],
) -> dict[str, dict[str, int]]:
    return {
        "source": dict(
            sorted(Counter(str(item["source_id"]) for item in records).items())
        ),
        "domain": dict(
            sorted(Counter(str(item["source_domain"]) for item in records).items())
        ),
        "family": dict(
            sorted(Counter(str(item["job_family_id"]) for item in records).items())
        ),
        "gate": dict(sorted(Counter(_gate_status(item) for item in records).items())),
        "date_trust": dict(
            sorted(
                Counter(
                    "trusted" if item.get("published_at_trusted") else "untrusted"
                    for item in records
                ).items()
            )
        ),
    }


async def _valid_unique_counts(db: AsyncSession | None) -> dict[str, int]:
    if db is None:
        return {}
    rows = await db.execute(
        select(JobPosting.job_family_id, func.count(distinct(JobPosting.record_id)))
        .where(
            JobPosting.status == "valid",
            JobPosting.gate_status == "valid",
            JobPosting.duplicate_of_id.is_(None),
        )
        .group_by(JobPosting.job_family_id)
    )
    return {str(code): int(count) for code, count in rows}


class CollectionService:
    def __init__(
        self,
        *,
        registry: SourceRegistry | None = None,
        registry_path: str | Path = DEFAULT_REGISTRY_PATH,
        collections_root: str | Path = DEFAULT_COLLECTIONS_ROOT,
        family_config: Mapping[str, FamilyDefinition] | None = None,
        clock: Callable[[], datetime] = _utc_now,
        adapter_factory: Callable[[SourceDefinition, SourceRegistry], object] = _default_adapter,
        fetcher_factory: Callable[[SourceDefinition, RunStorage, SourceRegistry], object] = _default_fetcher,
        attestation_key: bytes | str | None = None,
        control_root: str | Path | None = None,
        attestations_root: str | Path | None = None,
        locks_root: str | Path | None = None,
        keys_root: str | Path | None = None,
    ) -> None:
        self.registry = registry or SourceRegistry.load(registry_path)
        self.collections_root = Path(collections_root).resolve()
        self.family_config = dict(family_config or load_family_config())
        self.clock = clock
        self.adapter_factory = adapter_factory
        self.fetcher_factory = fetcher_factory
        self.attestation_key = attestation_key
        (
            self.control_root,
            default_attestations,
            default_locks,
            default_keys,
        ) = _control_state_paths(self.collections_root, control_root)
        self.attestations_root = Path(
            os.path.abspath(attestations_root or default_attestations)
        )
        self.locks_root = Path(os.path.abspath(locks_root or default_locks))
        self.keys_root = Path(os.path.abspath(keys_root or default_keys))

    async def run_dry_run(
        self,
        **kwargs: Any,
    ) -> dict[str, Any]:
        actual_run_id = kwargs.get("run_id") or (
            f"collection-{self.clock():%Y%m%d-%H%M%S}-{uuid4().hex[:8]}"
        )
        kwargs["run_id"] = actual_run_id
        ensure_secure_directory(self.control_root)
        with ExclusiveRunLock(self.locks_root, actual_run_id, "collect"):
            return await self._run_dry_run_unlocked(**kwargs)

    async def _run_dry_run_unlocked(
        self,
        *,
        source_ids: Sequence[str],
        max_records: int,
        max_pages: int = 20,
        max_requests: int = DEFAULT_RUN_REQUESTS,
        run_id: str | None = None,
        db: AsyncSession | None = None,
        manifest_path: str | Path | None = None,
        input_file_path: str | Path | None = None,
        record_offset: int = 0,
        authorization_note: str | None = None,
        authorization_manifest_path: str | Path | None = None,
        _resume: bool = False,
    ) -> dict[str, Any]:
        if not source_ids or len(source_ids) > len(self.registry.definitions):
            raise ValueError("source_ids must select one or more registered sources")
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source_ids must not contain duplicates")
        if not 1 <= max_records <= MAX_RUN_RECORDS:
            raise ValueError(f"max_records must be between 1 and {MAX_RUN_RECORDS}")
        if (
            isinstance(record_offset, bool)
            or not isinstance(record_offset, int)
            or not 0 <= record_offset <= MAX_RUN_RECORDS
        ):
            raise ValueError(
                f"record_offset must be between 0 and {MAX_RUN_RECORDS}"
            )
        if not 1 <= max_pages <= MAX_RUN_PAGES:
            raise ValueError(f"max_pages must be between 1 and {MAX_RUN_PAGES}")
        if not 1 <= max_requests <= MAX_RUN_REQUESTS:
            raise ValueError(f"max_requests must be between 1 and {MAX_RUN_REQUESTS}")
        actual_run_id = run_id or f"collection-{self.clock():%Y%m%d-%H%M%S}-{uuid4().hex[:8]}"
        trusted_attestation_key = _attestation_key(
            self.attestation_key, key_root=self.keys_root
        )
        storage = RunStorage(self.collections_root, actual_run_id, clock=self.clock)
        report_path = storage.resolve_path("report.json")
        if report_path.exists():
            if not _resume:
                raise ValueError(f"collection run already exists: {actual_run_id}")
            return _validate_staging_report(storage)
        if storage.run_root.exists() and not _resume:
            raise ValueError(f"collection run directory already exists: {actual_run_id}")

        started_at = self.clock()
        authorized_grant = None
        authorization_manifest_sha256: str | None = None
        input_file_sha256: str | None = None
        selected: list[SourceDefinition] = []
        for source_id in source_ids:
            definition = self.registry.get(source_id)
            if not definition.enabled:
                raise CollectionBlocked(f"source is disabled: {source_id}")
            if definition.compliance_status == "manual_only":
                manual_manifest = bool(
                    manifest_path is not None
                    and input_file_path is None
                    and definition.collection_mode == "manual_url_manifest"
                    and definition.source_id == "company_official_manifest"
                )
                local_file = bool(
                    input_file_path is not None
                    and manifest_path is None
                    and authorization_note
                    and definition.collection_mode == "file_import"
                    and definition.source_id == "zhaopin_legacy_import"
                )
                authorized_file = bool(
                    input_file_path is not None
                    and manifest_path is None
                    and not authorization_note
                    and _is_authorized_export(definition)
                )
                if not (manual_manifest or local_file or authorized_file):
                    raise CollectionBlocked(
                        f"manual_only source requires its explicit reviewed local input: {source_id}"
                    )
            else:
                definition = self.registry.require_automatic(source_id)
            selected.append(definition)

        authorized_sources = [source for source in selected if _is_authorized_export(source)]
        if record_offset and not (
            len(selected) == 1
            and len(authorized_sources) == 1
            and input_file_path is not None
        ):
            raise ValueError(
                "record_offset is limited to one authorized export file source"
            )
        if authorized_sources:
            if len(selected) != 1 or len(authorized_sources) != 1:
                raise CollectionBlocked(
                    "an authorized export run must select exactly one file source"
                )
            grant_path = Path(
                authorization_manifest_path or DEFAULT_AUTHORIZATION_MANIFEST_PATH
            )
            grants = load_authorized_source_grants(
                grant_path,
                today=started_at.date(),
            )
            authorized_grant = grants.require(
                authorized_sources[0].source_id, "file_export"
            )
            authorization_manifest_sha256 = grants.manifest_sha256
            input_payload = AuthorizedExportAdapter._read_file(Path(input_file_path))
            input_file_sha256 = _sha256(input_payload)
        elif authorization_manifest_path is not None:
            raise CollectionBlocked(
                "authorization manifest is only valid for an authorized export source"
            )

        provision_secure_directory(storage.collections_root)
        ensure_secure_directory(storage.run_root)
        ensure_secure_directory(storage.resolve_path("raw"))
        for folder in ("staged", "review", "quarantine"):
            ensure_secure_directory(storage.resolve_path(folder))
        run_document = {
            "run_id": actual_run_id,
            "source_ids": list(source_ids),
            "max_records": max_records,
            "max_pages": max_pages,
            "max_requests": max_requests,
            "record_offset": record_offset,
            "manifest_path": str(Path(manifest_path).resolve()) if manifest_path else None,
        }
        if input_file_path is not None:
            run_document.update(
                input_file_path=str(Path(input_file_path).resolve()),
                authorization_note=authorization_note,
            )
        if authorized_grant is not None:
            run_document.update(
                authorization_reference=authorized_grant.authorization_reference,
                authorization_valid_until=authorized_grant.valid_until.isoformat(),
                authorization_access_method="file_export",
                authorization_scope_sha256=_sha256(
                    authorized_grant.scope.encode("utf-8")
                ),
                authorization_manifest_sha256=authorization_manifest_sha256,
                input_file_sha256=input_file_sha256,
            )
        checkpoint = storage.initialize_checkpoint(run_document)

        buckets = {"staged": [], "review": [], "quarantine": []}
        for name in buckets:
            artifact = storage.resolve_path(name, "jobs.jsonl")
            if _resume and artifact.exists():
                buckets[name].extend(_read_jsonl(artifact))
        seen_record_ids = {
            str(record.get("record_id"))
            for records in buckets.values()
            for record in records
        }
        seen_content_hashes = {
            str(record.get("content_hash"))
            for records in buckets.values()
            for record in records
            if record.get("content_hash")
        }
        batch_counts = Counter(
            str(record.get("job_family_id"))
            for record in buckets["staged"]
            if record.get("job_family_id")
        )
        schedule = schedule_family_deficits(
            await _valid_unique_counts(db), batch_counts, self.family_config
        )
        source_reports: dict[str, dict[str, object]] = {}
        run_attempts = max(
            sum(len(records) for records in buckets.values()), checkpoint.records_used
        )
        run_pages = checkpoint.pages_used
        request_budget = RequestBudget(max_requests, used=checkpoint.requests_used)

        for source in selected:
            source_report: dict[str, object] = {
                "status": "completed",
                "errors": [],
                "fetched": 0,
                "parsed": 0,
                "pages": 0,
                "duplicates": 0,
            }
            source_reports[source.source_id] = source_report
            source_cap = min(max_records, source.max_records)
            page_cap = min(max_pages, source.max_pages)
            if source.collection_mode == "file_import":
                adapter = (
                    AuthorizedExportAdapter(source=source, registry=self.registry)
                    if _is_authorized_export(source)
                    else LegacyFileAdapter(source=source, registry=self.registry)
                )
                file_cap = min(source_cap, max_records - run_attempts)
                if file_cap <= 0:
                    source_report["parsed"] = 0
                    continue
                if _is_authorized_export(source):
                    if authorized_grant is None:
                        raise CollectionBlocked("authorized export grant is unavailable")
                    records = adapter.load_file(
                        Path(input_file_path),
                        run_id=actual_run_id,
                        authorization_reference=authorized_grant.authorization_reference,
                        authorization_scope=authorized_grant.scope,
                        max_records=file_cap,
                        record_offset=record_offset,
                        collected_at=started_at,
                    )
                else:
                    records = adapter.load_file(
                        Path(input_file_path),
                        run_id=actual_run_id,
                        authorization_note=str(authorization_note),
                        max_records=file_cap,
                    )
                source_report["errors"] = list(adapter.errors)
                source_report["rejected"] = len(adapter.errors)
                source_report["fetched"] = len(records) + len(adapter.errors)
                _snapshot_legacy_file_evidence(
                    storage,
                    source,
                    Path(input_file_path),
                    adapter,
                )
                for record in records:
                    run_attempts += 1
                    self._stage_record(
                        record,
                        buckets,
                        seen_record_ids,
                        seen_content_hashes,
                        source_report,
                    )
                    storage.mark_usage(
                        requests=request_budget.used,
                        pages=run_pages,
                        records=run_attempts,
                    )
                source_report["parsed"] = len(records)
                continue

            if source.compliance_status == "manual_only":
                adapter = ManualManifestAdapter(source=source, registry=self.registry)
                manual_cap = min(source_cap, max_records - run_attempts)
                if manual_cap <= 0:
                    source_report["parsed"] = 0
                    continue
                records = adapter.load_manifest(
                    Path(manifest_path),
                    run_id=actual_run_id,
                    collected_at=started_at,
                    max_records=manual_cap,
                )
                _snapshot_manual_evidence(
                    storage,
                    source,
                    Path(manifest_path),
                    adapter,
                    records,
                )
                for record in records:
                    run_attempts += 1
                    self._stage_record(
                        record,
                        buckets,
                        seen_record_ids,
                        seen_content_hashes,
                        source_report,
                    )
                    storage.mark_usage(
                        requests=request_budget.used,
                        pages=run_pages,
                        records=run_attempts,
                    )
                source_report["parsed"] = len(records)
                continue

            adapter = self.adapter_factory(source, self.registry)
            if self.fetcher_factory is _default_fetcher:
                fetcher = _default_fetcher(
                    source, storage, self.registry, request_budget=request_budget
                )
            else:
                fetcher = _BudgetedFetcher(
                    self.fetcher_factory(source, storage, self.registry), request_budget
                )
            source_attempts = 0
            stop_source = False
            try:
                if source.source_id == "mohrss_public_jobs":
                    bootstrap_request = adapter.build_bootstrap_request()
                    bootstrap_result = await fetcher.fetch(
                        bootstrap_request.url,
                        resume=False,
                    )
                    _persist_fetch_evidence(storage, source, bootstrap_result)
                    source_report["fetched"] = int(source_report["fetched"]) + 1
                    adapter.validate_bootstrap(
                        bootstrap_result.content,
                        bootstrap_result.content_type,
                    )
                    storage.mark_usage(
                        requests=request_budget.used,
                        pages=run_pages,
                        records=run_attempts,
                    )
                for work in schedule:
                    if (
                        run_attempts >= max_records
                        or run_pages >= max_pages
                        or source_attempts >= source_cap
                        or stop_source
                    ):
                        break
                    definition = self.family_config[work.family_code]
                    family_remaining = min(work.requested, source_cap - source_attempts)
                    for query in definition.queries:
                        if (
                            family_remaining <= 0
                            or run_attempts >= max_records
                            or run_pages >= max_pages
                            or source_attempts >= source_cap
                            or int(source_report["pages"]) >= page_cap
                        ):
                            break
                        cursor_key = _list_cursor_key(
                            source.source_id, work.family_code, query
                        )
                        ncss_offset = (checkpoint.list_cursors or {}).get(
                            cursor_key, 0
                        )
                        for page_index in range(page_cap):
                            if (
                                family_remaining <= 0
                                or run_attempts >= max_records
                                or run_pages >= max_pages
                                or source_attempts >= source_cap
                                or int(source_report["pages"]) >= page_cap
                            ):
                                break
                            is_mohrss = source.source_id == "mohrss_public_jobs"
                            adapter_limit = (
                                int(getattr(adapter, "site_page_size", 20))
                                if is_mohrss
                                else min(20, source.max_records)
                            )
                            page_limit = min(
                                adapter_limit,
                                max_records - run_attempts,
                                source_cap - source_attempts,
                                family_remaining,
                            )
                            position = page_index + 1 if is_mohrss else ncss_offset
                            request = adapter.build_list_request(query, position, page_limit)
                            list_url = _request_url(request.url, request.params)
                            try:
                                list_result = await fetcher.fetch(
                                    list_url,
                                    method=request.method,
                                    headers=request.headers,
                                    json_body=request.json_body,
                                    resume=True,
                                )
                            finally:
                                storage.mark_usage(
                                    requests=request_budget.used,
                                    pages=run_pages,
                                    records=run_attempts,
                                )
                            _persist_fetch_evidence(storage, source, list_result)
                            source_report["fetched"] = int(source_report["fetched"]) + 1
                            source_report["pages"] = int(source_report["pages"]) + 1
                            run_pages += 1
                            storage.mark_usage(
                                requests=request_budget.used,
                                pages=run_pages,
                                records=run_attempts,
                            )
                            page = adapter.parse_list(
                                list_result.content,
                                list_result.content_type,
                                position,
                                page_limit,
                            )
                            bounded_items = page.items[:page_limit]
                            source_report["parsed"] = int(source_report["parsed"]) + len(
                                bounded_items
                            )
                            page_completed = True
                            for item in bounded_items:
                                if (
                                    family_remaining <= 0
                                    or run_attempts >= max_records
                                    or source_attempts >= source_cap
                                ):
                                    break
                                source_attempts += 1
                                run_attempts += 1
                                storage.mark_usage(
                                    requests=request_budget.used,
                                    pages=run_pages,
                                    records=run_attempts,
                                )
                                detail_url = adapter.build_detail_url(item)
                                redirect_validator = None
                                if hasattr(adapter, "validate_detail_redirect"):
                                    def validate_redirect(
                                        current: str,
                                        target: str,
                                        detail_item: object = item,
                                    ) -> None:
                                        adapter.validate_detail_redirect(
                                            detail_item, current, target
                                        )

                                    redirect_validator = validate_redirect
                                try:
                                    detail_embedded = bool(
                                        getattr(adapter, "embedded_detail", False)
                                    )
                                    if detail_embedded:
                                        detail_content = list_result.content
                                        detail_final_url = detail_url
                                        detail_hash = list_result.content_hash
                                        detail_status = list_result.status_code
                                    else:
                                        try:
                                            detail_result = await fetcher.fetch(
                                                detail_url,
                                                resume=True,
                                                redirect_validator=redirect_validator,
                                            )
                                        finally:
                                            storage.mark_usage(
                                                requests=request_budget.used,
                                                pages=run_pages,
                                                records=run_attempts,
                                            )
                                        _persist_fetch_evidence(storage, source, detail_result)
                                        source_report["fetched"] = (
                                            int(source_report["fetched"]) + 1
                                        )
                                        detail_content = detail_result.content
                                        detail_final_url = detail_result.final_url
                                        detail_hash = detail_result.content_hash
                                        detail_status = detail_result.status_code
                                    raw = adapter.parse_detail(
                                        detail_content, item, detail_final_url
                                    )
                                    raw["collection_evidence"] = {
                                        "list_url": list_result.url,
                                        "list_final_url": list_result.final_url,
                                        "list_offset": position,
                                        "list_limit": page_limit,
                                        "detail_request_url": detail_url,
                                        "detail_final_url": detail_final_url,
                                        "detail_embedded": detail_embedded,
                                        "source_record_id": item.source_record_id,
                                        "requested_family": work.family_code,
                                    }
                                    raw["job_family_id"] = work.family_code
                                    classification = classify_job_family(
                                        str(raw.get("job_title") or raw.get("job_title_raw") or ""),
                                        str(raw.get("job_description_raw") or ""),
                                        self.family_config,
                                    )
                                    if classification.family_code:
                                        raw["job_family_id"] = classification.family_code
                                    normalized = normalize_job_record(
                                        raw,
                                        source=source,
                                        run_id=actual_run_id,
                                        snapshot_metadata={
                                            "snapshot_hash": detail_hash,
                                            "observed_at": _timestamp(started_at),
                                            "response_status": detail_status,
                                            "page_title": raw.get("page_title"),
                                        },
                                        collected_at=started_at,
                                    )
                                    normalized = _combined_quality(
                                        normalized,
                                        classification_status=classification.status,
                                        classification=asdict(classification),
                                        now=started_at,
                                    )
                                    added = self._stage_record(
                                        normalized,
                                        buckets,
                                        seen_record_ids,
                                        seen_content_hashes,
                                        source_report,
                                    )
                                    storage.mark_detail_completed(detail_final_url)
                                    if added and _gate_status(
                                        normalized.model_dump(mode="json")
                                    ) == "valid":
                                        family_remaining -= 1
                                except SourceStopped as exc:
                                    source_report["status"] = "stopped"
                                    source_report["errors"] = [str(exc)]
                                    page_completed = False
                                    stop_source = True
                                    break
                                except AdapterStructureError as exc:
                                    source_report["status"] = "stopped"
                                    source_report["errors"] = [str(exc)]
                                    page_completed = False
                                    stop_source = True
                                    break
                                except (NormalizationError, ValueError) as exc:
                                    source_report["errors"] = [
                                        *source_report["errors"],
                                        f"record {item.source_record_id}: {exc}",
                                    ]
                            if page_completed:
                                if not is_mohrss:
                                    ncss_offset = page.offset + len(page.items)
                                    storage.mark_list_cursor(cursor_key, ncss_offset)
                                storage.mark_page_completed(page_index)
                            if stop_source or not page.has_more:
                                break
            except (SourceStopped, AdapterStructureError) as exc:
                source_report["status"] = "stopped"
                source_report["errors"] = [str(exc)]
            finally:
                close = getattr(fetcher, "aclose", None)
                if close is not None:
                    await close()

        for name, records in buckets.items():
            _atomic_write(
                storage.resolve_path(name, "jobs.jsonl"),
                _jsonl_bytes(records),
                storage.run_root,
            )
        report = self._build_report(
            storage=storage,
            selected=selected,
            source_reports=source_reports,
            buckets=buckets,
            started_at=started_at,
            request=run_document,
            pages_used=run_pages,
            requests_used=request_budget.used,
        )
        report_bytes = _json_bytes(report)
        _atomic_write(report_path, report_bytes, storage.run_root)
        _write_attestation(
            path=self.attestations_root / f"{actual_run_id}.json",
            root=self.attestations_root,
            key=trusted_attestation_key,
            run_id=actual_run_id,
            report_bytes=report_bytes,
            artifacts=report["artifacts"],
        )
        return report

    async def resume_dry_run(
        self,
        run_id: str,
        *,
        db: AsyncSession | None = None,
        authorization_manifest_path: str | Path | None = None,
    ) -> dict[str, Any]:
        with ExclusiveRunLock(self.locks_root, run_id, "collect"):
            storage = RunStorage(self.collections_root, run_id, clock=self.clock)
            if storage.resolve_path("report.json").exists():
                report_bytes = secure_read_file(
                    storage.resolve_path("report.json"),
                    root=storage.run_root,
                    max_bytes=MAX_JSONL_LINE_BYTES,
                )
                artifact_bytes = {
                    name: secure_read_file(
                        storage.resolve_path(name, "jobs.jsonl"),
                        root=storage.run_root,
                        max_bytes=MAX_STAGED_BYTES,
                    )
                    for name in ("staged", "review", "quarantine")
                }
                report = _validate_staging_report(
                    storage,
                    report_bytes=report_bytes,
                    artifact_bytes=artifact_bytes,
                )
                attestation_bytes = secure_read_file(
                    self.attestations_root / f"{run_id}.json",
                    root=self.attestations_root,
                    max_bytes=MAX_JSONL_LINE_BYTES,
                )
                _verify_attestation(
                    attestation_bytes=attestation_bytes,
                    key=_attestation_key(
                        self.attestation_key, key_root=self.keys_root
                    ),
                    run_id=run_id,
                    report_bytes=report_bytes,
                    artifacts=report["artifacts"],
                )
                return report
            checkpoint = storage.load_checkpoint()
            if checkpoint.resume is None:
                raise CollectionReportError("checkpoint is missing resume metadata")
            request = checkpoint.resume
            return await self._run_dry_run_unlocked(
                source_ids=request.get("source_ids", []),
                run_id=run_id,
                max_records=request.get("max_records"),
                max_pages=request.get("max_pages"),
                max_requests=request.get("max_requests", DEFAULT_RUN_REQUESTS),
                manifest_path=request.get("manifest_path"),
                input_file_path=request.get("input_file_path"),
                record_offset=request.get("record_offset", 0),
                authorization_note=request.get("authorization_note"),
                authorization_manifest_path=authorization_manifest_path,
                db=db,
                _resume=True,
            )

    @staticmethod
    def _stage_record(
        record: UnifiedJobRecord,
        buckets: dict[str, list[dict[str, Any]]],
        seen_record_ids: set[str],
        seen_content_hashes: set[str],
        source_report: dict[str, object],
    ) -> bool:
        serialized = record.model_dump(mode="json")
        record_id = str(serialized["record_id"])
        content_hash = str(serialized.get("content_hash") or "")
        if record_id in seen_record_ids or (
            content_hash and content_hash in seen_content_hashes
        ):
            source_report["duplicates"] = int(source_report["duplicates"]) + 1
            return False
        destination = _destination(_gate_status(serialized))
        buckets[destination].append(serialized)
        seen_record_ids.add(record_id)
        if content_hash:
            seen_content_hashes.add(content_hash)
        return True

    def _build_report(
        self,
        *,
        storage: RunStorage,
        selected: Sequence[SourceDefinition],
        source_reports: Mapping[str, Mapping[str, object]],
        buckets: Mapping[str, Sequence[Mapping[str, object]]],
        started_at: datetime,
        request: Mapping[str, object],
        pages_used: int,
        requests_used: int,
    ) -> dict[str, Any]:
        all_records = [record for values in buckets.values() for record in values]
        counts = _record_counts(all_records)
        artifacts = {}
        for name, records in buckets.items():
            artifacts[name] = _artifact_descriptor(
                storage.resolve_path(name, "jobs.jsonl"), storage.run_root, len(records)
            )
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "run_id": storage.run_id,
            "mode": "dry-run",
            "status": "completed",
            "staging_valid": True,
            "started_at": _timestamp(started_at),
            "completed_at": _timestamp(self.clock()),
            "request": dict(request),
            "source_definitions": [item.model_dump(mode="json") for item in selected],
            "sources": {key: dict(source_reports[key]) for key in sorted(source_reports)},
            "artifacts": artifacts,
            "counts": counts,
            "totals": {
                "fetched": sum(int(value["fetched"]) for value in source_reports.values()),
                "parsed": sum(int(value["parsed"]) for value in source_reports.values()),
                "valid": len(buckets["staged"]),
                "review": len(buckets["review"]),
                "quarantined": len(buckets["quarantine"]),
                "duplicates": sum(int(value["duplicates"]) for value in source_reports.values()),
                "pages": pages_used,
                "requests": requests_used,
            },
        }


def _parse_json_object(payload: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise CollectionReportError(f"invalid JSON artifact {label}") from exc
    if not isinstance(value, dict):
        raise CollectionReportError(f"JSON artifact must be an object: {label}")
    _validate_json_depth(value, label=label)
    return value


def _validate_staging_report(
    storage: RunStorage,
    *,
    report_bytes: bytes | None = None,
    artifact_bytes: Mapping[str, bytes] | None = None,
) -> dict[str, Any]:
    report = (
        _parse_json_object(report_bytes, label="report.json")
        if report_bytes is not None
        else _read_json(storage.resolve_path("report.json"))
    )
    if (
        report.get("schema_version") != REPORT_SCHEMA_VERSION
        or report.get("run_id") != storage.run_id
        or report.get("mode") != "dry-run"
        or report.get("status") != "completed"
        or report.get("staging_valid") is not True
    ):
        raise CollectionReportError("commit requires a completed valid staging report")
    artifacts = report.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise CollectionReportError("staging report artifacts are invalid")
    artifact_records: dict[str, list[dict[str, Any]]] = {}
    for name in ("staged", "review", "quarantine"):
        descriptor = artifacts.get(name)
        if not isinstance(descriptor, Mapping):
            raise CollectionReportError(f"staging report is missing {name} artifact")
        expected_path = f"{name}/jobs.jsonl"
        if descriptor.get("path") != expected_path:
            raise CollectionReportError(f"invalid report path for {name}")
        path = storage.resolve_path(expected_path)
        payload = (
            artifact_bytes[name]
            if artifact_bytes is not None and name in artifact_bytes
            else path.read_bytes() if path.is_file() else b""
        )
        if (
            descriptor.get("sha256") != _sha256(payload)
            or descriptor.get("bytes") != len(payload)
        ):
            raise CollectionReportError(f"{name} artifact hash validation failed")
        records = (
            _parse_jsonl_bytes(payload, label=expected_path)
            if artifact_bytes is not None
            else _read_jsonl(path)
        )
        artifact_records[name] = records
        if descriptor.get("records") != len(records):
            raise CollectionReportError(f"{name} artifact record count validation failed")
    definitions = report.get("source_definitions")
    if not isinstance(definitions, list) or not definitions:
        raise CollectionReportError("staging report source definitions are invalid")
    try:
        SourceRegistry(SourceDefinition.model_validate(item) for item in definitions)
    except Exception as exc:
        raise CollectionReportError("staging report source definitions are invalid") from exc
    all_records = [
        record for name in ("staged", "review", "quarantine") for record in artifact_records[name]
    ]
    if report.get("counts") != _record_counts(all_records):
        raise CollectionReportError("staging report count validation failed")
    totals = report.get("totals")
    expected_gate_totals = {
        "valid": len(artifact_records["staged"]),
        "review": len(artifact_records["review"]),
        "quarantined": len(artifact_records["quarantine"]),
    }
    if not isinstance(totals, Mapping) or any(
        totals.get(key) != value for key, value in expected_gate_totals.items()
    ):
        raise CollectionReportError("staging report total count validation failed")
    return report


def _validate_commit_records(
    report: Mapping[str, object], staged_bytes: bytes
) -> list[UnifiedJobRecord]:
    records = _parse_jsonl_bytes(staged_bytes, label="staged/jobs.jsonl")
    validated: list[UnifiedJobRecord] = []
    for record in records:
        if _gate_status(record) != "valid":
            raise CollectionReportError(
                "commit accepts only valid staging records; review/quarantine promotion is forbidden"
            )
        normalization_status = str(record.get("normalization_status") or "valid")
        if normalization_status != "valid":
            raise CollectionReportError("commit requires valid staging normalization")
        try:
            validated.append(UnifiedJobRecord.model_validate(record))
        except Exception as exc:
            raise CollectionReportError("staged record schema validation failed") from exc
    totals = report.get("totals")
    if not isinstance(totals, Mapping) or totals.get("valid") != len(records):
        raise CollectionReportError("valid staging count does not match the report")
    return validated


def _validate_all_artifact_records(
    artifact_bytes: Mapping[str, bytes],
) -> dict[str, list[UnifiedJobRecord]]:
    validated: dict[str, list[UnifiedJobRecord]] = {}
    for bucket in ("staged", "review", "quarantine"):
        rows = _parse_jsonl_bytes(
            artifact_bytes[bucket], label=f"{bucket}/jobs.jsonl"
        )
        models: list[UnifiedJobRecord] = []
        for row in rows:
            try:
                model = UnifiedJobRecord.model_validate(row)
            except Exception as exc:
                raise CollectionReportError(
                    f"{bucket} record schema validation failed"
                ) from exc
            if _destination(_gate_status(model.model_dump(mode="json"))) != bucket:
                raise CollectionReportError(
                    f"{bucket} record gate does not match its artifact"
                )
            models.append(model)
        validated[bucket] = models
    return validated


def _current_source_definitions(
    report: Mapping[str, object], registry: SourceRegistry
) -> dict[str, SourceDefinition]:
    declared = report.get("source_definitions")
    if not isinstance(declared, list):
        raise CollectionReportError("staging report source definitions are invalid")
    current: dict[str, SourceDefinition] = {}
    for value in declared:
        staged_definition = SourceDefinition.model_validate(value)
        try:
            definition = registry.get(staged_definition.source_id)
        except Exception as exc:
            raise CollectionReportError(
                f"source is absent from the current registry: {staged_definition.source_id}"
            ) from exc
        if definition.model_dump(mode="json") != staged_definition.model_dump(mode="json"):
            raise CollectionReportError(
                f"source definition differs from the current registry: {definition.source_id}"
            )
        if not definition.enabled or definition.compliance_status not in {
            "approved",
            "manual_only",
        }:
            raise CollectionReportError(
                f"source is not currently approved for commit: {definition.source_id}"
            )
        if definition.market_scope != "china":
            raise CollectionBlocked(
                "source is not approved for China job data: "
                f"{definition.source_id} (market_scope={definition.market_scope})"
            )
        current[definition.source_id] = definition
    return current


def _secure_snapshot(
    storage: RunStorage, source: SourceDefinition, requested_url: str
) -> tuple[bytes, dict[str, Any]]:
    raw_path, metadata_path = storage.snapshot_paths(source.source_id, requested_url)
    content = secure_read_file(raw_path, root=storage.run_root, max_bytes=MAX_STAGED_BYTES)
    metadata_bytes = secure_read_file(
        metadata_path, root=storage.run_root, max_bytes=MAX_JSONL_LINE_BYTES
    )
    metadata = _parse_json_object(metadata_bytes, label=metadata_path.name)
    expected = {
        "source_id": source.source_id,
        "run_id": storage.run_id,
        "url": requested_url,
        "parser_version": source.parser_version,
        "content_hash": _sha256(content),
    }
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise CollectionReportError("raw evidence metadata validation failed")
    if not isinstance(metadata.get("final_url"), str) or not (
        isinstance(metadata.get("status"), int) and 200 <= metadata["status"] < 300
    ):
        raise CollectionReportError("raw evidence response metadata is invalid")
    return content, metadata


def _validate_record_provenance(
    record: UnifiedJobRecord,
    source: SourceDefinition,
    registry: SourceRegistry,
) -> None:
    if source.collection_mode == "file_import":
        serialized = record.model_dump(mode="json")
        expected = {
            "source_id": source.source_id,
            "source_name": source.source_name,
            "source_type": source.source_type,
            "source_domain": (urlsplit(source.base_url).hostname or "").lower(),
            "parser_name": source.parser_name,
            "parser_version": source.parser_version,
            "collection_method": source.collection_mode,
            "compliance_note": source.compliance_note,
        }
        extra = record.adapter_extra
        registry.validate_url(source.source_id, record.source_url)
        if _is_authorized_export(source):
            if (
                any(serialized.get(key) != value for key, value in expected.items())
                or extra.get("registry_compliance_note") != source.compliance_note
                or not isinstance(extra.get("authorization_reference"), str)
                or not str(extra.get("authorization_reference")).strip()
                or not isinstance(extra.get("authorization_scope"), str)
                or not str(extra.get("authorization_scope")).strip()
                or not isinstance(extra.get("input_filename"), str)
                or not isinstance(extra.get("input_row_number"), int)
                or not isinstance(extra.get("input_file_sha256"), str)
                or not isinstance(extra.get("input_row_sha256"), str)
            ):
                raise CollectionReportError(
                    "authorized-export staged record provenance differs from reviewed evidence"
                )
            return
        if (
            any(serialized.get(key) != value for key, value in expected.items())
            or extra.get("registry_compliance_note") != source.compliance_note
            or not isinstance(extra.get("collection_authorization_note"), str)
            or not str(extra.get("collection_authorization_note")).strip()
            or not isinstance(extra.get("input_filename"), str)
            or not isinstance(extra.get("input_line_number"), int)
            or not isinstance(extra.get("input_file_sha256"), str)
            or not isinstance(extra.get("input_line_sha256"), str)
        ):
            raise CollectionReportError(
                "file-import staged record provenance differs from reviewed evidence"
            )
        return
    if source.collection_mode == "manual_url_manifest":
        serialized = record.model_dump(mode="json")
        expected = {
            "source_id": source.source_id,
            "source_type": source.source_type,
            "parser_name": source.parser_name,
            "parser_version": source.parser_version,
            "collection_method": source.collection_mode,
        }
        extra = record.adapter_extra
        host = (urlsplit(record.source_url).hostname or "").lower()
        if (
            any(serialized.get(key) != value for key, value in expected.items())
            or record.source_domain != host
            or extra.get("registry_compliance_note") != source.compliance_note
            or extra.get("collection_authorization_note") != record.compliance_note
        ):
            raise CollectionReportError(
                "manual staged record provenance differs from reviewed evidence"
            )
        return
    try:
        registry.validate_url(source.source_id, record.source_url)
    except Exception as exc:
        raise CollectionReportError("staged source URL is outside the current registry") from exc
    expected = {
        "source_id": source.source_id,
        "source_name": source.source_name,
        "source_type": source.source_type,
        "source_domain": (urlsplit(source.base_url).hostname or "").lower(),
        "parser_name": source.parser_name,
        "parser_version": source.parser_version,
        "collection_method": source.collection_mode,
        "compliance_note": source.compliance_note,
    }
    serialized = record.model_dump(mode="json")
    if any(serialized.get(key) != value for key, value in expected.items()):
        raise CollectionReportError("staged record provenance differs from current registry")


def _recompute_automatic_record(
    *,
    storage: RunStorage,
    record: UnifiedJobRecord,
    source: SourceDefinition,
    registry: SourceRegistry,
    adapter_factory: Callable[[SourceDefinition, SourceRegistry], object],
    family_config: Mapping[str, FamilyDefinition],
    collected_at: datetime,
) -> UnifiedJobRecord:
    evidence = record.adapter_extra.get("collection_evidence")
    if not isinstance(evidence, Mapping):
        raise CollectionReportError("staged record is missing raw evidence linkage")
    required_strings = (
        "list_url",
        "list_final_url",
        "detail_request_url",
        "detail_final_url",
        "source_record_id",
        "requested_family",
    )
    if any(not isinstance(evidence.get(key), str) for key in required_strings):
        raise CollectionReportError("staged raw evidence linkage is invalid")
    detail_embedded = evidence.get("detail_embedded", False)
    if not isinstance(detail_embedded, bool):
        raise CollectionReportError("staged detail evidence mode is invalid")
    offset = evidence.get("list_offset")
    limit = evidence.get("list_limit")
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 0
        or isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= source.max_records
    ):
        raise CollectionReportError("staged raw evidence bounds are invalid")

    list_url = str(evidence["list_url"])
    detail_request_url = str(evidence["detail_request_url"])
    registry.validate_url(source.source_id, list_url)
    registry.validate_url(source.source_id, str(evidence["list_final_url"]))
    registry.validate_url(source.source_id, detail_request_url)
    registry.validate_url(source.source_id, str(evidence["detail_final_url"]))
    list_content, list_metadata = _secure_snapshot(storage, source, list_url)
    if list_metadata["final_url"] != evidence["list_final_url"]:
        raise CollectionReportError("raw evidence final URL validation failed")

    adapter = adapter_factory(source, registry)
    if bool(getattr(adapter, "embedded_detail", False)) != detail_embedded:
        raise CollectionReportError("staged detail evidence mode changed")
    if detail_embedded:
        detail_content = list_content
        detail_final_url = str(evidence["detail_final_url"])
        detail_snapshot_hash = str(list_metadata["content_hash"])
        detail_status = int(list_metadata["status"])
    else:
        detail_content, detail_metadata = _secure_snapshot(
            storage, source, detail_request_url
        )
        if detail_metadata["final_url"] != evidence["detail_final_url"]:
            raise CollectionReportError("raw evidence final URL validation failed")
        detail_final_url = str(detail_metadata["final_url"])
        detail_snapshot_hash = str(detail_metadata["content_hash"])
        detail_status = int(detail_metadata["status"])
    page = adapter.parse_list(
        list_content,
        str(list_metadata.get("content_type") or ""),
        offset,
        limit,
    )
    item = next(
        (
            candidate
            for candidate in page.items[:limit]
            if candidate.source_record_id == evidence["source_record_id"]
        ),
        None,
    )
    if item is None:
        raise CollectionReportError("staged record is absent from raw list evidence")
    if adapter.build_detail_url(item) != detail_request_url:
        raise CollectionReportError("staged detail URL differs from raw list evidence")
    raw = adapter.parse_detail(detail_content, item, detail_final_url)
    raw["collection_evidence"] = dict(evidence)
    requested_family = str(evidence["requested_family"])
    if requested_family not in family_config:
        raise CollectionReportError("staged requested family is not currently configured")
    raw["job_family_id"] = requested_family
    classification = classify_job_family(
        str(raw.get("job_title") or raw.get("job_title_raw") or ""),
        str(raw.get("job_description_raw") or ""),
        family_config,
    )
    if classification.family_code:
        raw["job_family_id"] = classification.family_code
    normalized = normalize_job_record(
        raw,
        source=source,
        run_id=storage.run_id,
        snapshot_metadata={
            "snapshot_hash": detail_snapshot_hash,
            "observed_at": _timestamp(collected_at),
            "response_status": detail_status,
            "page_title": raw.get("page_title"),
        },
        collected_at=collected_at,
    )
    return _combined_quality(
        normalized,
        classification_status=classification.status,
        classification=asdict(classification),
        now=collected_at,
    )


def _validate_semantic_evidence(
    *,
    storage: RunStorage,
    report: Mapping[str, object],
    records: Sequence[UnifiedJobRecord],
    registry: SourceRegistry,
    adapter_factory: Callable[[SourceDefinition, SourceRegistry], object],
    family_config: Mapping[str, FamilyDefinition],
    authorization_manifest_path: str | Path | None = None,
) -> None:
    definitions = _current_source_definitions(report, registry)
    try:
        collected_at = datetime.fromisoformat(str(report["started_at"]))
    except (KeyError, ValueError) as exc:
        raise CollectionReportError("staging report start time is invalid") from exc
    manual_expected: dict[str, UnifiedJobRecord] = {}
    file_expected: dict[str, UnifiedJobRecord] = {}
    request = report.get("request")
    manifest_value = request.get("manifest_path") if isinstance(request, Mapping) else None
    input_file_value = (
        request.get("input_file_path") if isinstance(request, Mapping) else None
    )
    authorization_note = (
        request.get("authorization_note") if isinstance(request, Mapping) else None
    )
    current_grants = None
    for source in definitions.values():
        if source.collection_mode == "file_import":
            if not isinstance(input_file_value, str):
                raise CollectionReportError(
                    "file-import run is missing reviewed input metadata"
                )
            snapshot_file = storage.resolve_path(
                "raw", source.source_id, Path(input_file_value).name
            )
            max_records = request.get("max_records") if isinstance(request, Mapping) else None
            try:
                if _is_authorized_export(source):
                    if current_grants is None:
                        current_grants = load_authorized_source_grants(
                            authorization_manifest_path
                            or DEFAULT_AUTHORIZATION_MANIFEST_PATH,
                            today=datetime.now(timezone.utc).date(),
                        )
                    grant = current_grants.require(source.source_id, "file_export")
                    expected_authorization = {
                        "authorization_reference": grant.authorization_reference,
                        "authorization_valid_until": grant.valid_until.isoformat(),
                        "authorization_access_method": "file_export",
                        "authorization_scope_sha256": _sha256(
                            grant.scope.encode("utf-8")
                        ),
                        "authorization_manifest_sha256": current_grants.manifest_sha256,
                        "input_file_sha256": _sha256(
                            secure_read_file(
                                snapshot_file,
                                root=storage.run_root,
                                max_bytes=MAX_STAGED_BYTES,
                            )
                        ),
                    }
                    if any(
                        request.get(key) != value
                        for key, value in expected_authorization.items()
                    ):
                        raise CollectionReportError(
                            "authorized-export grant or input identity changed after staging"
                        )
                    adapter = AuthorizedExportAdapter(source=source, registry=registry)
                    expected_records = adapter.load_file(
                        snapshot_file,
                        run_id=storage.run_id,
                        authorization_reference=grant.authorization_reference,
                        authorization_scope=grant.scope,
                        max_records=int(max_records),
                        record_offset=int(request.get("record_offset", 0)),
                        collected_at=collected_at,
                    )
                else:
                    if not isinstance(authorization_note, str):
                        raise CollectionReportError(
                            "file-import run is missing reviewed input metadata"
                        )
                    adapter = LegacyFileAdapter(source=source, registry=registry)
                    expected_records = adapter.load_file(
                        snapshot_file,
                        run_id=storage.run_id,
                        authorization_note=authorization_note,
                        max_records=int(max_records),
                    )
            except (TypeError, ValueError) as exc:
                raise CollectionReportError(
                    "file-import raw evidence cannot be recomputed"
                ) from exc
            file_expected.update(
                {item.record_id: item for item in expected_records}
            )
            continue
        if source.collection_mode != "manual_url_manifest":
            continue
        if not isinstance(manifest_value, str):
            raise CollectionReportError("manual run is missing manifest evidence metadata")
        snapshot_manifest = storage.resolve_path(
            "raw", source.source_id, Path(manifest_value).name
        )
        manifest_bytes = secure_read_file(
            snapshot_manifest, root=storage.run_root, max_bytes=MAX_STAGED_BYTES
        )
        referenced: dict[str, bytes] = {}
        for value in _parse_jsonl_bytes(manifest_bytes, label="manual evidence"):
            relative = value.get("exported_html_path")
            if isinstance(relative, str):
                source_path = storage.resolve_path("raw", source.source_id, relative)
                referenced[relative] = secure_read_file(
                    source_path, root=storage.run_root, max_bytes=MAX_STAGED_BYTES
                )
        with tempfile.TemporaryDirectory(prefix="job-collection-manual-") as temp_dir:
            temp_root = Path(temp_dir).resolve()
            temp_manifest = temp_root / snapshot_manifest.name
            temp_manifest.write_bytes(manifest_bytes)
            for relative, content in referenced.items():
                target = (temp_root / relative).resolve()
                if temp_root not in target.parents:
                    raise CollectionReportError("manual evidence path escapes snapshot")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            adapter = ManualManifestAdapter(source=source, registry=registry)
            expected_records = adapter.load_manifest(
                temp_manifest,
                run_id=storage.run_id,
                collected_at=collected_at,
            )
        manual_expected.update({item.record_id: item for item in expected_records})

    for record in records:
        try:
            source = definitions[record.source_id]
        except KeyError as exc:
            raise CollectionReportError("staged record source is not declared") from exc
        _validate_record_provenance(record, source, registry)
        if source.collection_mode == "file_import":
            expected = file_expected.get(record.record_id)
            if (
                expected is None
                or expected.model_dump(mode="json") != record.model_dump(mode="json")
            ):
                raise CollectionReportError(
                    "file-import staged record differs from canonical raw evidence"
                )
            continue
        if source.collection_mode == "manual_url_manifest":
            expected = manual_expected.get(record.record_id)
            if (
                expected is None
                or expected.model_dump(mode="json") != record.model_dump(mode="json")
            ):
                raise CollectionReportError(
                    "manual staged record differs from canonical raw evidence"
                )
            continue
        recomputed = _recompute_automatic_record(
            storage=storage,
            record=record,
            source=source,
            registry=registry,
            adapter_factory=adapter_factory,
            family_config=family_config,
            collected_at=collected_at,
        )
        if recomputed.model_dump(mode="json") != record.model_dump(mode="json"):
            raise CollectionReportError("staged record differs from recomputed raw evidence")


def _verified_backup(database_url: str, backup_dir: str | Path, run_root: Path) -> Path:
    database_path = sqlite_database_path(database_url)
    if database_path is None:
        raise ValueError("collection commit requires a SQLite database")
    database_path = database_path.resolve()
    backup_root = Path(backup_dir).resolve()
    if backup_root == database_path or run_root.resolve() in (
        backup_root,
        *backup_root.parents,
    ):
        raise ValueError("backup path must be separate from the database and collection run")
    backup = backup_sqlite_database(database_url, backup_root)
    if backup is None:
        raise ValueError("collection commit requires a SQLite database backup")
    backup = backup.resolve()
    if backup == database_path or backup.parent != backup_root:
        backup.unlink(missing_ok=True)
        raise ValueError("backup path failed safety validation")
    try:
        with sqlite3.connect(f"file:{backup.as_posix()}?mode=ro", uri=True) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as exc:
        backup.unlink(missing_ok=True)
        raise DatabaseOperationalError(
            f"database backup verification failed: {exc}"
        ) from exc
    if result is None or result[0] != "ok":
        backup.unlink(missing_ok=True)
        raise DatabaseOperationalError("database backup verification failed")
    return backup


def _database_lock_id(database_url: str) -> str:
    database_path = sqlite_database_path(database_url)
    if database_path is None:
        raise ValueError("collection commit requires a SQLite database")
    identity = hashlib.sha256(
        os.path.normcase(str(database_path.resolve())).encode("utf-8")
    ).hexdigest()[:48]
    return f"db-{identity}"


async def _commit_collection_run_unlocked(
    *,
    run_id: str,
    collections_root: str | Path = DEFAULT_COLLECTIONS_ROOT,
    database_url: str,
    backup_dir: str | Path,
    confirm: bool,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    attestation_key: bytes | str | None = None,
    control_root: str | Path | None = None,
    attestations_root: str | Path | None = None,
    keys_root: str | Path | None = None,
    registry: SourceRegistry | None = None,
    registry_path: str | Path = DEFAULT_REGISTRY_PATH,
    family_config: Mapping[str, FamilyDefinition] | None = None,
    adapter_factory: Callable[[SourceDefinition, SourceRegistry], object] = _default_adapter,
    authorization_manifest_path: str | Path | None = None,
) -> dict[str, Any]:
    if not confirm:
        raise ValueError("--commit requires --confirm")
    storage = RunStorage(collections_root, run_id)
    report_bytes = secure_read_file(
        storage.resolve_path("report.json"),
        root=storage.run_root,
        max_bytes=MAX_JSONL_LINE_BYTES,
    )
    artifact_bytes = {
        name: secure_read_file(
            storage.resolve_path(name, "jobs.jsonl"),
            root=storage.run_root,
            max_bytes=MAX_STAGED_BYTES,
        )
        for name in ("staged", "review", "quarantine")
    }
    report = _validate_staging_report(
        storage, report_bytes=report_bytes, artifact_bytes=artifact_bytes
    )
    staged_records = _validate_commit_records(report, artifact_bytes["staged"])
    validated_artifacts = _validate_all_artifact_records(artifact_bytes)
    _control_root, default_attestations, _default_locks, default_keys = (
        _control_state_paths(collections_root, control_root)
    )
    trusted_attestations_root = Path(
        os.path.abspath(attestations_root or default_attestations)
    )
    attestation_bytes = secure_read_file(
        trusted_attestations_root / f"{run_id}.json",
        root=trusted_attestations_root,
        max_bytes=MAX_JSONL_LINE_BYTES,
    )
    _verify_attestation(
        attestation_bytes=attestation_bytes,
        key=_attestation_key(
            attestation_key,
            key_root=keys_root or default_keys,
        ),
        run_id=run_id,
        report_bytes=report_bytes,
        artifacts=report["artifacts"],
    )
    current_registry = registry or SourceRegistry.load(registry_path)
    current_family_config = dict(family_config or load_family_config())
    _validate_semantic_evidence(
        storage=storage,
        report=report,
        records=[
            record
            for bucket in ("staged", "review", "quarantine")
            for record in validated_artifacts[bucket]
        ],
        registry=current_registry,
        adapter_factory=adapter_factory,
        family_config=current_family_config,
        authorization_manifest_path=authorization_manifest_path,
    )

    engine = create_async_engine(database_url, connect_args={"timeout": 0.1})
    try:
        async with engine.connect() as connection:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                existing = await session.scalar(
                    select(CollectionRun).where(CollectionRun.run_id == run_id)
                )
                if existing is not None:
                    if existing.status != "completed":
                        raise ValueError(
                            f"collection run is not safely idempotent: {run_id}"
                        )
                    summary = json.loads(existing.summary_json)
                    summary["idempotent"] = True
                    await connection.rollback()
                    return summary

                try:
                    backup = _verified_backup(
                        database_url, backup_dir, storage.run_root
                    )
                    definitions = [
                        current_registry.get(str(item["source_id"]))
                        for item in report["source_definitions"]
                    ]
                    commit_registry = SourceRegistry(definitions)
                    await commit_registry.upsert_job_sources(session)
                    run = CollectionRun(
                        run_id=run_id,
                        source_ids_json=json.dumps(
                            [item.source_id for item in definitions], ensure_ascii=False
                        ),
                        mode="commit",
                        status="running",
                        staging_dir=str(storage.run_root),
                        fetched_count=int(report["totals"]["fetched"]),
                        parsed_count=int(report["totals"]["parsed"]),
                        valid_count=int(report["totals"]["valid"]),
                        review_count=int(report["totals"]["review"]),
                        quarantined_count=int(report["totals"]["quarantined"]),
                        duplicate_count=int(report["totals"]["duplicates"]),
                    )
                    session.add(run)
                    await session.flush()
                    manual_source_ids = {
                        item.source_id
                        for item in definitions
                        if item.compliance_status == "manual_only"
                        and item.collection_mode == "manual_url_manifest"
                    }
                    file_source_ids = {
                        item.source_id
                        for item in definitions
                        if item.compliance_status == "manual_only"
                        and item.collection_mode == "file_import"
                    }
                    authorization = _verified_collection_import_capability(
                        {item.source_id for item in definitions},
                        manual_external_url_source_ids=manual_source_ids,
                        manual_file_import_source_ids=file_source_ids,
                    )
                    imported = await import_job_file(
                        session,
                        artifact_bytes["staged"],
                        "jobs.jsonl",
                        commit=False,
                        authorization=authorization,
                    )
                    unexpected = (
                        int(imported.get("quarantined", 0))
                        or int(imported.get("review", 0))
                        or bool(imported.get("errors"))
                    )
                    processed = sum(
                        int(imported.get(key, 0))
                        for key in ("imported", "revised", "skipped")
                    )
                    if unexpected or processed != len(staged_records):
                        raise RuntimeError(
                            "guarded commit aborted: importer reported quarantine, review, "
                            "errors, or an unreconciled record count"
                        )
                    run.imported_count = int(imported["imported"])
                    run.duplicate_count += int(imported["duplicates"])
                    run.status = "completed"
                    run.completed_at = datetime.now()
                    summary = {
                        **imported,
                        "run_id": run_id,
                        "backup_path": str(backup),
                        "report_sha256": _sha256(report_bytes),
                        "idempotent": False,
                    }
                    run.summary_json = json.dumps(
                        summary, ensure_ascii=False, sort_keys=True
                    )
                    await session.flush()
                    await connection.commit()
                    return summary
                except Exception:
                    await connection.rollback()
                    raise
    finally:
        await engine.dispose()


async def commit_collection_run(
    *,
    run_id: str,
    collections_root: str | Path = DEFAULT_COLLECTIONS_ROOT,
    database_url: str,
    backup_dir: str | Path,
    confirm: bool,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    attestation_key: bytes | str | None = None,
    control_root: str | Path | None = None,
    attestations_root: str | Path | None = None,
    locks_root: str | Path | None = None,
    keys_root: str | Path | None = None,
    registry: SourceRegistry | None = None,
    registry_path: str | Path = DEFAULT_REGISTRY_PATH,
    family_config: Mapping[str, FamilyDefinition] | None = None,
    adapter_factory: Callable[[SourceDefinition, SourceRegistry], object] = _default_adapter,
    authorization_manifest_path: str | Path | None = None,
) -> dict[str, Any]:
    if not confirm:
        raise ValueError("--commit requires --confirm")
    trusted_collections_root = Path(os.path.abspath(collections_root))
    (
        trusted_control_root,
        _default_attestations,
        default_locks,
        _default_keys,
    ) = _control_state_paths(trusted_collections_root, control_root)
    ensure_secure_directory(trusted_control_root)
    trusted_locks_root = Path(os.path.abspath(locks_root or default_locks))
    with ExclusiveRunLock(trusted_locks_root, run_id, "commit"):
        with ExclusiveRunLock(
            trusted_locks_root, _database_lock_id(database_url), "commit"
        ):
            return await _commit_collection_run_unlocked(
                run_id=run_id,
                collections_root=trusted_collections_root,
                database_url=database_url,
                backup_dir=backup_dir,
                confirm=confirm,
                session_factory=session_factory,
                attestation_key=attestation_key,
                control_root=control_root,
                attestations_root=attestations_root,
                keys_root=keys_root,
                registry=registry,
                registry_path=registry_path,
                family_config=family_config,
                adapter_factory=adapter_factory,
                authorization_manifest_path=authorization_manifest_path,
            )


__all__ = [
    "CollectionReportError",
    "CollectionService",
    "DEFAULT_AUTHORIZATION_MANIFEST_PATH",
    "DEFAULT_COLLECTIONS_ROOT",
    "DEFAULT_REGISTRY_PATH",
    "commit_collection_run",
]
