from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from model_class.base import Base
from model_class.job_competency import JobPosting
from model_class.knowledge_base import CollectionRun
from src.job_collection.http_client import FetchResult, SourceStopped
from src.job_collection.models import SourceDefinition
from src.job_collection.source_registry import CollectionBlocked, SourceRegistry


NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
ATTESTATION_KEY = "task-8-test-attestation-key-32-bytes-minimum"


@pytest.fixture(autouse=True)
def configured_attestation_key(monkeypatch):
    monkeypatch.setenv("JOB_COLLECTION_ATTESTATION_KEY", ATTESTATION_KEY)


def test_persist_fetch_evidence_keeps_error_response_non_reusable(tmp_path):
    from src.job_collection.service import _persist_fetch_evidence
    from src.job_collection.storage import RunStorage

    source = _source()
    storage = RunStorage(tmp_path / "data" / "collections", "error-evidence")
    result = FetchResult(
        source_id=source.source_id,
        run_id=storage.run_id,
        url="https://ncss.example.test/student/jobs/list",
        final_url="https://ncss.example.test/student/jobs/list",
        status_code=500,
        content_type="text/html",
        content=b"temporary error",
        content_hash=hashlib.sha256(b"temporary error").hexdigest(),
        parser_version="v1",
        from_cache=False,
    )

    _persist_fetch_evidence(storage, source, result)

    assert storage.load_snapshot(source.source_id, result.url) is None
    _, metadata_path = storage.snapshot_paths(source.source_id, result.url)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["status"] == 500
    assert metadata["reusable"] is False


def _source(**overrides: object) -> SourceDefinition:
    values = {
        "source_id": "ncss_public_jobs",
        "source_name": "NCSS public jobs",
        "source_type": "university_recruitment",
        "market_scope": "china",
        "base_url": "https://example.test",
        "allowed_paths": ["/jobs/"],
        "collection_mode": "public_json",
        "compliance_status": "approved",
        "compliance_note": "Reviewed public test fixture source.",
        "rate_limit_seconds": 0.01,
        "max_pages": 2,
        "max_records": 2,
        "parser_name": "ncss",
        "parser_version": "v1",
        "enabled": True,
    }
    values.update(overrides)
    return SourceDefinition.model_validate(values)


class FakeAdapter:
    source_id = "ncss_public_jobs"

    def __init__(self, source: SourceDefinition) -> None:
        self.source = source

    def build_list_request(self, query, offset, limit):
        from src.job_collection.adapters.base import RequestSpec

        return RequestSpec(
            url=f"{self.source.base_url}/jobs/list",
            params={"query": str(query), "offset": offset, "limit": limit},
        )

    def parse_list(self, content, content_type, expected_offset=None, expected_limit=None):
        from src.job_collection.adapters.base import ListPage, SourceJobRecord

        page = json.loads(content)
        items = tuple(
            SourceJobRecord(
                source_record_id=item["id"],
                job_title=item["title"],
                company_name=item["company"],
                raw=item,
            )
            for item in page["items"]
        )
        return ListPage(
            items=items,
            total=len(items),
            offset=int(expected_offset or 0),
            limit=int(expected_limit or 1),
            has_more=False,
        )

    def build_detail_url(self, item):
        return f"{self.source.base_url}/jobs/{item.source_record_id}"

    def parse_detail(self, content, item, url):
        detail = json.loads(content)
        return {
            "source_record_id": item.source_record_id,
            "job_title": item.job_title,
            "company_name": item.company_name,
            "source_url": url,
            "published_at": "2026-08-01T00:00:00+00:00",
            "published_at_evidence": "fixture published field",
            "published_at_confidence": 0.95,
            "industry": "软件和信息技术服务业",
            "region": "北京",
            "education": "本科",
            "experience": "3-5年",
            "salary": "20-30K",
            "job_description_raw": detail["description"],
        }


class StructureStoppingAdapter(FakeAdapter):
    def parse_detail(self, content, item, url):
        from src.job_collection.adapters.base import AdapterStructureError

        if item.source_record_id == "two":
            raise AdapterStructureError("fixture detail structure changed")
        return super().parse_detail(content, item, url)


class FakeFetcher:
    def __init__(self, run_id: str, *, stop_on_second_detail: bool = False) -> None:
        self.run_id = run_id
        self.stop_on_second_detail = stop_on_second_detail
        self.calls: list[str] = []

    async def fetch(self, url: str, **_kwargs: object) -> FetchResult:
        self.calls.append(url)
        if "/list?" in url:
            content = json.dumps(
                {
                    "items": [
                        {"id": "one", "title": "Python 后端工程师", "company": "甲公司"},
                        {"id": "two", "title": "Python 后端工程师", "company": "乙公司"},
                    ]
                },
                ensure_ascii=False,
            ).encode()
            content_type = "application/json"
        elif url.endswith("/two") and self.stop_on_second_detail:
            raise SourceStopped("fixture source stopped")
        else:
            content = json.dumps(
                {
                    "description": (
                        "负责 Python 服务和 FastAPI 接口设计、开发、自动化测试与维护，"
                        "使用 PostgreSQL 建设稳定可靠的数据处理和部署流程。"
                    )
                    * 8
                },
                ensure_ascii=False,
            ).encode()
            content_type = "application/json"
        return FetchResult(
            source_id="ncss_public_jobs",
            run_id=self.run_id,
            url=url,
            final_url=url,
            status_code=200,
            content_type=content_type,
            content=content,
            content_hash=hashlib.sha256(content).hexdigest(),
            parser_version="v1",
            from_cache=False,
        )


class EmbeddedAdapter(FakeAdapter):
    embedded_detail = True

    def build_list_request(self, query, offset, limit):
        from src.job_collection.adapters.base import RequestSpec

        return RequestSpec(
            method="POST",
            url=f"{self.source.base_url}/jobs/list",
            headers={"Portal-Channel": "office"},
            json_body={"query": str(query), "offset": offset, "limit": limit},
        )

    def parse_detail(self, content, item, url):
        return {
            "source_record_id": item.source_record_id,
            "job_title": item.job_title,
            "company_name": item.company_name,
            "source_url": url,
            "published_at": "2026-08-01T00:00:00+00:00",
            "published_at_evidence": "embedded fixture published field",
            "published_at_confidence": 0.95,
            "region": "北京",
            "education": "本科",
            "experience": "3-5年",
            "job_description_raw": item.raw["description"],
        }


class EmbeddedFetcher(FakeFetcher):
    def __init__(self, run_id: str) -> None:
        super().__init__(run_id)
        self.kwargs: list[dict[str, object]] = []

    async def fetch(self, url: str, **kwargs: object) -> FetchResult:
        self.calls.append(url)
        self.kwargs.append(dict(kwargs))
        if url.endswith("/jobs/list"):
            content = json.dumps(
                {
                    "items": [
                        {
                            "id": "embedded-one",
                            "title": "Python 后端工程师",
                            "company": "甲公司",
                            "description": (
                                "负责 Python FastAPI 后端接口设计、PostgreSQL 数据处理、"
                                "自动化测试、容器部署、监控告警、性能优化和服务稳定性建设。"
                            )
                            * 8,
                        }
                    ]
                },
                ensure_ascii=False,
            ).encode()
        else:
            content = json.dumps(
                {"description": "detail fetch must not happen"},
                ensure_ascii=False,
            ).encode()
        return FetchResult(
            source_id=self.source_id if hasattr(self, "source_id") else "ncss_public_jobs",
            run_id=self.run_id,
            url=url,
            final_url=url,
            status_code=200,
            content_type="application/json",
            content=content,
            content_hash=hashlib.sha256(content).hexdigest(),
            parser_version="v1",
            from_cache=False,
        )


