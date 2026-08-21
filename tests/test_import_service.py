import asyncio
import json
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from model_class.base import Base


def _job(record_id: str) -> dict:
    return {
        "record_id": record_id,
        "collector_id": "T",
        "job_family_id": "DATA_ENGINEER",
        "job_title_raw": "数据工程师",
        "company_name": "示例科技",
        "industry": "软件和信息技术服务业",
        "region": "北京",
        "source_name": "企业官网",
        "source_type": "company_official",
        "source_url": f"https://example.com/jobs/{record_id}",
        "published_at": "2026-07-01",
        "collected_at": "2026-07-22T10:00:00+08:00",
        "experience_requirement": "3-5年",
        "education_requirement": "本科",
        "salary_range": "20-30K",
        "job_description_raw": "负责数据平台建设，熟悉Python、Flink和Kafka实时计算。",
    }


def _mixed_jsonl() -> bytes:
    first = json.dumps(_job("T-JD-0001"), ensure_ascii=False)
    broken = '{"record_id":"broken\x01"}'
    third = json.dumps(_job("T-JD-0002"), ensure_ascii=False)
    return "\n".join((first, broken, third)).encode("utf-8")


def _job_bytes(*records: dict) -> bytes:
    return "\n".join(json.dumps(item, ensure_ascii=False) for item in records).encode(
        "utf-8"
    )


async def _approved_source(
    session,
    *,
    source_id="approved_jobs",
    compliance_status="approved",
    collection_mode="public_json",
):
    from model_class.knowledge_base import JobSource

    source = JobSource(
        source_id=source_id,
        source_name="Approved Jobs",
        source_type="company_official",
        base_url="https://trusted.example",
        allowed_paths_json='["/jobs/"]',
        collection_mode=collection_mode,
        compliance_status=compliance_status,
        compliance_note="Reviewed public source",
        rate_limit_seconds=1.0,
        max_pages_per_run=10,
        max_records_per_run=100,
        parser_name="trusted_parser",
        parser_version="v1",
        enabled=True,
    )
    session.add(source)
    await session.flush()
    return source


def _provenance_job(record_id: str) -> dict:
    record = _job(record_id)
    record.update(
        {
            "source_id": "approved_jobs",
            "source_name": "Approved Jobs",
            "source_type": "company_official",
            "source_url": f"https://TRUSTED.EXAMPLE/jobs/{record_id}",
            "source_domain": "caller-controlled.invalid",
            "source_record_id": record_id,
            "published_at_evidence": "Published 2026-07-01",
            "published_at_confidence": 0.95,
            "published_at_trusted": True,
            "snapshot_hash": "a" * 64,
            "parser_name": "trusted_parser",
            "parser_version": "v1",
            "collection_method": "public_json",
            "job_description_raw": (
                "Python Flink Kafka data platform engineering, architecture design, "
                "task scheduling, performance optimization, operations, and quality assurance."
            ),
        }
    )
    return record


@pytest_asyncio.fixture
async def memory_session():
    import model_class.knowledge_base  # noqa: F401
    import model_class.job_competency  # noqa: F401

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


def test_json_suffix_with_jsonl_content_preserves_bad_line():
    from src.import_service import parse_import_lines

    lines = parse_import_lines(_mixed_jsonl(), "jd_raw.json")

    assert [line.line_number for line in lines] == [1, 2, 3]
    assert lines[0].value["record_id"] == "T-JD-0001"
    assert lines[1].value is None
    assert lines[1].error_code == "invalid_json"
    assert lines[2].value["record_id"] == "T-JD-0002"


def test_knowledge_base_models_are_registered():
    import model_class.knowledge_base  # noqa: F401

    assert {
        "import_batch",
        "raw_job_record",
        "job_posting_revision",
        "quality_issue",
        "responsibility",
        "industry_scenario",
        "evidence_snippet",
        "job_profile_responsibility",
        "job_profile_scenario",
        "knowledge_chunk",
        "job_profile_snapshot",
        "evolution_event",
    }.issubset(Base.metadata.tables)


@pytest.mark.asyncio
async def test_raw_lines_and_batch_are_persisted(memory_session):
    from model_class.knowledge_base import ImportBatch, RawJobRecord
    from src.import_service import import_job_file

    result = await import_job_file(memory_session, _mixed_jsonl(), "jd_raw.json")

    assert result["raw_lines"] == 3
    assert result["parsed_lines"] == 2
    assert result["quarantined"] == 1
    assert (
        await memory_session.scalar(select(func.count()).select_from(ImportBatch)) == 1
    )
    assert (
        await memory_session.scalar(select(func.count()).select_from(RawJobRecord)) == 3
    )


