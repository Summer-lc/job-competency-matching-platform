import sqlite3
import json
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pytest


def _database_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def _create_legacy_database(
    path: Path, *, include_published_at: bool = True
) -> None:
    published_at_column = "published_at DATETIME," if include_published_at else ""
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(
            f"""
            CREATE TABLE job_posting (
                id INTEGER PRIMARY KEY,
                record_id VARCHAR(80) NOT NULL,
                job_family_id VARCHAR(80) NOT NULL,
                job_title_raw VARCHAR(255) NOT NULL,
                job_title_normalized VARCHAR(255) NOT NULL,
                company_name VARCHAR(255) NOT NULL,
                source_name VARCHAR(255) NOT NULL,
                source_type VARCHAR(50) NOT NULL,
                source_url TEXT NOT NULL,
                {published_at_column}
                collected_at DATETIME NOT NULL,
                job_description_raw TEXT NOT NULL,
                content_hash VARCHAR(64) NOT NULL,
                simhash VARCHAR(16) NOT NULL,
                source_score FLOAT NOT NULL,
                quality_score FLOAT NOT NULL,
                status VARCHAR(30) NOT NULL
            );
            CREATE TABLE job_profile (
                id INTEGER PRIMARY KEY,
                family_code VARCHAR(80) NOT NULL,
                name VARCHAR(255) NOT NULL,
                description TEXT NOT NULL,
                responsibilities_json TEXT NOT NULL,
                industry_scenarios_json TEXT NOT NULL,
                status VARCHAR(30) NOT NULL,
                level VARCHAR(30) NOT NULL,
                tech_stack VARCHAR(80) NOT NULL,
                version INTEGER NOT NULL,
                confidence FLOAT NOT NULL,
                review_status VARCHAR(30) NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );
            CREATE TABLE skill (
                id INTEGER PRIMARY KEY,
                name VARCHAR(150) NOT NULL,
                category VARCHAR(80) NOT NULL,
                aliases_json TEXT NOT NULL,
                created_at DATETIME NOT NULL
            );
            CREATE TABLE job_profile_skill (
                id INTEGER PRIMARY KEY,
                job_profile_id INTEGER NOT NULL,
                skill_id INTEGER NOT NULL,
                requirement_type VARCHAR(30) NOT NULL,
                proficiency_level VARCHAR(30) NOT NULL,
                confidence FLOAT NOT NULL,
                evidence_count INTEGER NOT NULL,
                prevalence FLOAT NOT NULL,
                FOREIGN KEY(job_profile_id) REFERENCES job_profile(id),
                FOREIGN KEY(skill_id) REFERENCES skill(id)
            );
            CREATE TABLE evolution_event (
                id INTEGER PRIMARY KEY,
                family_code VARCHAR(80) NOT NULL,
                current_profile_id INTEGER NOT NULL,
                entity_type VARCHAR(50) NOT NULL,
                entity_key VARCHAR(500) NOT NULL,
                change_type VARCHAR(30) NOT NULL,
                evidence_count INTEGER NOT NULL,
                created_at DATETIME NOT NULL
            );
            INSERT INTO job_posting (
                id, record_id, job_family_id, job_title_raw, job_title_normalized,
                company_name, source_name, source_type, source_url, collected_at,
                job_description_raw, content_hash, simhash, source_score,
                quality_score, status
            ) VALUES (
                1, 'LEGACY-1', 'DATA_ENGINEER', '数据工程师', '数据工程师',
                '示例企业', '企业官网', 'company_official',
                'https://example.com/jobs/1', '2026-07-01',
                '负责数据平台开发和维护，要求熟悉Python与Flink实时计算。',
                'hash', '0000000000000000', 0.95, 0.9, 'valid'
            );
            INSERT INTO job_profile (
                id, family_code, name, description, responsibilities_json,
                industry_scenarios_json, status, level, tech_stack, version,
                confidence, review_status, created_at, updated_at
            ) VALUES (
                1, 'DATA_ENGINEER', 'Data Engineer', '', '[]', '[]',
                'existing', 'all', 'general', 1, 0.8, 'approved',
                '2026-07-01', '2026-07-01'
            );
            INSERT INTO skill (
                id, name, category, aliases_json, created_at
            ) VALUES (1, 'Python', 'programming_language', '[]', '2026-07-01');
            INSERT INTO job_profile_skill (
                id, job_profile_id, skill_id, requirement_type,
                proficiency_level, confidence, evidence_count, prevalence
            ) VALUES (1, 1, 1, 'required', 'working', 0.8, 3, 0.6);
            """
        )