class MixedGateFetcher(FakeFetcher):
    async def fetch(self, url: str, **_kwargs: object) -> FetchResult:
        self.calls.append(url)
        if "/list?" in url:
            content = json.dumps(
                {
                    "items": [
                        {"id": "valid", "title": "Python 后端工程师", "company": "A"},
                        {"id": "review", "title": "Operations Coordinator", "company": "B"},
                        {"id": "quarantine", "title": "Python 后端工程师", "company": "C"},
                    ]
                },
                ensure_ascii=False,
            ).encode()
        elif url.endswith("/quarantine"):
            content = json.dumps(
                {"description": "Python FastAPI 后端开发测试维护"}, ensure_ascii=False
            ).encode()
        elif url.endswith("/review"):
            content = json.dumps(
                {
                    "description": (
                        "Coordinate operations while supporting FastAPI service delivery, "
                        "documentation, testing, release planning, and PostgreSQL reporting. "
                    )
                    * 8
                }
            ).encode()
        else:
            content = json.dumps(
                {
                    "description": (
                        "负责 Python 服务和 FastAPI 接口设计、开发、自动化测试与维护，"
                        "使用 PostgreSQL 建设稳定可靠的数据处理和部署流程。"
                    )
                    * 8
                },
                ensure_ascii=False,
            ).encode()
        return FetchResult(
            source_id="ncss_public_jobs",
            run_id=self.run_id,
            url=url,
            final_url=url,
            status_code=200,
            content_type="application/json",
            content=content,
            content_hash=hashlib.sha256(content).hexdigest(),
            parser_version="v1",
            from_cache=False,
        )


class CursorAdapter(FakeAdapter):
    def parse_list(self, content, content_type, expected_offset=None, expected_limit=None):
        from src.job_collection.adapters.base import ListPage, SourceJobRecord

        page = json.loads(content)
        assert page["offset"] == expected_offset
        items = tuple(
            SourceJobRecord(
                source_record_id=item,
                job_title="Python backend engineer",
                company_name=f"Company {item}",
                raw={"id": item},
            )
            for item in page["items"]
        )
        return ListPage(
            items=items,
            total=page["total"],
            offset=page["offset"],
            limit=int(expected_limit),
            has_more=page["offset"] + len(items) < page["total"],
        )


class CursorFetcher(FakeFetcher):
    def __init__(self, run_id: str, *, stop_at_offset: int | None = None) -> None:
        super().__init__(run_id)
        self.stop_at_offset = stop_at_offset
        self.list_offsets: list[int] = []
        self.list_limits: list[int] = []

    async def fetch(self, url: str, **_kwargs: object) -> FetchResult:
        from urllib.parse import parse_qs, urlsplit

        self.calls.append(url)
        if "/list?" in url:
            query = parse_qs(urlsplit(url).query)
            offset = int(query["offset"][0])
            limit = int(query["limit"][0])
            self.list_offsets.append(offset)
            self.list_limits.append(limit)
            if offset == self.stop_at_offset:
                raise SourceStopped("cursor fixture stopped")
            items = {0: ["one"], 1: ["one", "two"], 3: ["three"]}.get(
                offset, []
            )
            content = json.dumps(
                {"offset": offset, "total": 4, "items": items}
            ).encode()
        else:
            record_id = url.rsplit("/", 1)[-1]
            content = json.dumps(
                {
                    "description": (
                        f"Python FastAPI backend {record_id} with PostgreSQL, testing, "
                        "monitoring, deployment, reliability, and API ownership. "
                    )
                    * 8
                }
            ).encode()
        return FetchResult(
            source_id="ncss_public_jobs",
            run_id=self.run_id,
            url=url,
            final_url=url,
            status_code=200,
            content_type="application/json",
            content=content,
            content_hash=hashlib.sha256(content).hexdigest(),
            parser_version="v1",
            from_cache=False,
        )


@pytest.fixture
def family_config():
    from src.job_collection.family_classifier import FamilyDefinition

    return {
        "PYTHON_BACKEND": FamilyDefinition.model_validate(
            {
                "queries": ["Python 后端工程师"],
                "title_aliases": ["Python 后端工程师"],
                "skill_indicators": ["FastAPI"],
                "exclusions": [],
                "minimum_title_evidence": 1,
                "minimum_skill_evidence": 1,
                "confidence": 0.8,
                "quota": {"target": 100, "batch_size": 2},
            }
        )
    }


@pytest_asyncio.fixture
async def file_database(tmp_path):
    database = tmp_path / "jobs.db"
    url = f"sqlite+aiosqlite:///{database.as_posix()}"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield database, url, Session
    await engine.dispose()


def _service(tmp_path, family_config, fetcher, *, source=None):
    from src.job_collection.service import CollectionService

    selected_source = source or _source()
    return CollectionService(
        registry=SourceRegistry([selected_source]),
        collections_root=tmp_path / "data" / "collections",
        family_config=family_config,
        clock=lambda: NOW,
        adapter_factory=lambda definition, _registry: FakeAdapter(definition),
        fetcher_factory=lambda _source, storage, _registry: fetcher(storage.run_id),
    )


