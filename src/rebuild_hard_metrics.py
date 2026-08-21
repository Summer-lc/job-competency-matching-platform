from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config.DB_config import ASYNC_DATABASE_URL
from model_class.base import Base
from src.hard_metrics_pipeline import run_hard_metrics_pipeline
from src.job_collection.security import ExclusiveRunLock, default_control_state_root
from src.job_collection.source_registry import SourceRegistry
from src.job_data_repair import (
    DEFAULT_REPAIRS_ROOT,
    LegacySourceAuthorization,
    apply_job_data_repairs,
    audit_job_data,
    read_repair_report,
    repair_report_path,
    write_repair_report,
)
from src.schema_migration import (
    DatabaseOperationalError,
    backup_sqlite_database,
    ensure_competition_schema,
    migrate_database,
    sqlite_database_path,
)


DEFAULT_BACKUP_DIR = Path(__file__).resolve().parents[1] / "data" / "backups"
DEFAULT_SOURCE_REGISTRY = (
    Path(__file__).resolve().parents[1] / "config" / "job_sources.json"
)


def _default_locks_root(repairs_root: str | Path = DEFAULT_REPAIRS_ROOT) -> Path:
    return default_control_state_root(repairs_root) / "locks"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rebuild quality gates, quarterly profiles and evolution evidence."
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--dry-run", action="store_true", help="Inspect without writes.")
    modes.add_argument("--incremental", action="store_true", help="Rebuild selected data.")
    modes.add_argument("--full", action="store_true", help="Rebuild all existing data.")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required confirmation for a full rebuild.",
    )
    parser.add_argument("--database-url", default=ASYNC_DATABASE_URL)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    repairs = parser.add_mutually_exclusive_group()
    repairs.add_argument(
        "--repair-audit", action="store_true", help="Audit historical data only."
    )
    repairs.add_argument(
        "--repair", action="store_true", help="Apply historical data repair."
    )
    parser.add_argument("--repair-run-id")
    parser.add_argument("--repairs-root", type=Path, default=DEFAULT_REPAIRS_ROOT)
    parser.add_argument("--locks-root", type=Path, default=None)
    parser.add_argument(
        "--authorize-legacy-zhaopin",
        action="store_true",
        help="Authorize strict legacy Zhaopin file rows during repair.",
    )
    parser.add_argument("--authorization-note")
    parser.add_argument(
        "--source-registry", type=Path, default=DEFAULT_SOURCE_REGISTRY
    )
    parser.add_argument(
        "--family-code",
        action="append",
        dest="family_codes",
        help="Limit an incremental rebuild to one or more job-family codes.",
    )
    parser.add_argument(
        "--after-collection-run",
        help="Require a verified committed collection run before a full rebuild.",
    )
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.full and not args.confirm:
        raise ValueError("A full rebuild requires --confirm.")
    if args.confirm and not args.full:
        raise ValueError("--confirm is only valid with --full.")
    if args.incremental and not args.family_codes:
        raise ValueError("An incremental rebuild requires at least one --family-code.")
    if args.family_codes and not args.incremental:
        raise ValueError("--family-code is only valid with --incremental.")
    if args.repair_audit and not args.dry_run:
        raise ValueError("--repair-audit requires --dry-run.")
    if args.repair and not (args.full and args.confirm):
        raise ValueError("--repair requires --full --confirm.")
    if (args.repair or args.repair_audit) and not args.repair_run_id:
        raise ValueError("repair modes require --repair-run-id.")
    if args.repair_run_id and not (args.repair or args.repair_audit):
        raise ValueError("--repair-run-id requires a repair mode.")
    if args.locks_root and not args.repair:
        raise ValueError("--locks-root is only valid with --repair.")
    repair_mode = bool(args.repair or args.repair_audit)
    if args.authorize_legacy_zhaopin and not repair_mode:
        raise ValueError("legacy authorization switch requires a repair mode")
    if args.authorize_legacy_zhaopin and not str(args.authorization_note or "").strip():
        raise ValueError("legacy authorization requires an authorization note")
    if args.authorization_note and not args.authorize_legacy_zhaopin:
        raise ValueError("authorization note requires the legacy authorization switch")
    if args.repair_run_id:
        repair_report_path(args.repairs_root, args.repair_run_id)
    if args.after_collection_run and not (args.full and args.confirm):
        raise ValueError("--after-collection-run requires --full --confirm")
    if args.after_collection_run and (args.repair or args.repair_audit):
        raise ValueError("--after-collection-run cannot be combined with repair modes")
    if args.after_collection_run and re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", args.after_collection_run
    ) is None:
        raise ValueError("--after-collection-run is invalid")


