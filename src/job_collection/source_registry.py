from __future__ import annotations

import json
import posixpath
import string
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urljoin, urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from model_class.knowledge_base import JobSource
from src.job_collection.models import SourceDefinition


_MAX_PATH_DECODE_LAYERS = 4
_HEX_DIGITS = frozenset(string.hexdigits)


class SourceRegistryError(ValueError):
    """The reviewed source configuration is absent or invalid."""


class CollectionBlocked(PermissionError):
    """Automatic collection is not approved for the requested source."""


class URLScopeError(CollectionBlocked):
    """A URL resolves outside the reviewed source scope."""


class _RegistryDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[SourceDefinition] = Field(min_length=1)


def _effective_port(scheme: str, port: int | None) -> int:
    if port is not None:
        return port
    return 443 if scheme == "https" else 80


def _normalized_path_layer(path: str) -> str:
    if "\\" in path:
        raise URLScopeError("URL path contains an invalid separator")
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        raise URLScopeError("URL path contains control characters")
    index = 0
    while index < len(path):
        if path[index] == "%":
            if (
                index + 2 >= len(path)
                or path[index + 1] not in _HEX_DIGITS
                or path[index + 2] not in _HEX_DIGITS
            ):
                raise URLScopeError("URL path contains malformed percent encoding")
            index += 3
            continue
        index += 1
    normalized = posixpath.normpath(path or "/")
    return normalized if normalized.startswith("/") else f"/{normalized}"


def _normalized_path_layers(path: str) -> tuple[str, ...]:
    current = path
    layers: list[str] = []
    for _ in range(_MAX_PATH_DECODE_LAYERS + 1):
        layers.append(_normalized_path_layer(current))
        try:
            decoded = unquote(current, errors="strict")
        except UnicodeDecodeError as exc:
            raise URLScopeError("URL path contains invalid UTF-8 encoding") from exc
        if decoded == current:
            return tuple(layers)
        current = decoded
    raise URLScopeError("URL path exceeds the percent-decoding limit")


def _path_is_within(path: str, prefix: str) -> bool:
    normalized_prefix = _normalized_path_layer(prefix).rstrip("/") or "/"
    path_layers = _normalized_path_layers(path)
    if normalized_prefix == "/":
        return True
    return all(
        normalized_path == normalized_prefix
        or normalized_path.startswith(f"{normalized_prefix}/")
        for normalized_path in path_layers
    )