def _apply_hard_metrics_migration(path: Path) -> None:
    from src.schema_migration import HARD_METRICS_COLUMNS, HARD_METRICS_INDEXES

    with closing(sqlite3.connect(path)) as connection, connection:
        for table, definitions in HARD_METRICS_COLUMNS.items():
            for column, ddl in definitions:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        for _, _, ddl in HARD_METRICS_INDEXES:
            connection.execute(ddl)
        connection.execute(
            "CREATE TABLE schema_migration ("
            "migration_id VARCHAR(100) PRIMARY KEY, applied_at DATETIME NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_migration (migration_id, applied_at) "
            "VALUES ('competition_hard_metrics_v1', '2026-08-01')"
        )


def _apply_pre_task10_migrations(path: Path) -> None:
    from src.schema_migration import (
        MIGRATIONS,
        PROFILE_SKILL_EVIDENCE_MIGRATION_ID,
    )

    with closing(sqlite3.connect(path)) as connection, connection:
        tables = _tables(connection)
        for migration_id, additive_columns, _ in MIGRATIONS:
            if migration_id == PROFILE_SKILL_EVIDENCE_MIGRATION_ID:
                break
            for table, definitions in additive_columns.items():
                if table not in tables:
                    continue
                existing = _columns(connection, table)
                for column, ddl in definitions:
                    if column not in existing:
                        connection.execute(
                            f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
                        )
                        existing.add(column)
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration (migration_id, applied_at) "
                "VALUES (?, '2026-08-09')",
                (migration_id,),
            )


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _migration_ids(connection: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT migration_id FROM schema_migration ORDER BY applied_at, rowid"
        )
    ]


def _indexes(connection: sqlite3.Connection, table: str) -> dict[str, tuple[str, ...]]:
    result = {}
    for row in connection.execute(f"PRAGMA index_list({table})"):
        name = row[1]
        result[name] = tuple(
            item[2]
            for item in connection.execute(f'PRAGMA index_info("{name}")')
        )
    return result


