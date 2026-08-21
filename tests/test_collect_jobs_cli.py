from pathlib import Path

import httpx
import pytest


def test_cli_accepts_plan_dry_run_examples():
    from src.collect_jobs import build_parser, validate_args

    for source in ("ncss_public_jobs", "mohrss_public_jobs"):
        args = build_parser().parse_args(
            [
                "--source",
                source,
                "--max-records",
                "20",
                "--max-requests",
                "40",
                "--dry-run",
            ]
        )
        validate_args(args)
        assert args.source == [source]
        assert args.max_records == 20
        assert args.max_requests == 40


def test_cli_accepts_resume_dry_run_and_confirmed_commit_examples():
    from src.collect_jobs import build_parser, validate_args

    dry = build_parser().parse_args(["--resume-run", "run-001", "--dry-run"])
    commit = build_parser().parse_args(
        ["--resume-run", "run-001", "--commit", "--confirm"]
    )

    validate_args(dry)
    validate_args(commit)


def test_exact_cli_examples_provision_persistent_key_without_environment(
    monkeypatch, tmp_path, capsys
):
    import asyncio
    import hashlib
    import json

    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    import model_class.job_competency  # noqa: F401
    import model_class.knowledge_base  # noqa: F401
    import src.collect_jobs as cli
    from model_class.base import Base
    from src.job_collection.adapters.base import (
        ListPage,
        RequestSpec,
        SourceJobRecord,
    )
    from src.job_collection.family_classifier import FamilyDefinition
    from src.job_collection.http_client import FetchResult
    from src.job_collection.models import SourceDefinition
    from src.job_collection.service import (
        CollectionService as RealCollectionService,
        commit_collection_run as real_commit,
    )
    from src.job_collection.source_registry import SourceRegistry

    monkeypatch.delenv("JOB_COLLECTION_ATTESTATION_KEY", raising=False)
    data_root = tmp_path / "data"
    collections = data_root / "collections"
    database = tmp_path / "jobs.db"
    database_url = f"sqlite+aiosqlite:///{database.as_posix()}"
    engine = create_async_engine(database_url)

    async def initialize_database():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(initialize_database())
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    def source(source_id: str, source_type: str) -> SourceDefinition:
        return SourceDefinition.model_validate(
            {
                "source_id": source_id,
                "source_name": source_id,
                "source_type": source_type,
                "market_scope": "china",
                "base_url": f"https://{source_id}.example.test",
                "allowed_paths": ["/jobs/"],
                "collection_mode": "public_html"
                if source_id == "mohrss_public_jobs"
                else "public_json",
                "compliance_status": "approved",
                "compliance_note": "local CLI end-to-end fixture",
                "rate_limit_seconds": 0.01,
                "max_pages": 1,
                "max_records": 1,
                "parser_name": "mohrss"
                if source_id == "mohrss_public_jobs"
                else "ncss",
                "parser_version": "v1",
                "enabled": True,
            }
        )

    registry = SourceRegistry(
        [
            source("ncss_public_jobs", "university_recruitment"),
            source("mohrss_public_jobs", "public_service"),
        ]
    )
    family_config = {
        "PYTHON_BACKEND": FamilyDefinition.model_validate(
            {
                "queries": ["Python backend"],
                "title_aliases": ["Python backend engineer"],
                "skill_indicators": ["FastAPI"],
                "minimum_title_evidence": 1,
                "minimum_skill_evidence": 1,
                "confidence": 0.9,
                "quota": {"target": 1, "batch_size": 1},
            }
        )
    }

    class Adapter:
        def __init__(self, definition):
            self.source = definition
            self.site_page_size = 1

        def build_bootstrap_request(self):
            return RequestSpec(url=f"{self.source.base_url}/jobs/bootstrap")

        @staticmethod
        def validate_bootstrap(content, content_type):
            assert content
            assert content_type == "application/json"

        def build_list_request(self, query, offset, limit):
            return RequestSpec(
                url=f"{self.source.base_url}/jobs/list",
                params={"query": str(query), "offset": offset, "limit": limit},
            )

        def parse_list(
            self, content, content_type, expected_offset=None, expected_limit=None
        ):
            item = json.loads(content)
            return ListPage(
                items=(
                    SourceJobRecord(
                        source_record_id=item["id"],
                        job_title="Python backend engineer",
                        company_name="CLI Fixture Company",
                        raw=item,
                    ),
                ),
                total=1,
                offset=int(expected_offset),
                limit=int(expected_limit),
                has_more=False,
            )

        def build_detail_url(self, item):
            return f"{self.source.base_url}/jobs/{item.source_record_id}"

        def parse_detail(self, content, item, url):
            return {
                "source_record_id": item.source_record_id,
                "job_title": item.job_title,
                "company_name": item.company_name,
                "source_url": url,
                "published_at": "2026-08-01T00:00:00+00:00",
                "published_at_evidence": "local fixture date",
                "published_at_confidence": 0.95,
                "job_description_raw": json.loads(content)["description"],
            }

    class Fetcher:
        def __init__(self, definition, storage):
            self.source = definition
            self.run_id = storage.run_id

        async def fetch(self, url, **_kwargs):
            if "/list?" in url:
                content = json.dumps({"id": f"{self.source.source_id}-1"}).encode()
                content_type = "application/json"
            else:
                content = json.dumps(
                    {
                        "description": (
                            "Python FastAPI backend engineering with PostgreSQL, tests, "
                            "monitoring, deployment, security, and service ownership. "
                        )
                        * 8
                    }
                ).encode()
                content_type = "application/json"
            return FetchResult(
                source_id=self.source.source_id,
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

        async def aclose(self):
            return None

    def service_factory(**_kwargs):
        return RealCollectionService(
            registry=registry,
            collections_root=collections,
            family_config=family_config,
            adapter_factory=lambda definition, _registry: Adapter(definition),
            fetcher_factory=lambda definition, storage, _registry: Fetcher(
                definition, storage
            ),
        )

    async def commit_proxy(**kwargs):
        return await real_commit(
            **kwargs,
            registry=registry,
            family_config=family_config,
            adapter_factory=lambda definition, _registry: Adapter(definition),
        )

    monkeypatch.setattr(cli, "CollectionService", service_factory)
    monkeypatch.setattr(cli, "commit_collection_run", commit_proxy)
    monkeypatch.setattr(cli, "DEFAULT_COLLECTIONS_ROOT", collections)
    monkeypatch.setattr(cli, "DEFAULT_BACKUP_DIR", data_root / "backups")
    monkeypatch.setattr(cli, "ASYNC_DATABASE_URL", database_url)
    monkeypatch.setattr(cli, "AsyncSessionLocal", Session)

    assert cli.main(
        ["--source", "ncss_public_jobs", "--max-records", "20", "--dry-run"]
    ) == 0
    ncss_run = json.loads(capsys.readouterr().out)["run_id"]
    assert cli.main(
        ["--source", "mohrss_public_jobs", "--max-records", "20", "--dry-run"]
    ) == 0
    capsys.readouterr()
    assert cli.main(["--resume-run", ncss_run, "--dry-run"]) == 0
    capsys.readouterr()
    assert cli.main(["--resume-run", ncss_run, "--commit", "--confirm"]) == 0
    capsys.readouterr()

    from src.job_collection.security import (
        default_control_state_root,
        load_or_create_attestation_key,
    )

    key = default_control_state_root(collections) / "keys" / "attestation.key"
    assert key.is_file()
    assert len(load_or_create_attestation_key(root=key.parent)) == 32
    assert len(list((data_root / "backups").glob("*.db"))) == 1
    asyncio.run(engine.dispose())


@pytest.mark.parametrize(
    "argv",
    [
        ["--source", "ncss_public_jobs", "--dry-run", "--commit"],
        ["--source", "ncss_public_jobs", "--commit", "--confirm"],
        ["--resume-run", "run-001", "--commit"],
        ["--resume-run", "../escape", "--dry-run"],
        ["--resume-run", "CON", "--dry-run"],
        ["--source", "ncss_public_jobs", "--max-records", "0", "--dry-run"],
        ["--source", "ncss_public_jobs", "--max-records", "10001", "--dry-run"],
        ["--source", "ncss_public_jobs", "--max-requests", "0", "--dry-run"],
        ["--source", "ncss_public_jobs", "--resume-run", "run-001", "--dry-run"],
        ["--source", "zhaopin_legacy_import", "--input-file", "jobs.jsonl", "--dry-run"],
        ["--source", "zhaopin_legacy_import", "--authorization-note", "ok", "--dry-run"],
        ["--source", "ncss_public_jobs", "--input-file", "jobs.jsonl", "--authorization-note", "ok", "--dry-run"],
        ["--source", "zhaopin_legacy_import", "--input-file", "jobs.jsonl", "--manifest", "manifest.jsonl", "--authorization-note", "ok", "--dry-run"],
    ],
)
def test_cli_rejects_unsafe_or_conflicting_modes(argv):
    from src.collect_jobs import build_parser, validate_args

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return
    with pytest.raises(ValueError):
        validate_args(args)


@pytest.mark.asyncio
async def test_cli_dispatches_new_resume_and_commit_modes(monkeypatch, tmp_path):
    import src.collect_jobs as cli

    calls = []

    class FakeService:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        async def run_dry_run(self, **kwargs):
            calls.append(("dry-run", kwargs))
            return {"mode": "dry-run"}

        async def resume_dry_run(self, run_id, **kwargs):
            calls.append(("resume", {"run_id": run_id, **kwargs}))
            return {"mode": "resume"}

    async def fake_commit(**kwargs):
        calls.append(("commit", kwargs))
        return {"mode": "commit"}

    monkeypatch.setattr(cli, "CollectionService", FakeService)
    monkeypatch.setattr(cli, "commit_collection_run", fake_commit)
    monkeypatch.setattr(cli, "DEFAULT_COLLECTIONS_ROOT", tmp_path / "collections")
    monkeypatch.setattr(cli, "DEFAULT_BACKUP_DIR", tmp_path / "backups")

    parser = cli.build_parser()
    await cli.execute(
        parser.parse_args(
            ["--source", "ncss_public_jobs", "--run-id", "new-run", "--dry-run"]
        )
    )
    await cli.execute(parser.parse_args(["--resume-run", "old-run", "--dry-run"]))
    await cli.execute(
        parser.parse_args(["--resume-run", "old-run", "--commit", "--confirm"])
    )

    assert [call[0] for call in calls] == ["init", "dry-run", "init", "resume", "commit"]
    assert calls[1][1]["source_ids"] == ["ncss_public_jobs"]
    assert calls[1][1]["run_id"] == "new-run"
    assert calls[3][1]["run_id"] == "old-run"
    assert calls[4][1]["confirm"] is True
    assert Path(calls[4][1]["collections_root"]) == tmp_path / "collections"


@pytest.mark.asyncio
async def test_cli_dispatches_authorized_local_file_without_network(monkeypatch, tmp_path):
    import src.collect_jobs as cli

    calls = []

    class FakeService:
        def __init__(self, **kwargs):
            pass

        async def run_dry_run(self, **kwargs):
            calls.append(kwargs)
            return {"mode": "dry-run"}

    monkeypatch.setattr(cli, "CollectionService", FakeService)
    monkeypatch.setattr(cli, "AsyncSessionLocal", lambda: None)
    input_file = tmp_path / "jobs.jsonl"
    input_file.write_text("{}\n", encoding="utf-8")
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "--source",
            "zhaopin_legacy_import",
            "--input-file",
            str(input_file),
            "--authorization-note",
            "团队确认授权，仅用于比赛研究。",
            "--dry-run",
        ]
    )

    cli.validate_args(args)
    service = FakeService()
    await service.run_dry_run(
        source_ids=args.source,
        run_id=args.run_id,
        max_records=args.max_records,
        max_pages=args.max_pages,
        max_requests=args.max_requests,
        manifest_path=args.manifest,
        input_file_path=args.input_file,
        authorization_note=args.authorization_note,
    )

    assert calls[0]["input_file_path"] == input_file
    assert calls[0]["authorization_note"].startswith("团队确认授权")


