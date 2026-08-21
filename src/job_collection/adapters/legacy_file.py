from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pydantic import ValidationError

from src.job_collection.adapters.manual_manifest import ManualManifestAdapter
from src.job_collection.family_classifier import classify_job_family_with_hint
from src.job_collection.models import SourceDefinition, UnifiedJobRecord
from src.job_collection.normalizer import NormalizationError, normalize_job_record
from src.job_collection.source_registry import SourceRegistry, SourceRegistryError
from src.job_data_service import JOB_FAMILY_NAMES


MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024


class LegacyFileAdapterError(ValueError):
    """An authorized local export is unsafe or cannot be normalized."""


class _DuplicateJSONKey(ValueError):
    pass


def _object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJSONKey(key)
        value[key] = item
    return value


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant {value}")


def _parse_datetime(value: object, *, field_name: str) -> datetime:
    raw = str(value or "").strip().replace("Z", "+00:00")
    if not raw:
        raise LegacyFileAdapterError(f"{field_name} is required")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise LegacyFileAdapterError(f"{field_name} is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _canonical_zhaopin_url(value: object) -> tuple[str, str]:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").rstrip(".").casefold()
        port = parsed.port
    except ValueError as exc:
        raise LegacyFileAdapterError("source_url is invalid") from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or host not in {"zhaopin.com", "www.zhaopin.com"}
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 80, 443)
    ):
        raise LegacyFileAdapterError("source_url must be a Zhaopin HTTP(S) URL")
    path = parsed.path or "/"
    return (
        urlunsplit(("https", "www.zhaopin.com", path, parsed.query, "")),
        raw,
    )


