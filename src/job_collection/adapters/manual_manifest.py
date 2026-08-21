from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO, Iterator
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.competition_rules import assess_gate
from src.job_collection.adapters.base import AdapterRecordError
from src.job_collection.family_classifier import classify_job_family
from src.job_collection.models import SourceDefinition, UnifiedJobRecord
from src.job_collection.normalizer import NormalizationError, normalize_job_record
from src.job_collection.source_registry import (
    CollectionBlocked,
    SourceRegistry,
    SourceRegistryError,
)
from src.job_data_service import (
    JOB_FAMILY_NAMES,
    assess_job_quality,
    prepare_job_record,
)


_MAX_PATH_DECODE_LAYERS = 4
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_CONTENT_TYPE_CHARSET = re.compile(
    r"(?:^|;)\s*charset\s*=\s*['\"]?\s*([A-Za-z0-9._-]+)", re.IGNORECASE
)
_ALLOWED_HTML_ENCODINGS = {
    "utf-8": "utf-8-sig",
    "utf8": "utf-8-sig",
    "gb18030": "gb18030",
    "gbk": "gbk",
}

# Manual inputs are intentionally small enough for review and local inspection.
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_LINE_BYTES = 512 * 1024
MAX_JSON_DEPTH = 24
MAX_HTML_BYTES = 2 * 1024 * 1024