def test_cli_accepts_authorized_platform_export_with_grant_manifest(tmp_path):
    from src.collect_jobs import build_parser, validate_args

    input_file = tmp_path / "jobs.csv"
    grants = tmp_path / "authorized_job_sources.local.json"
    args = build_parser().parse_args(
        [
            "--source",
            "boss_zhipin_authorized",
            "--input-file",
            str(input_file),
            "--authorization-manifest",
            str(grants),
            "--record-offset",
            "1000",
            "--dry-run",
        ]
    )

    validate_args(args)
    assert args.authorization_manifest == grants
    assert args.record_offset == 1000


@pytest.mark.parametrize(
    "argv",
    [
        ["--coverage-report", "--record-offset", "1"],
        ["--authorization-preflight", "--source", "boss_zhipin_authorized", "--input-file", "jobs.jsonl", "--authorization-manifest", "grants.json", "--record-offset", "1"],
        ["--resume-run", "run-001", "--dry-run", "--record-offset", "1"],
        ["--resume-run", "run-001", "--commit", "--confirm", "--record-offset", "1"],
        ["--source", "ncss_public_jobs", "--dry-run", "--record-offset", "1"],
        ["--source", "zhaopin_legacy_import", "--input-file", "jobs.jsonl", "--authorization-note", "授权", "--dry-run", "--record-offset", "1"],
        ["--source", "boss_zhipin_authorized", "--input-file", "jobs.jsonl", "--authorization-manifest", "grants.json", "--dry-run", "--record-offset", "10001"],
    ],
)
def test_cli_rejects_record_offset_outside_new_authorized_export(argv):
    from src.collect_jobs import build_parser, validate_args

    args = build_parser().parse_args(argv)
    with pytest.raises(ValueError, match="record-offset"):
        validate_args(args)


