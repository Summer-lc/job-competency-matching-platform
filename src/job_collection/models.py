from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SourceType = Literal[
    "public_service",
    "university_recruitment",
    "company_official",
    "authorized_platform",
]
CollectionMode = Literal[
    "public_html",
    "public_json",
    "manual_url_manifest",
    "file_import",
]
ComplianceStatus = Literal[
    "approved", "manual_only", "blocked", "pending_review"
]
MarketScope = Literal["china", "excluded", "pending_review"]


def _validate_http_url(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("URL cannot contain control characters")
    parsed = urlsplit(value)
    host = parsed.hostname
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        raise ValueError("URL must use http(s) and include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL cannot contain username or password userinfo")
    if not host.isascii():
        raise ValueError("URL host must be ASCII; Unicode IDN hosts are not accepted")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("URL contains an invalid port") from exc
    return value


class SourceDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    source_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_]+$")
    source_name: str = Field(min_length=1, max_length=255)
    source_type: SourceType
    market_scope: MarketScope
    base_url: str
    allowed_paths: tuple[str, ...] = Field(min_length=1)
    collection_mode: CollectionMode
    compliance_status: ComplianceStatus
    compliance_note: str = Field(min_length=1)
    rate_limit_seconds: float = Field(gt=0)
    max_pages: int = Field(gt=0)
    max_records: int = Field(gt=0)
    parser_name: str = Field(min_length=1, max_length=100)
    parser_version: str = Field(min_length=1, max_length=50)
    organization_name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    portal_path: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=40,
        pattern=r"^[A-Za-z0-9_/-]+$",
    )
    enabled: bool

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        value = _validate_http_url(value)
        parsed = urlsplit(value)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url cannot contain credentials, query, or fragment")
        return value.rstrip("/")

    @field_validator("allowed_paths")
    @classmethod
    def validate_allowed_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            parsed = urlsplit(value)
            if (
                not value.startswith("/")
                or parsed.scheme
                or parsed.netloc
                or parsed.query
                or parsed.fragment
                or ".." in parsed.path.split("/")
            ):
                raise ValueError("allowed_paths must contain absolute path prefixes")
        return values

    @model_validator(mode="after")
    def validate_company_ats_metadata(self) -> "SourceDefinition":
        if self.parser_name in {"feishu_company_ats", "beisen_company_ats"}:
            if not self.organization_name:
                raise ValueError(
                    f"organization_name is required for {self.parser_name}"
                )
        if self.parser_name == "feishu_company_ats":
            if not self.portal_path:
                raise ValueError("portal_path is required for feishu_company_ats")
        return self


class CollectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    source_id: str = Field(min_length=1, max_length=100)
    family: str = Field(min_length=1, max_length=80)
    query: str = Field(min_length=1, max_length=255)
    max_pages: int = Field(gt=0)
    max_records: int = Field(gt=0)
    run_id: str = Field(min_length=1, max_length=64)
    resume: bool = False


class UnifiedJobRecord(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    record_id: str = Field(min_length=1, max_length=80)
    collector_id: Optional[str] = None
    job_family_id: str = Field(min_length=1, max_length=80)
    job_title_raw: str = Field(min_length=1, max_length=255)
    company_name: str = Field(min_length=1, max_length=255)
    industry: Optional[str] = None
    region: Optional[str] = None
    source_name: str = Field(min_length=1, max_length=255)
    source_type: SourceType
    source_url: str
    source_id: str = Field(min_length=1, max_length=100)
    source_domain: str = Field(min_length=1, max_length=255)
    source_record_id: Optional[str] = None
    published_at: Optional[datetime] = None
    published_at_evidence: Optional[str] = None
    published_at_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    published_at_trusted: bool = False
    collected_at: datetime
    first_seen_at: datetime
    last_seen_at: datetime
    snapshot_hash: str = Field(min_length=64, max_length=64)
    parser_name: str = Field(min_length=1, max_length=100)
    parser_version: str = Field(min_length=1, max_length=50)
    collection_method: CollectionMode
    compliance_note: str = Field(min_length=1, max_length=2000)
    page_title: Optional[str] = Field(max_length=500)
    response_status: int = Field(ge=100, le=599)
    run_id: str = Field(min_length=1, max_length=64)
    experience_requirement: Optional[str] = None
    education_requirement: Optional[str] = None
    salary_range: Optional[str] = None
    job_description_raw: str = Field(min_length=10)
    adapter_extra: dict[str, object] = Field(default_factory=dict)

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        return _validate_http_url(value)


class CollectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    source_id: str = Field(min_length=1, max_length=100)
    run_id: str = Field(min_length=1, max_length=64)
    status: Literal["pending", "running", "completed", "failed", "stopped"]
    fetched_count: int = Field(default=0, ge=0)
    parsed_count: int = Field(default=0, ge=0)
    valid_count: int = Field(default=0, ge=0)
    review_count: int = Field(default=0, ge=0)
    quarantined_count: int = Field(default=0, ge=0)
    duplicate_count: int = Field(default=0, ge=0)
    imported_count: int = Field(default=0, ge=0)
    output_paths: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
