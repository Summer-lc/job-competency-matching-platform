import json
import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from model_class.base import Base


NOW = datetime(2026, 8, 6, 12, 0, 0)
OLD_DATE = datetime(2010, 1, 2, 9, 30, 0)
DUPLICATE_DESCRIPTION = (
    "Responsible for data platform design, Python development, deployment, "
    "operations, testing, quality controls, and reliable production delivery."
)


@pytest.fixture(autouse=True)
def _keep_test_control_roots_cleanup_safe(monkeypatch):
    """ACL enforcement has dedicated tests; repair logic uses disposable roots here."""

    from src.job_collection import security

    monkeypatch.setattr(security, "_WINDOWS", False)


def _database_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def test_write_repair_report_provisions_existing_trusted_root(tmp_path, monkeypatch):
    import src.job_data_repair as repair_module

    root = tmp_path / "data" / "repairs"
    root.mkdir(parents=True)
    provisioned = []
    real_provision = repair_module.provision_secure_directory

    def provision(path):
        provisioned.append(Path(path).resolve())
        return real_provision(path)

    monkeypatch.setattr(repair_module, "provision_secure_directory", provision)
    report = {
        "repair_run_id": "repair-root-test",
        "rule_version": repair_module.REPAIR_RULE_VERSION,
        "mode": "dry-run",
    }

    path = repair_module.write_repair_report(root, "repair-root-test", report)

    assert provisioned == [root.resolve()]
    assert path.is_file()


def test_read_repair_report_accepts_large_internal_audit(tmp_path):
    from src import job_data_repair as repair_module

    root = tmp_path / "repairs"
    report = {
        "repair_run_id": "large-audit",
        "rule_version": repair_module.REPAIR_RULE_VERSION,
        "mode": "apply",
        "padding": "x" * (4 * 1024 * 1024 + 1024),
    }
    repair_module.write_repair_report(root, "large-audit", report)

    loaded = repair_module.read_repair_report(root, "large-audit")

    assert loaded is not None
    assert loaded["repair_run_id"] == "large-audit"
    assert len(loaded["padding"]) == len(report["padding"])


def _posting_values(record_id: str, **changes):
    raw_payload = json.dumps(
        {
            "source_record_id": record_id,
            "job_description_raw": DUPLICATE_DESCRIPTION,
            "contact": "private.person@example.com",
        },
        sort_keys=True,
    )
    values = {
        "record_id": record_id,
        "job_family_id": "DATA_ENGINEER",
        "job_title_raw": "Data Engineer",
        "job_title_normalized": "Data Engineer",
        "company_name": "Private Example Ltd",
        "industry": "Software",
        "region": "Shanghai",
        "source_name": "Historical import",
        "source_type": "authorized_platform",
        "source_url": f"https://jobs.example.test/{record_id}",
        "source_id": "legacy_import",
        "source_domain": "jobs.example.test",
        "source_record_id": record_id,
        "published_at": NOW,
        "published_at_evidence": None,
        "published_at_confidence": 0.0,
        "published_at_trusted": False,
        "collected_at": NOW,
        "first_seen_at": NOW,
        "last_seen_at": NOW,
        "experience_requirement": "3-5 years",
        "education_requirement": "Bachelor",
        "salary_range": "20K-30K/month",
        "job_description_raw": DUPLICATE_DESCRIPTION,
        "content_hash": f"stale-{record_id}",
        "simhash": "ffffffffffffffff",
        "snapshot_hash": f"snapshot-{record_id}",
        "parser_name": "legacy",
        "parser_version": "1",
        "collection_method": "file_import",
        "source_score": 0.9,
        "quality_score": 0.9,
        "status": "valid",
        "gate_status": "valid",
        "raw_payload": raw_payload,
    }
    values.update(changes)
    return values


async def _seed_database(path: Path):
    import model_class.job_competency  # noqa: F401
    import model_class.knowledge_base  # noqa: F401
    from model_class.job_competency import JobPosting

    engine = create_async_engine(_database_url(path))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    original_payloads = {}
    async with Session() as session:
        rows = [
            JobPosting(
                **_posting_values(
                    "old-unsupported",
                    published_at=OLD_DATE,
                    published_at_trusted=True,
                    published_at_evidence="Date mentioned in JD prose",
                    published_at_confidence=0.95,
                )
            ),
            JobPosting(**_posting_values("salary-industry", industry="20K-30K/month")),
            JobPosting(
                **_posting_values(
                    "requirement-industry",
                    industry="Bachelor degree, 3-5 years experience, responsible for APIs",
                )
            ),
            JobPosting(**_posting_values("valid-industry", industry="Manufacturing")),
            JobPosting(**_posting_values("duplicate-a")),
            JobPosting(**_posting_values("duplicate-b", source_score=0.7)),
        ]
        session.add_all(rows)
        await session.commit()
        original_payloads = {row.record_id: row.raw_payload for row in rows}
    await engine.dispose()
    return original_payloads