@pytest.mark.asyncio
async def test_cli_passes_record_offset_to_collection_service(monkeypatch):
    import src.collect_jobs as cli

    calls = []

    class FakeSessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return False

    class FakeService:
        def __init__(self, **_kwargs):
            pass

        async def run_dry_run(self, **kwargs):
            calls.append(kwargs)
            return {"mode": "dry-run"}

    monkeypatch.setattr(cli, "CollectionService", FakeService)
    monkeypatch.setattr(cli, "AsyncSessionLocal", FakeSessionContext)
    args = cli.build_parser().parse_args(
        [
            "--source", "boss_zhipin_authorized",
            "--input-file", "jobs.jsonl",
            "--authorization-manifest", "grants.json",
            "--record-offset", "1000",
            "--dry-run",
        ]
    )

    await cli.execute(args)

    assert calls[0]["record_offset"] == 1000


@pytest.mark.parametrize(
    "argv",
    [
        [
            "--source",
            "boss_zhipin_authorized",
            "--input-file",
            "jobs.jsonl",
            "--dry-run",
        ],
        [
            "--source",
            "boss_zhipin_authorized",
            "--source",
            "job51_authorized",
            "--input-file",
            "jobs.jsonl",
            "--authorization-manifest",
            "grants.json",
            "--dry-run",
        ],
        [
            "--source",
            "ncss_public_jobs",
            "--input-file",
            "jobs.jsonl",
            "--authorization-manifest",
            "grants.json",
            "--dry-run",
        ],
    ],
)
def test_cli_rejects_invalid_authorized_platform_export_combinations(argv):
    from src.collect_jobs import build_parser, validate_args

    args = build_parser().parse_args(argv)
    with pytest.raises(ValueError):
        validate_args(args)