@pytest.mark.asyncio
async def test_same_file_is_idempotent(memory_session):
    from model_class.job_competency import JobPosting
    from src.import_service import import_job_file

    payload = _job_bytes(_job("T-JD-0101"))
    first = await import_job_file(memory_session, payload, "jobs.json")
    second = await import_job_file(memory_session, payload, "jobs.json")

    assert second["batch_id"] == first["batch_id"]
    assert second["idempotent"] is True
    assert (
        await memory_session.scalar(select(func.count()).select_from(JobPosting)) == 1
    )


@pytest.mark.asyncio
async def test_direct_upload_cannot_gain_approved_registry_provenance(
    memory_session,
):
    from model_class.job_competency import JobPosting
    from src.import_service import import_job_file

    await _approved_source(memory_session)
    record = _provenance_job("approved-1")
    record.update(
        {
            "import_authorization": "verified_collection",
            "authorized_source_ids": ["approved_jobs"],
            "authorized_manual_source_ids": ["approved_jobs"],
        }
    )

    result = await import_job_file(
        memory_session, _job_bytes(record), "approved.jsonl"
    )
    posting = await memory_session.scalar(select(JobPosting))

    assert result["review"] == 1
    assert posting.provenance_status == "unverified"
    assert posting.source_domain == "trusted.example"
    assert posting.source_type == "company_official"
    assert posting.parser_name == "trusted_parser"
    assert posting.collection_method == "public_json"
    assert posting.published_at_trusted is False
    assert posting.gate_status == "review"


@pytest.mark.asyncio
async def test_direct_upload_cannot_spoof_source_or_trusted_publication(
    memory_session,
):
    from model_class.job_competency import JobPosting
    from model_class.knowledge_base import QualityIssue
    from src.import_service import import_job_file

    await _approved_source(memory_session)
    record = _provenance_job("spoofed-1")
    record.update(
        {
            "source_type": "invented_premium_feed",
            "source_domain": "three-independent-domains.invalid",
            "parser_name": "invented_parser",
            "parser_version": "v99",
            "collection_method": "invented_method",
            "published_at": "2026-01-01",
            "published_at_evidence": "invented structured date",
            "published_at_confidence": 1.0,
            "published_at_trusted": True,
        }
    )

    result = await import_job_file(memory_session, _job_bytes(record), "spoof.jsonl")
    posting = await memory_session.scalar(select(JobPosting))
    issues = (await memory_session.execute(select(QualityIssue))).scalars().all()

    assert result["review"] == 1
    assert posting.provenance_status == "unverified"
    assert posting.source_domain == "trusted.example"
    assert posting.source_type == "company_official"
    assert posting.parser_name == "trusted_parser"
    assert posting.parser_version == "v1"
    assert posting.collection_method == "public_json"
    assert posting.published_at_trusted is False
    assert posting.gate_status == "review"
    assert "provenance_mismatch" in {issue.code for issue in issues}


@pytest.mark.asyncio
async def test_unknown_legacy_source_is_imported_for_review_not_formal_use(
    memory_session,
):
    from model_class.job_competency import JobPosting
    from src.import_service import import_job_file

    record = _provenance_job("legacy-unknown")
    record.update(
        {
            "source_id": "invented_source",
            "source_type": "invented_type",
            "source_url": "https://unknown.example/jobs/legacy-unknown",
            "source_domain": "spoofed.example",
        }
    )

    result = await import_job_file(
        memory_session, _job_bytes(record), "legacy-unknown.jsonl"
    )
    posting = await memory_session.scalar(select(JobPosting))

    assert result["imported"] == 1
    assert result["review"] == 1
    assert posting.provenance_status == "unverified"
    assert posting.source_type == "unknown"
    assert posting.source_domain == "unknown.example"
    assert posting.published_at_trusted is False
    assert posting.gate_status == "review"


@pytest.mark.asyncio
async def test_manual_only_source_requires_guarded_commit_authorization(
    memory_session,
):
    from model_class.job_competency import JobPosting
    from src.import_service import import_job_file

    await _approved_source(
        memory_session,
        source_id="reviewed_manual_jobs",
        compliance_status="manual_only",
        collection_mode="manual_url_manifest",
    )
    record = _provenance_job("manual-direct")
    record.update(
        {
            "source_id": "reviewed_manual_jobs",
            "collection_method": "manual_url_manifest",
        }
    )

    result = await import_job_file(
        memory_session, _job_bytes(record), "manual-direct.jsonl"
    )
    posting = await memory_session.scalar(select(JobPosting))

    assert result["review"] == 1
    assert posting.provenance_status == "unverified"
    assert posting.published_at_trusted is False
    assert posting.gate_status == "review"