def test_plan_repairs_is_pure_and_requires_structured_publication_evidence():
    from src.job_data_repair import plan_repairs

    posting = SimpleNamespace(
        published_at=OLD_DATE,
        collected_at=NOW,
        published_at_trusted=True,
        industry="20K-30K/month",
        raw_payload=json.dumps(
            {
                "job_description_raw": "This prose says the role was published in 2010."
            }
        ),
    )
    before = vars(posting).copy()

    changes = plan_repairs(posting)

    assert [(item.field_name, item.before, item.after, item.reason_code) for item in changes] == [
        ("published_at", OLD_DATE, None, "unsupported_suspicious_publication"),
        ("published_at_trusted", True, False, "unsupported_suspicious_publication"),
        ("industry", "20K-30K/month", "unknown", "salary_contaminated_industry"),
    ]
    assert vars(posting) == before

    posting.raw_payload = json.dumps(
        {
            "published_at": OLD_DATE.isoformat(),
            "published_at_evidence": "Structured source field",
        }
    )
    assert all(item.field_name != "published_at" for item in plan_repairs(posting))


def _legacy_zhaopin_authorization():
    from src.job_data_repair import LegacySourceAuthorization

    return LegacySourceAuthorization(
        source_id="zhaopin_legacy_import",
        source_name="智联招聘授权历史文件导入",
        source_type="authorized_platform",
        source_domain="www.zhaopin.com",
        collection_method="file_import",
        parser_name="zhaopin_legacy",
        parser_version="v1",
        authorization_note=(
            "团队于2026-08-12确认jd_raw.json在允许范围内采集并授权用于本次比赛研究。"
        ),
    )


