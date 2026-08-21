from datetime import datetime

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import create_async_engine


def _source_definition_payload(**overrides):
    payload = {
        "source_id": "ncss_public_jobs",
        "source_name": "国家大学生就业服务平台公开岗位",
        "source_type": "university_recruitment",
        "market_scope": "china",
        "base_url": "https://cnu.ncss.cn",
        "allowed_paths": ["/student/jobs/"],
        "collection_mode": "public_json",
        "compliance_status": "approved",
        "compliance_note": "reviewed domestic public source",
        "rate_limit_seconds": 3.0,
        "max_pages": 20,
        "max_records": 1000,
        "parser_name": "ncss",
        "parser_version": "v1",
        "enabled": True,
    }
    payload.update(overrides)
    return payload


def test_source_definition_requires_reviewed_job_market_scope():
    from src.job_collection.models import SourceDefinition

    payload = _source_definition_payload()
    payload.pop("market_scope")

    with pytest.raises(ValidationError, match="market_scope"):
        SourceDefinition.model_validate(payload)


@pytest.mark.parametrize("value", ["china", "excluded", "pending_review"])
def test_source_definition_accepts_known_job_market_scopes(value):
    from src.job_collection.models import SourceDefinition

    source = SourceDefinition.model_validate(
        _source_definition_payload(market_scope=value)
    )

    assert source.market_scope == value


@pytest.mark.asyncio
async def test_job_domain_tables_can_be_created_in_sqlite():
    from model_class.base import Base
    import model_class.job_competency  # noqa: F401
    import model_class.knowledge_base  # noqa: F401

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    expected = {
        "job_posting",
        "skill",
        "job_posting_skill",
        "job_profile",
        "job_profile_skill",
        "evidence_record",
        "resume_record",
        "match_record",
        "review_item",
        "evaluation_run",
        "job_source",
        "collection_run",
        "collection_snapshot",
        "data_repair_audit",
    }
    assert expected.issubset(set(Base.metadata.tables))
    await engine.dispose()


def test_job_posting_has_traceability_fields():
    from model_class.job_competency import JobPosting

    columns = set(JobPosting.__table__.columns.keys())
    assert {
        "record_id",
        "source_name",
        "source_url",
        "published_at",
        "collected_at",
        "content_hash",
        "simhash",
        "source_score",
        "quality_score",
        "duplicate_of_id",
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
    }.issubset(columns)

    indexes = {
        tuple(column.name for column in index.columns)
        for index in JobPosting.__table__.indexes
    }
    assert ("source_domain",) in indexes
    assert ("source_id", "source_record_id") in indexes
    assert ("published_at_trusted", "published_at") in indexes


def test_job_profile_skill_has_cross_source_evidence_fields():
    from model_class.job_competency import JobPosting, JobProfileSkill

    columns = JobProfileSkill.__table__.columns
    assert {
        "source_type_count",
        "source_domain_count",
        "company_count",
        "cross_source_status",
        "ratio_evidence_status",
    } <= set(columns.keys())
    assert columns["source_type_count"].nullable is False
    assert columns["source_domain_count"].nullable is False
    assert columns["company_count"].nullable is False
    assert columns["cross_source_status"].nullable is False
    assert columns["ratio_evidence_status"].nullable is False
    assert JobPosting.__table__.c.published_at_confidence.default.arg == 0.0
    assert JobPosting.__table__.c.published_at_trusted.default.arg is False
    assert JobPosting.__table__.c.provenance_status.default.arg == "unverified"
    assert columns["source_type_count"].default.arg == 0
    assert columns["source_domain_count"].default.arg == 0
    assert columns["company_count"].default.arg == 0
    assert columns["cross_source_status"].default.arg == "single_source"
    assert columns["ratio_evidence_status"].default.arg == "unknown"