@pytest.mark.asyncio
async def test_changed_record_creates_revision(memory_session):
    from model_class.knowledge_base import JobPostingRevision
    from src.import_service import import_job_file

    first = _job("T-JD-0201")
    second = _job("T-JD-0201")
    second["job_description_raw"] = (
        "负责数据平台建设，熟悉Python、Flink、Kafka和Spark实时计算。"
    )

    await import_job_file(memory_session, _job_bytes(first), "first.jsonl")
    result = await import_job_file(memory_session, _job_bytes(second), "second.jsonl")

    assert result["revised"] == 1
    assert (
        await memory_session.scalar(
            select(func.count()).select_from(JobPostingRevision)
        )
        == 1
    )


@pytest.mark.asyncio
async def test_unified_provenance_preserves_first_seen_updates_last_seen_and_revises_content(
    memory_session,
):
    from model_class.job_competency import JobPosting
    from model_class.knowledge_base import JobPostingRevision
    from src.import_service import import_job_file

    first = _job("stable-unified-record")
    first.update(
        {
            "source_id": "ncss_public_jobs",
            "source_domain": "example.com",
            "source_record_id": "source-42",
            "snapshot_hash": "a" * 64,
            "parser_name": "ncss",
            "parser_version": "v1",
            "collection_method": "public_json",
            "first_seen_at": "2026-08-01T00:00:00+00:00",
            "last_seen_at": "2026-08-01T00:00:00+00:00",
        }
    )
    changed = dict(first)
    changed["job_description_raw"] = (
        "负责数据平台建设，熟悉 Python、Flink、Kafka、Spark 和 Airflow，"
        "持续改进数据质量、任务编排与实时计算稳定性。"
    )
    changed["snapshot_hash"] = "b" * 64
    changed["first_seen_at"] = "2026-08-05T00:00:00+00:00"
    changed["last_seen_at"] = "2026-08-06T00:00:00+00:00"

    await import_job_file(memory_session, _job_bytes(first), "unified-first.jsonl")
    result = await import_job_file(
        memory_session, _job_bytes(changed), "unified-changed.jsonl"
    )
    posting = await memory_session.scalar(
        select(JobPosting).where(JobPosting.record_id == "stable-unified-record")
    )

    assert result["revised"] == 1
    assert posting.record_id == "stable-unified-record"
    assert posting.first_seen_at == datetime(2026, 8, 1, 0, 0)
    assert posting.last_seen_at == datetime(2026, 8, 6, 0, 0)
    assert posting.source_id == "ncss_public_jobs"
    assert posting.source_domain == "example.com"
    assert posting.source_record_id == "source-42"
    assert posting.snapshot_hash == "b" * 64
    assert posting.parser_name == "ncss"
    assert posting.parser_version == "v1"
    assert posting.collection_method == "public_json"
    assert (
        await memory_session.scalar(
            select(func.count()).select_from(JobPostingRevision)
        )
        == 1
    )