def _legacy_source_authorization(
    args: argparse.Namespace,
) -> LegacySourceAuthorization | None:
    if not args.authorize_legacy_zhaopin:
        return None
    registry = SourceRegistry.load(args.source_registry)
    source = registry.get("zhaopin_legacy_import")
    host = (urlsplit(source.base_url).hostname or "").rstrip(".").lower()
    if not (
        source.enabled
        and source.market_scope == "china"
        and source.source_type == "authorized_platform"
        and source.collection_mode == "file_import"
        and source.compliance_status == "manual_only"
        and source.parser_name == "zhaopin_legacy"
        and host in {"zhaopin.com", "www.zhaopin.com"}
    ):
        raise ValueError(
            "zhaopin_legacy_import must be a reviewed China file-import source"
        )
    return LegacySourceAuthorization(
        source_id=source.source_id,
        source_name=source.source_name,
        source_type=source.source_type,
        source_domain=host,
        collection_method=source.collection_mode,
        parser_name=source.parser_name,
        parser_version=source.parser_version,
        authorization_note=str(args.authorization_note).strip(),
        domain_scope="zhaopin.com",
    )


def inspect_database(database_url: str, backup_dir: Path) -> dict[str, object]:
    database_path = sqlite_database_path(database_url)
    result: dict[str, object] = {
        "mode": "dry-run",
        "database_url": database_url,
        "database_path": str(database_path) if database_path else None,
        "database_exists": bool(database_path and database_path.exists()),
        "posting_count": None,
        "backup_dir": str(backup_dir.resolve()),
    }
    if not database_path or not database_path.exists():
        return result
    with sqlite3.connect(database_path) as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='job_posting'"
        ).fetchone()
        if table:
            result["posting_count"] = connection.execute(
                "SELECT COUNT(*) FROM job_posting"
            ).fetchone()[0]
    return result


def prepare_full_rebuild(
    database_url: str, backup_dir: Path, *, confirmed: bool
) -> Path | None:
    if not confirmed:
        raise ValueError("A full rebuild requires --confirm.")
    return backup_sqlite_database(database_url, backup_dir)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _database_identity(database_url: str) -> str:
    path = sqlite_database_path(database_url)
    if path is None:
        raise ValueError("repair requires a SQLite database")
    return hashlib.sha256(
        os.path.normcase(str(path.resolve())).encode("utf-8")
    ).hexdigest()


def _database_lock_id(database_url: str) -> str:
    return f"db-{_database_identity(database_url)[:48]}"


def _verify_backup(path: Path, *, expected_rows: int | None = None) -> None:
    try:
        with closing(
            sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        ) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise DatabaseOperationalError("database backup verification failed")
            if expected_rows is not None:
                table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_posting'"
                ).fetchone()
                count = (
                    connection.execute("SELECT COUNT(*) FROM job_posting").fetchone()[0]
                    if table
                    else 0
                )
                if count != expected_rows:
                    raise DatabaseOperationalError("database backup row count mismatch")
    except sqlite3.Error as exc:
        raise DatabaseOperationalError(
            f"database backup verification failed: {exc}"
        ) from exc