def test_collection_storage_tables_have_required_constraints_and_indexes():
    from model_class.knowledge_base import (
        CollectionRun,
        CollectionSnapshot,
        DataRepairAudit,
        JobSource,
    )

    required_columns = {
        JobSource: {
            "source_id",
            "source_name",
            "source_type",
            "market_scope",
            "base_url",
            "allowed_paths_json",
            "collection_mode",
            "compliance_status",
            "compliance_note",
            "rate_limit_seconds",
            "max_pages_per_run",
            "max_records_per_run",
            "parser_name",
            "parser_version",
            "enabled",
            "created_at",
            "updated_at",
        },
        CollectionRun: {
            "run_id",
            "source_ids_json",
            "mode",
            "status",
            "staging_dir",
            "fetched_count",
            "parsed_count",
            "valid_count",
            "review_count",
            "quarantined_count",
            "duplicate_count",
            "imported_count",
            "summary_json",
            "started_at",
            "completed_at",
        },
        CollectionSnapshot: {
            "collection_run_id",
            "job_source_id",
            "source_record_id",
            "source_url",
            "response_status",
            "content_hash",
            "relative_path",
            "fetched_at",
            "parser_version",
            "parse_status",
            "parse_error",
        },
        DataRepairAudit: {
            "repair_run_id",
            "job_posting_id",
            "field_name",
            "before_json",
            "after_json",
            "reason_code",
            "rule_version",
            "applied",
            "created_at",
        },
    }
    for model, expected in required_columns.items():
        assert expected <= set(model.__table__.columns.keys())

    assert JobSource.__table__.c.source_id.unique is True
    assert CollectionRun.__table__.c.run_id.unique is True

    snapshot_foreign_keys = {
        foreign_key.parent.name: foreign_key.target_fullname
        for foreign_key in CollectionSnapshot.__table__.foreign_keys
    }
    assert snapshot_foreign_keys == {
        "collection_run_id": "collection_run.id",
        "job_source_id": "job_source.id",
    }
    audit_foreign_keys = {
        foreign_key.parent.name: foreign_key.target_fullname
        for foreign_key in DataRepairAudit.__table__.foreign_keys
    }
    assert audit_foreign_keys == {"job_posting_id": "job_posting.id"}

    snapshot_unique_constraints = {
        tuple(column.name for column in constraint.columns)
        for constraint in CollectionSnapshot.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert (
        "collection_run_id",
        "job_source_id",
        "source_record_id",
    ) in snapshot_unique_constraints

    assert {
        tuple(column.name for column in index.columns)
        for index in CollectionRun.__table__.indexes
    } >= {("status", "started_at")}
    assert {
        tuple(column.name for column in index.columns)
        for index in CollectionSnapshot.__table__.indexes
    } >= {
        ("collection_run_id", "parse_status"),
        ("job_source_id", "source_record_id"),
        ("content_hash",),
    }
    assert {
        tuple(column.name for column in index.columns)
        for index in DataRepairAudit.__table__.indexes
    } >= {
        ("repair_run_id", "created_at"),
        ("job_posting_id", "field_name"),
    }


def test_job_posting_input_declares_optional_provenance_fields():
    from schemes.job_competency import JobPostingInput

    expected = {
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
    }
    assert expected <= set(JobPostingInput.model_fields)
    assert JobPostingInput.model_config["extra"] == "allow"


def test_prepare_job_record_parses_observation_times_and_accepts_legacy_input():
    from src.job_data_service import prepare_job_record

    legacy = {
        "record_id": "record-1",
        "job_family_id": "data-engineer",
        "job_title_raw": "Data Engineer",
        "company_name": "Example",
        "source_name": "Example Careers",
        "source_url": "https://example.com/jobs/1",
        "job_description_raw": "Build and maintain data pipelines.",
    }
    legacy_prepared = prepare_job_record(legacy)
    assert legacy_prepared["first_seen_at"] is None
    assert legacy_prepared["last_seen_at"] is None
    assert legacy_prepared["published_at_confidence"] == 0.0
    assert legacy_prepared["published_at_trusted"] is False

    prepared = prepare_job_record(
        {
            **legacy,
            "record_id": "record-2",
            "first_seen_at": "2026-08-05T10:00:00+08:00",
            "last_seen_at": "2026-08-05T11:00:00+08:00",
        }
    )
    assert prepared["first_seen_at"] == datetime(2026, 8, 5, 2, 0)
    assert prepared["last_seen_at"] == datetime(2026, 8, 5, 3, 0)
    assert prepared["first_seen_at"].tzinfo is None
    assert prepared["last_seen_at"].tzinfo is None