def test_cli_accepts_read_only_coverage_report_and_rejects_collection_options():
    from src.collect_jobs import build_parser, validate_args

    valid = build_parser().parse_args(["--coverage-report"])
    validate_args(valid)

    invalid = build_parser().parse_args(
        ["--coverage-report", "--source", "ncss_public_jobs"]
    )
    with pytest.raises(ValueError, match="coverage"):
        validate_args(invalid)


def test_coverage_query_includes_publication_trust_fields():
    import inspect
    import src.collect_jobs as cli

    source = inspect.getsource(cli.execute)
    assert "JobPosting.published_at" in source
    assert "JobPosting.published_at_trusted" in source


def test_coverage_output_is_accepted_only_for_coverage_mode():
    from src.collect_jobs import build_parser, validate_args

    valid = build_parser().parse_args(
        ["--coverage-report", "--output", "data/expansion-reports/report.json"]
    )
    validate_args(valid)

    invalid = build_parser().parse_args(
        ["--source", "ncss_public_jobs", "--dry-run", "--output", "report.json"]
    )
    with pytest.raises(ValueError, match="output"):
        validate_args(invalid)


def test_coverage_output_writer_rejects_nested_or_non_json_and_round_trips(
    monkeypatch, tmp_path
):
    import json
    import src.collect_jobs as cli

    fake_module = tmp_path / "project" / "src" / "collect_jobs.py"
    root = tmp_path / "project" / "data" / "expansion-reports"
    monkeypatch.setattr(cli, "__file__", str(fake_module))

    with pytest.raises(ValueError, match="directly under"):
        cli._write_coverage_output(root / "nested" / "report.json", "{}")
    with pytest.raises(ValueError, match="JSON file"):
        cli._write_coverage_output(root / "report.txt", "{}")

    expected = {"usable_unique": {"current": 546, "gap_to_minimum": 4454}}
    payload = json.dumps(expected, ensure_ascii=False, indent=2, sort_keys=True)
    target = cli._write_coverage_output(root / "report.json", payload)

    assert json.loads(target.read_text(encoding="utf-8")) == expected
    assert not (root / ".report.json.tmp").exists()