class SourceRegistry:
    def __init__(self, definitions: Iterable[SourceDefinition]):
        ordered = tuple(definitions)
        by_id: dict[str, SourceDefinition] = {}
        for definition in ordered:
            if definition.source_id in by_id:
                raise SourceRegistryError(
                    f"duplicate source_id: {definition.source_id}"
                )
            by_id[definition.source_id] = definition
        if not ordered:
            raise SourceRegistryError("source registry cannot be empty")
        self._definitions = ordered
        self._by_id = by_id

    @property
    def definitions(self) -> tuple[SourceDefinition, ...]:
        return self._definitions

    @classmethod
    def load(cls, path: str | Path) -> "SourceRegistry":
        registry_path = Path(path)
        try:
            raw = registry_path.read_text(encoding="utf-8")
            document = _RegistryDocument.model_validate_json(raw)
            return cls(document.sources)
        except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
            raise SourceRegistryError(
                f"invalid source registry {registry_path}: {exc}"
            ) from exc

    def get(self, source_id: str) -> SourceDefinition:
        try:
            return self._by_id[source_id]
        except KeyError as exc:
            raise SourceRegistryError(f"unknown source_id: {source_id}") from exc

    def require_automatic(self, source_id: str) -> SourceDefinition:
        try:
            definition = self.get(source_id)
        except SourceRegistryError as exc:
            raise CollectionBlocked(str(exc)) from exc
        if not (
            definition.enabled
            and definition.compliance_status == "approved"
            and definition.collection_mode in {"public_html", "public_json"}
            and definition.market_scope == "china"
        ):
            raise CollectionBlocked(
                f"automatic collection blocked for {source_id}: "
                f"enabled={definition.enabled}, "
                f"status={definition.compliance_status}, "
                f"mode={definition.collection_mode}, "
                f"market_scope={definition.market_scope}"
            )
        return definition

    def validate_url(self, source_id: str, url: str) -> str:
        try:
            definition = self.get(source_id)
        except SourceRegistryError as exc:
            raise URLScopeError(str(exc)) from exc
        if any(ord(character) < 32 or ord(character) == 127 for character in url):
            raise URLScopeError(f"URL contains control characters for {source_id}")
        try:
            resolved = urljoin(f"{definition.base_url}/", url)
            base = urlsplit(definition.base_url)
            target = urlsplit(resolved)
            base_host = base.hostname or ""
            target_host = target.hostname or ""
        except ValueError as exc:
            raise URLScopeError(f"invalid URL for {source_id}") from exc

        try:
            base_port = _effective_port(base.scheme.lower(), base.port)
            target_port = _effective_port(target.scheme.lower(), target.port)
        except ValueError as exc:
            raise URLScopeError(f"invalid URL port for {source_id}") from exc

        if target.username is not None or target.password is not None:
            raise URLScopeError(f"URL credentials are outside scope for {source_id}")
        if not target_host.isascii():
            raise URLScopeError(f"URL host must be ASCII for {source_id}")
        if (
            target.scheme.lower() != base.scheme.lower()
            or target_host.lower() != base_host.lower()
            or target_port != base_port
        ):
            raise URLScopeError(f"URL origin is outside scope for {source_id}: {resolved}")
        if not any(
            _path_is_within(target.path, prefix)
            for prefix in definition.allowed_paths
        ):
            raise URLScopeError(f"URL path is outside scope for {source_id}: {resolved}")
        return resolved

    def validate_redirect(
        self, source_id: str, current_url: str, location: str
    ) -> str:
        current = self.validate_url(source_id, current_url)
        if any(
            ord(character) < 32 or ord(character) == 127 for character in location
        ):
            raise URLScopeError(
                f"redirect Location contains control characters for {source_id}"
            )
        try:
            target = urljoin(current, location)
        except ValueError as exc:
            raise URLScopeError(f"invalid redirect Location for {source_id}") from exc
        return self.validate_url(source_id, target)

    def validate_redirect_target(
        self, source_id: str, current_url: str, location: str
    ) -> str:
        return self.validate_redirect(source_id, current_url, location)

    async def upsert_job_sources(self, session: AsyncSession) -> list[JobSource]:
        """Synchronize reviewed definitions during single-process run startup.

        This read-then-write operation is not a cross-process atomic upsert. The caller
        owns the transaction and must roll it back after any failure. IntegrityError is
        deliberately allowed to propagate so concurrent initialization cannot be hidden.
        """
        source_ids = [definition.source_id for definition in self._definitions]
        existing = {
            row.source_id: row
            for row in (
                await session.scalars(
                    select(JobSource).where(JobSource.source_id.in_(source_ids))
                )
            ).all()
        }
        rows: list[JobSource] = []
        for definition in self._definitions:
            row = existing.get(definition.source_id)
            if row is None:
                row = JobSource(source_id=definition.source_id)
                session.add(row)
            row.source_name = definition.source_name
            row.source_type = definition.source_type
            row.market_scope = definition.market_scope
            row.base_url = definition.base_url
            row.allowed_paths_json = json.dumps(
                list(definition.allowed_paths), ensure_ascii=False
            )
            row.collection_mode = definition.collection_mode
            row.compliance_status = definition.compliance_status
            row.compliance_note = definition.compliance_note
            row.rate_limit_seconds = definition.rate_limit_seconds
            row.max_pages_per_run = definition.max_pages
            row.max_records_per_run = definition.max_records
            row.parser_name = definition.parser_name
            row.parser_version = definition.parser_version
            row.enabled = definition.enabled
            rows.append(row)
        await session.flush()
        return rows