def _validate_collection_run_for_rebuild(
    database_url: str,
    run_id: str,
) -> dict[str, object]:
    database = sqlite_database_path(database_url)
    if database is None or not database.is_file():
        raise ValueError("after-collection rebuild requires an existing SQLite database")
    try:
        with sqlite3.connect(database) as connection:
            row = connection.execute(
                "SELECT status, staging_dir, summary_json FROM collection_run WHERE run_id = ?",
                (run_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise ValueError(f"collection run validation failed: {exc}") from exc
    if row is None:
        raise ValueError(f"collection run does not exist: {run_id}")
    status, staging_dir, summary_json = row
    if status != "completed":
        raise ValueError(f"collection run is not committed: {run_id}")
    try:
        summary = json.loads(summary_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("collection run summary is invalid") from exc
    if not isinstance(summary, dict):
        raise ValueError("collection run summary is invalid")

    run_root = Path(str(staging_dir)).resolve()
    report = (run_root / "report.json").resolve()
    if report.parent != run_root or report.is_symlink() or not report.is_file():
        raise ValueError("collection run report is missing")
    expected_report_hash = summary.get("report_sha256")
    if (
        not isinstance(expected_report_hash, str)
        or _file_sha256(report) != expected_report_hash
    ):
        raise ValueError("collection run report checksum mismatch")

    backup_value = summary.get("backup_path")
    if not isinstance(backup_value, str):
        raise ValueError("collection run backup evidence is missing")
    collection_backup = Path(backup_value).resolve()
    if collection_backup.is_symlink() or not collection_backup.is_file():
        raise ValueError("collection run backup is missing")
    _verify_backup(collection_backup)
    return {
        "run_id": run_id,
        "report_path": str(report),
        "report_sha256": expected_report_hash,
        "collection_backup_path": str(collection_backup),
        "collection_backup_sha256": _file_sha256(collection_backup),
    }


def _verified_repair_backup(
    database_url: str,
    backup_dir: Path,
    *,
    repair_run_id: str,
    expected_rows: int,
) -> Path:
    source = sqlite_database_path(database_url)
    if source is None:
        raise ValueError("repair requires a SQLite database")
    backup_root = backup_dir.resolve()
    if backup_root == source.resolve():
        raise ValueError("backup directory must differ from the database")
    target = backup_root / f"{source.stem}-{repair_run_id}.db"
    temporary = backup_sqlite_database(database_url, backup_root)
    if temporary is None:
        raise ValueError("repair requires a SQLite database backup")
    temporary = temporary.resolve()
    if temporary.parent != backup_root or temporary == source.resolve():
        temporary.unlink(missing_ok=True)
        raise ValueError("backup path failed safety validation")
    _verify_backup(temporary, expected_rows=expected_rows)
    try:
        os.replace(temporary, target)
        _verify_backup(target, expected_rows=expected_rows)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _reported_backup_path(backup_dir: Path, report: dict[str, object]) -> Path:
    backup_name = report.get("backup_file")
    if not isinstance(backup_name, str) or Path(backup_name).name != backup_name:
        raise ValueError("repair report backup path is invalid")
    backup_root = backup_dir.resolve()
    backup = (backup_root / backup_name).resolve()
    if backup.parent != backup_root:
        raise ValueError("repair report backup path escapes backup directory")
    _verify_backup(backup, expected_rows=int(report["row_count_before"]))
    expected_sha256 = report.get("backup_sha256")
    if not isinstance(expected_sha256, str) or _file_sha256(backup) != expected_sha256:
        raise ValueError("repair backup checksum mismatch")
    return backup


def _repair_commit_state(
    database_url: str, report: dict[str, object]
) -> str:
    database_path = sqlite_database_path(database_url)
    if database_path is None:
        raise ValueError("repair requires a SQLite database")
    pipeline = report.get("pipeline")
    if not isinstance(pipeline, dict) or not isinstance(pipeline.get("run_id"), str):
        raise ValueError("repair report pipeline identity is invalid")
    with sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True) as connection:
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM data_repair_audit WHERE repair_run_id = ?",
            (report["repair_run_id"],),
        ).fetchone()[0]
        pipeline_row = connection.execute(
            "SELECT status, result_signature FROM pipeline_run WHERE run_id = ?",
            (pipeline["run_id"],),
        ).fetchone()
    expected_audits = int(report.get("applied_change_count", -1))
    if audit_count == 0 and pipeline_row is None:
        return "absent"
    if (
        audit_count == expected_audits
        and pipeline_row
        == ("completed", pipeline.get("result_signature"))
    ):
        return "committed"
    return "inconsistent"


def _validate_repair_report_database(
    database_url: str, report: dict[str, object]
) -> None:
    if report.get("database_identity") != _database_identity(database_url):
        raise ValueError("repair run belongs to a different database")


async def _execute_repair_audit(args: argparse.Namespace) -> dict[str, object]:
    database_path = sqlite_database_path(args.database_url)
    if database_path is None or not database_path.exists():
        raise ValueError("repair audit requires an existing SQLite database")
    engine = create_async_engine(args.database_url)
    authorization = _legacy_source_authorization(args)
    Session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with Session() as session:
            report = await audit_job_data(
                session,
                repair_run_id=args.repair_run_id,
                authorization=authorization,
            )
        path = write_repair_report(
            args.repairs_root, args.repair_run_id, {**report, "status": "completed"}
        )
        return {**report, "status": "completed", "report_path": str(path)}
    finally:
        await engine.dispose()


async def _execute_repair(args: argparse.Namespace) -> dict[str, object]:
    database_path = sqlite_database_path(args.database_url)
    if database_path is None or not database_path.exists():
        raise ValueError("repair requires an existing SQLite database")
    authorization = _legacy_source_authorization(args)
    locks_root = args.locks_root or _default_locks_root(args.repairs_root)
    with ExclusiveRunLock(locks_root, args.repair_run_id, "commit"):
        with ExclusiveRunLock(
            locks_root, _database_lock_id(args.database_url), "commit"
        ):
            existing = read_repair_report(args.repairs_root, args.repair_run_id)
            if existing is not None and existing.get("mode") == "apply":
                _validate_repair_report_database(args.database_url, existing)
                backup = _reported_backup_path(args.backup_dir, existing)
                state = _repair_commit_state(args.database_url, existing)
                if state == "inconsistent":
                    raise ValueError("repair database state does not match its report")
                if state == "committed":
                    if existing.get("status") == "prepared":
                        existing = {**existing, "status": "completed"}
                        write_repair_report(
                            args.repairs_root, args.repair_run_id, existing
                        )
                    elif existing.get("status") != "completed":
                        raise ValueError("repair report status is invalid")
                    return {
                        **existing.get("pipeline", {}),
                        "repair": {**existing, "idempotent": True},
                        "backup_path": str(backup),
                        "migrations": [],
                    }
                if existing.get("status") != "prepared":
                    raise ValueError("completed repair is absent from the database")
                repair_report_path(
                    args.repairs_root, args.repair_run_id
                ).unlink(missing_ok=True)

            engine = create_async_engine(
                args.database_url, connect_args={"timeout": 0.1}
            )
            try:
                async with engine.connect() as connection:
                    await connection.exec_driver_sql("BEGIN IMMEDIATE")
                    row_count = int(
                        (
                            await connection.execute(
                                text("SELECT COUNT(*) FROM job_posting")
                            )
                        ).scalar_one()
                    )
                    backup = _verified_repair_backup(
                        args.database_url,
                        args.backup_dir,
                        repair_run_id=args.repair_run_id,
                        expected_rows=row_count,
                    )
                    try:
                        migrations = await ensure_competition_schema(connection)
                        await connection.run_sync(Base.metadata.create_all)
                        async with AsyncSession(
                            bind=connection, expire_on_commit=False
                        ) as session:
                            repair = await apply_job_data_repairs(
                                session,
                                repair_run_id=args.repair_run_id,
                                authorization=authorization,
                            )
                            pipeline = await run_hard_metrics_pipeline(
                                session, mode="full", family_codes=None
                            )
                        completed_report = {
                            **repair,
                            "status": "completed",
                            "database_identity": _database_identity(args.database_url),
                            "backup_file": backup.name,
                            "backup_sha256": _file_sha256(backup),
                            "pipeline": pipeline,
                        }
                        write_repair_report(
                            args.repairs_root,
                            args.repair_run_id,
                            {**completed_report, "status": "prepared"},
                        )
                        await connection.commit()
                    except Exception:
                        await connection.rollback()
                        prepared = read_repair_report(
                            args.repairs_root, args.repair_run_id
                        )
                        if prepared is not None and prepared.get("status") == "prepared":
                            repair_report_path(
                                args.repairs_root, args.repair_run_id
                            ).unlink(missing_ok=True)
                        raise
                    report_path = write_repair_report(
                        args.repairs_root,
                        args.repair_run_id,
                        completed_report,
                    )
            finally:
                await engine.dispose()
    return {
        **pipeline,
        "repair": {**completed_report, "idempotent": False},
        "backup_path": str(backup),
        "report_path": str(report_path),
        "migrations": migrations,
    }


async def execute_rebuild(args: argparse.Namespace) -> dict[str, object]:
    validate_args(args)
    if args.repair_audit:
        return await _execute_repair_audit(args)
    if args.repair:
        return await _execute_repair(args)
    if args.dry_run:
        return inspect_database(args.database_url, args.backup_dir)

    collection_evidence = None
    if args.after_collection_run:
        collection_evidence = _validate_collection_run_for_rebuild(
            args.database_url,
            args.after_collection_run,
        )

    backup_path = None
    if args.full:
        backup_path = prepare_full_rebuild(
            args.database_url, args.backup_dir, confirmed=args.confirm
        )

    migrations = await migrate_database(args.database_url)
    engine = create_async_engine(args.database_url)
    Session = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        async with Session() as session:
            result = await run_hard_metrics_pipeline(
                session,
                mode="full" if args.full else "incremental",
                family_codes=args.family_codes,
            )
    finally:
        await engine.dispose()

    return {
        **result,
        "backup_path": str(backup_path) if backup_path else None,
        "after_collection_run": collection_evidence,
        "migrations": migrations,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(execute_rebuild(args))
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