@pytest.mark.asyncio
async def test_embedded_detail_uses_one_post_list_snapshot_as_record_evidence(
    tmp_path, family_config, file_database
):
    _database, _url, Session = file_database
    source = _source(max_records=1, max_pages=1)
    fetcher = EmbeddedFetcher("embedded-run")

    from src.job_collection.service import CollectionService

    service = CollectionService(
        registry=SourceRegistry([source]),
        collections_root=tmp_path / "data" / "collections",
        family_config=family_config,
        clock=lambda: NOW,
        adapter_factory=lambda definition, _registry: EmbeddedAdapter(definition),
        fetcher_factory=lambda _source, _storage, _registry: fetcher,
    )
    async with Session() as session:
        await service.run_dry_run(
            source_ids=[source.source_id],
            run_id="embedded-run",
            max_records=1,
            max_pages=1,
            max_requests=5,
            db=session,
        )

    staged_path = (
        tmp_path
        / "data"
        / "collections"
        / "embedded-run"
        / "staged"
        / "jobs.jsonl"
    )
    staged = json.loads(staged_path.read_text(encoding="utf-8").strip())
    evidence = staged["adapter_extra"]["collection_evidence"]

    assert fetcher.calls == ["https://example.test/jobs/list"]
    assert fetcher.kwargs[0]["method"] == "POST"
    assert fetcher.kwargs[0]["headers"] == {"Portal-Channel": "office"}
    assert fetcher.kwargs[0]["json_body"]["limit"] == 1
    assert evidence["detail_embedded"] is True
    assert evidence["list_url"] == "https://example.test/jobs/list"
    assert evidence["detail_request_url"].endswith("/jobs/embedded-one")
    expected_hash = hashlib.sha256(
        json.dumps(
            {
                "items": [
                    {
                        "id": "embedded-one",
                        "title": "Python 后端工程师",
                        "company": "甲公司",
                        "description": (
                            "负责 Python FastAPI 后端接口设计、PostgreSQL 数据处理、"
                            "自动化测试、容器部署、监控告警、性能优化和服务稳定性建设。"
                        )
                        * 8,
                    }
                ]
            },
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    assert staged["snapshot_hash"] == expected_hash


@pytest.mark.asyncio
async def test_embedded_detail_commit_recomputes_from_list_snapshot(
    tmp_path, family_config, file_database
):
    _database, database_url, Session = file_database
    source = _source(max_records=1, max_pages=1)
    fetcher = EmbeddedFetcher("embedded-commit")
    registry = SourceRegistry([source])

    from src.job_collection.service import CollectionService, commit_collection_run

    collections = tmp_path / "data" / "collections"
    service = CollectionService(
        registry=registry,
        collections_root=collections,
        family_config=family_config,
        clock=lambda: NOW,
        adapter_factory=lambda definition, _registry: EmbeddedAdapter(definition),
        fetcher_factory=lambda _source, _storage, _registry: fetcher,
    )
    async with Session() as session:
        report = await service.run_dry_run(
            source_ids=[source.source_id],
            run_id="embedded-commit",
            max_records=1,
            max_pages=1,
            max_requests=5,
            db=session,
        )
    assert report["totals"]["valid"] == 1

    result = await commit_collection_run(
        run_id="embedded-commit",
        collections_root=collections,
        database_url=database_url,
        backup_dir=tmp_path / "data" / "backups",
        confirm=True,
        session_factory=Session,
        registry=registry,
        family_config=family_config,
        adapter_factory=lambda definition, _registry: EmbeddedAdapter(definition),
    )

    assert result["imported"] == 1
    async with Session() as session:
        posting = await session.scalar(select(JobPosting))
        assert posting is not None
        assert posting.source_record_id == "embedded-one"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows LocalAppData control state")
async def test_default_control_state_ignores_insecure_legacy_workspace_directory(
    tmp_path, family_config, monkeypatch
):
    from src.job_collection.security import _verify_windows_path_acl

    monkeypatch.delenv("JOB_COLLECTION_ATTESTATION_KEY", raising=False)
    local_app_data = tmp_path / "LocalAppData"
    local_app_data.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    legacy_locks = tmp_path / "data" / "collection_locks"
    legacy_locks.parent.mkdir(parents=True)
    legacy_locks.write_text("legacy workspace state must be ignored", encoding="utf-8")
    service = _service(tmp_path, family_config, lambda run_id: FakeFetcher(run_id))

    await service.run_dry_run(
        source_ids=["ncss_public_jobs"],
        run_id="local-state",
        max_records=1,
        max_pages=1,
    )

    run_root = tmp_path / "data" / "collections" / "local-state"
    assert service.locks_root.is_relative_to(local_app_data)
    assert service.keys_root.is_relative_to(local_app_data)
    assert service.attestations_root.is_relative_to(local_app_data)
    assert legacy_locks.is_file()
    assert not any(
        path.name.startswith("collection_") for path in run_root.rglob("*")
    )
    for protected in (
        service.control_root,
        service.locks_root,
        service.keys_root,
        service.attestations_root,
    ):
        _verify_windows_path_acl(protected)


@pytest.mark.asyncio
async def test_dry_run_creates_complete_artifact_tree_without_database_writes(
    tmp_path, family_config, file_database
):
    database, _url, Session = file_database
    service = _service(tmp_path, family_config, lambda run_id: FakeFetcher(run_id))

    async with Session() as session:
        report = await service.run_dry_run(
            source_ids=["ncss_public_jobs"],
            run_id="run-001",
            max_records=1,
            max_pages=1,
            db=session,
        )
        assert await session.scalar(select(func.count()).select_from(JobPosting)) == 0
        assert await session.scalar(select(func.count()).select_from(CollectionRun)) == 0

    root = tmp_path / "data" / "collections" / "run-001"
    assert (root / "raw").is_dir()
    for relative in (
        "staged/jobs.jsonl",
        "review/jobs.jsonl",
        "quarantine/jobs.jsonl",
        "checkpoint.json",
        "report.json",
    ):
        assert (root / relative).is_file()
    assert {path.name for path in root.iterdir()} == {
        "raw",
        "staged",
        "review",
        "quarantine",
        "checkpoint.json",
        "report.json",
    }
    raw_files = [path for path in (root / "raw").rglob("*") if path.is_file()]
    assert raw_files
    assert {path.suffix for path in raw_files} == {".bin", ".json"}
    checkpoint = json.loads((root / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["resume"] == {
        "manifest_path": None,
        "max_pages": 1,
            "max_records": 1,
            "max_requests": 100,
            "record_offset": 0,
            "run_id": "run-001",
        "source_ids": ["ncss_public_jobs"],
    }
    staged = [json.loads(line) for line in (root / "staged/jobs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(staged) == 1
    assert report["status"] == "completed"
    assert report["staging_valid"] is True
    assert report["counts"]["gate"] == {"valid": 1}
    assert report["counts"]["source"] == {"ncss_public_jobs": 1}
    assert report["counts"]["domain"] == {"example.test": 1}
    assert report["counts"]["family"] == {"PYTHON_BACKEND": 1}
    assert report["counts"]["date_trust"] == {"trusted": 1}
    assert database.exists()


@pytest.mark.asyncio
async def test_source_stop_keeps_completed_records_and_writes_completed_report(
    tmp_path, family_config
):
    service = _service(
        tmp_path,
        family_config,
        lambda run_id: FakeFetcher(run_id, stop_on_second_detail=True),
    )

    report = await service.run_dry_run(
        source_ids=["ncss_public_jobs"],
        run_id="stopped-run",
        max_records=2,
        max_pages=1,
    )

    root = tmp_path / "data" / "collections" / "stopped-run"
    assert len((root / "staged/jobs.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert report["status"] == "completed"
    assert report["sources"]["ncss_public_jobs"]["status"] == "stopped"
    assert "fixture source stopped" in report["sources"]["ncss_public_jobs"]["errors"]


@pytest.mark.asyncio
async def test_structure_anomaly_stops_source_without_discarding_completed_record(
    tmp_path, family_config
):
    from src.job_collection.service import CollectionService

    source = _source()
    service = CollectionService(
        registry=SourceRegistry([source]),
        collections_root=tmp_path / "data" / "collections",
        family_config=family_config,
        clock=lambda: NOW,
        adapter_factory=lambda definition, _registry: StructureStoppingAdapter(definition),
        fetcher_factory=lambda _source, storage, _registry: FakeFetcher(storage.run_id),
    )

    report = await service.run_dry_run(
        source_ids=["ncss_public_jobs"],
        run_id="structure-stop",
        max_records=2,
        max_pages=1,
    )

    staged = tmp_path / "data" / "collections" / "structure-stop" / "staged" / "jobs.jsonl"
    assert len(staged.read_text(encoding="utf-8").splitlines()) == 1
    assert report["sources"]["ncss_public_jobs"]["status"] == "stopped"
    assert "structure changed" in report["sources"]["ncss_public_jobs"]["errors"][0]


@pytest.mark.asyncio
async def test_completed_resume_is_idempotent_and_does_not_fetch_again(
    tmp_path, family_config
):
    fetchers: list[FakeFetcher] = []

    def make_fetcher(run_id):
        fetcher = FakeFetcher(run_id)
        fetchers.append(fetcher)
        return fetcher

    service = _service(tmp_path, family_config, make_fetcher)
    first = await service.run_dry_run(
        source_ids=["ncss_public_jobs"],
        run_id="resume-run",
        max_records=1,
        max_pages=1,
    )
    second = await service.resume_dry_run("resume-run")

    assert second == first
    assert len(fetchers) == 1


@pytest.mark.asyncio
async def test_dry_run_provisions_persistent_key_without_environment(
    tmp_path, family_config, monkeypatch
):
    monkeypatch.delenv("JOB_COLLECTION_ATTESTATION_KEY")
    service = _service(tmp_path, family_config, lambda run_id: FakeFetcher(run_id))

    await service.run_dry_run(
        source_ids=["ncss_public_jobs"],
        run_id="provisioned-key",
        max_records=1,
        max_pages=1,
    )

    key_path = service.keys_root / "attestation.key"
    assert key_path.is_file()
    from src.job_collection.security import load_or_create_attestation_key

    assert len(load_or_create_attestation_key(root=key_path.parent)) == 32
    if os.name == "nt":
        assert key_path.read_bytes().startswith(b"JCK1DPAPI\x00")


@pytest.mark.asyncio
async def test_resume_verifies_attestation_and_shares_lock_with_commit(
    tmp_path, family_config
):
    from src.job_collection.security import ExclusiveRunLock, LockUnavailable

    service = _service(tmp_path, family_config, lambda run_id: FakeFetcher(run_id))
    await service.run_dry_run(
        source_ids=["ncss_public_jobs"],
        run_id="resume-locked",
        max_records=1,
        max_pages=1,
    )
    locks = service.locks_root
    with ExclusiveRunLock(locks, "resume-locked", "commit"):
        with pytest.raises(LockUnavailable, match="already claimed"):
            await service.resume_dry_run("resume-locked")

    report_path = (
        tmp_path / "data" / "collections" / "resume-locked" / "report.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["completed_at"] = "2026-08-06T13:00:00+00:00"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="attestation"):
        await service.resume_dry_run("resume-locked")


@pytest.mark.asyncio
async def test_source_page_and_record_quotas_do_not_restart_for_each_query(
    tmp_path, family_config
):
    fetchers: list[FakeFetcher] = []
    two_queries = dict(family_config)
    two_queries["PYTHON_BACKEND"] = family_config["PYTHON_BACKEND"].model_copy(
        update={"queries": ("Python 后端工程师", "FastAPI 工程师")}
    )

    def make_fetcher(run_id):
        fetcher = FakeFetcher(run_id)
        fetchers.append(fetcher)
        return fetcher

    service = _service(tmp_path, two_queries, make_fetcher)
    await service.run_dry_run(
        source_ids=["ncss_public_jobs"],
        run_id="quota-run",
        max_records=2,
        max_pages=1,
    )

    assert sum("/list?" in url for url in fetchers[0].calls) == 1
    assert sum("/list?" not in url for url in fetchers[0].calls) <= 2


@pytest.mark.asyncio
async def test_list_limit_is_capped_to_remaining_run_records(tmp_path, family_config):
    fetchers: list[FakeFetcher] = []

    def make_fetcher(run_id):
        fetcher = FakeFetcher(run_id)
        fetchers.append(fetcher)
        return fetcher

    service = _service(tmp_path, family_config, make_fetcher)
    await service.run_dry_run(
        source_ids=["ncss_public_jobs"],
        run_id="remaining-limit",
        max_records=1,
        max_pages=1,
    )

    list_url = next(url for url in fetchers[0].calls if "/list?" in url)
    assert "limit=1" in list_url


@pytest.mark.asyncio
async def test_ncss_cursor_uses_consumed_span_across_short_and_duplicate_pages(
    tmp_path, family_config
):
    from src.job_collection.service import CollectionService

    source = _source(max_pages=3, max_records=10)
    expanded = {
        "PYTHON_BACKEND": family_config["PYTHON_BACKEND"].model_copy(
            update={
                "quota": family_config["PYTHON_BACKEND"].quota.model_copy(
                    update={"batch_size": 4}
                )
            }
        )
    }
    fetchers: list[CursorFetcher] = []

    def make_fetcher(_source, storage, _registry):
        fetcher = CursorFetcher(storage.run_id)
        fetchers.append(fetcher)
        return fetcher

    service = CollectionService(
        registry=SourceRegistry([source]),
        collections_root=tmp_path / "data" / "collections",
        family_config=expanded,
        clock=lambda: NOW,
        adapter_factory=lambda definition, _registry: CursorAdapter(definition),
        fetcher_factory=make_fetcher,
    )
    await service.run_dry_run(
        source_ids=["ncss_public_jobs"],
        run_id="ncss-consumed-cursor",
        max_records=4,
        max_pages=3,
        max_requests=20,
    )

    assert fetchers[0].list_offsets == [0, 1, 3]
    assert fetchers[0].list_limits == [4, 3, 1]


@pytest.mark.asyncio
async def test_ncss_resume_starts_at_checkpointed_consumed_offset(
    tmp_path, family_config
):
    from src.job_collection.service import CollectionService

    source = _source(max_pages=3, max_records=10)
    fetchers: list[CursorFetcher] = []

    def make_fetcher(_source, storage, _registry):
        fetcher = CursorFetcher(
            storage.run_id,
            stop_at_offset=1 if not fetchers else None,
        )
        fetchers.append(fetcher)
        return fetcher

    service = CollectionService(
        registry=SourceRegistry([source]),
        collections_root=tmp_path / "data" / "collections",
        family_config=family_config,
        clock=lambda: NOW,
        adapter_factory=lambda definition, _registry: CursorAdapter(definition),
        fetcher_factory=make_fetcher,
    )
    await service.run_dry_run(
        source_ids=["ncss_public_jobs"],
        run_id="ncss-cursor-resume",
        max_records=4,
        max_pages=3,
        max_requests=20,
    )
    root = tmp_path / "data" / "collections" / "ncss-cursor-resume"
    (root / "report.json").unlink()

    await service.resume_dry_run("ncss-cursor-resume")

    assert fetchers[0].list_offsets == [0, 1]
    assert fetchers[1].list_offsets[0] == 1


@pytest.mark.asyncio
async def test_page_and_record_budgets_are_run_wide_across_sources(
    tmp_path, family_config
):
    from src.job_collection.service import CollectionService

    first = _source()
    second = _source(
        source_id="second_public_jobs",
        source_name="Second public jobs",
        base_url="https://second.example.test",
    )
    fetchers: list[FakeFetcher] = []

    def make_fetcher(_source, storage, _registry):
        fetcher = FakeFetcher(storage.run_id)
        fetchers.append(fetcher)
        return fetcher

    service = CollectionService(
        registry=SourceRegistry([first, second]),
        collections_root=tmp_path / "data" / "collections",
        family_config=family_config,
        clock=lambda: NOW,
        adapter_factory=lambda definition, _registry: FakeAdapter(definition),
        fetcher_factory=make_fetcher,
    )
    report = await service.run_dry_run(
        source_ids=["ncss_public_jobs", "second_public_jobs"],
        run_id="global-budget",
        max_records=1,
        max_pages=1,
    )

    assert sum("/list?" in url for fetcher in fetchers for url in fetcher.calls) == 1
    assert report["totals"]["valid"] == 1
    assert report["totals"]["pages"] == 1


@pytest.mark.asyncio
async def test_request_budget_is_run_wide_across_sources(tmp_path, family_config):
    from src.job_collection.service import CollectionService

    first = _source()
    second = _source(
        source_id="second_public_jobs",
        source_name="Second public jobs",
        base_url="https://second.example.test",
    )
    fetchers: list[FakeFetcher] = []

    def make_fetcher(_source, storage, _registry):
        fetcher = FakeFetcher(storage.run_id)
        fetchers.append(fetcher)
        return fetcher

    service = CollectionService(
        registry=SourceRegistry([first, second]),
        collections_root=tmp_path / "data" / "collections",
        family_config=family_config,
        clock=lambda: NOW,
        adapter_factory=lambda definition, _registry: FakeAdapter(definition),
        fetcher_factory=make_fetcher,
    )
    report = await service.run_dry_run(
        source_ids=["ncss_public_jobs", "second_public_jobs"],
        run_id="request-budget",
        max_records=2,
        max_pages=2,
        max_requests=1,
    )

    assert sum(len(fetcher.calls) for fetcher in fetchers) == 1
    assert report["totals"]["valid"] == 0
    assert report["sources"]["ncss_public_jobs"]["status"] == "stopped"


@pytest.mark.asyncio
async def test_mohrss_bootstraps_anonymous_session_before_first_list_request(
    tmp_path, family_config
):
    from src.job_collection.adapters.base import ListPage, RequestSpec
    from src.job_collection.service import CollectionService

    source = _source(
        source_id="mohrss_public_jobs",
        source_name="中国公共招聘网公开岗位",
        source_type="public_service",
        base_url="http://job.mohrss.gov.cn",
        allowed_paths=["/cjobs/"],
        collection_mode="public_html",
        parser_name="mohrss",
        max_pages=20,
        max_records=1000,
    )

    class BootstrapAdapter:
        source_id = "mohrss_public_jobs"
        site_page_size = 20

        def __init__(self, definition):
            self.source = definition

        def build_bootstrap_request(self):
            return RequestSpec(
                url=(
                    "http://job.mohrss.gov.cn/cjobs/jobinfolist/"
                    "listJobinfolistIndex"
                )
            )

        @staticmethod
        def validate_bootstrap(content, content_type):
            assert content
            assert content_type == "text/html; charset=utf-8"

        def build_list_request(self, query, page_no, limit):
            return RequestSpec(
                url=(
                    "http://job.mohrss.gov.cn/cjobs/jobinfolist/"
                    "listJobinfolist"
                ),
                params={"textfield": str(query), "pageNo": page_no},
            )

        def parse_list(
            self, content, content_type, expected_page_no=None, expected_limit=None
        ):
            return ListPage(
                items=(),
                total=0,
                offset=0,
                limit=20,
                has_more=False,
            )

    class BootstrapFetcher:
        def __init__(self, run_id):
            self.run_id = run_id
            self.calls = []

        async def fetch(self, url, **kwargs):
            self.calls.append((url, kwargs))
            content = b"<html><title>public jobs</title></html>"
            return FetchResult(
                source_id=source.source_id,
                run_id=self.run_id,
                url=url,
                final_url=url,
                status_code=200,
                content_type="text/html; charset=utf-8",
                content=content,
                content_hash=hashlib.sha256(content).hexdigest(),
                parser_version="v1",
                from_cache=False,
            )

    fetchers = []

    def make_fetcher(_source, storage, _registry):
        fetcher = BootstrapFetcher(storage.run_id)
        fetchers.append(fetcher)
        return fetcher

    service = CollectionService(
        registry=SourceRegistry([source]),
        collections_root=tmp_path / "data" / "collections",
        family_config=family_config,
        clock=lambda: NOW,
        adapter_factory=lambda definition, _registry: BootstrapAdapter(definition),
        fetcher_factory=make_fetcher,
    )

    report = await service.run_dry_run(
        source_ids=[source.source_id],
        run_id="mohrss-bootstrap",
        max_records=1,
        max_pages=1,
        max_requests=2,
    )

    assert len(fetchers[0].calls) == 2
    assert fetchers[0].calls[0][0].endswith("/listJobinfolistIndex")
    assert fetchers[0].calls[0][1]["resume"] is False
    assert "/listJobinfolist?" in fetchers[0].calls[1][0]
    assert report["totals"]["requests"] == 2
    assert report["totals"]["pages"] == 1


@pytest.mark.asyncio
async def test_incomplete_resume_reads_request_from_checkpoint_only(
    tmp_path, family_config
):
    service = _service(tmp_path, family_config, lambda run_id: FakeFetcher(run_id))
    await service.run_dry_run(
        source_ids=["ncss_public_jobs"],
        run_id="checkpoint-resume",
        max_records=1,
        max_pages=1,
    )
    root = tmp_path / "data" / "collections" / "checkpoint-resume"
    (root / "report.json").unlink()

    resumed = await service.resume_dry_run("checkpoint-resume")

    assert resumed["status"] == "completed"
    assert not (root / "run.json").exists()


@pytest.mark.asyncio
async def test_resume_does_not_reset_consumed_request_budget(tmp_path, family_config):
    fetchers: list[FakeFetcher] = []

    def make_fetcher(run_id):
        fetcher = FakeFetcher(run_id)
        fetchers.append(fetcher)
        return fetcher

    service = _service(tmp_path, family_config, make_fetcher)
    first = await service.run_dry_run(
        source_ids=["ncss_public_jobs"],
        run_id="resume-request-budget",
        max_records=1,
        max_pages=1,
        max_requests=1,
    )
    assert first["totals"]["valid"] == 0
    root = tmp_path / "data" / "collections" / "resume-request-budget"
    (root / "report.json").unlink()

    resumed = await service.resume_dry_run("resume-request-budget")

    assert resumed["totals"]["valid"] == 0
    assert sum(len(fetcher.calls) for fetcher in fetchers) == 1


@pytest.mark.asyncio
async def test_manual_only_source_is_never_automatically_collected(tmp_path, family_config):
    manual = _source(
        source_id="company_official_manifest",
        source_type="company_official",
        collection_mode="manual_url_manifest",
        compliance_status="manual_only",
        parser_name="company_manifest",
    )
    service = _service(
        tmp_path,
        family_config,
        lambda run_id: pytest.fail(f"network fetcher created for {run_id}"),
        source=manual,
    )

    with pytest.raises(CollectionBlocked, match="manual_only"):
        await service.run_dry_run(
            source_ids=["company_official_manifest"],
            run_id="manual-network-blocked",
            max_records=1,
            max_pages=1,
        )


def test_commit_definition_validation_rejects_non_china_source():
    from src.job_collection.service import _current_source_definitions

    source = _source(market_scope="excluded")
    registry = SourceRegistry([source])
    report = {"source_definitions": [source.model_dump(mode="json")]}

    with pytest.raises(
        CollectionBlocked, match="not approved for China job data"
    ):
        _current_source_definitions(report, registry)


@pytest.mark.asyncio
async def test_authorized_file_import_is_snapshotted_and_committed_offline(
    tmp_path, family_config, file_database
):
    from model_class.job_competency import JobPosting
    from src.job_collection.service import CollectionService, commit_collection_run

    _database, url, Session = file_database
    source = _source(
        source_id="zhaopin_legacy_import",
        source_name="智联招聘授权历史文件导入",
        source_type="authorized_platform",
        base_url="https://www.zhaopin.com",
        allowed_paths=["/"],
        collection_mode="file_import",
        compliance_status="manual_only",
        parser_name="zhaopin_legacy",
        max_pages=1,
        max_records=10,
    )
    record = {
        "record_id": "A-JD-0001",
        "collector_id": "A",
        "job_family_id": "PYTHON_BACKEND",
        "job_title_raw": "Java后端开发工程师",
        "company_name": "测试科技有限公司",
        "industry": "软件和信息技术服务业",
        "region": "北京",
        "source_name": "智联招聘",
        "source_url": "http://www.zhaopin.com/jobdetail/CC1J1.htm",
        "published_at": "2026-08-05",
        "collected_at": "2026-08-06T12:00:00+00:00",
        "experience_requirement": "3-5年",
        "education_requirement": "本科",
        "salary_range": "20-30K",
        "job_description_raw": (
            "负责Java后端服务设计与开发，使用Spring Boot、MySQL和Redis完成接口、"
            "性能优化、自动化测试及生产维护；负责服务拆分、数据库设计、代码评审、"
            "故障排查和持续交付，能够独立完成需求分析、方案设计、开发联调、上线验证"
            "与运行监控，并持续改进系统稳定性和可维护性。"
        ),
    }
    input_file = tmp_path / "authorized-jobs.jsonl"
    input_file.write_text(
        json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    collections = tmp_path / "data" / "collections"
    service = CollectionService(
        registry=SourceRegistry([source]),
        collections_root=collections,
        family_config=family_config,
        clock=lambda: NOW,
        fetcher_factory=lambda *_args: pytest.fail("file import attempted network access"),
    )

    report = await service.run_dry_run(
        source_ids=[source.source_id],
        run_id="authorized-file-import",
        max_records=10,
        max_pages=1,
        input_file_path=input_file,
        authorization_note="团队确认授权，仅用于比赛研究。",
    )

    assert report["totals"]["valid"] == 1
    snapshot = collections / "authorized-file-import" / "raw" / source.source_id / input_file.name
    assert snapshot.read_bytes() == input_file.read_bytes()
    input_file.unlink()

    result = await commit_collection_run(
        run_id="authorized-file-import",
        collections_root=collections,
        database_url=url,
        backup_dir=tmp_path / "data" / "backups",
        confirm=True,
        session_factory=Session,
        registry=SourceRegistry([source]),
        family_config=family_config,
    )

    assert result["imported"] == 1
    async with Session() as session:
        posting = await session.scalar(select(JobPosting))
        assert posting is not None
        assert posting.provenance_status == "approved"
        assert posting.source_id == source.source_id
        assert posting.job_family_id == "JAVA_DEVELOPER"


@pytest.mark.asyncio
async def test_manual_service_passes_remaining_quota_before_manifest_record_two(
    tmp_path, family_config
):
    from src.job_collection.service import CollectionService

    manual = _source(
        source_id="company_official_manifest",
        source_type="company_official",
        base_url="https://company-official.invalid",
        allowed_paths=["/"],
        collection_mode="manual_url_manifest",
        compliance_status="manual_only",
        parser_name="company_manifest",
        max_pages=1,
        max_records=5,
    )
    fixture_html = Path(__file__).parent / "fixtures" / "manual" / "company-job.html"
    (tmp_path / "company-job.html").write_bytes(fixture_html.read_bytes())
    valid = {
        "source_name": "Example Careers",
        "source_url": "https://careers.example.test/jobs/python-1",
        "company_name": "Example Company",
        "collection_authorization_note": "Reviewed local export for research.",
        "exported_html_path": "company-job.html",
    }
    manifest = tmp_path / "bounded-manual.jsonl"
    manifest.write_text(
        json.dumps(valid, ensure_ascii=False) + "\nnot-json\n",
        encoding="utf-8",
    )
    service = CollectionService(
        registry=SourceRegistry([manual]),
        collections_root=tmp_path / "data" / "collections",
        family_config=family_config,
        clock=lambda: NOW,
    )

    report = await service.run_dry_run(
        source_ids=[manual.source_id],
        run_id="manual-bounded-one",
        max_records=1,
        max_pages=1,
        manifest_path=manifest,
    )

    assert report["sources"][manual.source_id]["parsed"] == 1
    assert sum(report["totals"][name] for name in ("valid", "review", "quarantined")) == 1


@pytest.mark.asyncio
async def test_manual_commit_recomputes_from_snapshotted_local_manifest(
    tmp_path, family_config, file_database
):
    from src.job_collection.service import CollectionService, commit_collection_run

    _database, url, Session = file_database
    manual = _source(
        source_id="company_official_manifest",
        source_name="Reviewed manual company jobs",
        source_type="company_official",
        base_url="https://company-official.invalid",
        allowed_paths=["/"],
        collection_mode="manual_url_manifest",
        compliance_status="manual_only",
        parser_name="company_manifest",
        max_pages=1,
        max_records=5,
    )
    fixture_html = Path(__file__).parent / "fixtures" / "manual" / "company-job.html"
    (tmp_path / "company-job.html").write_bytes(fixture_html.read_bytes())
    manifest = tmp_path / "manual.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "source_name": "Example Careers",
                "source_url": "https://careers.example.test/jobs/python-1",
                "company_name": "Example Company",
                "collection_authorization_note": "Reviewed local export for research.",
                "exported_html_path": "company-job.html",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    service = CollectionService(
        registry=SourceRegistry([manual]),
        collections_root=tmp_path / "data" / "collections",
        family_config=family_config,
        clock=lambda: NOW,
    )
    report = await service.run_dry_run(
        source_ids=[manual.source_id],
        run_id="manual-commit",
        max_records=1,
        max_pages=1,
        manifest_path=manifest,
    )
    assert report["totals"]["valid"] == 1
    manifest.unlink()

    result = await commit_collection_run(
        run_id="manual-commit",
        collections_root=tmp_path / "data" / "collections",
        database_url=url,
        backup_dir=tmp_path / "data" / "backups",
        confirm=True,
        session_factory=Session,
        registry=SourceRegistry([manual]),
        family_config=family_config,
    )

    assert result["imported"] == 1


@pytest.mark.asyncio
async def test_commit_requires_valid_report_backs_up_imports_and_is_idempotent(
    tmp_path, family_config, file_database
):
    from src.job_collection.service import commit_collection_run

    database, url, Session = file_database
    service = _service(tmp_path, family_config, lambda run_id: FakeFetcher(run_id))
    await service.run_dry_run(
        source_ids=["ncss_public_jobs"],
        run_id="commit-run",
        max_records=1,
        max_pages=1,
    )

    with pytest.raises(ValueError, match="--confirm"):
        await commit_collection_run(
            run_id="commit-run",
            collections_root=tmp_path / "data" / "collections",
            database_url=url,
            backup_dir=tmp_path / "data" / "backups",
            confirm=False,
            session_factory=Session,
        )

    first = await commit_collection_run(
        run_id="commit-run",
        collections_root=tmp_path / "data" / "collections",
        database_url=url,
        backup_dir=tmp_path / "data" / "backups",
        confirm=True,
        session_factory=Session,
        registry=SourceRegistry([_source()]),
        family_config=family_config,
        adapter_factory=lambda definition, _registry: FakeAdapter(definition),
    )
    second = await commit_collection_run(
        run_id="commit-run",
        collections_root=tmp_path / "data" / "collections",
        database_url=url,
        backup_dir=tmp_path / "data" / "backups",
        confirm=True,
        session_factory=Session,
        registry=SourceRegistry([_source()]),
        family_config=family_config,
        adapter_factory=lambda definition, _registry: FakeAdapter(definition),
    )

    backups = list((tmp_path / "data" / "backups").glob("*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert first["imported"] == 1
    assert second["idempotent"] is True
    assert second["backup_path"] == first["backup_path"]
    async with Session() as session:
        assert await session.scalar(select(func.count()).select_from(JobPosting)) == 1
        posting = await session.scalar(select(JobPosting))
        assert posting.provenance_status == "approved"
        assert posting.published_at_trusted is True
        run = await session.scalar(select(CollectionRun).where(CollectionRun.run_id == "commit-run"))
        assert run is not None
        assert run.status == "completed"
        assert run.imported_count == 1
    assert database.exists()


@pytest.mark.asyncio
async def test_commit_preserves_signed_staging_review_and_quarantine_counts(
    tmp_path, family_config, file_database
):
    from src.job_collection.service import commit_collection_run

    _database, url, Session = file_database
    source = _source(max_records=3)
    three_record_config = dict(family_config)
    three_record_config["PYTHON_BACKEND"] = family_config["PYTHON_BACKEND"].model_copy(
        update={
            "quota": family_config["PYTHON_BACKEND"].quota.model_copy(
                update={"batch_size": 3}
            )
        }
    )
    service = _service(
        tmp_path,
        three_record_config,
        lambda run_id: MixedGateFetcher(run_id),
        source=source,
    )
    report = await service.run_dry_run(
        source_ids=[source.source_id],
        run_id="mixed-gates",
        max_records=3,
        max_pages=1,
    )
    assert report["totals"]["valid"] == 1
    assert report["totals"]["review"] == 1
    assert report["totals"]["quarantined"] == 1

    await commit_collection_run(
        run_id="mixed-gates",
        collections_root=tmp_path / "data" / "collections",
        database_url=url,
        backup_dir=tmp_path / "data" / "backups",
        confirm=True,
        session_factory=Session,
        registry=SourceRegistry([source]),
        family_config=three_record_config,
        adapter_factory=lambda definition, _registry: FakeAdapter(definition),
    )

    async with Session() as session:
        run = await session.scalar(
            select(CollectionRun).where(CollectionRun.run_id == "mixed-gates")
        )
        assert run is not None
        assert run.review_count == 1
        assert run.quarantined_count == 1


@pytest.mark.asyncio
async def test_commit_rejects_tampered_staging_and_review_promotion(
    tmp_path, family_config, file_database
):
    from src.job_collection.service import commit_collection_run

    _database, url, Session = file_database
    service = _service(tmp_path, family_config, lambda run_id: FakeFetcher(run_id))
    await service.run_dry_run(
        source_ids=["ncss_public_jobs"],
        run_id="tampered-run",
        max_records=1,
        max_pages=1,
    )
    staged_path = tmp_path / "data" / "collections" / "tampered-run" / "staged" / "jobs.jsonl"
    record = json.loads(staged_path.read_text(encoding="utf-8"))
    record["adapter_extra"]["quality_gate"]["status"] = "review"
    staged_path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="hash|valid staging"):
        await commit_collection_run(
            run_id="tampered-run",
            collections_root=tmp_path / "data" / "collections",
            database_url=url,
            backup_dir=tmp_path / "data" / "backups",
            confirm=True,
            session_factory=Session,
        )
    assert not (tmp_path / "data" / "backups").exists()


@pytest.mark.asyncio
async def test_commit_rejects_report_count_tampering_before_backup(
    tmp_path, family_config, file_database
):
    from src.job_collection.service import commit_collection_run

    _database, url, Session = file_database
    service = _service(tmp_path, family_config, lambda run_id: FakeFetcher(run_id))
    await service.run_dry_run(
        source_ids=["ncss_public_jobs"],
        run_id="count-tamper",
        max_records=1,
        max_pages=1,
    )
    report_path = tmp_path / "data" / "collections" / "count-tamper" / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["counts"]["source"] = {"ncss_public_jobs": 999}
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="count"):
        await commit_collection_run(
            run_id="count-tamper",
            collections_root=tmp_path / "data" / "collections",
            database_url=url,
            backup_dir=tmp_path / "data" / "backups",
            confirm=True,
            session_factory=Session,
        )
    assert not (tmp_path / "data" / "backups").exists()


@pytest.mark.asyncio
async def test_commit_rejects_report_and_artifact_forged_together(
    tmp_path, family_config, file_database
):
    from src.job_collection.service import commit_collection_run

    _database, url, Session = file_database
    service = _service(tmp_path, family_config, lambda run_id: FakeFetcher(run_id))
    await service.run_dry_run(
        source_ids=["ncss_public_jobs"],
        run_id="forged-together",
        max_records=1,
        max_pages=1,
    )
    root = tmp_path / "data" / "collections" / "forged-together"
    staged_path = root / "staged" / "jobs.jsonl"
    staged = json.loads(staged_path.read_text(encoding="utf-8"))
    staged["company_name"] = "伪造企业"
    staged_bytes = (
        json.dumps(staged, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    staged_path.write_bytes(staged_bytes)
    report_path = root / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["artifacts"]["staged"]["sha256"] = hashlib.sha256(staged_bytes).hexdigest()
    report["artifacts"]["staged"]["bytes"] = len(staged_bytes)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="attestation"):
        await commit_collection_run(
            run_id="forged-together",
            collections_root=tmp_path / "data" / "collections",
            database_url=url,
            backup_dir=tmp_path / "data" / "backups",
            confirm=True,
            session_factory=Session,
        )
    assert not (tmp_path / "data" / "backups").exists()


@pytest.mark.asyncio
async def test_commit_fails_closed_with_unsafe_persistent_attestation_key(
    tmp_path, family_config, file_database, monkeypatch
):
    from src.job_collection.security import UnsafeArtifact
    from src.job_collection.service import commit_collection_run

    _database, url, Session = file_database
    service = _service(tmp_path, family_config, lambda run_id: FakeFetcher(run_id))
    await service.run_dry_run(
        source_ids=["ncss_public_jobs"],
        run_id="missing-key",
        max_records=1,
        max_pages=1,
    )
    monkeypatch.delenv("JOB_COLLECTION_ATTESTATION_KEY")
    key_root = service.keys_root
    key_root.mkdir()
    (key_root / "attestation.key").write_bytes(b"short")

    with pytest.raises(UnsafeArtifact, match="attestation key"):
        await commit_collection_run(
            run_id="missing-key",
            collections_root=tmp_path / "data" / "collections",
            database_url=url,
            backup_dir=tmp_path / "data" / "backups",
            confirm=True,
            session_factory=Session,
        )


@pytest.mark.asyncio
async def test_commit_recomputes_staged_record_from_signed_raw_evidence(
    tmp_path, family_config, file_database
):
    import src.job_collection.service as service_module

    _database, url, Session = file_database
    service = _service(tmp_path, family_config, lambda run_id: FakeFetcher(run_id))
    await service.run_dry_run(
        source_ids=["ncss_public_jobs"],
        run_id="signed-forgery",
        max_records=1,
        max_pages=1,
    )
    root = tmp_path / "data" / "collections" / "signed-forgery"
    staged_path = root / "staged" / "jobs.jsonl"
    staged = json.loads(staged_path.read_text(encoding="utf-8"))
    staged["company_name"] = "Forged Company"
    staged_bytes = service_module._jsonl_bytes([staged])
    staged_path.write_bytes(staged_bytes)
    report_path = root / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["artifacts"]["staged"].update(
        sha256=hashlib.sha256(staged_bytes).hexdigest(), bytes=len(staged_bytes)
    )
    report_bytes = service_module._json_bytes(report)
    report_path.write_bytes(report_bytes)
    attestations = service.attestations_root
    service_module._write_attestation(
        path=attestations / "signed-forgery.json",
        root=attestations,
        key=ATTESTATION_KEY.encode(),
        run_id="signed-forgery",
        report_bytes=report_bytes,
        artifacts=report["artifacts"],
    )

    with pytest.raises(ValueError, match="raw evidence|recomputed"):
        await service_module.commit_collection_run(
            run_id="signed-forgery",
            collections_root=tmp_path / "data" / "collections",
            database_url=url,
            backup_dir=tmp_path / "data" / "backups",
            confirm=True,
            session_factory=Session,
            registry=SourceRegistry([_source()]),
            family_config=family_config,
            adapter_factory=lambda definition, _registry: FakeAdapter(definition),
        )


@pytest.mark.asyncio
async def test_commit_rejects_source_definition_drift_from_current_registry(
    tmp_path, family_config, file_database
):
    from src.job_collection.service import commit_collection_run

    _database, url, Session = file_database
    service = _service(tmp_path, family_config, lambda run_id: FakeFetcher(run_id))
    await service.run_dry_run(
        source_ids=["ncss_public_jobs"],
        run_id="registry-drift",
        max_records=1,
        max_pages=1,
    )

    with pytest.raises(ValueError, match="current registry"):
        await commit_collection_run(
            run_id="registry-drift",
            collections_root=tmp_path / "data" / "collections",
            database_url=url,
            backup_dir=tmp_path / "data" / "backups",
            confirm=True,
            session_factory=Session,
            registry=SourceRegistry(
                [_source(compliance_note="A changed post-collection approval note.")]
            ),
            family_config=family_config,
            adapter_factory=lambda definition, _registry: FakeAdapter(definition),
        )


@pytest.mark.asyncio
async def test_commit_rolls_back_collection_run_when_import_fails(
    tmp_path, family_config, file_database, monkeypatch
):
    import src.job_collection.service as service_module

    _database, url, Session = file_database
    service = _service(tmp_path, family_config, lambda run_id: FakeFetcher(run_id))
    await service.run_dry_run(
        source_ids=["ncss_public_jobs"],
        run_id="rollback-run",
        max_records=1,
        max_pages=1,
    )

    async def fail_import(db, raw, filename, *, commit=True, authorization=None):
        db.add(CollectionRun(run_id="must-rollback", source_ids_json="[]", mode="commit", staging_dir="x"))
        await db.flush()
        raise RuntimeError("forced import failure")

    monkeypatch.setattr(service_module, "import_job_file", fail_import)
    with pytest.raises(RuntimeError, match="forced import failure"):
        await service_module.commit_collection_run(
            run_id="rollback-run",
            collections_root=tmp_path / "data" / "collections",
            database_url=url,
            backup_dir=tmp_path / "data" / "backups",
            confirm=True,
            session_factory=Session,
            registry=SourceRegistry([_source()]),
            family_config=family_config,
            adapter_factory=lambda definition, _registry: FakeAdapter(definition),
        )

    async with Session() as session:
        assert await session.scalar(select(func.count()).select_from(CollectionRun)) == 0
        assert await session.scalar(select(func.count()).select_from(JobPosting)) == 0


@pytest.mark.asyncio
async def test_commit_rolls_back_when_importer_reports_unexpected_quarantine(
    tmp_path, family_config, file_database, monkeypatch
):
    import src.job_collection.service as service_module

    _database, url, Session = file_database
    service = _service(tmp_path, family_config, lambda run_id: FakeFetcher(run_id))
    await service.run_dry_run(
        source_ids=["ncss_public_jobs"],
        run_id="guarded-result",
        max_records=1,
        max_pages=1,
    )

    async def quarantined_import(
        _db, _raw, _filename, *, commit=True, authorization=None
    ):
        return {
            "imported": 0,
            "revised": 0,
            "review": 0,
            "quarantined": 1,
            "duplicates": 0,
            "skipped": 0,
            "errors": [{"row": 1, "message": "persistence_error"}],
        }

    monkeypatch.setattr(service_module, "import_job_file", quarantined_import)
    with pytest.raises(RuntimeError, match="guarded commit|quarantine"):
        await service_module.commit_collection_run(
            run_id="guarded-result",
            collections_root=tmp_path / "data" / "collections",
            database_url=url,
            backup_dir=tmp_path / "data" / "backups",
            confirm=True,
            session_factory=Session,
            registry=SourceRegistry([_source()]),
            family_config=family_config,
            adapter_factory=lambda definition, _registry: FakeAdapter(definition),
        )

    async with Session() as session:
        assert await session.scalar(select(func.count()).select_from(CollectionRun)) == 0


@pytest.mark.asyncio
async def test_commit_fails_fast_when_database_writer_is_reserved(
    tmp_path, family_config, file_database
):
    import src.job_collection.service as service_module
    from src.job_collection.security import ExclusiveRunLock, LockUnavailable

    _database, url, Session = file_database
    service = _service(tmp_path, family_config, lambda run_id: FakeFetcher(run_id))
    await service.run_dry_run(
        source_ids=["ncss_public_jobs"],
        run_id="writer-reservation",
        max_records=1,
        max_pages=1,
    )
    locks_root = service.locks_root
    writer_id = service_module._database_lock_id(url)

    with ExclusiveRunLock(locks_root, writer_id, "commit"):
        with pytest.raises(LockUnavailable, match="already claimed"):
            await service_module.commit_collection_run(
                run_id="writer-reservation",
                collections_root=tmp_path / "data" / "collections",
                database_url=url,
                backup_dir=tmp_path / "data" / "backups",
                confirm=True,
                session_factory=Session,
                registry=SourceRegistry([_source()]),
                family_config=family_config,
                adapter_factory=lambda definition, _registry: FakeAdapter(definition),
            )

    assert not (tmp_path / "data" / "backups").exists()


@pytest.mark.asyncio
async def test_commit_holds_sqlite_writer_reservation_during_import(
    tmp_path, family_config, file_database, monkeypatch
):
    import src.job_collection.service as service_module

    database, url, Session = file_database
    service = _service(tmp_path, family_config, lambda run_id: FakeFetcher(run_id))
    await service.run_dry_run(
        source_ids=["ncss_public_jobs"],
        run_id="sqlite-reservation",
        max_records=1,
        max_pages=1,
    )
    real_import = service_module.import_job_file
    real_backup = service_module._verified_backup

    def verify_backup_reservation(database_url, backup_dir, run_root):
        locked = False
        with sqlite3.connect(database, timeout=0, isolation_level=None) as contender:
            try:
                contender.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                locked = "locked" in str(exc).lower()
            else:
                contender.rollback()
        assert locked, "backup began without SQLite's writer slot reserved"
        return real_backup(database_url, backup_dir, run_root)

    async def verify_reservation(
        db, raw, filename, *, commit=True, authorization=None
    ):
        locked = False
        with sqlite3.connect(database, timeout=0, isolation_level=None) as contender:
            try:
                contender.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                locked = "locked" in str(exc).lower()
            else:
                contender.rollback()
        assert locked, "commit did not reserve SQLite's writer slot"
        return await real_import(
            db, raw, filename, commit=commit, authorization=authorization
        )

    monkeypatch.setattr(service_module, "import_job_file", verify_reservation)
    monkeypatch.setattr(service_module, "_verified_backup", verify_backup_reservation)

    result = await service_module.commit_collection_run(
        run_id="sqlite-reservation",
        collections_root=tmp_path / "data" / "collections",
        database_url=url,
        backup_dir=tmp_path / "data" / "backups",
        confirm=True,
        session_factory=Session,
        registry=SourceRegistry([_source()]),
        family_config=family_config,
        adapter_factory=lambda definition, _registry: FakeAdapter(definition),
    )

    assert result["imported"] == 1
