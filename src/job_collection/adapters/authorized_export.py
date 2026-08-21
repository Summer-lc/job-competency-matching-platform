from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import unicodedata
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from pydantic import ValidationError

from src.job_collection.adapters.manual_manifest import ManualManifestAdapter
from src.job_collection.adapters.mohrss import MOHRSSAdapter
from src.job_collection.family_classifier import (
    classify_job_family,
    classify_job_family_with_hint,
)
from src.job_collection.models import SourceDefinition, UnifiedJobRecord
from src.job_collection.normalizer import NormalizationError, normalize_job_record
from src.job_collection.source_registry import SourceRegistry, SourceRegistryError
from src.job_data_service import JOB_FAMILY_NAMES


MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
SUPPORTED_SUFFIXES = frozenset({".jsonl", ".json", ".csv"})
FIELD_ALIASES = {
    "source_record_id": ("岗位ID", "职位ID", "job_id", "position_id", "record_id"),
    "job_title": ("岗位名称", "职位名称", "job_name", "position_name", "job_title"),
    "company": ("公司名称", "企业名称", "company_name", "company"),
    "industry": ("行业", "所属行业", "industry"),
    "region": ("工作城市", "工作地点", "城市", "city", "location", "region"),
    "salary": ("薪资", "薪资范围", "salary", "salary_desc"),
    "experience": ("经验要求", "工作经验", "experience"),
    "education": ("学历要求", "学历", "education"),
    "description": (
        "岗位描述",
        "职位描述",
        "任职要求",
        "description",
        "job_description",
    ),
    "published_at": (
        "发布时间",
        "更新日期",
        "发布日期",
        "published_at",
        "update_time",
    ),
    "source_url": ("原始链接", "职位链接", "source_url", "job_url"),
    "job_family_id": ("岗位族", "岗位族编码", "job_family_id"),
}


class AuthorizedExportAdapterError(ValueError):
    """An authorized platform export is unsafe or cannot be normalized."""


class _DuplicateJSONKey(ValueError):
    pass