@pytest.mark.asyncio
async def test_migration_preserves_legacy_rows_and_adds_hard_metric_schema(tmp_path):
    from src.schema_migration import migrate_database

    database = tmp_path / "legacy.db"
    _create_legacy_database(database)

    applied = await migrate_database(_database_url(database))

    assert applied == [
        "competition_hard_metrics_v1",
        "multisource_provenance_v1",
        "job_posting_observation_version_v1",
        "job_posting_revision_observation_v1",
        "profile_skill_evidence_v1",
        "task10_quality_corrections_v1",
        "domestic_job_market_scope_v1",
    ]
    with closing(sqlite3.connect(database)) as connection, connection:
        assert {"machine_level", "gate_status", "gate_rule_version"} <= _columns(
            connection, "job_posting"
        )
        assert {"observation_at", "observation_identity"} <= _columns(
            connection, "job_posting_revision"
        )
        assert _indexes(connection, "job_posting_revision")[
            "uq_posting_revision_observation"
        ] == ("job_posting_id", "payload_hash", "observation_identity")
        assert {"profile_kind", "period_key", "generation_key"} <= _columns(
            connection, "job_profile"
        )
        assert {"previous_period", "current_period", "generation_key"} <= _columns(
            connection, "evolution_event"
        )
        assert connection.execute("SELECT COUNT(*) FROM job_posting").fetchone()[0] == 1
        assert {
            "source_id",
            "source_domain",
            "source_record_id",
            "published_at_evidence",
            "published_at_confidence",
            "published_at_trusted",
            "first_seen_at",
            "last_seen_at",
            "snapshot_hash",
            "parser_name",
            "parser_version",
            "collection_method",
        } <= _columns(connection, "job_posting")
        legacy = connection.execute(
            "SELECT source_id, published_at_confidence, published_at_trusted "
            "FROM job_posting WHERE id = 1"
        ).fetchone()
        assert legacy == (None, 0.0, 0)
        assert {"pipeline_run", "evolution_evidence", "acceptance_snapshot"} <= _tables(
            connection
        )
        assert {
            "job_source",
            "collection_run",
            "collection_snapshot",
            "data_repair_audit",
        } <= _tables(connection)
        assert "market_scope" in _columns(connection, "job_source")
        assert {
            "source_type_count",
            "source_domain_count",
            "company_count",
            "cross_source_status",
            "required_ratio",
            "preferred_ratio",
            "first_published_at",
            "last_published_at",
            "ratio_evidence_status",
        } <= _columns(connection, "job_profile_skill")
        legacy_profile_skill = connection.execute(
            "SELECT id, source_type_count, source_domain_count, company_count, "
            "cross_source_status, required_ratio, preferred_ratio, "
            "first_published_at, last_published_at, ratio_evidence_status "
            "FROM job_profile_skill WHERE id = 1"
        ).fetchone()
        assert legacy_profile_skill == (
            1,
            0,
            0,
            0,
            "single_source",
            -1.0,
            -1.0,
            None,
            None,
            "unknown",
        )
        posting_indexes = _indexes(connection, "job_posting")
        assert posting_indexes["idx_job_posting_source_domain"] == (
            "source_domain",
        )
        assert posting_indexes["idx_job_posting_source_record"] == (
            "source_id",
            "source_record_id",
        )
        assert posting_indexes["idx_job_posting_trusted_published_at"] == (
            "published_at_trusted",
            "published_at",
        )
        assert _migration_ids(connection) == [
            "competition_hard_metrics_v1",
            "multisource_provenance_v1",
            "job_posting_observation_version_v1",
            "job_posting_revision_observation_v1",
            "profile_skill_evidence_v1",
            "task10_quality_corrections_v1",
            "domestic_job_market_scope_v1",
        ]