@pytest.mark.asyncio
async def test_authorization_preflight_returns_only_non_sensitive_inventory(tmp_path):
    import json

    from src.collect_jobs import build_parser, execute

    root = Path(__file__).resolve().parents[1]
    grants = tmp_path / "authorized_job_sources.local.json"
    grants.write_text(
        json.dumps(
            {
                "sources": {
                    "boss_zhipin_authorized": {
                        "authorization_reference": "AUTH-BOSS-2026-001",
                        "valid_until": "2026-12-31",
                        "access_methods": ["file_export"],
                        "scope": "Nationwide authorized competition research export.",
                        "credential_env_vars": [],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    args = build_parser().parse_args(
        [
            "--authorization-preflight",
            "--source",
            "boss_zhipin_authorized",
            "--input-file",
            str(root / "tests" / "fixtures" / "authorized_exports" / "boss_jobs.jsonl"),
            "--authorization-manifest",
            str(grants),
        ]
    )

    result = await execute(args)

    assert set(result) == {
        "source_id",
        "authorization_reference",
        "valid_until",
        "access_method",
        "input_filename",
        "input_file_sha256",
        "row_count",
        "accepted_count",
        "rejected_count",
        "valid_candidate_count",
        "review_candidate_count",
        "quarantined_candidate_count",
        "trusted_window_candidate_count",
        "candidate_family_counts",
    }
    assert result["source_id"] == "boss_zhipin_authorized"
    assert result["row_count"] == 1
    assert result["valid_candidate_count"] == 1
    assert result["review_candidate_count"] == 0
    assert result["quarantined_candidate_count"] == 0
    assert result["trusted_window_candidate_count"] == 1
    assert result["candidate_family_counts"] == {"JAVA_DEVELOPER": 1}
    assert len(result["input_file_sha256"]) == 64


@pytest.mark.parametrize(
    ("exception", "expected_code", "message"),
    [
        ("storage", 3, "cannot read checkpoint"),
        ("network", 4, "network unavailable"),
        ("database", 5, "database is locked"),
        ("backup", 5, "database backup verification failed"),
    ],
)
def test_cli_expected_operational_errors_have_stable_concise_exit_codes(
    monkeypatch, capsys, exception, expected_code, message
):
    import sqlite3

    import src.collect_jobs as cli
    from src.job_collection.storage import StorageError
    from src.schema_migration import DatabaseOperationalError

    async def fail(_args):
        if exception == "storage":
            raise StorageError(message)
        if exception == "network":
            request = httpx.Request("GET", "https://example.test/jobs")
            raise httpx.ConnectError(message, request=request)
        if exception == "backup":
            raise DatabaseOperationalError(message)
        raise sqlite3.OperationalError(message)

    monkeypatch.setattr(cli, "execute", fail)

    code = cli.main(["--resume-run", "run-001", "--dry-run"])

    captured = capsys.readouterr()
    assert code == expected_code
    assert captured.out == ""
    assert captured.err.strip() == f"error: {message}"


def test_cli_does_not_hide_unexpected_defects(monkeypatch):
    import src.collect_jobs as cli

    async def fail(_args):
        raise RuntimeError("unexpected defect")

    monkeypatch.setattr(cli, "execute", fail)

    with pytest.raises(RuntimeError, match="unexpected defect"):
        cli.main(["--resume-run", "run-001", "--dry-run"])


def test_focused_and_full_suite_coverage_configs_are_separate():
    root = Path(__file__).resolve().parents[1]
    focused = (root / "pytest.ini").read_text(encoding="utf-8")
    full = (root / "pytest-full.ini").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")

    assert "cov-fail-under" not in focused
    assert "--cov-fail-under=60" in full
    assert "python -m pytest -c pytest-full.ini -q" in readme
