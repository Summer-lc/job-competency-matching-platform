import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from model_class.knowledge_base import JobSource
from src.job_collection.models import (
    CollectionRequest,
    CollectionResult,
    SourceDefinition,
    UnifiedJobRecord,
)
from src.job_collection.source_registry import (
    CollectionBlocked,
    SourceRegistry,
    SourceRegistryError,
    URLScopeError,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "config" / "job_sources.json"
AUTHORIZED_EXPORT_SOURCES = {
    "boss_zhipin_authorized": "zhipin.com",
    "job51_authorized": "51job.com",
    "liepin_authorized": "liepin.com",
    "lagou_authorized": "lagou.com",
    "newjobs_authorized": "newjobs.com.cn",
    "jobonline_authorized": "jobonline.cn",
}


def source_payload(**overrides):
    payload = {
        "source_id": "example_public_jobs",
        "source_name": "Example Public Jobs",
        "source_type": "public_service",
        "market_scope": "china",
        "base_url": "https://example.com",
        "allowed_paths": ["/jobs/"],
        "collection_mode": "public_html",
        "compliance_status": "approved",
        "compliance_note": "人工审核日期 2026-08-05：公开、无需登录的岗位页面。",
        "rate_limit_seconds": 3.0,
        "max_pages": 5,
        "max_records": 100,
        "parser_name": "example",
        "parser_version": "v1",
        "enabled": True,
    }
    payload.update(overrides)
    return payload


def test_feishu_company_source_requires_reviewed_company_and_portal_path():
    payload = source_payload(
        source_id="company_feishu_zhipu",
        source_name="智谱AI官方招聘",
        source_type="company_official",
        base_url="https://zhipu-ai.jobs.feishu.cn",
        allowed_paths=["/api/v1/search/job/posts", "/index/position/"],
        collection_mode="public_json",
        parser_name="feishu_company_ats",
        organization_name="智谱AI",
        portal_path="index",
    )

    source = SourceDefinition.model_validate(payload)

    assert source.organization_name == "智谱AI"
    assert source.portal_path == "index"

    for missing_field in ("organization_name", "portal_path"):
        invalid = dict(payload)
        invalid.pop(missing_field)
        with pytest.raises(ValidationError, match=missing_field):
            SourceDefinition.model_validate(invalid)


@pytest.mark.parametrize("portal_path", ["", "../index", "index?x=1", "路径"])
def test_feishu_company_source_rejects_unsafe_portal_path(portal_path):
    with pytest.raises(ValidationError, match="portal_path"):
        SourceDefinition.model_validate(
            source_payload(
                source_id="company_feishu_zhipu",
                source_name="智谱AI官方招聘",
                source_type="company_official",
                base_url="https://zhipu-ai.jobs.feishu.cn",
                allowed_paths=["/api/v1/search/job/posts", "/index/position/"],
                collection_mode="public_json",
                parser_name="feishu_company_ats",
                organization_name="智谱AI",
                portal_path=portal_path,
            )
        )


def write_registry(tmp_path: Path, sources: list[dict]) -> Path:
    path = tmp_path / "job_sources.json"
    path.write_text(
        json.dumps({"sources": sources}, ensure_ascii=False), encoding="utf-8"
    )
    return path


def test_default_registry_allows_only_reviewed_automatic_sources():
    registry = SourceRegistry.load(DEFAULT_REGISTRY_PATH)

    assert registry.require_automatic("ncss_public_jobs").collection_mode == "public_json"
    assert registry.require_automatic("mohrss_public_jobs").collection_mode == "public_html"

    for source_id in (
        "company_official_manifest",
        "iguopin_public_jobs",
        "zhaopin_legacy_import",
    ):
        with pytest.raises(CollectionBlocked):
            registry.require_automatic(source_id)

    with pytest.raises(CollectionBlocked):
        registry.require_automatic("unknown_source")


def test_default_registry_records_review_and_collection_boundaries():
    registry = SourceRegistry.load(DEFAULT_REGISTRY_PATH)

    for definition in registry.definitions:
        assert "2026-" in definition.compliance_note
        assert definition.max_pages > 0
        assert definition.max_records > 0

    assert "公开、无需登录" in registry.get("ncss_public_jobs").compliance_note
    assert "公开、无需登录" in registry.get("mohrss_public_jobs").compliance_note
    assert "2026-08-12" in registry.get("mohrss_public_jobs").compliance_note
    assert registry.get("mohrss_public_jobs").enabled is True
    assert registry.get("mohrss_public_jobs").rate_limit_seconds == 5.0
    assert "匿名会话" in registry.get("mohrss_public_jobs").compliance_note
    assert "人工" in registry.get("company_official_manifest").compliance_note
    assert registry.get("company_official_manifest").market_scope == "excluded"
    assert registry.get("company_official_manifest").enabled is False
    assert registry.get("iguopin_public_jobs").market_scope == "pending_review"
    assert registry.get("iguopin_public_jobs").enabled is False
    assert "不得自动联网" in registry.get("zhaopin_legacy_import").compliance_note


def test_national_authorized_sources_are_china_file_imports():
    registry = SourceRegistry.load(DEFAULT_REGISTRY_PATH)

    for source_id, domain in AUTHORIZED_EXPORT_SOURCES.items():
        source = registry.get(source_id)
        assert source.market_scope == "china"
        assert source.source_type in {"authorized_platform", "public_service"}
        assert source.collection_mode == "file_import"
        assert source.compliance_status == "manual_only"
        assert domain in source.base_url
        assert source.enabled is True
        with pytest.raises(CollectionBlocked):
            registry.require_automatic(source_id)


@pytest.mark.parametrize(
    "overrides",
    [
        {"enabled": False},
        {"compliance_status": "pending_review"},
        {"compliance_status": "blocked"},
        {"compliance_status": "manual_only"},
        {"collection_mode": "manual_url_manifest"},
        {"collection_mode": "file_import"},
    ],
)
def test_require_automatic_rejects_disabled_or_nonautomatic_sources(
    tmp_path, overrides
):
    registry = SourceRegistry.load(write_registry(tmp_path, [source_payload(**overrides)]))

    with pytest.raises(CollectionBlocked):
        registry.require_automatic("example_public_jobs")


def test_require_automatic_rejects_non_china_job_market_scope():
    registry = SourceRegistry(
        [
            SourceDefinition.model_validate(
                source_payload(market_scope="excluded")
            )
        ]
    )

    with pytest.raises(CollectionBlocked, match="market_scope=excluded"):
        registry.require_automatic("example_public_jobs")


def test_url_scope_accepts_registered_absolute_and_relative_urls():
    registry = SourceRegistry.load(DEFAULT_REGISTRY_PATH)

    assert registry.validate_url(
        "ncss_public_jobs", "https://CNU.NCSS.CN/student/jobs/123/detail.html"
    ) == "https://CNU.NCSS.CN/student/jobs/123/detail.html"
    assert registry.validate_url(
        "ncss_public_jobs", "/student/jobs/jobslist/ajax/?limit=10"
    ) == "https://cnu.ncss.cn/student/jobs/jobslist/ajax/?limit=10"
    assert registry.validate_url(
        "ncss_public_jobs", "https://cnu.ncss.cn:443/student/jobs/1"
    ) == "https://cnu.ncss.cn:443/student/jobs/1"
    assert registry.validate_url(
        "mohrss_public_jobs", "http://job.mohrss.gov.cn:80/cjobs/1"
    ) == "http://job.mohrss.gov.cn:80/cjobs/1"


@pytest.mark.parametrize(
    ("source_id", "url"),
    [
        ("ncss_public_jobs", "https://evil-cnu.ncss.cn/student/jobs/1"),
        ("ncss_public_jobs", "https://cnu.ncss.cn/student/jobs-evil/1"),
        ("ncss_public_jobs", "https://cnu.ncss.cn:8443/student/jobs/1"),
        ("mohrss_public_jobs", "http://job.mohrss.gov.cn:8080/cjobs/1"),
        ("ncss_public_jobs", "http://cnu.ncss.cn/student/jobs/1"),
        ("ncss_public_jobs", "https://user:secret@cnu.ncss.cn/student/jobs/1"),
        ("mohrss_public_jobs", "https://job.mohrss.gov.cn/cjobs/1"),
        ("mohrss_public_jobs", "http://job.mohrss.gov.cn.evil.test/cjobs/1"),
        ("mohrss_public_jobs", "http://job.mohrss.gov.cn/cjobs-evil/1"),
    ],
)
def test_url_scope_rejects_scheme_host_port_and_path_boundary_escapes(
    source_id, url
):
    registry = SourceRegistry.load(DEFAULT_REGISTRY_PATH)

    with pytest.raises(URLScopeError):
        registry.validate_url(source_id, url)


@pytest.mark.parametrize(
    "url",
    [
        "https://cnu.ncss.cn/student/jobs/%252e%252e/admin",
        "https://cnu.ncss.cn/student/jobs/%252f%252e%252e%252fadmin",
        "https://cnu.ncss.cn/student/jobs/%255c..%255cadmin",
        "https://cnu.ncss.cn/student/jobs/%250aadmin",
        "https://cnu.ncss.cn/student/jobs/%2Gadmin",
    ],
)
def test_url_scope_rejects_repeated_or_malformed_path_encoding(url):
    registry = SourceRegistry.load(DEFAULT_REGISTRY_PATH)

    with pytest.raises(URLScopeError):
        registry.validate_url("ncss_public_jobs", url)


@pytest.mark.parametrize("path", ["/%2Gadmin", "/%250aadmin"])
def test_root_allowed_path_still_rejects_unsafe_encoding(path):
    registry = SourceRegistry.load(DEFAULT_REGISTRY_PATH)

    with pytest.raises(URLScopeError):
        registry.validate_url(
            "zhaopin_legacy_import", f"https://www.zhaopin.com{path}"
        )


def test_redirect_target_resolves_each_location_against_the_current_url():
    registry = SourceRegistry.load(DEFAULT_REGISTRY_PATH)

    assert registry.validate_redirect(
        "mohrss_public_jobs",
        "http://job.mohrss.gov.cn/cjobs/list/page.html",
        "detail/42",
    ) == "http://job.mohrss.gov.cn/cjobs/list/detail/42"


@pytest.mark.parametrize(
    ("current_url", "location"),
    [
        (
            "http://evil-example.com/cjobs/list/page.html",
            "http://job.mohrss.gov.cn/cjobs/detail/42",
        ),
        (
            "http://job.mohrss.gov.cn/cjobs/list/page.html",
            "../../../admin",
        ),
        (
            "http://job.mohrss.gov.cn/cjobs/list/page.html",
            "http://evil-example.com/cjobs/detail/42",
        ),
    ],
)
def test_redirect_target_rejects_out_of_scope_current_or_final_url(
    current_url, location
):
    registry = SourceRegistry.load(DEFAULT_REGISTRY_PATH)

    with pytest.raises(URLScopeError):
        registry.validate_redirect_target(
            "mohrss_public_jobs", current_url, location
        )


@pytest.mark.parametrize(
    "document",
    [
        {"sources": [source_payload(), source_payload()]},
        {"sources": [source_payload(compliance_status="accepted")]},
        {"sources": [source_payload(collection_mode="browser_scrape")]},
        {"sources": [source_payload(base_url="ftp://example.com")]},
        {"sources": [source_payload(base_url="https://例子.测试")]},
        {"sources": [{key: value for key, value in source_payload().items() if key != "parser_name"}]},
        {"sources": [source_payload()], "unexpected": True},
    ],
)
def test_registry_fails_closed_for_duplicate_invalid_or_missing_configuration(
    tmp_path, document
):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(SourceRegistryError):
        SourceRegistry.load(path)


def test_registry_fails_closed_for_missing_or_malformed_json(tmp_path):
    with pytest.raises(SourceRegistryError):
        SourceRegistry.load(tmp_path / "missing.json")

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not-json", encoding="utf-8")
    with pytest.raises(SourceRegistryError):
        SourceRegistry.load(malformed)


def test_typed_models_validate_minimum_viable_records():
    definition = SourceDefinition.model_validate(source_payload())
    request = CollectionRequest(
        source_id=definition.source_id,
        family="python_backend",
        query="Python 后端",
        max_pages=2,
        max_records=20,
        run_id="run-20260805-001",
        resume=True,
    )
    observed_at = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    record_payload = {
        "record_id": "record-1",
        "collector_id": "collector-1",
        "job_family_id": request.family,
        "job_title_raw": "Python 开发工程师",
        "company_name": "示例公司",
        "source_name": definition.source_name,
        "source_type": definition.source_type,
        "source_url": "https://example.com/jobs/1",
        "source_id": definition.source_id,
        "source_domain": "example.com",
        "source_record_id": "1",
        "published_at": observed_at,
        "published_at_evidence": "页面发布时间：2026-08-05",
        "published_at_confidence": 0.95,
        "published_at_trusted": True,
        "collected_at": observed_at,
        "first_seen_at": observed_at,
        "last_seen_at": observed_at,
        "snapshot_hash": "a" * 64,
        "parser_name": "example",
        "parser_version": "v1",
        "collection_method": "public_html",
        "compliance_note": "人工审核日期 2026-08-05：公开、无需登录。",
        "page_title": "Python 开发工程师 - 示例公司",
        "response_status": 200,
        "run_id": request.run_id,
        "job_description_raw": "负责 Python 服务设计、开发与维护。",
    }
    record = UnifiedJobRecord.model_validate(record_payload)
    result = CollectionResult(
        source_id=definition.source_id,
        run_id=request.run_id,
        status="completed",
        fetched_count=1,
        parsed_count=1,
        valid_count=1,
        output_paths=["data/collections/run-20260805-001/staged.jsonl"],
        errors=[],
    )

    posting_fields = {
        "record_id",
        "collector_id",
        "job_family_id",
        "job_title_raw",
        "company_name",
        "industry",
        "region",
        "source_name",
        "source_type",
        "source_url",
        "source_id",
        "source_domain",
        "source_record_id",
        "published_at",
        "published_at_evidence",
        "published_at_confidence",
        "published_at_trusted",
        "collected_at",
        "first_seen_at",
        "last_seen_at",
        "snapshot_hash",
        "parser_name",
        "parser_version",
        "collection_method",
        "compliance_note",
        "page_title",
        "response_status",
        "run_id",
        "experience_requirement",
        "education_requirement",
        "salary_range",
        "job_description_raw",
    }
    assert posting_fields <= set(UnifiedJobRecord.model_fields)
    assert record.source_record_id == "1"
    assert record.response_status == 200
    assert UnifiedJobRecord.model_validate(
        {**record_payload, "page_title": None}
    ).page_title is None
    assert result.valid_count == 1

    with pytest.raises(ValidationError):
        CollectionRequest(
            source_id="example_public_jobs",
            family="python_backend",
            query="Python 后端",
            max_pages=0,
            max_records=20,
            run_id="run-1",
        )
    with pytest.raises(ValidationError):
        UnifiedJobRecord(
            record_id="record-1",
            job_family_id="python_backend",
            job_title_raw="Python 开发工程师",
            company_name="示例公司",
            source_name="Example",
            source_type="public_service",
            source_url="https://example.com/jobs/1",
            job_description_raw="too short",
            published_at_confidence=1.5,
        )

    for required_field in (
        "compliance_note",
        "page_title",
        "response_status",
        "run_id",
    ):
        missing_payload = dict(record_payload)
        missing_payload.pop(required_field)
        with pytest.raises(ValidationError) as exc_info:
            UnifiedJobRecord.model_validate(missing_payload)
        assert any(
            error["loc"] == (required_field,) and error["type"] == "missing"
            for error in exc_info.value.errors()
        )

    for field_name in ("compliance_note", "run_id"):
        empty_payload = {**record_payload, field_name: " "}
        with pytest.raises(ValidationError):
            UnifiedJobRecord.model_validate(empty_payload)

    for response_status in (99, 600):
        with pytest.raises(ValidationError):
            UnifiedJobRecord.model_validate(
                {**record_payload, "response_status": response_status}
            )

    for unsafe_source_url in (
        "https://user@example.com/jobs/1",
        "https://user:secret@example.com/jobs/1",
        "https://例子.测试/jobs/1",
    ):
        with pytest.raises(ValidationError):
            UnifiedJobRecord.model_validate(
                {**record_payload, "source_url": unsafe_source_url}
            )


@pytest.mark.asyncio
async def test_upsert_job_sources_inserts_then_updates_without_duplicates(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(JobSource.__table__.create)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    initial_path = write_registry(tmp_path, [source_payload()])
    registry = SourceRegistry.load(initial_path)
    async with Session() as session:
        rows = await registry.upsert_job_sources(session)
        await session.commit()
        assert len(rows) == 1
        assert rows[0].source_name == "Example Public Jobs"

    updated_path = write_registry(
        tmp_path,
        [source_payload(source_name="Updated Public Jobs", rate_limit_seconds=7.5)],
    )
    updated_registry = SourceRegistry.load(updated_path)
    async with Session() as session:
        await updated_registry.upsert_job_sources(session)
        await session.commit()
        count = await session.scalar(select(func.count()).select_from(JobSource))
        row = await session.scalar(
            select(JobSource).where(JobSource.source_id == "example_public_jobs")
        )

    assert count == 1
    assert row.source_name == "Updated Public Jobs"
    assert row.rate_limit_seconds == 7.5
    assert json.loads(row.allowed_paths_json) == ["/jobs/"]
    await engine.dispose()