@pytest.mark.asyncio
async def test_hard_metrics_database_applies_only_provenance_migration(tmp_path):
    from src.schema_migration import HARD_METRICS_COLUMNS, migrate_database

    database = tmp_path / "hard-metrics.db"
    _create_legacy_database(database)
    _apply_hard_metrics_migration(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        assert {
            column for column, _ in HARD_METRICS_COLUMNS["job_posting"]
        } <= _columns(connection, "job_posting")

    assert await migrate_database(_database_url(database)) == [
        "multisource_provenance_v1",
        "job_posting_observation_version_v1",
        "job_posting_revision_observation_v1",
        "profile_skill_evidence_v1",
        "task10_quality_corrections_v1",
        "domestic_job_market_scope_v1",
    ]
    with closing(sqlite3.connect(database)) as connection, connection:
        assert _migration_ids(connection) == [
            "competition_hard_metrics_v1",
            "multisource_provenance_v1",
            "job_posting_observation_version_v1",
            "job_posting_revision_observation_v1",
            "profile_skill_evidence_v1",
            "task10_quality_corrections_v1",
            "domestic_job_market_scope_v1",
        ]
        assert "source_domain" in _columns(connection, "job_posting")
        assert connection.execute("SELECT COUNT(*) FROM job_posting").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_recorded_migration_repairs_index_after_dependency_is_added(tmp_path):
    from src.schema_migration import migrate_database

    database = tmp_path / "partial-legacy.db"
    _create_legacy_database(database, include_published_at=False)

    assert await migrate_database(_database_url(database)) == [
        "competition_hard_metrics_v1",
        "multisource_provenance_v1",
        "job_posting_observation_version_v1",
        "job_posting_revision_observation_v1",
        "profile_skill_evidence_v1",
        "task10_quality_corrections_v1",
        "domestic_job_market_scope_v1",
    ]
    with closing(sqlite3.connect(database)) as connection, connection:
        assert "idx_job_posting_trusted_published_at" not in _indexes(
            connection, "job_posting"
        )
        connection.execute("ALTER TABLE job_posting ADD COLUMN published_at DATETIME")

    assert await migrate_database(_database_url(database)) == []
    with closing(sqlite3.connect(database)) as connection, connection:
        assert _indexes(connection, "job_posting")[
            "idx_job_posting_trusted_published_at"
        ] == ("published_at_trusted", "published_at")

    assert await migrate_database(_database_url(database)) == []


@pytest.mark.asyncio
async def test_migration_is_idempotent(tmp_path):
    from src.schema_migration import migrate_database

    database = tmp_path / "legacy.db"
    _create_legacy_database(database)

    assert await migrate_database(_database_url(database)) == [
        "competition_hard_metrics_v1",
        "multisource_provenance_v1",
        "job_posting_observation_version_v1",
        "job_posting_revision_observation_v1",
        "profile_skill_evidence_v1",
        "task10_quality_corrections_v1",
        "domestic_job_market_scope_v1",
    ]
    assert await migrate_database(_database_url(database)) == []


@pytest.mark.asyncio
async def test_task10_migration_upgrades_pre_task10_schema_and_backfills_preferred(
    tmp_path,
):
    from src.schema_migration import migrate_database

    database = tmp_path / "pre-task10.db"
    _create_legacy_database(database)
    _apply_hard_metrics_migration(database)
    _apply_pre_task10_migrations(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "UPDATE job_profile_skill SET requirement_type = 'preferred' WHERE id = 1"
        )

    assert await migrate_database(_database_url(database)) == [
        "profile_skill_evidence_v1",
        "task10_quality_corrections_v1",
        "domestic_job_market_scope_v1",
    ]
    with closing(sqlite3.connect(database)) as connection, connection:
        assert connection.execute(
            "SELECT required_ratio, preferred_ratio, first_published_at, "
            "last_published_at, ratio_evidence_status "
            "FROM job_profile_skill WHERE id = 1"
        ).fetchone() == (-1.0, -1.0, None, None, "unknown")
    assert await migrate_database(_database_url(database)) == []


@pytest.mark.asyncio
async def test_quality_correction_migration_marks_legacy_evidence_unknown(tmp_path):
    from src.schema_migration import migrate_database

    database = tmp_path / "legacy-evidence-unknown.db"
    _create_legacy_database(database)

    applied = await migrate_database(_database_url(database))

    assert "task10_quality_corrections_v1" in applied
    assert applied[-1] == "domestic_job_market_scope_v1"
    with closing(sqlite3.connect(database)) as connection, connection:
        assert "provenance_status" in _columns(connection, "job_posting")
        assert "ratio_evidence_status" in _columns(
            connection, "job_profile_skill"
        )
        assert connection.execute(
            "SELECT provenance_status, status, gate_status "
            "FROM job_posting WHERE id = 1"
        ).fetchone() == ("unverified", "review", "review")
        assert connection.execute(
            "SELECT required_ratio, preferred_ratio, ratio_evidence_status "
            "FROM job_profile_skill WHERE id = 1"
        ).fetchone() == (-1.0, -1.0, "unknown")


@pytest.mark.asyncio
async def test_recorded_task10_migration_repairs_missing_columns_after_interruption(
    tmp_path,
):
    from src.schema_migration import migrate_database

    database = tmp_path / "partial-task10.db"
    _create_legacy_database(database)
    _apply_hard_metrics_migration(database)
    _apply_pre_task10_migrations(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "ALTER TABLE job_profile_skill ADD COLUMN "
            "required_ratio FLOAT NOT NULL DEFAULT 0"
        )
        connection.execute(
            "INSERT INTO schema_migration (migration_id, applied_at) "
            "VALUES ('profile_skill_evidence_v1', '2026-08-10')"
        )

    assert await migrate_database(_database_url(database)) == [
        "task10_quality_corrections_v1",
        "domestic_job_market_scope_v1",
    ]
    with closing(sqlite3.connect(database)) as connection, connection:
        assert {
            "required_ratio",
            "preferred_ratio",
            "first_published_at",
            "last_published_at",
            "ratio_evidence_status",
        } <= _columns(connection, "job_profile_skill")
        assert connection.execute(
            "SELECT required_ratio, preferred_ratio, ratio_evidence_status "
            "FROM job_profile_skill WHERE id = 1"
        ).fetchone() == (-1.0, -1.0, "unknown")


@pytest.mark.asyncio
async def test_quality_correction_resets_recorded_v1_fabricated_ratios_idempotently(
    tmp_path,
):
    from src.schema_migration import migrate_database

    database = tmp_path / "recorded-fabricated-ratios.db"
    _create_legacy_database(database)
    _apply_hard_metrics_migration(database)
    _apply_pre_task10_migrations(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "ALTER TABLE job_profile_skill ADD COLUMN "
            "required_ratio FLOAT NOT NULL DEFAULT 0"
        )
        connection.execute(
            "ALTER TABLE job_profile_skill ADD COLUMN "
            "preferred_ratio FLOAT NOT NULL DEFAULT 0"
        )
        connection.execute(
            "ALTER TABLE job_profile_skill ADD COLUMN first_published_at DATETIME"
        )
        connection.execute(
            "ALTER TABLE job_profile_skill ADD COLUMN last_published_at DATETIME"
        )
        connection.execute(
            "UPDATE job_profile_skill SET required_ratio=1.0, preferred_ratio=0.0"
        )
        connection.execute(
            "INSERT INTO schema_migration (migration_id, applied_at) "
            "VALUES ('profile_skill_evidence_v1', '2026-08-09')"
        )

    assert await migrate_database(_database_url(database)) == [
        "task10_quality_corrections_v1",
        "domestic_job_market_scope_v1",
    ]
    with closing(sqlite3.connect(database)) as connection, connection:
        assert connection.execute(
            "SELECT required_ratio, preferred_ratio, ratio_evidence_status "
            "FROM job_profile_skill WHERE id=1"
        ).fetchone() == (-1.0, -1.0, "unknown")

    assert await migrate_database(_database_url(database)) == []
    with closing(sqlite3.connect(database)) as connection, connection:
        assert connection.execute(
            "SELECT required_ratio, preferred_ratio, ratio_evidence_status "
            "FROM job_profile_skill WHERE id=1"
        ).fetchone() == (-1.0, -1.0, "unknown")


@pytest.mark.asyncio
async def test_migration_adds_observation_version_to_existing_multisource_database(
    tmp_path,
):
    from src.schema_migration import migrate_database

    database = tmp_path / "multisource.db"
    _create_legacy_database(database)
    assert await migrate_database(_database_url(database)) == [
        "competition_hard_metrics_v1",
        "multisource_provenance_v1",
        "job_posting_observation_version_v1",
        "job_posting_revision_observation_v1",
        "profile_skill_evidence_v1",
        "task10_quality_corrections_v1",
        "domestic_job_market_scope_v1",
    ]

    with closing(sqlite3.connect(database)) as connection, connection:
        assert "observation_version" in _columns(connection, "job_posting")
        assert connection.execute(
            "SELECT observation_version FROM job_posting WHERE id = 1"
        ).fetchone() == (1,)

    assert await migrate_database(_database_url(database)) == []


@pytest.mark.asyncio
async def test_revision_observation_migration_backfills_dedupes_and_blocks_nulls(
    tmp_path,
):
    from src.schema_migration import migrate_database

    database = tmp_path / "legacy-revisions.db"
    _create_legacy_database(database)
    duplicate_payload = json.dumps(
        {
            "record_id": "LEGACY-1",
            "source_name": "legacy-source",
            "last_seen_at": "2026-07-02T03:04:05+08:00",
            "job_description_raw": "same historical payload",
        },
        sort_keys=True,
    )
    equivalent_payload = json.dumps(
        {
            "record_id": "LEGACY-1",
            "source_name": "legacy-source",
            "last_seen_at": "2026-07-01T19:04:05Z",
            "job_description_raw": "same historical payload",
        },
        sort_keys=True,
    )
    fallback_payload = json.dumps(
        {
            "record_id": "LEGACY-1",
            "source_name": "legacy-source",
            "job_description_raw": "different historical payload",
        },
        sort_keys=True,
    )
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE import_batch (id INTEGER PRIMARY KEY);
            INSERT INTO import_batch (id) VALUES (1);
            CREATE TABLE job_posting_revision (
                id INTEGER PRIMARY KEY,
                job_posting_id INTEGER NOT NULL,
                import_batch_id INTEGER NOT NULL,
                revision_no INTEGER NOT NULL,
                payload_hash VARCHAR(64) NOT NULL,
                raw_payload TEXT NOT NULL,
                created_at DATETIME NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO job_posting_revision "
            "(id, job_posting_id, import_batch_id, revision_no, payload_hash, "
            "raw_payload, created_at) VALUES (?, 1, 1, ?, ?, ?, ?)",
            [
                (1, 1, "a" * 64, duplicate_payload, "2026-07-03 00:00:00"),
                (2, 2, "a" * 64, equivalent_payload, "2026-07-04 00:00:00"),
                (3, 3, "b" * 64, fallback_payload, "2026-07-05 06:07:08"),
            ],
        )

    await migrate_database(_database_url(database))

    with closing(sqlite3.connect(database)) as connection, connection:
        rows = connection.execute(
            "SELECT id, revision_no, observation_at, observation_identity "
            "FROM job_posting_revision ORDER BY id"
        ).fetchall()
        assert [row[:2] for row in rows] == [(1, 1), (3, 3)]
        assert rows[0][2].startswith("2026-07-01 19:04:05")
        assert rows[1][2].startswith("2026-07-05 06:07:08")
        assert all(len(row[3]) == 64 for row in rows)
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='job_posting_revision'"
            )
        }
        assert {
            "trg_revision_observation_not_null_insert",
            "trg_revision_observation_not_null_update",
        } <= triggers
        with pytest.raises(sqlite3.IntegrityError, match="observation"):
            connection.execute(
                "INSERT INTO job_posting_revision "
                "(job_posting_id, import_batch_id, revision_no, payload_hash, "
                "raw_payload, created_at, observation_at, observation_identity) "
                "VALUES (1, 1, 4, ?, '{}', '2026-07-06', NULL, NULL)",
                ("c" * 64,),
            )


