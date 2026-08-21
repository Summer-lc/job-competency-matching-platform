from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.job_collection.authorization import AuthorizationBlocked
from src.job_collection.service import CollectionService
from src.job_collection.source_registry import SourceRegistry


NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
REGISTRY_PATH = Path(__file__).parents[1] / "config" / "job_sources.json"
FIXTURES = Path(__file__).parent / "fixtures" / "authorized_exports"


def _write_grants(tmp_path: Path, *, source_id: str, valid_until: str = "2026-12-31") -> Path:
    path = tmp_path / "authorized_job_sources.local.json"
    path.write_text(
        json.dumps(
            {
                "sources": {
                    source_id: {
                        "authorization_reference": "AUTH-EXPORT-2026-001",
                        "valid_until": valid_until,
                        "access_methods": ["file_export"],
                        "scope": "Nationwide public job export for competition research only.",
                        "credential_env_vars": [],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
async def test_authorized_export_run_records_grant_and_input_identity(tmp_path):
    registry = SourceRegistry.load(REGISTRY_PATH)
    input_file = FIXTURES / "boss_jobs.jsonl"
    grants = _write_grants(tmp_path, source_id="boss_zhipin_authorized")
    collections = tmp_path / "collections"
    service = CollectionService(
        registry=registry,
        collections_root=collections,
        clock=lambda: NOW,
        fetcher_factory=lambda *_args: pytest.fail("authorized export attempted network access"),
    )

    report = await service.run_dry_run(
        source_ids=["boss_zhipin_authorized"],
        run_id="boss-authorized-export",
        max_records=10,
        max_pages=1,
        input_file_path=input_file,
        authorization_manifest_path=grants,
    )

    request = report["request"]
    assert request["authorization_reference"] == "AUTH-EXPORT-2026-001"
    assert request["authorization_valid_until"] == "2026-12-31"
    assert request["authorization_scope_sha256"] == hashlib.sha256(
        b"Nationwide public job export for competition research only."
    ).hexdigest()
    assert request["authorization_manifest_sha256"] == hashlib.sha256(
        grants.read_bytes()
    ).hexdigest()
    assert request["input_file_sha256"] == hashlib.sha256(input_file.read_bytes()).hexdigest()
    assert "authorization_manifest_path" not in request
    snapshot = collections / "boss-authorized-export" / "raw" / "boss_zhipin_authorized" / input_file.name
    assert snapshot.read_bytes() == input_file.read_bytes()


@pytest.mark.asyncio
async def test_authorized_export_offsets_create_non_overlapping_batches(tmp_path):
    registry = SourceRegistry.load(REGISTRY_PATH)
    grants = _write_grants(tmp_path, source_id="boss_zhipin_authorized")
    template = json.loads((FIXTURES / "boss_jobs.jsonl").read_text(encoding="utf-8"))
    rows = []
    for index in range(1, 4):
        row = dict(template)
        row["岗位ID"] = f"BOSS-{index:03d}"
        row["职位链接"] = f"https://www.zhipin.com/job_detail/BOSS-{index:03d}.html"
        rows.append(row)
    input_file = tmp_path / "jobs.jsonl"
    input_file.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    collections = tmp_path / "collections"
    service = CollectionService(
        registry=registry,
        collections_root=collections,
        clock=lambda: NOW,
        fetcher_factory=lambda *_args: pytest.fail("authorized export attempted network access"),
    )

    first = await service.run_dry_run(
        source_ids=["boss_zhipin_authorized"],
        run_id="offset-first",
        max_records=1,
        max_pages=1,
        input_file_path=input_file,
        authorization_manifest_path=grants,
        record_offset=0,
    )
    second = await service.run_dry_run(
        source_ids=["boss_zhipin_authorized"],
        run_id="offset-second",
        max_records=1,
        max_pages=1,
        input_file_path=input_file,
        authorization_manifest_path=grants,
        record_offset=2,
    )

    def staged_source_ids(report):
        path = collections / report["run_id"] / "staged" / "jobs.jsonl"
        return {
            json.loads(line)["source_record_id"]
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }

    assert first["request"]["record_offset"] == 0
    assert second["request"]["record_offset"] == 2
    assert staged_source_ids(first) == {"BOSS-001"}
    assert staged_source_ids(second) == {"BOSS-003"}


@pytest.mark.asyncio
async def test_authorized_export_blocks_missing_source_grant(tmp_path):
    grants = _write_grants(tmp_path, source_id="job51_authorized")
    service = CollectionService(
        registry=SourceRegistry.load(REGISTRY_PATH),
        collections_root=tmp_path / "collections",
        clock=lambda: NOW,
    )

    with pytest.raises(AuthorizationBlocked, match="not granted"):
        await service.run_dry_run(
            source_ids=["boss_zhipin_authorized"],
            run_id="missing-source-grant",
            max_records=10,
            max_pages=1,
            input_file_path=FIXTURES / "boss_jobs.jsonl",
            authorization_manifest_path=grants,
        )


@pytest.mark.asyncio
async def test_authorized_export_blocks_expired_grant(tmp_path):
    grants = _write_grants(
        tmp_path,
        source_id="boss_zhipin_authorized",
        valid_until="2026-08-11",
    )
    service = CollectionService(
        registry=SourceRegistry.load(REGISTRY_PATH),
        collections_root=tmp_path / "collections",
        clock=lambda: NOW,
    )

    with pytest.raises(AuthorizationBlocked, match="expired"):
        await service.run_dry_run(
            source_ids=["boss_zhipin_authorized"],
            run_id="expired-source-grant",
            max_records=10,
            max_pages=1,
            input_file_path=FIXTURES / "boss_jobs.jsonl",
            authorization_manifest_path=grants,
        )


@pytest.mark.asyncio
async def test_authorized_export_commit_rejects_changed_grant_then_recomputes_snapshot(
    tmp_path,
    monkeypatch,
):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    import model_class.job_competency  # noqa: F401
    import model_class.knowledge_base  # noqa: F401
    from model_class.base import Base
    from src.job_collection.family_classifier import load_family_config
    from src.job_collection.service import CollectionReportError, commit_collection_run

    monkeypatch.setenv(
        "JOB_COLLECTION_ATTESTATION_KEY",
        "authorized-export-commit-test-key-32-bytes",
    )
    source = SourceRegistry.load(REGISTRY_PATH).get("boss_zhipin_authorized")
    registry = SourceRegistry([source])
    grants = _write_grants(tmp_path, source_id=source.source_id)
    original_grants = grants.read_bytes()
    database = tmp_path / "jobs.db"
    database_url = f"sqlite+aiosqlite:///{database.as_posix()}"
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    collections = tmp_path / "collections"
    service = CollectionService(
        registry=registry,
        collections_root=collections,
        family_config=load_family_config(),
        clock=lambda: NOW,
    )
    await service.run_dry_run(
        source_ids=[source.source_id],
        run_id="authorized-export-commit",
        max_records=10,
        max_pages=1,
        input_file_path=FIXTURES / "boss_jobs.jsonl",
        authorization_manifest_path=grants,
    )

    changed = json.loads(original_grants)
    changed["sources"][source.source_id]["authorization_reference"] = "AUTH-CHANGED-2026-001"
    grants.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(CollectionReportError, match="grant|identity|recomputed"):
        await commit_collection_run(
            run_id="authorized-export-commit",
            collections_root=collections,
            database_url=database_url,
            backup_dir=tmp_path / "backups",
            confirm=True,
            session_factory=Session,
            registry=registry,
            family_config=load_family_config(),
            authorization_manifest_path=grants,
        )

    grants.write_bytes(original_grants)
    result = await commit_collection_run(
        run_id="authorized-export-commit",
        collections_root=collections,
        database_url=database_url,
        backup_dir=tmp_path / "backups",
        confirm=True,
        session_factory=Session,
        registry=registry,
        family_config=load_family_config(),
        authorization_manifest_path=grants,
    )

    assert result["run_id"] == "authorized-export-commit"
    assert result["imported"] >= 0
    await engine.dispose()
