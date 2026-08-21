from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AdapterStructureError(RuntimeError):
    """A response no longer has the reviewed source structure."""


class AdapterRecordError(ValueError):
    """One source record is unusable without invalidating the whole response."""


class RequestSpec(BaseModel):
    """A network request description for the bounded HTTP service."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    method: Literal["GET", "POST"] = "GET"
    url: str = Field(min_length=1)
    params: dict[str, str | int | float | bool] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    json_body: dict[str, Any] | None = None

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, headers: dict[str, str]) -> dict[str, str]:
        blocked = {
            "authorization",
            "proxy-authorization",
            "cookie",
            "set-cookie",
            "x-api-key",
            "x-auth-token",
            "host",
        }
        token = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
        for name, value in headers.items():
            if not token.fullmatch(name) or name.casefold() in blocked:
                raise ValueError(f"request header is not allowed: {name}")
            if any(ord(character) < 32 or ord(character) == 127 for character in value):
                raise ValueError(f"request header contains control characters: {name}")
        return headers

    @field_validator("json_body")
    @classmethod
    def validate_json_body(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        try:
            json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("json_body must contain finite JSON values") from exc
        return value

    @model_validator(mode="after")
    def validate_method_body(self) -> "RequestSpec":
        if self.method == "GET" and self.json_body is not None:
            raise ValueError("GET requests cannot include json_body")
        if self.method == "POST" and self.json_body is None:
            raise ValueError("POST requests require json_body")
        return self


class SourceJobRecord(BaseModel):
    """Typed list-page fields plus the untouched JSON record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_record_id: str = Field(min_length=1)
    job_title: str | None = None
    company_name: str | None = None
    region: str | None = None
    industry: str | None = None
    salary: str | None = None
    education: str | None = None
    published_at: str | None = None
    raw: dict[str, Any]


class ListPage(BaseModel):
    """A bounded list response suitable for later collection orchestration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[SourceJobRecord, ...]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(gt=0)
    has_more: bool


class SourceAdapter(ABC):
    """Contract implemented by each reviewed public job source."""

    source_id: str

    @abstractmethod
    def build_list_request(
        self, query: str | Mapping[str, object], offset: int, limit: int
    ) -> RequestSpec:
        """Build one scoped, bounded list request."""

    @abstractmethod
    def parse_list(
        self,
        content: bytes,
        content_type: str | None,
        expected_offset: int | None = None,
        expected_limit: int | None = None,
    ) -> ListPage:
        """Parse one list response without performing network access."""

    @abstractmethod
    def build_detail_url(self, item: SourceJobRecord) -> str:
        """Build and scope-check the detail URL for a list item."""

    @abstractmethod
    def parse_detail(
        self, content: bytes, item: SourceJobRecord, url: str
    ) -> dict[str, object]:
        """Parse one detail response into the normalizer's raw input shape."""