@pytest.mark.asyncio
async def test_recorded_revision_migration_recomputes_old_columns_and_reimport_dedupes(
    tmp_path,
):
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from model_class.knowledge_base import JobPostingRevision
    from src.import_service import import_job_file
    from src.observation import observation_identity
    from src.schema_migration import migrate_database

    database = tmp_path / "pre-fix-observations.db"
    database_url = _database_url(database)
    await migrate_database(database_url)

    older = {
        "record_id": "OFFSET-MIGRATION-1",
        "collector_id": "migration-test",
        "job_family_id": "DATA_ENGINEER",
        "job_title_raw": "Data Engineer",
        "company_name": "Example Technology",
        "industry": "Software",
        "region": "Beijing",
        "source_name": "Example Careers",
        "source_type": "company_official",
        "source_url": "https://example.com/jobs/offset-migration-1",
        "published_at": "2026-07-01",
        "collected_at": "2026-08-01T08:00:00+08:00",
        "first_seen_at": "2026-08-01T08:00:00+08:00",
        "last_seen_at": "2026-08-01T08:00:00+08:00",
        "job_description_raw": (
            "Build and operate Python, Flink, and Kafka data pipelines with monitoring, "
            "quality controls, reliable orchestration, and production ownership."
        ),
    }
    newer = dict(
        older,
        collected_at="2026-08-02T00:00:00Z",
        last_seen_at="2026-08-02T00:00:00Z",
        job_description_raw=(
            "Own newer Python, Flink, and Kafka data platforms with governance, "
            "observability, reliable orchestration, and production support."
        ),
    )

    engine = create_async_engine(database_url)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        await import_job_file(
            session,
            json.dumps(older, ensure_ascii=False).encode("utf-8"),
            "migration-older.jsonl",
        )
        await import_job_file(
            session,
            json.dumps(newer, ensure_ascii=False).encode("utf-8"),
            "migration-newer.jsonl",
        )
    await engine.dispose()

    equivalent = dict(
        older,
        collected_at="2026-08-01T00:00:00Z",
        first_seen_at="2026-08-01T00:00:00Z",
        last_seen_at="2026-08-01T00:00:00Z",
        run_id="legacy-equivalent-package",
    )
    equivalent_raw = json.dumps(equivalent, ensure_ascii=False)
    with closing(sqlite3.connect(database)) as connection, connection:
        revision = connection.execute(
            "SELECT job_posting_id, import_batch_id, revision_no, payload_hash, "
            "raw_payload, created_at FROM job_posting_revision"
        ).fetchone()
        assert revision is not None
        connection.execute(
            "UPDATE job_posting_revision SET observation_at=?, "
            "observation_identity=?",
            ("2026-08-01 08:00:00", "1" * 64),
        )
        connection.execute(
            "INSERT INTO job_posting_revision "
            "(job_posting_id, import_batch_id, revision_no, payload_hash, raw_payload, "
            "created_at, observation_at, observation_identity) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                revision[0],
                revision[1],
                revision[2] + 1,
                revision[3],
                equivalent_raw,
                revision[5],
                "2026-08-01 00:00:00",
                "2" * 64,
            ),
        )

    assert await migrate_database(database_url) == []

    expected_identity = observation_identity(older, datetime(2026, 8, 1, 0, 0))
    with closing(sqlite3.connect(database)) as connection, connection:
        rows = connection.execute(
            "SELECT observation_at, observation_identity "
            "FROM job_posting_revision ORDER BY revision_no, id"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0].startswith("2026-08-01 00:00:00")
        assert rows[0][1] == expected_identity

    engine = create_async_engine(database_url)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        reimported = await import_job_file(
            session,
            json.dumps(
                dict(equivalent, run_id="post-migration-repackage"),
                ensure_ascii=False,
            ).encode("utf-8"),
            "migration-reimport.jsonl",
        )
        revision_count = await session.scalar(
            select(func.count()).select_from(JobPostingRevision)
        )
    await engine.dispose()

    assert reimported["revised"] == 0
    assert reimported["skipped"] == 1
    assert revision_count == 1


def test_sqlite_backup_is_readable_and_preserves_rows(tmp_path):
    from src.schema_migration import backup_sqlite_database

    database = tmp_path / "source.db"
    _create_legacy_database(database)

    backup = backup_sqlite_database(_database_url(database), tmp_path / "backups")

    assert backup is not None and backup.exists()
    with closing(sqlite3.connect(backup)) as connection, connection:
        assert connection.execute("SELECT COUNT(*) FROM job_posting").fetchone()[0] == 1


def test_sqlite_backup_missing_database_is_typed_operational_failure(tmp_path):
    from src.schema_migration import DatabaseOperationalError, backup_sqlite_database

    missing = tmp_path / "missing.db"
    with pytest.raises(DatabaseOperationalError, match="does not exist"):
        backup_sqlite_database(_database_url(missing), tmp_path / "backups")
