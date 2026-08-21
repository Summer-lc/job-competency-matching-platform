from __future__ import annotations

import json
import hashlib
import re
from datetime import date
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


MAX_AUTHORIZATION_MANIFEST_BYTES = 256 * 1024
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,80}$")
_CREDENTIAL_KEY = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|access[_-]?key|token|password|passwd|cookie|secret|session)(?:$|[_-])",
    re.IGNORECASE,
)


class AuthorizationBlocked(PermissionError):
    """A local authorization grant is absent, invalid, expired, or out of scope."""


class AuthorizedSourceGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    authorization_reference: str = Field(min_length=6, max_length=200)
    valid_until: date
    access_methods: tuple[Literal["file_export", "api"], ...] = Field(min_length=1)
    scope: str = Field(min_length=6, max_length=1000)
    credential_env_vars: tuple[str, ...] = ()

    @field_validator("access_methods")
    @classmethod
    def validate_access_methods(
        cls, values: tuple[Literal["file_export", "api"], ...]
    ) -> tuple[Literal["file_export", "api"], ...]:
        if len(values) != len(set(values)):
            raise ValueError("access methods must be unique")
        return values

    @field_validator("credential_env_vars")
    @classmethod
    def validate_credential_env_vars(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("credential environment variable names must be unique")
        if any(_ENV_NAME.fullmatch(value) is None for value in values):
            raise ValueError("credential environment variable name is invalid")
        return values


class AuthorizedSourceGrants:
    def __init__(
        self,
        grants: Mapping[str, AuthorizedSourceGrant],
        *,
        today: date,
        manifest_sha256: str,
    ) -> None:
        self._grants = dict(grants)
        self._today = today
        self.manifest_sha256 = manifest_sha256

    @property
    def source_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._grants))

    def require(
        self, source_id: str, method: Literal["file_export", "api"]
    ) -> AuthorizedSourceGrant:
        grant = self._grants.get(source_id)
        if grant is None:
            raise AuthorizationBlocked(f"authorization is not granted for {source_id}")
        if grant.valid_until < self._today:
            raise AuthorizationBlocked(
                f"authorization expired for {source_id} on {grant.valid_until.isoformat()}"
            )
        if method not in grant.access_methods:
            raise AuthorizationBlocked(
                f"authorization method {method} is not granted for {source_id}"
            )
        return grant


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AuthorizationBlocked(f"duplicate authorization field: {key}")
        result[key] = value
    return result


def _reject_credential_fields(value: object, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if _CREDENTIAL_KEY.search(str(key)):
                raise AuthorizationBlocked(
                    f"credential or secret field is forbidden at {path}.{key}"
                )
            _reject_credential_fields(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_credential_fields(nested, f"{path}[{index}]")


def load_authorized_source_grants(
    path: str | Path,
    *,
    today: date | None = None,
) -> AuthorizedSourceGrants:
    manifest = Path(path)
    try:
        if manifest.is_symlink():
            raise AuthorizationBlocked("authorization manifest cannot be a symbolic link")
        if not manifest.is_file():
            raise AuthorizationBlocked("authorization manifest is missing")
        if manifest.stat().st_size > MAX_AUTHORIZATION_MANIFEST_BYTES:
            raise AuthorizationBlocked("authorization manifest exceeds the size limit")
        payload = manifest.read_bytes()
        raw = payload.decode("utf-8")
        document = json.loads(raw, object_pairs_hook=_unique_object)
    except AuthorizationBlocked:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuthorizationBlocked(f"authorization manifest is invalid: {exc}") from exc

    _reject_credential_fields(document)
    if not isinstance(document, dict) or set(document) != {"sources"}:
        raise AuthorizationBlocked(
            "authorization manifest must contain only the sources object"
        )
    sources = document["sources"]
    if not isinstance(sources, dict) or not sources:
        raise AuthorizationBlocked("authorization sources must be a non-empty object")

    grants: dict[str, AuthorizedSourceGrant] = {}
    try:
        for source_id, value in sources.items():
            if not isinstance(source_id, str) or not re.fullmatch(
                r"[a-z0-9_]{1,100}", source_id
            ):
                raise AuthorizationBlocked(
                    f"authorization source id is invalid: {source_id!r}"
                )
            grants[source_id] = AuthorizedSourceGrant.model_validate(value)
    except ValidationError as exc:
        raise AuthorizationBlocked(f"authorization grant validation failed: {exc}") from exc

    return AuthorizedSourceGrants(
        grants,
        today=today or date.today(),
        manifest_sha256=hashlib.sha256(payload).hexdigest(),
    )