def _object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey(key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant {value}")


def _field_key(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


_ALIASES = {
    canonical: tuple(_field_key(alias) for alias in aliases)
    for canonical, aliases in FIELD_ALIASES.items()
}


def _field(row: Mapping[str, object], canonical: str) -> object | None:
    normalized = {_field_key(key): value for key, value in row.items()}
    for alias in _ALIASES[canonical]:
        value = normalized.get(alias)
        if value is not None and str(value).strip():
            return value
    return None


def _parse_datetime(value: object, *, field_name: str) -> datetime:
    raw = str(value or "").strip().replace("Z", "+00:00")
    if not raw:
        raise AuthorizedExportAdapterError(f"{field_name} is required")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise AuthorizedExportAdapterError(f"{field_name} is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class AuthorizedExportAdapter:
    """Normalize an explicitly authorized local platform export without networking."""

    def __init__(self, *, source: SourceDefinition, registry: SourceRegistry) -> None:
        try:
            registered = registry.get(source.source_id)
        except SourceRegistryError as exc:
            raise ValueError(str(exc)) from exc
        if source != registered:
            raise ValueError("source must equal the registered SourceDefinition")
        if not (
            source.enabled
            and source.market_scope == "china"
            and source.compliance_status == "manual_only"
            and source.collection_mode == "file_import"
            and source.parser_name.endswith("_authorized_export")
        ):
            raise ValueError(
                "authorized export source must be an enabled China manual file import"
            )
        self.source = source
        self.registry = registry
        self._errors: list[dict[str, object]] = []

    @property
    def errors(self) -> tuple[dict[str, object], ...]:
        return tuple(dict(item) for item in self._errors)

    def load_file(
        self,
        path: str | Path,
        *,
        run_id: str,
        authorization_reference: str,
        authorization_scope: str,
        max_records: int,
        record_offset: int = 0,
        collected_at: datetime | None = None,
    ) -> tuple[UnifiedJobRecord, ...]:
        reference = authorization_reference.strip()
        scope = authorization_scope.strip()
        if not reference:
            raise AuthorizedExportAdapterError("authorization_reference is required")
        if not scope:
            raise AuthorizedExportAdapterError("authorization_scope is required")
        if (
            isinstance(max_records, bool)
            or not isinstance(max_records, int)
            or not 1 <= max_records <= self.source.max_records
        ):
            raise ValueError(
                f"max_records must be between 1 and {self.source.max_records}"
            )
        if (
            isinstance(record_offset, bool)
            or not isinstance(record_offset, int)
            or record_offset < 0
        ):
            raise ValueError("record_offset must be a non-negative integer")

        input_path = Path(path).absolute()
        suffix = input_path.suffix.casefold()
        if suffix not in SUPPORTED_SUFFIXES:
            raise AuthorizedExportAdapterError(
                "authorized export must use .jsonl, .json, or .csv"
            )
        payload = self._read_file(input_path)
        file_hash = hashlib.sha256(payload).hexdigest()
        rows = self._parse_rows(payload, suffix=suffix)
        self._errors = []
        records: list[UnifiedJobRecord] = []
        observed_at = collected_at or datetime.now(timezone.utc)
        for row_index, (row_number, row) in enumerate(rows):
            if row_index < record_offset:
                continue
            if len(records) + len(self._errors) >= max_records:
                break
            row_payload = json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            row_hash = hashlib.sha256(row_payload).hexdigest()
            try:
                record = self._normalize_row(
                    row,
                    row_number=row_number,
                    row_hash=row_hash,
                    file_name=input_path.name,
                    file_hash=file_hash,
                    run_id=run_id,
                    authorization_reference=reference,
                    authorization_scope=scope,
                    collected_at=observed_at,
                )
            except AuthorizedExportAdapterError as exc:
                self._errors.append(
                    {
                        "line": row_number,
                        "code": "record_validation_error",
                        "message": str(exc),
                    }
                )
                continue
            records.append(record)
        return tuple(records)

    @staticmethod
    def _read_file(path: Path) -> bytes:
        try:
            with ManualManifestAdapter._open_verified_file(
                path, trusted_root=path.parent, label="authorized export"
            ) as stream:
                size = os.fstat(stream.fileno()).st_size
                if size > MAX_FILE_BYTES:
                    raise AuthorizedExportAdapterError(
                        f"authorized export exceeds {MAX_FILE_BYTES} bytes"
                    )
                payload = stream.read(MAX_FILE_BYTES + 1)
        except AuthorizedExportAdapterError:
            raise
        except Exception as exc:
            raise AuthorizedExportAdapterError(str(exc)) from exc
        if len(payload) > MAX_FILE_BYTES:
            raise AuthorizedExportAdapterError(
                f"authorized export exceeds {MAX_FILE_BYTES} bytes"
            )
        return payload

    def _parse_rows(
        self, payload: bytes, *, suffix: str
    ) -> tuple[tuple[int, dict[str, object]], ...]:
        if suffix == ".jsonl":
            return tuple(self._parse_jsonl(payload))
        if suffix == ".json":
            return tuple(self._parse_json_array(payload))
        return tuple(self._parse_csv(payload))

    @staticmethod
    def _json_loads(payload: str) -> object:
        try:
            return json.loads(
                payload,
                object_pairs_hook=_object_without_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except _DuplicateJSONKey as exc:
            raise AuthorizedExportAdapterError(
                f"authorized export contains duplicate JSON key: {exc}"
            ) from exc
        except (json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise AuthorizedExportAdapterError(
                f"authorized export contains invalid JSON: {exc}"
            ) from exc

    def _parse_jsonl(
        self, payload: bytes
    ) -> Iterable[tuple[int, dict[str, object]]]:
        for line_number, raw_line in enumerate(payload.split(b"\n"), start=1):
            if not raw_line.strip():
                continue
            if len(raw_line) > MAX_LINE_BYTES:
                raise AuthorizedExportAdapterError(
                    f"authorized export line {line_number} exceeds {MAX_LINE_BYTES} bytes"
                )
            try:
                text = raw_line.decode("utf-8-sig" if line_number == 1 else "utf-8")
            except UnicodeError as exc:
                raise AuthorizedExportAdapterError(
                    f"authorized export line {line_number} is not UTF-8"
                ) from exc
            value = self._json_loads(text)
            if not isinstance(value, dict):
                raise AuthorizedExportAdapterError(
                    f"authorized export line {line_number} must be a JSON object"
                )
            yield line_number, value

    def _parse_json_array(
        self, payload: bytes
    ) -> Iterable[tuple[int, dict[str, object]]]:
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeError as exc:
            raise AuthorizedExportAdapterError("authorized export is not UTF-8") from exc
        value = self._json_loads(text)
        if not isinstance(value, list):
            raise AuthorizedExportAdapterError(
                "authorized .json export must contain an array"
            )
        for index, row in enumerate(value, start=1):
            if not isinstance(row, dict):
                raise AuthorizedExportAdapterError(
                    f"authorized export row {index} must be an object"
                )
            yield index, row

    @staticmethod
    def _parse_csv(payload: bytes) -> Iterable[tuple[int, dict[str, object]]]:
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeError as exc:
            raise AuthorizedExportAdapterError("authorized CSV export is not UTF-8") from exc
        try:
            reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
            if not reader.fieldnames or any(not str(name or "").strip() for name in reader.fieldnames):
                raise AuthorizedExportAdapterError(
                    "authorized CSV export has invalid headings"
                )
            normalized_headings = [_field_key(name) for name in reader.fieldnames]
            if len(normalized_headings) != len(set(normalized_headings)):
                raise AuthorizedExportAdapterError(
                    "authorized CSV export has duplicate headings"
                )
            for row_number, row in enumerate(reader, start=2):
                if None in row:
                    raise AuthorizedExportAdapterError(
                        f"authorized CSV row {row_number} has extra cells"
                    )
                clean = {str(key): value for key, value in row.items()}
                for value in clean.values():
                    if str(value or "").lstrip().startswith(("=", "+", "-", "@")):
                        raise AuthorizedExportAdapterError(
                            f"authorized CSV row {row_number} contains a formula-prefixed cell"
                        )
                yield row_number, clean
        except csv.Error as exc:
            raise AuthorizedExportAdapterError(
                f"authorized CSV export is malformed: {exc}"
            ) from exc

    def _normalize_row(
        self,
        row: Mapping[str, object],
        *,
        row_number: int,
        row_hash: str,
        file_name: str,
        file_hash: str,
        run_id: str,
        authorization_reference: str,
        authorization_scope: str,
        collected_at: datetime,
    ) -> UnifiedJobRecord:
        title = str(_field(row, "job_title") or "").strip()
        description = str(_field(row, "description") or "").strip()
        description, pii_removed = MOHRSSAdapter._sanitize_description(description)
        supplied_family = str(_field(row, "job_family_id") or "").strip()
        if supplied_family:
            if supplied_family not in JOB_FAMILY_NAMES:
                raise AuthorizedExportAdapterError(
                    f"authorized export row {row_number} has an unknown job_family_id"
                )
            classification = classify_job_family_with_hint(
                title, description, supplied_family
            )
        else:
            classification = classify_job_family(title, description)
        family_code = classification.family_code
        if family_code is None:
            family_code = classification.candidates[0] if classification.candidates else "UNKNOWN"

        published_text = str(_field(row, "published_at") or "").strip()
        published_at: str | None = None
        evidence: str | None = None
        confidence = 0.0
        if published_text:
            parsed = _parse_datetime(published_text, field_name="published_at")
            published_at = parsed.isoformat()
            evidence = (
                "authorized export field published_at="
                f"{published_text}; row_sha256={row_hash}"
            )
            confidence = 0.9

        source_record_id = str(_field(row, "source_record_id") or "").strip()
        if not source_record_id:
            raise AuthorizedExportAdapterError(
                f"authorized export row {row_number} is missing source_record_id"
            )
        raw: dict[str, object] = {
            "source_record_id": source_record_id,
            "job_title_raw": title,
            "company_name": str(_field(row, "company") or "").strip(),
            "industry": _field(row, "industry"),
            "region": _field(row, "region"),
            "salary_range": _field(row, "salary"),
            "experience_requirement": _field(row, "experience"),
            "education_requirement": _field(row, "education"),
            "job_description_raw": description,
            "published_at": published_at,
            "published_at_evidence": evidence,
            "published_at_confidence": confidence,
            "source_url": str(_field(row, "source_url") or "").strip(),
            "job_family_id": family_code,
            "authorization_reference": authorization_reference,
            "authorization_scope": authorization_scope,
            "input_filename": file_name,
            "input_file_sha256": file_hash,
            "input_row_number": row_number,
            "input_row_sha256": row_hash,
            "pii_removed": pii_removed,
            "family_classification": asdict(classification),
            "registry_compliance_note": self.source.compliance_note,
        }
        try:
            record = normalize_job_record(
                raw,
                source=self.source,
                run_id=run_id,
                snapshot_metadata={
                    "snapshot_hash": row_hash,
                    "response_status": 200,
                    "page_title": title or None,
                    "observed_at": collected_at,
                },
                collected_at=collected_at,
            )
        except (NormalizationError, ValidationError) as exc:
            raise AuthorizedExportAdapterError(
                f"authorized export row {row_number} failed normalization: {exc}"
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
                "authorization_reference": authorization_reference,
                "authorization_scope": authorization_scope,
                "input_filename": file_name,
                "input_file_sha256": file_hash,
                "input_row_number": row_number,
                "input_row_sha256": row_hash,
                "pii_removed": pii_removed,
                "family_classification": asdict(classification),
                "registry_compliance_note": self.source.compliance_note,
            }
        )
        return record.model_copy(update={"adapter_extra": extra})


__all__ = ["AuthorizedExportAdapter", "AuthorizedExportAdapterError"]
