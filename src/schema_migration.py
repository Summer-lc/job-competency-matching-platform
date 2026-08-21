from __future__ import annotations

import sqlite3
import json
from contextlib import closing
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from config.DB_config import ASYNC_DATABASE_URL
from model_class.base import Base
from src.observation import observation_datetime, observation_identity, observation_time


MIGRATION_ID = "competition_hard_metrics_v1"
MULTISOURCE_MIGRATION_ID = "multisource_provenance_v1"
OBSERVATION_VERSION_MIGRATION_ID = "job_posting_observation_version_v1"
REVISION_OBSERVATION_MIGRATION_ID = "job_posting_revision_observation_v1"
PROFILE_SKILL_EVIDENCE_MIGRATION_ID = "profile_skill_evidence_v1"
TASK10_QUALITY_CORRECTION_MIGRATION_ID = "task10_quality_corrections_v1"
DOMESTIC_JOB_MARKET_SCOPE_MIGRATION_ID = "domestic_job_market_scope_v1"

HARD_METRICS_COLUMNS = {
    "job_posting": (
        ("machine_level", "VARCHAR(30) NOT NULL DEFAULT 'unspecified'"),
        ("machine_level_confidence", "FLOAT NOT NULL DEFAULT 0"),
        ("machine_level_evidence_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("manual_level", "VARCHAR(30)"),
        ("manual_level_review_json", "TEXT"),
        ("gate_status", "VARCHAR(30) NOT NULL DEFAULT 'review'"),
        ("gate_issue_codes_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("gate_rule_version", "VARCHAR(50)"),
        ("gated_at", "DATETIME"),
    ),
    "job_profile": (
        ("profile_kind", "VARCHAR(30) NOT NULL DEFAULT 'legacy'"),
        ("period_key", "VARCHAR(10)"),
        ("sample_count", "INTEGER NOT NULL DEFAULT 0"),
        ("sample_status", "VARCHAR(30) NOT NULL DEFAULT 'insufficient'"),
        ("input_signature", "VARCHAR(64)"),
        ("pipeline_run_id", "INTEGER"),
        ("generation_key", "VARCHAR(64)"),
        ("derivation_status", "VARCHAR(30) NOT NULL DEFAULT 'active'"),
    ),
    "evolution_event": (
        ("previous_period", "VARCHAR(10)"),
        ("current_period", "VARCHAR(10)"),
        ("before_rate", "FLOAT"),
        ("after_rate", "FLOAT"),
        ("change_delta", "FLOAT"),
        ("event_status", "VARCHAR(30) NOT NULL DEFAULT 'legacy'"),
        ("pipeline_run_id", "INTEGER"),
        ("generation_key", "VARCHAR(64)"),
    ),
}

MULTISOURCE_COLUMNS = {
    "job_posting": (
        ("source_id", "VARCHAR(100)"),
        ("source_domain", "VARCHAR(255)"),
        ("source_record_id", "VARCHAR(255)"),
        ("published_at_evidence", "TEXT"),
        ("published_at_confidence", "FLOAT NOT NULL DEFAULT 0"),
        ("published_at_trusted", "BOOLEAN NOT NULL DEFAULT 0"),
        ("first_seen_at", "DATETIME"),
        ("last_seen_at", "DATETIME"),
        ("snapshot_hash", "VARCHAR(64)"),
        ("parser_name", "VARCHAR(100)"),
        ("parser_version", "VARCHAR(50)"),
        ("collection_method", "VARCHAR(50)"),
    ),
    "job_profile_skill": (
        ("source_type_count", "INTEGER NOT NULL DEFAULT 0"),
        ("source_domain_count", "INTEGER NOT NULL DEFAULT 0"),
        ("company_count", "INTEGER NOT NULL DEFAULT 0"),
        (
            "cross_source_status",
            "VARCHAR(30) NOT NULL DEFAULT 'single_source'",
        ),
    ),
}

OBSERVATION_VERSION_COLUMNS = {
    "job_posting": (
        ("observation_version", "INTEGER NOT NULL DEFAULT 1"),
    ),
}

REVISION_OBSERVATION_COLUMNS = {
    "job_posting_revision": (
        ("observation_at", "DATETIME"),
        ("observation_identity", "VARCHAR(64)"),
    ),
}

PROFILE_SKILL_EVIDENCE_COLUMNS = {
    "job_profile_skill": (
        ("required_ratio", "FLOAT NOT NULL DEFAULT 0"),
        ("preferred_ratio", "FLOAT NOT NULL DEFAULT 0"),
        ("first_published_at", "DATETIME"),
        ("last_published_at", "DATETIME"),
    ),
}

TASK10_QUALITY_CORRECTION_COLUMNS = {
    "job_posting": (
        (
            "provenance_status",
            "VARCHAR(30) NOT NULL DEFAULT 'unverified'",
        ),
    ),
    "job_profile_skill": (
        (
            "ratio_evidence_status",
            "VARCHAR(30) NOT NULL DEFAULT 'unknown'",
        ),
    ),
}

DOMESTIC_JOB_MARKET_SCOPE_COLUMNS = {
    "job_source": (
        (
            "market_scope",
            "VARCHAR(30) NOT NULL DEFAULT 'pending_review'",
        ),
    ),
}

REVISION_OBSERVATION_INDEXES = (
    (
        "job_posting_revision",
        ("job_posting_id", "payload_hash", "observation_identity"),
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_posting_revision_observation "
        "ON job_posting_revision(job_posting_id, payload_hash, observation_identity)",
    ),
)

HARD_METRICS_INDEXES = (
    (
        "job_profile",
        ("generation_key",),
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_job_profile_generation_key ON job_profile(generation_key)",
    ),
    (
        "evolution_event",
        ("generation_key",),
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_evolution_generation_key ON evolution_event(generation_key)",
    ),
)

MULTISOURCE_INDEXES = (
    (
        "job_posting",
        ("source_domain",),
        "CREATE INDEX IF NOT EXISTS "
        "idx_job_posting_source_domain ON job_posting(source_domain)",
    ),
    (
        "job_posting",
        ("source_id", "source_record_id"),
        "CREATE INDEX IF NOT EXISTS idx_job_posting_source_record "
        "ON job_posting(source_id, source_record_id)",
    ),
    (
        "job_posting",
        ("published_at_trusted", "published_at"),
        "CREATE INDEX IF NOT EXISTS idx_job_posting_trusted_published_at "
        "ON job_posting(published_at_trusted, published_at)",
    ),
)

MIGRATIONS = (
    (MIGRATION_ID, HARD_METRICS_COLUMNS, HARD_METRICS_INDEXES),
    (MULTISOURCE_MIGRATION_ID, MULTISOURCE_COLUMNS, MULTISOURCE_INDEXES),
    (OBSERVATION_VERSION_MIGRATION_ID, OBSERVATION_VERSION_COLUMNS, ()),
    (
        REVISION_OBSERVATION_MIGRATION_ID,
        REVISION_OBSERVATION_COLUMNS,
        REVISION_OBSERVATION_INDEXES,
    ),
    (PROFILE_SKILL_EVIDENCE_MIGRATION_ID, PROFILE_SKILL_EVIDENCE_COLUMNS, ()),
    (
        TASK10_QUALITY_CORRECTION_MIGRATION_ID,
        TASK10_QUALITY_CORRECTION_COLUMNS,
        (),
    ),
    (
        DOMESTIC_JOB_MARKET_SCOPE_MIGRATION_ID,
        DOMESTIC_JOB_MARKET_SCOPE_COLUMNS,
        (),
    ),
)

# Kept as a compatibility alias for callers that inspect the original migration.
ADDITIVE_COLUMNS = HARD_METRICS_COLUMNS


class DatabaseOperationalError(RuntimeError):
    """A database or verified-backup operation could not be completed."""


def sqlite_database_path(database_url: str) -> Path | None:
    prefixes = ("sqlite+aiosqlite:///", "sqlite:///")
    for prefix in prefixes:
        if database_url.startswith(prefix):
            raw_path = database_url.split("?", 1)[0][len(prefix) :]
            return Path(unquote(raw_path)).resolve()
    return None


def _backup_sqlite_database_untyped(
    database_url: str, backup_dir: Path
) -> Path | None:
    source_path = sqlite_database_path(database_url)
    if source_path is None:
        return None
    if not source_path.exists():
        raise FileNotFoundError(f"数据库文件不存在: {source_path}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target_path = backup_dir / f"{source_path.stem}-{stamp}.db"
    with closing(sqlite3.connect(source_path)) as source, closing(
        sqlite3.connect(target_path)
    ) as target:
        source.backup(target)
    return target_path


def backup_sqlite_database(database_url: str, backup_dir: Path) -> Path | None:
    source_path = sqlite_database_path(database_url)
    if source_path is not None and not source_path.exists():
        raise DatabaseOperationalError(
            f"database file does not exist: {source_path}"
        )
    try:
        return _backup_sqlite_database_untyped(database_url, backup_dir)
    except (OSError, sqlite3.Error) as exc:
        raise DatabaseOperationalError(f"database backup failed: {exc}") from exc


def _schema_state(sync_connection) -> tuple[set[str], dict[str, set[str]]]:
    inspector = inspect(sync_connection)
    tables = set(inspector.get_table_names())
    migration_tables = {
        table
        for _, additive_columns, _ in MIGRATIONS
        for table in additive_columns
    }
    columns = {
        table: {item["name"] for item in inspector.get_columns(table)}
        for table in migration_tables
        if table in tables
    }
    return tables, columns


async def _repair_revision_observations(connection: AsyncConnection) -> None:
    tables, columns = await connection.run_sync(_schema_state)
    required = {
        "id",
        "job_posting_id",
        "revision_no",
        "payload_hash",
        "raw_payload",
        "created_at",
        "observation_at",
        "observation_identity",
    }
    if (
        "job_posting_revision" not in tables
        or not required <= columns.get("job_posting_revision", set())
    ):
        return
    rows = (
        await connection.execute(
            text(
                "SELECT id, job_posting_id, revision_no, payload_hash, raw_payload, "
                "created_at, observation_at, observation_identity "
                "FROM job_posting_revision ORDER BY revision_no, id"
            )
        )
    ).mappings().all()
    await connection.execute(
        text("DROP INDEX IF EXISTS uq_posting_revision_observation")
    )
    repaired: list[tuple[int, int, str, str]] = []
    for row in rows:
        try:
            payload = json.loads(row["raw_payload"] or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        observed = observation_time(payload)
        if observed == datetime.min:
            observed = observation_datetime(row["observation_at"])
        if observed == datetime.min:
            observed = observation_datetime(row["created_at"])
        if observed == datetime.min:
            raise DatabaseOperationalError(
                "legacy revision has no deterministic observation timestamp"
            )
        identity = observation_identity(payload, observed)
        await connection.execute(
            text(
                "UPDATE job_posting_revision SET observation_at=:observed, "
                "observation_identity=:identity WHERE id=:id"
            ),
            {"observed": observed, "identity": identity, "id": row["id"]},
        )
        repaired.append(
            (row["id"], row["job_posting_id"], row["payload_hash"], identity)
        )

    keepers: dict[tuple[int, str, str], int] = {}
    duplicate_ids: list[int] = []
    for row_id, posting_id, payload_hash, identity in repaired:
        key = (posting_id, payload_hash, identity)
        if key in keepers:
            duplicate_ids.append(row_id)
        else:
            keepers[key] = row_id
    for row_id in duplicate_ids:
        await connection.execute(
            text("DELETE FROM job_posting_revision WHERE id=:id"), {"id": row_id}
        )

    await connection.execute(
        text(
            "CREATE TRIGGER IF NOT EXISTS trg_revision_observation_not_null_insert "
            "BEFORE INSERT ON job_posting_revision "
            "WHEN NEW.observation_at IS NULL OR NEW.observation_identity IS NULL "
            "BEGIN SELECT RAISE(ABORT, 'revision observation fields cannot be null'); END"
        )
    )
    await connection.execute(
        text(
            "CREATE TRIGGER IF NOT EXISTS trg_revision_observation_not_null_update "
            "BEFORE UPDATE OF observation_at, observation_identity "
            "ON job_posting_revision "
            "WHEN NEW.observation_at IS NULL OR NEW.observation_identity IS NULL "
            "BEGIN SELECT RAISE(ABORT, 'revision observation fields cannot be null'); END"
        )
    )


async def _backfill_profile_skill_evidence(connection: AsyncConnection) -> None:
    # Profile-level requirement labels cannot reconstruct posting-level ratios.
    return None


async def _backfill_task10_quality_corrections(
    connection: AsyncConnection,
) -> None:
    tables, columns = await connection.run_sync(_schema_state)
    posting_columns = columns.get("job_posting", set())
    profile_skill_columns = columns.get("job_profile_skill", set())
    if "job_profile_skill" in tables and {
        "required_ratio",
        "preferred_ratio",
        "ratio_evidence_status",
    } <= profile_skill_columns:
        await connection.execute(
            text(
                "UPDATE job_profile_skill SET required_ratio=-1, "
                "preferred_ratio=-1 WHERE ratio_evidence_status='unknown'"
            )
        )
    if "job_posting" in tables and {
        "provenance_status",
        "published_at_trusted",
    } <= posting_columns:
        await connection.execute(
            text(
                "UPDATE job_posting SET published_at_trusted=0 "
                "WHERE provenance_status <> 'approved'"
            )
        )
    if "job_posting" in tables and {
        "provenance_status",
        "gate_status",
    } <= posting_columns:
        await connection.execute(
            text(
                "UPDATE job_posting SET gate_status='review' "
                "WHERE provenance_status <> 'approved' AND gate_status='valid'"
            )
        )
    if "job_posting" in tables and {
        "provenance_status",
        "status",
    } <= posting_columns:
        await connection.execute(
            text(
                "UPDATE job_posting SET status='review' "
                "WHERE provenance_status <> 'approved' AND status='valid'"
            )
        )


async def ensure_competition_schema(connection: AsyncConnection) -> list[str]:
    await connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS schema_migration ("
            "migration_id VARCHAR(100) PRIMARY KEY, applied_at DATETIME NOT NULL)"
        )
    )
    recorded = set(
        (
            await connection.execute(
                text("SELECT migration_id FROM schema_migration")
            )
        ).scalars()
    )
    applied: list[str] = []
    for migration_id, additive_columns, indexes in MIGRATIONS:
        tables, existing_columns = await connection.run_sync(_schema_state)
        for table, definitions in additive_columns.items():
            if table not in tables:
                continue
            for column, ddl in definitions:
                if column in existing_columns.get(table, set()):
                    continue
                await connection.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                )
                existing_columns.setdefault(table, set()).add(column)

        if migration_id == REVISION_OBSERVATION_MIGRATION_ID:
            await _repair_revision_observations(connection)
        elif migration_id == PROFILE_SKILL_EVIDENCE_MIGRATION_ID:
            await _backfill_profile_skill_evidence(connection)
        elif migration_id == TASK10_QUALITY_CORRECTION_MIGRATION_ID:
            await _backfill_task10_quality_corrections(connection)

        for table, required_columns, ddl in indexes:
            if table not in tables:
                continue
            if not set(required_columns) <= existing_columns.get(table, set()):
                continue
            await connection.execute(text(ddl))

        if migration_id not in recorded:
            await connection.execute(
                text(
                    "INSERT INTO schema_migration (migration_id, applied_at) "
                    "VALUES (:migration_id, :applied_at)"
                ),
                {"migration_id": migration_id, "applied_at": datetime.now()},
            )
            applied.append(migration_id)
    return applied


async def migrate_database(database_url: str = ASYNC_DATABASE_URL) -> list[str]:
    import model_class.job_competency  # noqa: F401
    import model_class.knowledge_base  # noqa: F401

    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            applied = await ensure_competition_schema(connection)
            await connection.run_sync(Base.metadata.create_all)
        return applied
    finally:
        await engine.dispose()
