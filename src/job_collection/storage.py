from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.job_collection.models import SourceDefinition


_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SOURCE_ID_PATTERN = re.compile(r"^[a-z0-9_]{1,100}$")
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
_METADATA_FIELDS = {
    "source_id",
    "run_id",
    "url",
    "final_url",
    "status",
    "content_type",
    "fetched_at",
    "content_hash",
    "parser_version",
    "from_cache",
}


class StorageError(RuntimeError):
    """Collection artifacts cannot be read or written safely."""


class SnapshotCorrupt(StorageError):
    """A cached snapshot is incomplete, malformed, or fails integrity checks."""


@dataclass(frozen=True)
class StoredSnapshot:
    content: bytes
    metadata: dict[str, Any]
    raw_path: Path
    metadata_path: Path


@dataclass(frozen=True)
class Checkpoint:
    last_completed_page: int | None
    completed_detail_urls: tuple[str, ...]
    updated_at: str | None
    resume: dict[str, Any] | None = None
    requests_used: int = 0
    pages_used: int = 0
    records_used: int = 0
    list_cursors: dict[str, int] | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_windows_path_segment(segment: str, *, label: str) -> None:
    if segment in {".", ".."}:
        return
    if segment.endswith((".", " ")):
        raise StorageError(f"{label} has a trailing dot or space: {segment!r}")
    device_name = segment.split(".", 1)[0].upper()
    if device_name in _WINDOWS_RESERVED_NAMES:
        raise StorageError(f"{label} uses a Windows reserved device name: {segment!r}")


def _validate_windows_path_parts(parts: tuple[str | Path, ...]) -> None:
    for value in parts:
        for segment in Path(value).parts:
            _validate_windows_path_segment(segment, label="path segment")