@pytest.mark.asyncio
async def test_failed_existing_posting_update_rolls_back_revision_and_mutation(
    memory_session, monkeypatch
):
    import src.import_service as import_module
    from model_class.job_competency import JobPosting
    from model_class.knowledge_base import JobPostingRevision

    original = _job("atomic-update")
    changed = dict(original)
    changed["job_description_raw"] = (
        "负责数据平台架构升级，熟悉 Python、Flink、Kafka、Spark 与 Airflow，"
        "持续建设可靠的实时计算和数据质量治理体系。"
    )
    await import_module.import_job_file(
        memory_session, _job_bytes(original), "atomic-original.jsonl"
    )
    persisted = await memory_session.scalar(
        select(JobPosting).where(JobPosting.record_id == "atomic-update")
    )
    original_description = persisted.job_description_raw
    real_persist = import_module.persist_prepared_job_record

    async def fail_after_mutation(*args, **kwargs):
        await real_persist(*args, **kwargs)
        raise RuntimeError("fault after posting mutation")

    monkeypatch.setattr(
        import_module, "persist_prepared_job_record", fail_after_mutation
    )
    result = await import_module.import_job_file(
        memory_session, _job_bytes(changed), "atomic-changed.jsonl"
    )
    memory_session.expire_all()
    persisted = await memory_session.scalar(
        select(JobPosting).where(JobPosting.record_id == "atomic-update")
    )

    assert result["quarantined"] == 1
    assert persisted.job_description_raw == original_description
    assert (
        await memory_session.scalar(
            select(func.count()).select_from(JobPostingRevision)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_unchanged_observation_advances_last_seen_without_revision(memory_session):
    from model_class.job_competency import JobPosting
    from model_class.knowledge_base import JobPostingRevision
    from src.import_service import import_job_file

    first = _job("observation-only")
    first.update(
        first_seen_at="2026-08-01T00:00:00+00:00",
        last_seen_at="2026-08-01T00:00:00+00:00",
        snapshot_hash="a" * 64,
    )
    observed = dict(first)
    observed["first_seen_at"] = "2026-08-03T00:00:00+00:00"
    observed["last_seen_at"] = "2026-08-06T00:00:00+00:00"
    observed["snapshot_hash"] = "b" * 64

    await import_job_file(memory_session, _job_bytes(first), "observation-first.jsonl")
    result = await import_job_file(
        memory_session, _job_bytes(observed), "observation-later.jsonl"
    )
    posting = await memory_session.scalar(
        select(JobPosting).where(JobPosting.record_id == "observation-only")
    )

    assert result["skipped"] == 1
    assert result["revised"] == 0
    assert posting.first_seen_at == datetime(2026, 8, 1, 0, 0)
    assert posting.last_seen_at == datetime(2026, 8, 6, 0, 0)
    assert (
        await memory_session.scalar(
            select(func.count()).select_from(JobPostingRevision)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_stale_changed_observation_is_archived_without_replacing_current(
    memory_session,
):
    from model_class.job_competency import JobPosting
    from model_class.knowledge_base import JobPostingRevision
    from src.import_service import import_job_file

    current = _job("stale-observation")
    current.update(
        first_seen_at="2026-08-05T00:00:00+00:00",
        last_seen_at="2026-08-06T00:00:00+00:00",
        snapshot_hash="n" * 64,
        parser_name="new-parser",
        parser_version="v2",
    )
    current["job_description_raw"] = (
        "Current Python Flink Kafka data platform content with reliable production "
        "pipelines, quality controls, monitoring, and current provenance."
    )
    stale = dict(current)
    stale.update(
        first_seen_at="2026-08-01T00:00:00+00:00",
        last_seen_at="2026-08-02T00:00:00+00:00",
        snapshot_hash="o" * 64,
        parser_name="old-parser",
        parser_version="v1",
    )
    stale["job_description_raw"] = (
        "Older Python Flink Kafka data platform content retained only for audit and "
        "never restored over the current observation."
    )

    await import_job_file(memory_session, _job_bytes(current), "current.jsonl")
    result = await import_job_file(memory_session, _job_bytes(stale), "stale.jsonl")
    posting = await memory_session.scalar(
        select(JobPosting).where(JobPosting.record_id == "stale-observation")
    )
    revision = await memory_session.scalar(select(JobPostingRevision))

    assert result["revised"] == 1
    assert posting.job_description_raw == current["job_description_raw"]
    assert posting.snapshot_hash == "n" * 64
    assert posting.parser_name == "new-parser"
    assert posting.parser_version == "v2"
    assert posting.first_seen_at == datetime(2026, 8, 1, 0, 0)
    assert posting.last_seen_at == datetime(2026, 8, 6, 0, 0)
    assert json.loads(revision.raw_payload)["job_description_raw"] == stale[
        "job_description_raw"
    ]


@pytest.mark.asyncio
async def test_repackaged_stale_observation_is_deduped_but_later_reversion_is_audited(
    memory_session,
):
    from model_class.job_competency import JobPosting
    from model_class.knowledge_base import JobPostingRevision
    from src.import_service import import_job_file

    current = _job("stale-dedupe")
    current.update(
        first_seen_at="2026-08-03T00:00:00+00:00",
        last_seen_at="2026-08-03T00:00:00+00:00",
    )
    current["job_description_raw"] = (
        "Current Python Flink Kafka platform observation with production governance, "
        "monitoring, quality controls, and reliable orchestration responsibilities."
    )
    stale = dict(current)
    stale.update(
        first_seen_at="2026-08-01T00:00:00+00:00",
        last_seen_at="2026-08-01T00:00:00+00:00",
    )
    stale["job_description_raw"] = (
        "Earlier Python Flink Kafka platform observation with ingestion, transforms, "
        "basic monitoring, and data quality responsibilities retained for audit."
    )
    repackaged = dict(stale, run_id="different-package")
    reversion = dict(stale)
    reversion["last_seen_at"] = "2026-08-04T00:00:00+00:00"

    await import_job_file(memory_session, _job_bytes(current), "current.jsonl")
    first_stale = await import_job_file(
        memory_session, _job_bytes(stale), "stale-one.jsonl"
    )
    repeated_stale = await import_job_file(
        memory_session, _job_bytes(repackaged), "stale-repackaged.jsonl"
    )
    assert first_stale["revised"] == 1
    assert repeated_stale["revised"] == 0
    assert repeated_stale["skipped"] == 1
    assert (
        await memory_session.scalar(
            select(func.count()).select_from(JobPostingRevision)
        )
        == 1
    )

    reverted = await import_job_file(
        memory_session, _job_bytes(reversion), "later-reversion.jsonl"
    )
    posting = await memory_session.scalar(
        select(JobPosting).where(JobPosting.record_id == "stale-dedupe")
    )
    revisions = list(
        (
            await memory_session.scalars(
                select(JobPostingRevision).order_by(JobPostingRevision.revision_no)
            )
        ).all()
    )

    assert reverted["revised"] == 1
    assert posting.job_description_raw == stale["job_description_raw"]
    assert len(revisions) == 2
    assert revisions[0].observation_at == datetime(2026, 8, 1, 0, 0)
    assert revisions[0].observation_identity
    assert revisions[1].observation_at == datetime(2026, 8, 3, 0, 0)


@pytest.mark.asyncio
async def test_offset_timestamp_stale_revision_dedupes_against_equivalent_utc(
    memory_session,
):
    from model_class.knowledge_base import JobPostingRevision
    from src.import_service import import_job_file

    older = _job("offset-stale-dedupe")
    older.update(
        first_seen_at="2026-08-01T08:00:00+08:00",
        last_seen_at="2026-08-01T08:00:00+08:00",
        snapshot_hash="a" * 64,
    )
    older["job_description_raw"] = (
        "Earlier Python Flink Kafka platform observation with ingestion, transforms, "
        "monitoring, data quality, and reliable batch processing responsibilities."
    )
    newer = dict(older)
    newer.update(
        last_seen_at="2026-08-02T00:00:00Z",
        snapshot_hash="b" * 64,
    )
    newer["job_description_raw"] = (
        "Newer Python Flink Kafka platform observation with governance, orchestration, "
        "observability, data quality, and production ownership responsibilities."
    )
    repackaged = dict(older)
    repackaged.update(
        first_seen_at="2026-08-01T00:00:00Z",
        last_seen_at="2026-08-01T00:00:00Z",
        run_id="repackaged-equivalent-time",
    )

    await import_job_file(memory_session, _job_bytes(older), "offset-older.jsonl")
    await import_job_file(memory_session, _job_bytes(newer), "offset-newer.jsonl")
    repeated = await import_job_file(
        memory_session, _job_bytes(repackaged), "offset-repackaged.jsonl"
    )

    revisions = list(
        (
            await memory_session.scalars(
                select(JobPostingRevision).order_by(JobPostingRevision.revision_no)
            )
        ).all()
    )
    assert repeated["revised"] == 0
    assert repeated["skipped"] == 1
    assert len(revisions) == 1
    assert revisions[0].observation_at == datetime(2026, 8, 1, 0, 0)


@pytest.mark.asyncio
async def test_equal_time_conflicts_choose_same_current_state_in_both_orders(tmp_path):
    from model_class.job_competency import JobPosting
    from model_class.knowledge_base import JobPostingRevision
    from src.import_service import import_job_file

    first = _job("equal-time-conflict")
    first.update(
        first_seen_at="2026-08-02T00:00:00+00:00",
        last_seen_at="2026-08-02T00:00:00+00:00",
    )
    first["job_description_raw"] = (
        "Alpha Python Flink Kafka platform content with orchestration, monitoring, "
        "testing, reliability, and data quality ownership responsibilities."
    )
    second = dict(first)
    second["job_description_raw"] = (
        "Beta Python Flink Kafka platform content with governance, observability, "
        "stream processing, reliability, and production ownership responsibilities."
    )

    async def import_order(name, records):
        database = tmp_path / f"{name}.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{database.as_posix()}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        async with Session() as session:
            for index, record in enumerate(records):
                result = await import_job_file(
                    session, _job_bytes(record), f"{name}-{index}.jsonl"
                )
                assert result["quarantined"] == 0
            posting = await session.scalar(
                select(JobPosting).where(
                    JobPosting.record_id == "equal-time-conflict"
                )
            )
            revision_count = await session.scalar(
                select(func.count()).select_from(JobPostingRevision)
            )
            current = posting.job_description_raw
        await engine.dispose()
        return current, revision_count

    forward = await import_order("forward", [first, second])
    reverse = await import_order("reverse", [second, first])

    assert forward == reverse
    assert forward[1] == 1


@pytest.mark.asyncio
async def test_equal_time_unchanged_content_chooses_same_provenance_in_both_orders(
    tmp_path,
):
    from model_class.job_competency import JobPosting
    from src.import_service import import_job_file

    first = _job("equal-time-unchanged")
    first.update(
        first_seen_at="2026-08-02T00:00:00+00:00",
        last_seen_at="2026-08-02T00:00:00+00:00",
        snapshot_hash="a" * 64,
        parser_name="parser-a",
        parser_version="v1",
        collection_method="public_json",
        run_id="run-a",
    )
    second = dict(first)
    second.update(
        snapshot_hash="b" * 64,
        run_id="run-b",
    )

    async def import_order(name, records):
        database = tmp_path / f"unchanged-{name}.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{database.as_posix()}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        async with Session() as session:
            for index, record in enumerate(records):
                result = await import_job_file(
                    session, _job_bytes(record), f"{name}-{index}.jsonl"
                )
                assert result["quarantined"] == 0
            posting = await session.scalar(
                select(JobPosting).where(
                    JobPosting.record_id == "equal-time-unchanged"
                )
            )
            current = (
                posting.snapshot_hash,
                posting.parser_name,
                posting.parser_version,
                posting.collection_method,
                posting.raw_payload,
            )
        await engine.dispose()
        return current

    forward = await import_order("forward", [first, second])
    reverse = await import_order("reverse", [second, first])

    assert forward == reverse
    assert json.loads(forward[-1])["run_id"] in {"run-a", "run-b"}


@pytest.mark.asyncio
async def test_concurrent_changed_observations_serialize_revision_numbers(tmp_path):
    from model_class.job_competency import JobPosting
    from model_class.knowledge_base import JobPostingRevision
    from src.import_service import import_job_file

    database = tmp_path / "concurrent-import.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database.as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    initial = _job("concurrent-observation")
    initial.update(
        first_seen_at="2026-08-01T00:00:00+00:00",
        last_seen_at="2026-08-01T00:00:00+00:00",
        snapshot_hash="a" * 64,
    )
    second = dict(initial)
    second.update(last_seen_at="2026-08-02T00:00:00+00:00", snapshot_hash="b" * 64)
    second["job_description_raw"] = (
        "Second Python Flink Kafka observation with expanded orchestration, quality, "
        "monitoring, testing, and platform reliability responsibilities."
    )
    third = dict(initial)
    third.update(last_seen_at="2026-08-03T00:00:00+00:00", snapshot_hash="c" * 64)
    third["job_description_raw"] = (
        "Third and newest Python Flink Kafka observation with streaming architecture, "
        "governance, observability, testing, and production ownership."
    )
    async with Session() as session:
        await import_job_file(session, _job_bytes(initial), "initial.jsonl")

    async def run_import(record, filename):
        async with Session() as session:
            return await import_job_file(session, _job_bytes(record), filename)

    results = await asyncio.gather(
        run_import(second, "second.jsonl"),
        run_import(third, "third.jsonl"),
    )

    async with Session() as session:
        posting = await session.scalar(
            select(JobPosting).where(JobPosting.record_id == "concurrent-observation")
        )
        revisions = list(
            (
                await session.scalars(
                    select(JobPostingRevision).order_by(JobPostingRevision.revision_no)
                )
            ).all()
        )
    await engine.dispose()

    assert all(result["quarantined"] == 0 for result in results)
    assert posting.last_seen_at == datetime(2026, 8, 3, 0, 0)
    assert posting.snapshot_hash == "c" * 64
    assert [revision.revision_no for revision in revisions] == [1, 2]


def test_direct_import_rejects_oversized_bytes(monkeypatch):
    import src.import_service as import_module

    monkeypatch.setattr(import_module, "MAX_IMPORT_BYTES", 32)
    with pytest.raises(import_module.ImportLimitError, match="bytes"):
        import_module.parse_import_lines(b"x" * 33, "jobs.jsonl")


def test_direct_import_rejects_oversized_line(monkeypatch):
    import src.import_service as import_module

    monkeypatch.setattr(import_module, "MAX_IMPORT_BYTES", 1024)
    monkeypatch.setattr(import_module, "MAX_IMPORT_LINE_BYTES", 32)
    payload = json.dumps(_job("long-line"), ensure_ascii=False).encode("utf-8")
    with pytest.raises(import_module.ImportLimitError, match="line 1"):
        import_module.parse_import_lines(payload, "jobs.jsonl")


def test_direct_import_rejects_record_count_over_limit(monkeypatch):
    import src.import_service as import_module

    monkeypatch.setattr(import_module, "MAX_IMPORT_RECORDS", 1)
    with pytest.raises(import_module.ImportLimitError, match="record count"):
        import_module.parse_import_lines(
            _job_bytes(_job("count-one"), _job("count-two")), "jobs.jsonl"
        )


def test_direct_import_rejects_json_nesting_over_limit(monkeypatch):
    import src.import_service as import_module

    monkeypatch.setattr(import_module, "MAX_IMPORT_JSON_DEPTH", 3)
    payload = b'{"a":{"b":{"c":{"d":1}}}}'
    with pytest.raises(import_module.ImportLimitError, match="nesting"):
        import_module.parse_import_lines(payload, "jobs.jsonl")


@pytest.mark.parametrize(
    ("filename", "payload"),
    [
        ("jobs.json", b"[" + b",".join([b"{}"] * 10_001) + b"]"),
        ("jobs.jsonl", b"{}\n" * 10_001),
        ("jobs.csv", b"record_id\n" + b"x\n" * 10_001),
    ],
    ids=("json-array", "jsonl", "csv"),
)
def test_direct_import_stops_incrementally_at_record_10001(filename, payload):
    from src.import_service import ImportLimitError, parse_import_lines

    with pytest.raises(ImportLimitError, match="record count"):
        parse_import_lines(payload, filename)


def test_direct_import_rejects_depth_1100_before_decoding(monkeypatch):
    import src.import_service as service

    def decoder_must_not_run(*args, **kwargs):
        raise AssertionError("deep JSON reached the decoder")

    monkeypatch.setattr(service, "decode_json_array_incrementally", decoder_must_not_run)

    payload = ("[" * 1_100 + "0" + "]" * 1_100).encode()
    with pytest.raises(service.ImportLimitError, match="nesting"):
        service.parse_import_lines(payload, "jobs.json")


def test_direct_json_array_import_uses_incremental_raw_decode(monkeypatch):
    import src.import_service as service

    real_loads = service.json.loads

    def reject_whole_array(value, *args, **kwargs):
        if isinstance(value, str) and value.lstrip().startswith("["):
            raise AssertionError("whole JSON array was materialized")
        return real_loads(value, *args, **kwargs)

    monkeypatch.setattr(service.json, "loads", reject_whole_array)

    lines = service.parse_import_lines(b'[{"id":1},{"id":2}]', "jobs.json")
    assert [line.value for line in lines] == [{"id": 1}, {"id": 2}]


@pytest.mark.asyncio
async def test_invalid_and_suspicious_records_are_classified(memory_session):
    from model_class.knowledge_base import QualityIssue
    from src.import_service import import_job_file

    invalid = _job("T-JD-0301")
    invalid["job_description_raw"] = "过短"
    suspicious = _job("T-JD-0302")
    suspicious["industry"] = "8000-12000元"

    result = await import_job_file(
        memory_session, _job_bytes(invalid, suspicious), "mixed-quality.jsonl"
    )
    issues = (await memory_session.execute(select(QualityIssue))).scalars().all()

    assert result["quarantined"] == 1
    assert result["review"] >= 1
    assert {issue.code for issue in issues} >= {
        "description_too_short",
        "suspicious_industry",
    }


@pytest.mark.asyncio
async def test_direct_import_applies_level_but_keeps_unverified_gate_review(
    memory_session,
):
    from model_class.job_competency import JobPosting
    from src.import_service import import_job_file

    await _approved_source(memory_session)
    record = _provenance_job("T-JD-0401")
    record["job_description_raw"] = (
        "负责数据平台开发、部署和日常维护，能够独立完成数据任务，要求熟悉Python、"
        "Flink和Kafka，并持续优化数据质量、处理性能和交付流程。"
    )

    await import_job_file(memory_session, _job_bytes(record), "hard-metric.jsonl")
    posting = await memory_session.scalar(
        select(JobPosting).where(JobPosting.record_id == "T-JD-0401")
    )

    assert posting is not None
    assert posting.gate_status == "review"
    assert "unverified_provenance" in json.loads(posting.gate_issue_codes_json)
    assert posting.gate_rule_version == "competition-gate-v1"
    assert posting.machine_level == "mid"


@pytest.mark.asyncio
async def test_import_honors_staged_normalization_review_after_reclassification(
    memory_session,
):
    from model_class.job_competency import JobPosting
    from model_class.knowledge_base import QualityIssue, RawJobRecord
    from src.import_service import import_job_file

    record = _job("T-JD-STAGED-REVIEW")
    record["job_description_raw"] = (
        "负责数据平台开发、部署和日常维护，能够独立完成数据任务，要求熟悉Python、"
        "Flink和Kafka，并持续优化数据质量、处理性能和交付流程。"
    )
    record["normalization_status"] = "review"
    record["normalization_findings"] = [
        {
            "code": "record_id_from_url",
            "severity": "review",
            "field_name": "source_record_id",
            "reason": "missing_source_record_id",
        }
    ]
    record["adapter_extra"] = {
        "quality_gate": {
            "status": "review",
            "issue_codes": ["record_id_from_url"],
        }
    }

    result = await import_job_file(
        memory_session,
        _job_bytes(record),
        "staged-review.jsonl",
    )
    posting = await memory_session.scalar(
        select(JobPosting).where(JobPosting.record_id == record["record_id"])
    )
    raw_record = await memory_session.scalar(
        select(RawJobRecord).where(RawJobRecord.job_posting_id == posting.id)
    )
    issues = (
        (
            await memory_session.execute(
                select(QualityIssue).where(QualityIssue.job_posting_id == posting.id)
            )
        )
        .scalars()
        .all()
    )

    assert result["review"] == 1
    assert posting.status == "review"
    assert posting.gate_status == "review"
    assert "record_id_from_url" in json.loads(posting.gate_issue_codes_json)
    assert raw_record.status == "review"
    assert "record_id_from_url" in {issue.code for issue in issues}


@pytest.mark.asyncio
async def test_import_quarantines_staged_quarantine_without_creating_posting(
    memory_session,
):
    from model_class.job_competency import JobPosting
    from model_class.knowledge_base import QualityIssue, RawJobRecord
    from src.import_service import import_job_file

    record = _job("T-JD-STAGED-QUARANTINE")
    record["normalization_status"] = "quarantine"
    record["normalization_findings"] = [
        {
            "code": "short_description",
            "severity": "quarantine",
            "field_name": "job_description_raw",
            "reason": "fewer_than_40_effective_characters",
        }
    ]
    record["adapter_extra"] = {
        "quality_gate": {
            "status": "quarantined",
            "issue_codes": ["short_description"],
        }
    }

    result = await import_job_file(
        memory_session,
        _job_bytes(record),
        "staged-quarantine.jsonl",
    )
    raw_record = await memory_session.scalar(select(RawJobRecord))
    issues = (await memory_session.execute(select(QualityIssue))).scalars().all()

    assert result["imported"] == 0
    assert result["quarantined"] == 1
    assert (
        await memory_session.scalar(select(func.count()).select_from(JobPosting)) == 0
    )
    assert raw_record.status == "quarantined"
    assert "short_description" in {issue.code for issue in issues}


@pytest.mark.asyncio
async def test_cross_company_manual_ids_import_without_overwrite(
    memory_session, tmp_path
):
    from model_class.job_competency import JobPosting
    from src.import_service import import_job_file
    from src.job_collection.adapters.manual_manifest import ManualManifestAdapter
    from src.job_collection.models import SourceDefinition
    from src.job_collection.source_registry import SourceRegistry

    source = SourceDefinition.model_validate(
        {
            "source_id": "company_official_manifest",
            "source_name": "企业官方岗位人工清单",
            "source_type": "company_official",
            "market_scope": "china",
            "base_url": "https://company-official.invalid",
            "allowed_paths": ["/"],
            "collection_mode": "manual_url_manifest",
            "compliance_status": "manual_only",
            "compliance_note": "仅处理人工审核并在本地提供的官方岗位内容；禁止联网。",
            "rate_limit_seconds": 5.0,
            "max_pages": 1,
            "max_records": 10,
            "parser_name": "company_manifest",
            "parser_version": "v1",
            "enabled": True,
        }
    )
    description = (
        "负责 Python 服务和 FastAPI 接口设计、开发、自动化测试与维护，"
        "使用 PostgreSQL 建设稳定可靠的数据处理和部署流程。"
    )
    manifest_records = [
        {
            "source_name": f"{domain} 官方招聘",
            "source_url": f"https://{domain}/jobs/42",
            "company_name": f"{domain} 测试企业",
            "collection_authorization_note": "人工确认公开岗位，仅限本地研究处理。",
            "source_record_id": "job-42",
            "job_title_raw": "Python 后端工程师",
            "job_description_raw": description,
        }
        for domain in ("careers.alpha.test", "jobs.beta.test")
    ]
    manifest = tmp_path / "manual.jsonl"
    manifest.write_text(
        "\n".join(
            json.dumps(record, ensure_ascii=False) for record in manifest_records
        ),
        encoding="utf-8",
    )
    adapter = ManualManifestAdapter(source=source, registry=SourceRegistry([source]))
    staged = adapter.load_manifest(
        manifest,
        run_id="manual-import-cross-company",
        collected_at=datetime.fromisoformat("2026-08-06T12:00:00+00:00"),
    )
    payload = _job_bytes(*(record.model_dump(mode="json") for record in staged))

    result = await import_job_file(memory_session, payload, "manual-staged.jsonl")
    postings = (await memory_session.execute(select(JobPosting))).scalars().all()

    assert result["imported"] == 2
    assert result["revised"] == 0
    assert len(postings) == 2
    assert len({posting.record_id for posting in postings}) == 2
    assert len({posting.source_record_id for posting in postings}) == 2