def _legacy_zhaopin_posting(**changes):
    values = {
        "record_id": "C-JD-0001",
        "source_id": None,
        "source_name": "智联招聘",
        "source_type": None,
        "source_domain": None,
        "source_record_id": None,
        "source_url": "http://www.zhaopin.com/jobdetail/CC123J456.htm?refcode=4019",
        "provenance_status": "unverified",
        "published_at": None,
        "published_at_trusted": False,
        "collected_at": NOW,
        "first_seen_at": None,
        "last_seen_at": None,
        "parser_name": None,
        "parser_version": None,
        "collection_method": None,
        "industry": "Software",
        "raw_payload": json.dumps(
            {
                "record_id": "C-JD-0001",
                "source_name": "智联招聘",
                "source_url": (
                    "http://www.zhaopin.com/jobdetail/CC123J456.htm?refcode=4019"
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_legacy_zhaopin_candidate_requires_exact_identity_and_safe_domain():
    from src.job_data_repair import is_legacy_source_candidate

    authorization = _legacy_zhaopin_authorization()

    assert is_legacy_source_candidate(
        _legacy_zhaopin_posting(), authorization
    ) is True
    assert is_legacy_source_candidate(
        _legacy_zhaopin_posting(source_id="already_registered"), authorization
    ) is False
    assert is_legacy_source_candidate(
        _legacy_zhaopin_posting(provenance_status="approved"), authorization
    ) is False
    assert is_legacy_source_candidate(
        _legacy_zhaopin_posting(source_name="其他招聘网站"), authorization
    ) is False
    assert is_legacy_source_candidate(
        _legacy_zhaopin_posting(
            source_url="https://www.zhaopin.com.example.test/job/1"
        ),
        authorization,
    ) is False
    assert is_legacy_source_candidate(
        _legacy_zhaopin_posting(
            source_url="https://user:secret@www.zhaopin.com/jobdetail/1"
        ),
        authorization,
    ) is False
    assert is_legacy_source_candidate(
        _legacy_zhaopin_posting(source_url="https://www.zhaopin.com:bad/job/1"),
        authorization,
    ) is False


def test_authorization_changes_are_deterministic_and_preserve_raw_payload():
    from src.job_data_repair import plan_repairs

    authorization = _legacy_zhaopin_authorization()
    posting = _legacy_zhaopin_posting()
    raw_payload = posting.raw_payload

    changes = plan_repairs(posting, authorization=authorization)
    by_field = {item.field_name: item for item in changes}

    assert {
        "source_id",
        "source_name",
        "source_type",
        "source_domain",
        "source_record_id",
        "first_seen_at",
        "last_seen_at",
        "parser_name",
        "parser_version",
        "collection_method",
        "provenance_status",
    } <= set(by_field)
    assert by_field["source_id"].after == "zhaopin_legacy_import"
    assert by_field["source_domain"].after == "www.zhaopin.com"
    assert by_field["source_record_id"].after == "C-JD-0001"
    assert by_field["first_seen_at"].after == NOW
    assert by_field["last_seen_at"].after == NOW
    assert by_field["provenance_status"].after == "approved"
    assert all(
        item.reason_code == "authorized_legacy_zhaopin_source"
        for item in changes
    )
    assert posting.raw_payload == raw_payload
    assert plan_repairs(posting) == ()


def test_authorization_repairs_do_not_overwrite_existing_observation_fields():
    from src.job_data_repair import plan_repairs

    first_seen = datetime(2026, 7, 1, 8, 0, 0)
    last_seen = datetime(2026, 7, 2, 8, 0, 0)
    posting = _legacy_zhaopin_posting(
        source_record_id="source-record-1",
        first_seen_at=first_seen,
        last_seen_at=last_seen,
    )

    changes = plan_repairs(
        posting, authorization=_legacy_zhaopin_authorization()
    )
    changed_fields = {item.field_name for item in changes}

    assert "source_record_id" not in changed_fields
    assert "first_seen_at" not in changed_fields
    assert "last_seen_at" not in changed_fields


@pytest.mark.asyncio
async def test_authorization_audit_reports_scope_without_writes(tmp_path):
    from model_class.job_competency import JobPosting
    from model_class.knowledge_base import DataRepairAudit
    from src.job_data_repair import audit_job_data

    database = tmp_path / "legacy-zhaopin.db"
    engine = create_async_engine(_database_url(database))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        eligible = JobPosting(
            **_posting_values(
                "C-JD-0001",
                source_id=None,
                source_name="智联招聘",
                source_type="public",
                source_domain=None,
                source_record_id=None,
                source_url="http://www.zhaopin.com/jobdetail/CC123J456.htm",
                provenance_status="unverified",
                first_seen_at=None,
                last_seen_at=None,
                parser_name=None,
                parser_version=None,
                collection_method=None,
            )
        )
        ineligible = JobPosting(
            **_posting_values(
                "foreign-row",
                source_id=None,
                source_name="智联招聘",
                source_domain=None,
                source_url="https://jobs.example.test/foreign-row",
                provenance_status="unverified",
            )
        )
        session.add_all([eligible, ineligible])
        await session.commit()

        report = await audit_job_data(
            session,
            repair_run_id="legacy-auth-audit",
            authorization=_legacy_zhaopin_authorization(),
        )

        assert report["authorization"] == {
            "source_id": "zhaopin_legacy_import",
            "source_name": "智联招聘授权历史文件导入",
            "source_type": "authorized_platform",
            "source_domain": "www.zhaopin.com",
            "collection_method": "file_import",
            "parser_name": "zhaopin_legacy",
            "parser_version": "v1",
            "domain_scope": "zhaopin.com",
            "authorization_note": (
                "团队于2026-08-12确认jd_raw.json在允许范围内采集并授权用于本次比赛研究。"
            ),
        }
        authorization_changes = [
            item
            for item in report["changes"]
            if item["reason_code"] == "authorized_legacy_zhaopin_source"
        ]
        eligible_id = eligible.id
        assert authorization_changes
        assert {item["posting_id"] for item in authorization_changes} == {eligible_id}
        session.expire_all()
        unchanged = await session.get(JobPosting, eligible_id)
        assert unchanged.source_id is None
        assert unchanged.provenance_status == "unverified"
        assert unchanged.first_seen_at is None
        assert await session.scalar(
            select(func.count()).select_from(DataRepairAudit)
        ) == 0
    await engine.dispose()


def test_legacy_authorization_cli_requires_explicit_switch_and_note():
    from src.rebuild_hard_metrics import build_parser, validate_args

    parser = build_parser()
    common = [
        "--dry-run",
        "--repair-audit",
        "--repair-run-id",
        "legacy-auth-cli",
    ]

    with pytest.raises(ValueError, match="authorization note"):
        validate_args(
            parser.parse_args([*common, "--authorize-legacy-zhaopin"])
        )
    with pytest.raises(ValueError, match="authorization switch"):
        validate_args(
            parser.parse_args([*common, "--authorization-note", "confirmed scope"])
        )
    with pytest.raises(ValueError, match="repair mode"):
        validate_args(
            parser.parse_args(
                [
                    "--dry-run",
                    "--authorize-legacy-zhaopin",
                    "--authorization-note",
                    "confirmed scope",
                ]
            )
        )

    args = parser.parse_args(
        [
            *common,
            "--authorize-legacy-zhaopin",
            "--authorization-note",
            "confirmed scope",
        ]
    )
    validate_args(args)


def test_legacy_authorization_is_built_from_reviewed_china_registry():
    from src.rebuild_hard_metrics import (
        _legacy_source_authorization,
        build_parser,
        validate_args,
    )

    args = build_parser().parse_args(
        [
            "--dry-run",
            "--repair-audit",
            "--repair-run-id",
            "legacy-auth-registry",
            "--authorize-legacy-zhaopin",
            "--authorization-note",
            "confirmed scope",
        ]
    )
    validate_args(args)

    authorization = _legacy_source_authorization(args)

    assert authorization is not None
    assert authorization.source_id == "zhaopin_legacy_import"
    assert authorization.source_type == "authorized_platform"
    assert authorization.source_domain == "www.zhaopin.com"
    assert authorization.collection_method == "file_import"
    assert authorization.authorization_note == "confirmed scope"


def test_legacy_authorization_rejects_registry_outside_china(tmp_path):
    from src.rebuild_hard_metrics import (
        _legacy_source_authorization,
        build_parser,
        validate_args,
    )

    registry_path = Path("config/job_sources.json")
    document = json.loads(registry_path.read_text(encoding="utf-8"))
    source = next(
        item
        for item in document["sources"]
        if item["source_id"] == "zhaopin_legacy_import"
    )
    source["market_scope"] = "excluded"
    invalid_registry = tmp_path / "job_sources.json"
    invalid_registry.write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    args = build_parser().parse_args(
        [
            "--dry-run",
            "--repair-audit",
            "--repair-run-id",
            "legacy-auth-invalid-registry",
            "--authorize-legacy-zhaopin",
            "--authorization-note",
            "confirmed scope",
            "--source-registry",
            str(invalid_registry),
        ]
    )
    validate_args(args)

    with pytest.raises(ValueError, match="reviewed China file-import source"):
        _legacy_source_authorization(args)


@pytest.mark.asyncio
async def test_dry_run_reports_exact_changes_and_duplicates_without_writes(tmp_path):
    from model_class.job_competency import JobPosting
    from model_class.knowledge_base import DataRepairAudit
    from src.job_data_repair import audit_job_data

    database = tmp_path / "jobs.db"
    await _seed_database(database)
    engine = create_async_engine(_database_url(database))
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        before = {
            row.record_id: (row.published_at, row.published_at_trusted, row.industry)
            for row in (await session.execute(select(JobPosting))).scalars()
        }
        report = await audit_job_data(session, repair_run_id="audit-001")
        session.expire_all()
        after = {
            row.record_id: (row.published_at, row.published_at_trusted, row.industry)
            for row in (await session.execute(select(JobPosting))).scalars()
        }

        assert report["mode"] == "dry-run"
        assert report["row_count_before"] == report["row_count_after"] == 6
        assert report["changes"] == [
            {
                "posting_id": report["changes"][0]["posting_id"],
                "field_name": "published_at",
                "before": "2010-01-02T09:30:00",
                "after": None,
                "reason_code": "unsupported_suspicious_publication",
            },
            {
                "posting_id": report["changes"][1]["posting_id"],
                "field_name": "published_at_trusted",
                "before": True,
                "after": False,
                "reason_code": "unsupported_suspicious_publication",
            },
            {
                "posting_id": report["changes"][2]["posting_id"],
                "field_name": "industry",
                "before": "20K-30K/month",
                "after": "unknown",
                "reason_code": "salary_contaminated_industry",
            },
            {
                "posting_id": report["changes"][3]["posting_id"],
                "field_name": "industry",
                "before": "Bachelor degree, 3-5 years experience, responsible for APIs",
                "after": "unknown",
                "reason_code": "requirement_contaminated_industry",
            },
        ]
        assert report["duplicate_summary"] == {"groups": 1, "duplicates": 5}
        assert len(report["duplicate_groups"]) == 1
        assert before == after
        assert await session.scalar(
            select(func.count()).select_from(DataRepairAudit)
        ) == 0

    serialized = json.dumps(report, ensure_ascii=False)
    assert "private.person@example.com" not in serialized
    assert DUPLICATE_DESCRIPTION not in serialized
    assert "Private Example Ltd" not in serialized
    await engine.dispose()


@pytest.mark.asyncio
async def test_repair_full_confirm_applies_audits_and_is_idempotent(tmp_path):
    from model_class.job_competency import JobPosting
    from model_class.knowledge_base import DataRepairAudit, PipelineRun
    from src.job_data_service import content_hash, simhash64
    from src.rebuild_hard_metrics import build_parser, execute_rebuild

    database = tmp_path / "jobs.db"
    originals = await _seed_database(database)
    repairs_root = tmp_path / "data" / "repairs"
    backup_dir = tmp_path / "data" / "backups"
    args = build_parser().parse_args(
        [
            "--full",
            "--repair",
            "--confirm",
            "--repair-run-id",
            "repair-001",
            "--database-url",
            _database_url(database),
            "--backup-dir",
            str(backup_dir),
            "--repairs-root",
            str(repairs_root),
            "--locks-root",
            str(tmp_path / "locks"),
        ]
    )

    first = await execute_rebuild(args)

    assert first["repair"]["status"] == "completed"
    assert {
        "duplicate_summary",
        "gate_summary",
        "profile_summary",
        "evolution_summary",
        "knowledge_summary",
        "acceptance",
    } <= first.keys()
    assert first["repair"]["applied_change_count"] == len(
        first["repair"]["applied_changes"]
    )
    assert Path(first["backup_path"]).is_file()
    with sqlite3.connect(first["backup_path"]) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT COUNT(*) FROM job_posting").fetchone() == (6,)
    report_path = repairs_root / "repair-001" / "report.json"
    assert json.loads(report_path.read_text(encoding="utf-8"))["status"] == "completed"

    engine = create_async_engine(_database_url(database))
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        postings = {
            row.record_id: row
            for row in (await session.execute(select(JobPosting))).scalars()
        }
        assert len(postings) == 6
        assert postings["old-unsupported"].published_at is None
        assert postings["old-unsupported"].published_at_trusted is False
        assert postings["salary-industry"].industry == "unknown"
        assert postings["requirement-industry"].industry == "unknown"
        assert postings["valid-industry"].industry == "Manufacturing"
        for record_id, posting in postings.items():
            assert posting.raw_payload == originals[record_id]
            assert posting.source_id == "legacy_import"
            assert posting.snapshot_hash == f"snapshot-{record_id}"
            assert posting.content_hash == content_hash(posting.job_description_raw)
            assert posting.simhash == simhash64(posting.job_description_raw)
        assert sum(row.duplicate_of_id is not None for row in postings.values()) == 5
        audit_count = await session.scalar(
            select(func.count()).select_from(DataRepairAudit)
        )
        pipeline_count = await session.scalar(select(func.count()).select_from(PipelineRun))
        assert audit_count >= 4
        audits = list(
            (
                await session.execute(
                    select(DataRepairAudit).order_by(DataRepairAudit.id)
                )
            ).scalars()
        )
        assert all(item.repair_run_id == "repair-001" and item.applied for item in audits)
        audited_fields = {item.field_name for item in audits}
        assert {
            "published_at",
            "industry",
            "content_hash",
            "simhash",
            "duplicate_of_id",
        } <= audited_fields
        for item in audits:
            json.loads(item.before_json)
            json.loads(item.after_json)

    second = await execute_rebuild(args)
    assert second["repair"]["idempotent"] is True
    assert second["backup_path"] == first["backup_path"]
    assert len(list(backup_dir.glob("*.db"))) == 1
    async with Session() as session:
        assert await session.scalar(
            select(func.count()).select_from(DataRepairAudit)
        ) == audit_count
        assert await session.scalar(select(func.count()).select_from(PipelineRun)) == pipeline_count
    await engine.dispose()


@pytest.mark.asyncio
async def test_completed_audit_can_be_applied_with_the_same_run_id(tmp_path):
    from model_class.job_competency import JobPosting
    from model_class.knowledge_base import DataRepairAudit
    from src.rebuild_hard_metrics import build_parser, execute_rebuild

    database = tmp_path / "jobs.db"
    await _seed_database(database)
    repairs_root = tmp_path / "repairs"
    common = [
        "--repair-run-id",
        "audit-then-apply",
        "--database-url",
        _database_url(database),
        "--repairs-root",
        str(repairs_root),
    ]
    audit_args = build_parser().parse_args(["--dry-run", "--repair-audit", *common])

    audit = await execute_rebuild(audit_args)

    assert audit["mode"] == "dry-run"
    assert audit["status"] == "completed"
    assert audit["changes"][0] == {
        "posting_id": audit["changes"][0]["posting_id"],
        "field_name": "published_at",
        "before": "2010-01-02T09:30:00",
        "after": None,
        "reason_code": "unsupported_suspicious_publication",
    }
    engine = create_async_engine(_database_url(database))
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        old = await session.scalar(
            select(JobPosting).where(JobPosting.record_id == "old-unsupported")
        )
        assert old.published_at == OLD_DATE
        assert await session.scalar(
            select(func.count()).select_from(DataRepairAudit)
        ) == 0

    apply_args = build_parser().parse_args(
        [
            "--full",
            "--repair",
            "--confirm",
            *common,
            "--backup-dir",
            str(tmp_path / "backups"),
            "--locks-root",
            str(tmp_path / "locks"),
        ]
    )
    applied = await execute_rebuild(apply_args)

    assert applied["repair"]["status"] == "completed"
    assert applied["repair"]["idempotent"] is False
    async with Session() as session:
        old = await session.scalar(
            select(JobPosting).where(JobPosting.record_id == "old-unsupported")
        )
        assert old.published_at is None
        assert await session.scalar(
            select(func.count()).select_from(DataRepairAudit)
        ) == applied["repair"]["applied_change_count"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_prepared_report_recovers_committed_repair_idempotently(tmp_path):
    from model_class.knowledge_base import DataRepairAudit, PipelineRun
    from src.rebuild_hard_metrics import build_parser, execute_rebuild

    database = tmp_path / "jobs.db"
    await _seed_database(database)
    repairs_root = tmp_path / "repairs"
    backup_dir = tmp_path / "backups"
    args = build_parser().parse_args(
        [
            "--full",
            "--repair",
            "--confirm",
            "--repair-run-id",
            "prepared-recovery",
            "--database-url",
            _database_url(database),
            "--backup-dir",
            str(backup_dir),
            "--repairs-root",
            str(repairs_root),
            "--locks-root",
            str(tmp_path / "locks"),
        ]
    )
    first = await execute_rebuild(args)
    report_path = repairs_root / "prepared-recovery" / "report.json"
    prepared = json.loads(report_path.read_text(encoding="utf-8"))
    prepared["status"] = "prepared"
    report_path.write_text(
        json.dumps(prepared, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )

    engine = create_async_engine(_database_url(database))
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        audit_count = await session.scalar(
            select(func.count()).select_from(DataRepairAudit)
        )
        pipeline_count = await session.scalar(
            select(func.count()).select_from(PipelineRun)
        )

    recovered = await execute_rebuild(args)

    assert recovered["repair"]["idempotent"] is True
    assert recovered["repair"]["status"] == "completed"
    assert recovered["backup_path"] == first["backup_path"]
    assert json.loads(report_path.read_text(encoding="utf-8"))["status"] == "completed"
    async with Session() as session:
        assert await session.scalar(
            select(func.count()).select_from(DataRepairAudit)
        ) == audit_count
        assert await session.scalar(
            select(func.count()).select_from(PipelineRun)
        ) == pipeline_count
    await engine.dispose()


def test_existing_unfinished_backup_is_refreshed_from_current_database(tmp_path):
    from src.rebuild_hard_metrics import _verified_repair_backup

    database = tmp_path / "jobs.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE job_posting (id INTEGER PRIMARY KEY, job_title_raw TEXT)"
        )
        connection.execute(
            "INSERT INTO job_posting (id, job_title_raw) VALUES (1, 'before')"
        )
    database_url = _database_url(database)
    backup_dir = tmp_path / "backups"
    first = _verified_repair_backup(
        database_url,
        backup_dir,
        repair_run_id="retry-backup",
        expected_rows=1,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE job_posting SET job_title_raw = 'current' WHERE id = 1"
        )

    second = _verified_repair_backup(
        database_url,
        backup_dir,
        repair_run_id="retry-backup",
        expected_rows=1,
    )

    assert second == first
    with sqlite3.connect(second) as connection:
        assert connection.execute(
            "SELECT job_title_raw FROM job_posting WHERE id = 1"
        ).fetchone() == ("current",)


@pytest.mark.asyncio
async def test_repair_rolls_back_all_database_changes_when_rebuild_fails(
    tmp_path, monkeypatch
):
    from model_class.job_competency import JobPosting
    from model_class.knowledge_base import DataRepairAudit
    from src import rebuild_hard_metrics

    database = tmp_path / "jobs.db"
    await _seed_database(database)

    async def fail_pipeline(*_args, **_kwargs):
        raise RuntimeError("pipeline failed")

    monkeypatch.setattr(rebuild_hard_metrics, "run_hard_metrics_pipeline", fail_pipeline)
    args = rebuild_hard_metrics.build_parser().parse_args(
        [
            "--full",
            "--repair",
            "--confirm",
            "--repair-run-id",
            "repair-failure",
            "--database-url",
            _database_url(database),
            "--backup-dir",
            str(tmp_path / "backups"),
            "--repairs-root",
            str(tmp_path / "repairs"),
            "--locks-root",
            str(tmp_path / "locks"),
        ]
    )

    with pytest.raises(RuntimeError, match="pipeline failed"):
        await rebuild_hard_metrics.execute_rebuild(args)

    engine = create_async_engine(_database_url(database))
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        old = await session.scalar(
            select(JobPosting).where(JobPosting.record_id == "old-unsupported")
        )
        assert old.published_at == OLD_DATE
        assert old.published_at_trusted is True
        assert await session.scalar(
            select(func.count()).select_from(DataRepairAudit)
        ) == 0
    await engine.dispose()


@pytest.mark.parametrize(
    "run_id", ["../escape", "..", "repair/escape", "CON", "name.", "white space"]
)
def test_repair_run_id_rejects_unsafe_paths(run_id):
    from src.job_data_repair import RepairStorageError, repair_report_path

    with pytest.raises(RepairStorageError):
        repair_report_path(Path("data/repairs"), run_id)


@pytest.mark.asyncio
async def test_changed_quarterly_input_supersedes_the_previous_profile(tmp_path):
    import model_class.job_competency  # noqa: F401
    import model_class.knowledge_base  # noqa: F401
    from model_class.job_competency import JobPosting, JobProfile
    from model_class.knowledge_base import PipelineRun
    from src.quarterly_profile_service import rebuild_quarterly_profiles

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        postings = []
        for index in range(10):
            posting = JobPosting(
                **_posting_values(
                    f"profile-{index}",
                    published_at=datetime(2026, 4, index + 1),
                    content_hash=f"profile-hash-{index}",
                    simhash=f"{index + 100:016x}",
                    machine_level="mid",
                    gate_status="valid",
                    provenance_status="approved",
                    published_at_trusted=True,
                )
            )
            session.add(posting)
            postings.append(posting)
        first_run = PipelineRun(
            run_id="pipeline-one",
            mode="full",
            rule_version="test",
            status="running",
        )
        session.add(first_run)
        await session.flush()
        await rebuild_quarterly_profiles(session, pipeline_run_id=first_run.id)
        old_profile = await session.scalar(
            select(JobProfile).where(JobProfile.derivation_status == "active")
        )
        assert old_profile is not None

        postings[0].content_hash = "repaired-content-hash"
        second_run = PipelineRun(
            run_id="pipeline-two",
            mode="full",
            rule_version="test",
            status="running",
        )
        session.add(second_run)
        await session.flush()
        result = await rebuild_quarterly_profiles(session, pipeline_run_id=second_run.id)
        profiles = list(
            (
                await session.execute(
                    select(JobProfile).where(JobProfile.profile_kind == "quarterly")
                )
            ).scalars()
        )

        assert result["profiles_superseded"] == 1
        assert len(profiles) == 2
        assert old_profile.derivation_status == "superseded"
        assert sum(item.derivation_status == "active" for item in profiles) == 1
    await engine.dispose()