class RunStorage:
    """Filesystem storage constrained to one collection run directory."""

    def __init__(
        self,
        collections_root: str | Path,
        run_id: str,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        _validate_windows_path_segment(run_id, label="run_id")
        if not _RUN_ID_PATTERN.fullmatch(run_id) or run_id in {".", ".."}:
            raise StorageError(f"invalid run_id: {run_id!r}")
        self.run_id = run_id
        self._clock = clock
        self.collections_root = Path(os.path.abspath(collections_root))
        self.run_root = Path(os.path.abspath(self.collections_root / run_id))
        if self.run_root.parent != self.collections_root:
            raise StorageError(f"run path is outside run root: {self.run_root}")
        self.checkpoint_path = self.resolve_path("checkpoint.json")

    def resolve_path(self, *parts: str | Path) -> Path:
        _validate_windows_path_parts(parts)
        candidate = Path(os.path.abspath(self.run_root.joinpath(*parts)))
        if candidate != self.run_root and self.run_root not in candidate.parents:
            raise StorageError(f"target path is outside run root: {candidate}")
        return candidate

    def snapshot_paths(self, source_id: str, url: str) -> tuple[Path, Path]:
        _validate_windows_path_segment(source_id, label="source_id")
        if not _SOURCE_ID_PATTERN.fullmatch(source_id):
            raise StorageError(f"invalid source_id for storage: {source_id!r}")
        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
        raw_path = self.resolve_path("raw", source_id, f"{url_hash}.bin")
        metadata_path = self.resolve_path("raw", source_id, f"{url_hash}.json")
        return raw_path, metadata_path

    def load_snapshot(self, source_id: str, url: str) -> StoredSnapshot | None:
        raw_path, metadata_path = self.snapshot_paths(source_id, url)
        if not raw_path.exists() and not metadata_path.exists():
            return None
        if not metadata_path.exists():
            raise SnapshotCorrupt(f"snapshot metadata is missing for {url}")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SnapshotCorrupt(f"snapshot metadata is invalid for {url}: {exc}") from exc

        self._validate_success_metadata(metadata, source_id, url)
        if metadata.get("reusable") is False:
            return None
        if not 200 <= metadata["status"] < 300:
            return None
        if not raw_path.is_file():
            raise SnapshotCorrupt(f"snapshot content is missing for {url}")
        try:
            content = raw_path.read_bytes()
        except OSError as exc:
            raise SnapshotCorrupt(f"snapshot content cannot be read for {url}: {exc}") from exc
        actual_hash = hashlib.sha256(content).hexdigest()
        if actual_hash != metadata["content_hash"]:
            raise SnapshotCorrupt(
                f"snapshot hash mismatch for {url}: expected "
                f"{metadata['content_hash']}, got {actual_hash}"
            )
        cached_metadata = dict(metadata)
        cached_metadata["from_cache"] = True
        return StoredSnapshot(content, cached_metadata, raw_path, metadata_path)

    def write_success(
        self,
        *,
        source: SourceDefinition,
        url: str,
        final_url: str,
        status: int,
        content_type: str,
        content: bytes,
    ) -> StoredSnapshot:
        if not 200 <= status < 300:
            raise StorageError("only successful 2xx responses can be reusable snapshots")
        raw_path, metadata_path = self.snapshot_paths(source.source_id, url)
        content_hash = hashlib.sha256(content).hexdigest()
        metadata = self._metadata(
            source=source,
            url=url,
            final_url=final_url,
            status=status,
            content_type=content_type,
            content_hash=content_hash,
        )
        self._atomic_write_bytes(raw_path, content)
        self._atomic_write_json(metadata_path, metadata)
        return StoredSnapshot(content, metadata, raw_path, metadata_path)

    def write_error_metadata(
        self,
        *,
        source: SourceDefinition,
        url: str,
        final_url: str,
        status: int,
        content_type: str,
        content: bytes,
    ) -> Path:
        _, metadata_path = self.snapshot_paths(source.source_id, url)
        metadata = self._metadata(
            source=source,
            url=url,
            final_url=final_url,
            status=status,
            content_type=content_type,
            content_hash=hashlib.sha256(content).hexdigest(),
        )
        metadata["reusable"] = False
        self._atomic_write_json(metadata_path, metadata)
        return metadata_path

    def load_checkpoint(self) -> Checkpoint:
        if not self.checkpoint_path.exists():
            return Checkpoint(None, (), None, None, 0, 0, 0)
        try:
            document = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
            return self._validate_checkpoint(document)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            if isinstance(exc, StorageError):
                raise
            raise StorageError(f"invalid checkpoint {self.checkpoint_path}: {exc}") from exc

    def mark_page_completed(self, page: int) -> Checkpoint:
        if isinstance(page, bool) or not isinstance(page, int) or page < 0:
            raise StorageError("completed page must be a non-negative integer")
        current = self.load_checkpoint()
        checkpoint = Checkpoint(
            page,
            current.completed_detail_urls,
            self._timestamp(),
            current.resume,
            current.requests_used,
            current.pages_used,
            current.records_used,
            current.list_cursors,
        )
        self._write_checkpoint(checkpoint)
        return checkpoint

    def mark_detail_completed(self, url: str) -> Checkpoint:
        if not isinstance(url, str) or not url:
            raise StorageError("completed detail URL must be a non-empty string")
        current = self.load_checkpoint()
        urls = list(current.completed_detail_urls)
        if url not in urls:
            urls.append(url)
        checkpoint = Checkpoint(
            current.last_completed_page,
            tuple(urls),
            self._timestamp(),
            current.resume,
            current.requests_used,
            current.pages_used,
            current.records_used,
            current.list_cursors,
        )
        self._write_checkpoint(checkpoint)
        return checkpoint

    def initialize_checkpoint(self, resume: dict[str, Any]) -> Checkpoint:
        if self.checkpoint_path.exists():
            current = self.load_checkpoint()
            if current.resume != resume:
                raise StorageError("checkpoint resume metadata does not match the run")
            return current
        checkpoint = Checkpoint(None, (), self._timestamp(), dict(resume), 0, 0, 0)
        self._write_checkpoint(checkpoint)
        return checkpoint

    def mark_list_cursor(self, key: str, offset: int) -> Checkpoint:
        if not isinstance(key, str) or not key or len(key) > 1024:
            raise StorageError("list cursor key must be a bounded non-empty string")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise StorageError("list cursor offset must be a non-negative integer")
        current = self.load_checkpoint()
        cursors = dict(current.list_cursors or {})
        if offset < cursors.get(key, 0):
            raise StorageError("list cursor cannot decrease")
        cursors[key] = offset
        checkpoint = Checkpoint(
            current.last_completed_page,
            current.completed_detail_urls,
            self._timestamp(),
            current.resume,
            current.requests_used,
            current.pages_used,
            current.records_used,
            cursors,
        )
        self._write_checkpoint(checkpoint)
        return checkpoint

    def mark_usage(self, *, requests: int, pages: int, records: int) -> Checkpoint:
        values = {"requests": requests, "pages": pages, "records": records}
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values.values()):
            raise StorageError("checkpoint usage counters must be non-negative integers")
        current = self.load_checkpoint()
        if (
            requests < current.requests_used
            or pages < current.pages_used
            or records < current.records_used
        ):
            raise StorageError("checkpoint usage counters cannot decrease")
        checkpoint = Checkpoint(
            current.last_completed_page,
            current.completed_detail_urls,
            self._timestamp(),
            current.resume,
            requests,
            pages,
            records,
            current.list_cursors,
        )
        self._write_checkpoint(checkpoint)
        return checkpoint

    def _metadata(
        self,
        *,
        source: SourceDefinition,
        url: str,
        final_url: str,
        status: int,
        content_type: str,
        content_hash: str,
    ) -> dict[str, Any]:
        return {
            "source_id": source.source_id,
            "run_id": self.run_id,
            "url": url,
            "final_url": final_url,
            "status": status,
            "content_type": content_type,
            "fetched_at": self._timestamp(),
            "content_hash": content_hash,
            "parser_version": source.parser_version,
            "from_cache": False,
        }

    def _validate_success_metadata(
        self, metadata: Any, source_id: str, url: str
    ) -> None:
        if not isinstance(metadata, dict) or not _METADATA_FIELDS <= metadata.keys():
            raise SnapshotCorrupt(f"snapshot metadata fields are invalid for {url}")
        string_fields = (
            "source_id",
            "run_id",
            "url",
            "final_url",
            "content_type",
            "fetched_at",
            "content_hash",
            "parser_version",
        )
        if any(not isinstance(metadata[field], str) for field in string_fields):
            raise SnapshotCorrupt(f"snapshot metadata types are invalid for {url}")
        if (
            isinstance(metadata["status"], bool)
            or not isinstance(metadata["status"], int)
            or not isinstance(metadata["from_cache"], bool)
        ):
            raise SnapshotCorrupt(f"snapshot metadata types are invalid for {url}")
        if (
            metadata["source_id"] != source_id
            or metadata["run_id"] != self.run_id
            or metadata["url"] != url
        ):
            raise SnapshotCorrupt(f"snapshot metadata identity mismatch for {url}")
        if len(metadata["content_hash"]) != 64:
            raise SnapshotCorrupt(f"snapshot content hash is invalid for {url}")

    def _validate_checkpoint(self, document: Any) -> Checkpoint:
        if not isinstance(document, dict):
            raise ValueError("checkpoint must be a JSON object")
        required = {"last_completed_page", "completed_detail_urls", "updated_at"}
        allowed = required | {"resume", "usage", "list_cursors"}
        if not required <= document.keys() or not document.keys() <= allowed:
            raise ValueError("checkpoint fields do not match the required schema")
        page = document["last_completed_page"]
        if page is not None and (
            isinstance(page, bool) or not isinstance(page, int) or page < 0
        ):
            raise ValueError("last_completed_page must be null or a non-negative integer")
        urls = document["completed_detail_urls"]
        if not isinstance(urls, list) or any(
            not isinstance(url, str) or not url for url in urls
        ):
            raise ValueError("completed_detail_urls must be a list of non-empty strings")
        if len(urls) != len(dict.fromkeys(urls)):
            raise ValueError("completed_detail_urls must not contain duplicates")
        updated_at = document["updated_at"]
        if not isinstance(updated_at, str) or not updated_at:
            raise ValueError("updated_at must be a non-empty timestamp string")
        parsed_time = datetime.fromisoformat(updated_at)
        if parsed_time.tzinfo is None:
            raise ValueError("updated_at must include a timezone")
        resume = document.get("resume")
        if resume is not None:
            required_resume = {
                "run_id",
                "source_ids",
                "max_records",
                "max_pages",
                "max_requests",
                "manifest_path",
            }
            allowed_resume = required_resume | {
                "record_offset",
                "input_file_path",
                "authorization_note",
                "authorization_reference",
                "authorization_valid_until",
                "authorization_access_method",
                "authorization_scope_sha256",
                "authorization_manifest_sha256",
                "input_file_sha256",
            }
            if (
                not isinstance(resume, dict)
                or not required_resume <= set(resume)
                or not set(resume) <= allowed_resume
            ):
                raise ValueError("checkpoint resume metadata is invalid")
            if (
                resume["run_id"] != self.run_id
                or not isinstance(resume["source_ids"], list)
                or not resume["source_ids"]
                or any(
                    not isinstance(source_id, str) or not source_id
                    for source_id in resume["source_ids"]
                )
                or isinstance(resume["max_records"], bool)
                or not isinstance(resume["max_records"], int)
                or resume["max_records"] < 1
                or isinstance(resume["max_pages"], bool)
                or not isinstance(resume["max_pages"], int)
                or resume["max_pages"] < 1
                or isinstance(resume["max_requests"], bool)
                or not isinstance(resume["max_requests"], int)
                or resume["max_requests"] < 1
                or (
                    "record_offset" in resume
                    and (
                        isinstance(resume["record_offset"], bool)
                        or not isinstance(resume["record_offset"], int)
                        or resume["record_offset"] < 0
                    )
                )
                or (
                    resume["manifest_path"] is not None
                    and not isinstance(resume["manifest_path"], str)
                )
                or any(
                    key in resume
                    and resume[key] is not None
                    and not isinstance(resume[key], str)
                    for key in (
                        "input_file_path",
                        "authorization_note",
                        "authorization_reference",
                        "authorization_valid_until",
                        "authorization_access_method",
                        "authorization_scope_sha256",
                        "authorization_manifest_sha256",
                        "input_file_sha256",
                    )
                )
            ):
                raise ValueError("checkpoint resume metadata is invalid")
        usage = document.get("usage", {"requests": 0, "pages": 0, "records": 0})
        if (
            not isinstance(usage, dict)
            or set(usage) != {"requests", "pages", "records"}
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in usage.values()
            )
        ):
            raise ValueError("checkpoint usage counters are invalid")
        list_cursors = document.get("list_cursors", {})
        if (
            not isinstance(list_cursors, dict)
            or len(list_cursors) > 1000
            or any(
                not isinstance(key, str)
                or not key
                or len(key) > 1024
                or isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                for key, value in list_cursors.items()
            )
        ):
            raise ValueError("checkpoint list cursors are invalid")
        return Checkpoint(
            page,
            tuple(urls),
            updated_at,
            resume,
            usage["requests"],
            usage["pages"],
            usage["records"],
            dict(list_cursors),
        )

    def _write_checkpoint(self, checkpoint: Checkpoint) -> None:
        document = {
            "last_completed_page": checkpoint.last_completed_page,
            "completed_detail_urls": list(checkpoint.completed_detail_urls),
            "updated_at": checkpoint.updated_at,
        }
        if checkpoint.resume is not None:
            document["resume"] = checkpoint.resume
        document["usage"] = {
            "requests": checkpoint.requests_used,
            "pages": checkpoint.pages_used,
            "records": checkpoint.records_used,
        }
        document["list_cursors"] = dict(checkpoint.list_cursors or {})
        self._atomic_write_json(self.checkpoint_path, document)

    def _timestamp(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime):
            raise StorageError("storage clock must return datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise StorageError("storage clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc).isoformat()

    def _atomic_write_json(self, path: Path, value: dict[str, Any]) -> None:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self._atomic_write_bytes(path, payload)

    def _atomic_write_bytes(self, path: Path, content: bytes) -> None:
        from src.job_collection.security import (
            ensure_secure_directory,
            secure_atomic_write,
        )

        checked_path = self.resolve_path(path.relative_to(self.run_root))
        ensure_secure_directory(self.collections_root)
        secure_atomic_write(checked_path, content, root=self.run_root)