class _HeadCharsetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.in_head = False
        self.charsets: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag == "head":
            self.in_head = True
        elif normalized_tag == "body":
            self.in_head = False
        elif normalized_tag == "meta" and self.in_head:
            self._read_meta(attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "meta" and self.in_head:
            self._read_meta(attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "head":
            self.in_head = False

    def _read_meta(self, attrs: list[tuple[str, str | None]]) -> None:
        normalized = [(name.casefold(), (value or "").strip()) for name, value in attrs]
        direct = [value.casefold() for name, value in normalized if name == "charset"]
        if direct:
            self.charsets.update(value for value in direct if value)
            return

        http_equiv = [
            value.casefold() for name, value in normalized if name == "http-equiv"
        ]
        if "content-type" not in http_equiv:
            return
        for name, value in normalized:
            if name != "content":
                continue
            match = _CONTENT_TYPE_CHARSET.search(value)
            if match:
                self.charsets.add(match.group(1).casefold())


class _DuplicateJSONKey(ValueError):
    pass


@dataclass(frozen=True)
class _ParsedEntry:
    line_number: int
    entry: "_ManifestEntry"
    manifest_line_hash: str
    submitted_description_bytes: bytes | None


class ManualManifestError(AdapterRecordError):
    """A manual manifest or one of its local inputs is unsafe or unusable."""


class _ManifestEntry(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    source_name: str = Field(min_length=1, max_length=255)
    source_url: str = Field(min_length=1)
    company_name: str = Field(min_length=1, max_length=255)
    collection_authorization_note: str = Field(min_length=1, max_length=2000)
    exported_html_path: str | None = None
    job_description_raw: str | None = None
    job_title_raw: str | None = Field(default=None, max_length=255)
    source_record_id: str | None = None
    job_family_id: str | None = Field(default=None, max_length=80)
    industry: str | None = None
    region: str | None = None
    education_requirement: str | None = None
    experience_requirement: str | None = None
    salary_range: str | None = None
    published_at: str | None = None
    published_at_evidence: str | None = None
    published_at_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    documentation_only: bool = False

    @model_validator(mode="after")
    def validate_content_choice(self) -> "_ManifestEntry":
        choices = (
            bool(self.exported_html_path and self.exported_html_path.strip()),
            bool(self.job_description_raw and self.job_description_raw.strip()),
        )
        if sum(choices) != 1:
            raise ValueError(
                "exactly one usable exported_html_path or job_description_raw is required"
            )
        return self


class ManualManifestAdapter:
    """Convert reviewed local company exports into unified pipeline records."""

    source_id = "company_official_manifest"

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
            and source.collection_mode == "manual_url_manifest"
            and source.parser_name == "company_manifest"
        ):
            raise ValueError(
                "manual manifest source must be enabled, manual_only, and use "
                "manual_url_manifest"
            )
        self.source = source
        self.registry = registry

    def build_list_request(self, *_args: object, **_kwargs: object) -> None:
        raise CollectionBlocked(
            "manual_only source cannot build or perform a network request"
        )

    def build_detail_url(self, *_args: object, **_kwargs: object) -> None:
        raise CollectionBlocked(
            "manual_only source cannot build or perform a network request"
        )

    def validate_redirect(self, *_args: object, **_kwargs: object) -> None:
        raise CollectionBlocked("manual_only source cannot follow a network redirect")

    def load_manifest(
        self,
        manifest_path: str | Path,
        *,
        run_id: str,
        collected_at: datetime,
        max_records: int | None = None,
    ) -> tuple[UnifiedJobRecord, ...]:
        limit = self.source.max_records if max_records is None else max_records
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= self.source.max_records
        ):
            raise ValueError(
                f"max_records must be between 1 and {self.source.max_records}"
            )
        path = self._manifest_file(manifest_path)
        records: list[UnifiedJobRecord] = []
        entries = self._iter_entries(path)
        while len(records) < limit:
            try:
                parsed_entry = next(entries)
            except StopIteration:
                break
            if parsed_entry.entry.documentation_only:
                continue
            records.append(
                self._process_entry(
                    parsed_entry,
                    manifest_path=path,
                    run_id=run_id,
                    collected_at=collected_at,
                )
            )
        return tuple(records)

    parse_manifest = load_manifest

    @staticmethod
    def _manifest_file(manifest_path: str | Path) -> Path:
        path = Path(manifest_path).absolute()
        if path.suffix.lower() != ".jsonl":
            raise ManualManifestError("manifest must be a local .jsonl file")
        return path

    def _iter_entries(self, path: Path) -> Iterator[_ParsedEntry]:
        with self._open_verified_file(
            path,
            trusted_root=path.parent,
            label="manifest",
        ) as stream:
            size = os.fstat(stream.fileno()).st_size
            if size > MAX_MANIFEST_BYTES:
                raise ManualManifestError(
                    f"manifest exceeds {MAX_MANIFEST_BYTES} bytes"
                )

            record_count = 0
            physical_line = 0
            while True:
                raw_line = stream.readline(MAX_MANIFEST_LINE_BYTES + 1)
                if not raw_line:
                    break
                physical_line += 1
                if len(raw_line) > MAX_MANIFEST_LINE_BYTES:
                    raise ManualManifestError(
                        f"manifest line {physical_line} exceeds "
                        f"{MAX_MANIFEST_LINE_BYTES} bytes"
                    )
                if not raw_line.strip():
                    continue
                record_count += 1
                if record_count > self.source.max_records:
                    raise ManualManifestError(
                        f"manifest exceeds source max_records={self.source.max_records}"
                    )
                yield self._parse_manifest_line(raw_line, physical_line)

    @classmethod
    def _parse_manifest_line(cls, raw_line: bytes, line_number: int) -> _ParsedEntry:
        encoding = "utf-8-sig" if line_number == 1 else "utf-8"
        try:
            line = raw_line.decode(encoding)
        except UnicodeDecodeError as exc:
            raise ManualManifestError(
                f"manifest line {line_number} is not valid UTF-8"
            ) from exc
        cls._validate_json_depth(line, line_number)
        try:
            value = json.loads(
                line,
                object_pairs_hook=cls._object_without_duplicate_keys,
                parse_constant=cls._reject_json_constant,
            )
        except _DuplicateJSONKey as exc:
            raise ManualManifestError(
                f"manifest line {line_number} contains duplicate JSON key: {exc}"
            ) from exc
        except (json.JSONDecodeError, RecursionError, ValueError) as exc:
            message = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
            raise ManualManifestError(
                f"manifest line {line_number} is invalid JSON: {message}"
            ) from exc
        if not isinstance(value, dict):
            raise ManualManifestError(
                f"manifest line {line_number} must be a JSON object"
            )

        submitted = value.get("job_description_raw")
        submitted_bytes = (
            submitted.encode("utf-8") if isinstance(submitted, str) else None
        )
        try:
            canonical = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            entry = _ManifestEntry.model_validate(value)
        except (TypeError, ValueError, ValidationError) as exc:
            if isinstance(exc, ValidationError):
                fields = ", ".join(
                    ".".join(str(part) for part in error["loc"])
                    for error in exc.errors()
                )
                message = "; ".join(str(error["msg"]) for error in exc.errors())
            else:
                fields = "JSON value"
                message = str(exc)
            raise ManualManifestError(
                f"manifest line {line_number} is invalid ({fields}): {message}"
            ) from exc
        return _ParsedEntry(
            line_number=line_number,
            entry=entry,
            manifest_line_hash=hashlib.sha256(canonical).hexdigest(),
            submitted_description_bytes=submitted_bytes,
        )

    @staticmethod
    def _object_without_duplicate_keys(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise _DuplicateJSONKey(key)
            value[key] = item
        return value

    @staticmethod
    def _reject_json_constant(value: str) -> object:
        raise ValueError(f"non-finite JSON constant {value}")

    @staticmethod
    def _validate_json_depth(line: str, line_number: int) -> None:
        depth = 0
        in_string = False
        escaped = False
        for character in line:
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
                if depth > MAX_JSON_DEPTH:
                    raise ManualManifestError(
                        f"manifest line {line_number} exceeds JSON nesting depth "
                        f"{MAX_JSON_DEPTH}"
                    )
            elif character in "]}":
                depth -= 1

    def _process_entry(
        self,
        parsed_entry: _ParsedEntry,
        *,
        manifest_path: Path,
        run_id: str,
        collected_at: datetime,
    ) -> UnifiedJobRecord:
        entry = parsed_entry.entry
        line_number = parsed_entry.line_number
        if entry.exported_html_path:
            content_path = self._resolve_content_path(
                manifest_path, entry.exported_html_path
            )
            content = self._read_bounded_file(
                content_path,
                trusted_root=manifest_path.parent,
                max_bytes=MAX_HTML_BYTES,
                label="HTML",
            )
            raw, page_title = self._parse_html(content, entry, line_number)
            content_input_type = "exported_html_path"
            content_input_path: str | None = entry.exported_html_path
        else:
            description = entry.job_description_raw or ""
            content = parsed_entry.submitted_description_bytes or b""
            raw = self._entry_fields(entry)
            raw["job_description_raw"] = description
            page_title = None
            content_input_type = "job_description_raw"
            content_input_path = None

        title = str(raw.get("job_title_raw") or "").strip()
        description = str(raw.get("job_description_raw") or "").strip()
        if not title:
            raise ManualManifestError(
                f"manifest line {line_number} has no job title in the reviewed input"
            )
        if not description:
            raise ManualManifestError(
                f"manifest line {line_number} has no job description in the reviewed input"
            )

        if entry.job_family_id and entry.job_family_id not in JOB_FAMILY_NAMES:
            raise ManualManifestError(
                f"manifest line {line_number} job_family_id must be a canonical "
                "job_family_id from the configured 22 families"
            )
        classification = classify_job_family(title, description)
        if (
            classification.status == "auto"
            and entry.job_family_id
            and entry.job_family_id != classification.family_code
        ):
            raise ManualManifestError(
                f"manifest line {line_number} explicit job_family_id "
                f"{entry.job_family_id} conflicts with automatic classifier "
                f"{classification.family_code}"
            )
        family_id = classification.family_code or entry.job_family_id
        if not family_id:
            raise ManualManifestError(
                f"manifest line {line_number} requires job_family_id because "
                "classification needs review"
            )
        record_source, identity_origin = self._record_source(entry)
        original_source_record_id = raw.get("source_record_id")
        if original_source_record_id not in (None, ""):
            original_id = str(original_source_record_id).strip()
            identity_digest = hashlib.sha256(
                f"{identity_origin}\0{original_id}".encode("utf-8")
            ).hexdigest()
            raw["source_record_id"] = f"manual-{identity_digest}"
        else:
            original_id = None

        content_hash = hashlib.sha256(content).hexdigest()
        raw["job_family_id"] = family_id
        raw.update(
            {
                "source_url": entry.source_url,
                "company_name": entry.company_name,
                "collection_authorization_note": entry.collection_authorization_note,
                "content_input_type": content_input_type,
                "exported_html_path": content_input_path,
                "manifest_line_number": line_number,
                "manifest_filename": manifest_path.name,
                "manifest_line_hash": parsed_entry.manifest_line_hash,
                "manifest_line_hash_algorithm": "sha256-canonical-json-v1",
                "original_content_hash": content_hash,
                "original_source_record_id": original_id,
                "source_identity_origin": identity_origin,
                "registry_compliance_note": self.source.compliance_note,
                "family_classification": asdict(classification),
            }
        )

        try:
            record = normalize_job_record(
                raw,
                source=record_source,
                run_id=run_id,
                snapshot_metadata={
                    "snapshot_hash": content_hash,
                    "response_status": 200,
                    "page_title": page_title,
                    "observed_at": collected_at,
                },
                collected_at=collected_at,
            )
        except NormalizationError as exc:
            raise ManualManifestError(
                f"manifest line {line_number} failed normalization: {exc}"
            ) from exc
        return self._apply_quality_pipeline(record, collected_at)

    @staticmethod
    def _entry_fields(entry: _ManifestEntry) -> dict[str, object]:
        fields = entry.model_dump(exclude_none=True)
        for key in (
            "source_name",
            "source_url",
            "collection_authorization_note",
            "exported_html_path",
            "documentation_only",
        ):
            fields.pop(key, None)
        return fields

    def _record_source(self, entry: _ManifestEntry) -> tuple[SourceDefinition, str]:
        if any(
            ord(character) < 32 or ord(character) == 127
            for character in entry.source_url
        ):
            raise ManualManifestError("source_url contains control characters")
        try:
            parsed = urlsplit(entry.source_url)
            host = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise ManualManifestError("source_url is invalid") from exc
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or not host.isascii()
        ):
            raise ManualManifestError(
                "source_url must be a credential-free HTTP(S) URL"
            )
        authority = host.lower()
        if ":" in authority:
            authority = f"[{authority}]"
        default_port = 443 if parsed.scheme.lower() == "https" else 80
        if port is not None and port != default_port:
            authority = f"{authority}:{port}"
        origin = f"{parsed.scheme.lower()}://{authority}"
        payload = self.source.model_dump()
        payload.update(
            {
                "source_name": entry.source_name,
                "base_url": origin,
                "allowed_paths": ["/"],
                "compliance_note": entry.collection_authorization_note,
            }
        )
        try:
            return SourceDefinition.model_validate(payload), origin
        except ValidationError as exc:
            raise ManualManifestError(
                "source_url cannot define a safe local record scope"
            ) from exc

    @classmethod
    def _resolve_content_path(cls, manifest_path: Path, value: str) -> Path:
        cls._validate_relative_path(value)
        root = manifest_path.parent.absolute()
        platform_path = value.replace("\\", os.sep).replace("/", os.sep)
        candidate = (root / platform_path).absolute()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ManualManifestError(
                f"exported_html_path escapes the manifest directory: {value}"
            ) from exc
        if candidate.suffix.lower() not in {".html", ".htm"}:
            raise ManualManifestError(
                f"exported_html_path must name a local HTML file: {value}"
            )
        return candidate

    @classmethod
    @contextmanager
    def _open_verified_file(
        cls,
        path: Path,
        *,
        trusted_root: Path,
        label: str,
    ) -> Iterator[BinaryIO]:
        candidate = path.absolute()
        root = trusted_root.absolute()
        try:
            relative = candidate.relative_to(root)
        except ValueError as exc:
            raise ManualManifestError(
                f"{label} file is outside its trusted directory"
            ) from exc

        before = cls._inspect_file_components(root, relative, label)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(candidate, flags)
        except OSError as exc:
            raise ManualManifestError(
                f"{label} file cannot be opened securely"
            ) from exc
        try:
            opened = os.fstat(descriptor)
            after = cls._inspect_file_components(root, relative, label)
            if not stat.S_ISREG(opened.st_mode):
                raise ManualManifestError(f"{label} input must be a regular file")
            if not cls._same_file_identity(
                before, opened
            ) or not cls._same_file_identity(opened, after):
                raise ManualManifestError(f"{label} file changed while opening")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                yield stream
        finally:
            os.close(descriptor)

    @classmethod
    def _inspect_file_components(
        cls, root: Path, relative: Path, label: str
    ) -> os.stat_result:
        current = root
        result: os.stat_result | None = None
        try:
            for part in relative.parts:
                current /= part
                result = os.stat(current, follow_symlinks=False)
                attributes = getattr(result, "st_file_attributes", 0)
                if stat.S_ISLNK(result.st_mode) or (
                    attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                ):
                    raise ManualManifestError(
                        f"{label} path contains a symlink or reparse point"
                    )
        except ManualManifestError:
            raise
        except OSError as exc:
            raise ManualManifestError(f"{label} file is not readable") from exc
        if result is None or not stat.S_ISREG(result.st_mode):
            raise ManualManifestError(f"{label} input must be a regular file")
        return result

    @staticmethod
    def _same_file_identity(first: os.stat_result, second: os.stat_result) -> bool:
        if first.st_ino and second.st_ino:
            return first.st_dev == second.st_dev and first.st_ino == second.st_ino
        return (
            first.st_dev,
            first.st_ctime_ns,
            first.st_size,
        ) == (
            second.st_dev,
            second.st_ctime_ns,
            second.st_size,
        )

    @classmethod
    def _read_bounded_file(
        cls,
        path: Path,
        *,
        trusted_root: Path,
        max_bytes: int,
        label: str,
    ) -> bytes:
        with cls._open_verified_file(
            path,
            trusted_root=trusted_root,
            label=label,
        ) as stream:
            if os.fstat(stream.fileno()).st_size > max_bytes:
                raise ManualManifestError(f"{label} exceeds {max_bytes} bytes")
            content = stream.read(max_bytes + 1)
            if len(content) > max_bytes:
                raise ManualManifestError(f"{label} exceeds {max_bytes} bytes")
            return content

    @classmethod
    def _validate_relative_path(cls, value: str) -> None:
        if not value or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise ManualManifestError(
                "exported_html_path is empty or contains control characters"
            )

        current = value
        for _ in range(_MAX_PATH_DECODE_LAYERS + 1):
            cls._validate_path_layer(current)
            cls._validate_percent_escapes(current)
            try:
                decoded = unquote(current, errors="strict")
            except UnicodeDecodeError as exc:
                raise ManualManifestError(
                    "exported_html_path contains invalid UTF-8 encoding"
                ) from exc
            if decoded == current:
                return
            current = decoded
        raise ManualManifestError(
            "exported_html_path exceeds the percent-decoding safety limit"
        )

    @staticmethod
    def _validate_path_layer(value: str) -> None:
        parsed = urlsplit(value)
        windows_path = PureWindowsPath(value)
        posix_path = PurePosixPath(value)
        segments = value.replace("\\", "/").split("/")
        if (
            parsed.scheme
            or value.startswith(("//", "\\\\"))
            or windows_path.drive
            or windows_path.root
            or posix_path.is_absolute()
            or ".." in segments
        ):
            raise ManualManifestError(
                f"exported_html_path must stay relative to the manifest: {value}"
            )

    @staticmethod
    def _validate_percent_escapes(value: str) -> None:
        index = 0
        while index < len(value):
            if value[index] == "%":
                if _PERCENT_ESCAPE.fullmatch(value[index : index + 3]) is None:
                    raise ManualManifestError(
                        "exported_html_path contains malformed percent encoding"
                    )
                index += 3
                continue
            index += 1

    @classmethod
    def _parse_html(
        cls,
        content: bytes,
        entry: _ManifestEntry,
        line_number: int,
    ) -> tuple[dict[str, object], str | None]:
        if not content:
            raise ManualManifestError(
                f"manifest line {line_number} exported_html_path is empty"
            )
        text = cls._decode_html(content, line_number)
        try:
            soup = BeautifulSoup(text, "html.parser")
            description_node = soup.select_one(
                "[data-job-description], [data-job-field='job-description']"
            )
            if description_node is None:
                raise ManualManifestError(
                    f"manifest line {line_number} exported HTML lacks a job description"
                )
            description = description_node.get_text("\n", strip=True)
            title = entry.job_title_raw or cls._field(soup, "job-title")
            if not title:
                heading = soup.select_one("main h1, article h1, h1")
                title = heading.get_text(" ", strip=True) if heading else None

            raw = cls._entry_fields(entry)
            raw.update(
                {
                    "job_title_raw": title,
                    "job_description_raw": description,
                    "source_record_id": entry.source_record_id
                    or cls._source_record_id(soup),
                    "industry": entry.industry or cls._field(soup, "industry"),
                    "region": entry.region or cls._field(soup, "region"),
                    "education_requirement": entry.education_requirement
                    or cls._field(soup, "education"),
                    "experience_requirement": entry.experience_requirement
                    or cls._field(soup, "experience"),
                    "salary_range": entry.salary_range or cls._field(soup, "salary"),
                }
            )
            page_title = soup.title.get_text(" ", strip=True) if soup.title else None
            return raw, page_title
        except ManualManifestError:
            raise
        except Exception as exc:
            raise ManualManifestError(
                f"manifest line {line_number} HTML parser failed"
            ) from exc

    @staticmethod
    def _decode_html(content: bytes, line_number: int) -> str:
        if content.startswith(b"\xef\xbb\xbf"):
            encoding = "utf-8-sig"
        else:
            parser = _HeadCharsetParser()
            parser.feed(content.decode("latin-1"))
            parser.close()
            declared = parser.charsets
            if len(declared) > 1:
                raise ManualManifestError(
                    f"manifest line {line_number} declares conflicting HTML charsets"
                )
            charset = next(iter(declared), "utf-8")
            try:
                encoding = _ALLOWED_HTML_ENCODINGS[charset]
            except KeyError as exc:
                raise ManualManifestError(
                    f"manifest line {line_number} uses unsupported HTML charset {charset}"
                ) from exc
        try:
            return content.decode(encoding, errors="strict")
        except UnicodeDecodeError as exc:
            raise ManualManifestError(
                f"manifest line {line_number} HTML is invalid for declared charset"
            ) from exc

    @staticmethod
    def _field(soup: BeautifulSoup, name: str) -> str | None:
        node = soup.select_one(f"[data-job-field='{name}']")
        if node is None:
            return None
        value = node.get_text(" ", strip=True)
        return value or None

    @staticmethod
    def _source_record_id(soup: BeautifulSoup) -> str | None:
        node = soup.select_one("[data-source-record-id]")
        if node is None:
            return None
        value = node.get("data-source-record-id")
        return value.strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _apply_quality_pipeline(
        record: UnifiedJobRecord, collected_at: datetime
    ) -> UnifiedJobRecord:
        serialized = record.model_dump(mode="json")
        prepared = prepare_job_record(serialized)
        findings = [asdict(finding) for finding in assess_job_quality(prepared)]
        has_capability_evidence = bool(
            prepared.get("skills") or prepared.get("responsibilities")
        )
        gate_payload = dict(serialized)
        gate_payload.update(
            {
                "quality_score": prepared["quality_score"],
                "has_capability_evidence": has_capability_evidence,
            }
        )
        now = collected_at.replace(tzinfo=None) if collected_at.tzinfo else collected_at
        gate = assess_gate(gate_payload, now=now)
        issue_codes = {
            str(finding.get("code"))
            for finding in record.normalization_findings
            if finding.get("code")
        }
        issue_codes.update(finding["code"] for finding in findings)
        issue_codes.update(gate.issue_codes)
        severities = [record.normalization_status, gate.status]
        severities.extend(
            str(finding.get("severity") or "review") for finding in findings
        )
        status_rank = {
            "valid": 0,
            "duplicate": 0,
            "review": 1,
            "quarantine": 2,
            "quarantined": 2,
        }
        highest = max((status_rank.get(status, 1) for status in severities), default=0)
        combined_status = ("valid", "review", "quarantined")[highest]
        adapter_extra = {
            **record.adapter_extra,
            "quality_findings": findings,
            "quality_gate": {
                "status": combined_status,
                "issue_codes": sorted(issue_codes),
                "quality_score": prepared["quality_score"],
                "has_capability_evidence": has_capability_evidence,
                "component_statuses": {
                    "normalizer": record.normalization_status,
                    "quality": "review" if findings else "valid",
                    "hard_gate": gate.status,
                },
            },
        }
        return record.model_copy(update={"adapter_extra": adapter_extra})


__all__ = ["ManualManifestAdapter", "ManualManifestError"]
