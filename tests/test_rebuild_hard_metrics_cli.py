import sqlite3

import pytest


def _database_url(path):
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def test_full_mode_requires_explicit_confirmation(tmp_path):
    from src.rebuild_hard_metrics import build_parser, validate_args

    args = build_parser().parse_args(
        ["--full", "--database-url", _database_url(tmp_path / "jobs.db")]
    )

    with pytest.raises(ValueError, match="--confirm"):
        validate_args(args)


@pytest.mark.parametrize(
    "argv",
    [
        ["--dry-run", "--repair"],
        ["--full", "--confirm", "--repair-audit"],
        ["--incremental", "--family-code", "DATA_ENGINEER", "--repair"],
        ["--full", "--confirm", "--repair"],
        ["--dry-run", "--confirm"],
        ["--incremental", "--confirm", "--family-code", "DATA_ENGINEER"],
        ["--full", "--confirm", "--family-code", "DATA_ENGINEER"],
        ["--dry-run", "--family-code", "DATA_ENGINEER"],
        ["--dry-run", "--locks-root", "ignored-locks"],
    ],
)
def test_repair_modes_require_exact_safe_flag_combinations(argv):
    from src.rebuild_hard_metrics import build_parser, validate_args

    args = build_parser().parse_args(argv)

    with pytest.raises(ValueError):
        validate_args(args)


def test_dry_run_repair_audit_accepts_safe_run_id(tmp_path):
    from src.rebuild_hard_metrics import build_parser, validate_args

    args = build_parser().parse_args(
        [
            "--dry-run",
            "--repair-audit",
            "--repair-run-id",
            "audit-20260806",
            "--database-url",
            _database_url(tmp_path / "jobs.db"),
        ]
    )

    validate_args(args)


def test_custom_repairs_root_selects_its_own_default_control_namespace(tmp_path):
    from src.job_collection.security import default_control_state_root
    from src.rebuild_hard_metrics import _default_locks_root

    repairs_root = tmp_path / "repairs"

    assert _default_locks_root(repairs_root) == (
        default_control_state_root(repairs_root) / "locks"
    )


def test_dry_run_reads_database_without_creating_backup(tmp_path):
    from src.rebuild_hard_metrics import inspect_database

    database = tmp_path / "jobs.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE job_posting (id INTEGER PRIMARY KEY)")
        connection.executemany(
            "INSERT INTO job_posting (id) VALUES (?)", [(1,), (2,), (3,)]
        )

    result = inspect_database(_database_url(database), tmp_path / "backups")

    assert result["mode"] == "dry-run"
    assert result["posting_count"] == 3
    assert result["database_exists"] is True
    assert list((tmp_path / "backups").glob("*.db")) == []


def test_confirmed_full_mode_creates_readable_backup(tmp_path):
    from src.rebuild_hard_metrics import prepare_full_rebuild

    database = tmp_path / "jobs.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE job_posting (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO job_posting (id) VALUES (1)")

    backup = prepare_full_rebuild(
        _database_url(database), tmp_path / "backups", confirmed=True
    )

    assert backup is not None and backup.exists()
    with sqlite3.connect(backup) as connection:
        assert connection.execute("SELECT COUNT(*) FROM job_posting").fetchone()[0] == 1


def test_after_collection_run_requires_confirmed_full_mode():
    from src.rebuild_hard_metrics import build_parser, validate_args

    parser = build_parser()
    valid = parser.parse_args(
        ["--full", "--confirm", "--after-collection-run", "boss-production-001"]
    )
    validate_args(valid)

    for argv in (
        ["--dry-run", "--after-collection-run", "boss-production-001"],
        [
            "--incremental",
            "--family-code",
            "JAVA_DEVELOPER",
            "--after-collection-run",
            "boss-production-001",
        ],
    ):
        with pytest.raises(ValueError, match="after-collection"):
            validate_args(parser.parse_args(argv))


def test_after_collection_run_verifies_committed_report_and_backup(tmp_path):
    import hashlib
    import json

    from src.rebuild_hard_metrics import _validate_collection_run_for_rebuild

    database = tmp_path / "jobs.db"
    backup = tmp_path / "collection-backup.db"
    run_root = tmp_path / "collections" / "boss-production-001"
    run_root.mkdir(parents=True)
    report = run_root / "report.json"
    report.write_bytes(b'{"run_id":"boss-production-001"}')
    with sqlite3.connect(backup) as connection:
        connection.execute("CREATE TABLE evidence (id INTEGER PRIMARY KEY)")
    summary = json.dumps(
        {
            "backup_path": str(backup),
            "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
        }
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE collection_run (run_id TEXT, status TEXT, staging_dir TEXT, summary_json TEXT)"
        )
        connection.execute(
            "INSERT INTO collection_run VALUES (?, ?, ?, ?)",
            ("boss-production-001", "completed", str(run_root), summary),
        )

    result = _validate_collection_run_for_rebuild(
        _database_url(database), "boss-production-001"
    )

    assert result["run_id"] == "boss-production-001"
    assert result["report_path"] == str(report.resolve())
    assert result["collection_backup_path"] == str(backup.resolve())