class LegacyFileAdapter:
    """Parse a reviewed local Zhaopin export without making network requests."""

    source_id = "zhaopin_legacy_import"

    def __init__(self, *, source: SourceDefinition, registry: SourceRegistry) -> None:
        try:
            registered = registry.get(self.source_id)
        except SourceRegistryError as exc:
            raise ValueError(str(exc)) from exc
        if source != registered:
            raise ValueError("source must equal the registered SourceDefinition")
        if not (
            source.enabled
            and source.compliance_status == "manual_only"
            and source.collection_mode == "file_import"
            and source.parser_name == "zhaopin_legacy"
        ):
            raise ValueError(
                "legacy source must be enabled, manual_only, and use file_import"
            )
        self.source = source
        self.registry = registry
        self._errors: list[dict[str, object]] = []

    @property
    def errors(self) -> tuple[dict[str, object], ...]:
        return tuple(dict(item) for item in self._errors)

    def load_file(
        self,
        input_path: str | Path,
        *,
        run_id: str,
        authorization_note: str,
        max_records: int | None = None,
    ) -> tuple[UnifiedJobRecord, ...]:
        note = authorization_note.strip()
        if not note:
            raise LegacyFileAdapterError("authorization_note is required")
        limit = self.source.max_records if max_records is None else max_records
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= self.source.max_records
        ):
            raise ValueError(
                f"max_records must be between 1 and {self.source.max_records}"
            )
        path = Path(input_path).absolute()
        if path.suffix.casefold() != ".jsonl":
            raise LegacyFileAdapterError("input file must use .jsonl")

        payload = self._read_file(path)
        file_hash = hashlib.sha256(payload).hexdigest()
        self._errors = []
        records: list[UnifiedJobRecord] = []
        attempts = 0
        for line_number, raw_line in enumerate(payload.split(b"\n"), start=1):
            if not raw_line.strip():
                continue
            if attempts >= limit:
                break
            attempts += 1
            if len(raw_line) > MAX_LINE_BYTES:
                raise LegacyFileAdapterError(
                    f"input line {line_number} exceeds {MAX_LINE_BYTES} bytes"
                )
            value = self._parse_line(raw_line, line_number)
            try:
                record = self._normalize_line(
                    value,
                    raw_line=raw_line,
                    line_number=line_number,
                    file_name=path.name,
                    file_hash=file_hash,
                    run_id=run_id,
                    authorization_note=note,
                )
            except LegacyFileAdapterError as exc:
                self._errors.append(
                    {
                        "line": line_number,
                        "code": "record_validation_error",
                        "message": str(exc),
                    }
                )
                continue
            records.append(record)
        return tuple(records)

    @staticmethod
    def _parse_line(raw_line: bytes, line_number: int) -> dict[str, object]:
        try:
            value = json.loads(
                raw_line.decode("utf-8-sig" if line_number == 1 else "utf-8"),
                object_pairs_hook=_object_without_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except _DuplicateJSONKey as exc:
            raise LegacyFileAdapterError(
                f"input line {line_number} contains duplicate JSON key: {exc}"
            ) from exc
        except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise LegacyFileAdapterError(
                f"input line {line_number} is invalid JSON: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise LegacyFileAdapterError(
                f"input line {line_number} must be a JSON object"
            )
        return value

    @staticmethod
    def _read_file(path: Path) -> bytes:
        try:
            with ManualManifestAdapter._open_verified_file(
                path, trusted_root=path.parent, label="legacy input"
            ) as stream:
                size = os.fstat(stream.fileno()).st_size
                if size > MAX_FILE_BYTES:
                    raise LegacyFileAdapterError(
                        f"input file exceeds {MAX_FILE_BYTES} bytes"
                    )
                payload = stream.read(MAX_FILE_BYTES + 1)
        except LegacyFileAdapterError:
            raise
        except Exception as exc:
            raise LegacyFileAdapterError(str(exc)) from exc
        if len(payload) > MAX_FILE_BYTES:
            raise LegacyFileAdapterError(
                f"input file exceeds {MAX_FILE_BYTES} bytes"
            )
        return payload

    def _normalize_line(
        self,
        value: dict[str, object],
        *,
        raw_line: bytes,
        line_number: int,
        file_name: str,
        file_hash: str,
        run_id: str,
        authorization_note: str,
    ) -> UnifiedJobRecord:
        line_hash = hashlib.sha256(raw_line).hexdigest()
        title = str(value.get("job_title_raw") or "").strip()
        description = str(value.get("job_description_raw") or "").strip()
        supplied_family = str(value.get("job_family_id") or "").strip()
        if supplied_family not in JOB_FAMILY_NAMES:
            raise LegacyFileAdapterError(
                f"input line {line_number} has an unknown job_family_id"
            )
        classification = classify_job_family_with_hint(
            title, description, supplied_family
        )
        family_code = classification.family_code or supplied_family
        source_url, original_url = _canonical_zhaopin_url(value.get("source_url"))
        collected_at = _parse_datetime(
            value.get("collected_at"), field_name="collected_at"
        )

        publication_text = str(value.get("published_at") or "").strip()
        publication_issue: str | None = None
        published_at: str | None = None
        publication_evidence: str | None = None
        publication_confidence = 0.0
        if publication_text:
            try:
                parsed_publication = _parse_datetime(
                    publication_text, field_name="published_at"
                )
            except LegacyFileAdapterError:
                publication_issue = "invalid_legacy_publication"
            else:
                if collected_at - timedelta(days=365) <= parsed_publication <= collected_at:
                    published_at = parsed_publication.isoformat()
                    publication_evidence = (
                        "authorized local export field published_at="
                        f"{publication_text}; line_sha256={line_hash}"
                    )
                    publication_confidence = 0.85
                else:
                    publication_issue = "implausible_legacy_publication"

        source_record_id = str(value.get("record_id") or "").strip()
        if not source_record_id:
            raise LegacyFileAdapterError(
                f"input line {line_number} is missing record_id"
            )
        raw = dict(value)
        raw.update(
            {
                "source_url": source_url,
                "source_record_id": source_record_id,
                "job_family_id": family_code,
                "published_at": published_at,
                "published_at_evidence": publication_evidence,
                "published_at_confidence": publication_confidence,
                "collection_authorization_note": authorization_note,
                "input_filename": file_name,
                "input_file_sha256": file_hash,
                "input_line_number": line_number,
                "input_line_sha256": line_hash,
                "original_source_url": original_url,
                "supplied_job_family_id": supplied_family,
                "family_classification": asdict(classification),
                "registry_compliance_note": self.source.compliance_note,
            }
        )
        if publication_issue:
            raw["legacy_publication_issue"] = publication_issue

        try:
            record = normalize_job_record(
                raw,
                source=self.source,
                run_id=run_id,
                snapshot_metadata={
                    "snapshot_hash": line_hash,
                    "response_status": 200,
                    "page_title": title or None,
                    "observed_at": collected_at,
                },
                collected_at=collected_at,
            )
        except (NormalizationError, ValidationError) as exc:
            raise LegacyFileAdapterError(
                f"input line {line_number} failed normalization: {exc}"
            ) from exc

        record = ManualManifestAdapter._apply_quality_pipeline(record, collected_at)
        extra = dict(record.adapter_extra)
        gate = dict(extra.get("quality_gate") or {})
        issue_codes = set(gate.get("issue_codes") or [])
        status = str(gate.get("status") or "quarantined")
        if classification.status == "review":
            issue_codes.add("family_classification_review")
            if status == "valid":
                status = "review"
        gate.update(status=status, issue_codes=sorted(issue_codes))
        extra.update(
            {
                "quality_gate": gate,
                "input_filename": file_name,
                "input_file_sha256": file_hash,
                "input_line_number": line_number,
                "input_line_sha256": line_hash,
                "original_source_url": original_url,
                "supplied_job_family_id": supplied_family,
                "family_classification": asdict(classification),
                "collection_authorization_note": authorization_note,
                "registry_compliance_note": self.source.compliance_note,
            }
        )
        if publication_issue:
            extra["legacy_publication_issue"] = publication_issue
        return record.model_copy(update={"adapter_extra": extra})


__all__ = ["LegacyFileAdapter", "LegacyFileAdapterError"]
