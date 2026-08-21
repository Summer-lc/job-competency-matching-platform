import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from model_class.base import Base
import model_class.job_competency  # noqa: F401


def test_official_evidence_source_scores_follow_trust_policy():
    from src.job_data_service import SOURCE_SCORES

    assert SOURCE_SCORES["occupation_standard"] == 1.0
    assert SOURCE_SCORES["technical_standard"] == 0.98
    assert SOURCE_SCORES["policy_document"] == 0.95
    assert SOURCE_SCORES["official_document"] == 0.92


def _record(record_id="A-JD-0001", description=None):
    return {
        "record_id": record_id,
        "collector_id": "A",
        "job_family_id": "JAVA_DEVELOPER",
        "job_title_raw": "上海-高级Java后端工程师",
        "company_name": "示例科技",
        "source_name": "企业官网",
        "source_type": "company_official",
        "source_url": f"https://example.com/{record_id}",
        "published_at": "2026-06-01",
        "collected_at": "2026-06-15T10:00:00+08:00",
        "job_description_raw": description
        or "负责Java和Spring Boot微服务开发，熟悉MySQL、Redis和Docker；有Kubernetes经验优先。",
    }


def test_parse_jsonl_and_csv_records():
    from src.job_data_service import parse_records

    rows = [_record(), _record("A-JD-0002")]
    jsonl = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows).encode("utf-8")
    assert len(parse_records(jsonl, "jobs.jsonl")) == 2

    csv_data = (
        "record_id,collector_id,job_family_id,job_title_raw,company_name,source_name,source_type,"
        "source_url,published_at,collected_at,job_description_raw\n"
        "A-JD-0003,A,JAVA_DEVELOPER,Java开发工程师,示例科技,企业官网,company_official,"
        "https://example.com/3,2026-06-01,2026-06-15T10:00:00+08:00,熟悉Java和MySQL\n"
    ).encode("utf-8")
    parsed = parse_records(csv_data, "jobs.csv")
    assert parsed[0]["record_id"] == "A-JD-0003"


def test_parse_records_enforces_evidence_import_bounds(monkeypatch):
    import src.job_data_service as service

    monkeypatch.setattr(service, "MAX_RECORD_IMPORT_BYTES", 32)
    with pytest.raises(service.RecordImportLimitError, match="byte limit"):
        service.parse_records(b"x" * 33, "evidence.jsonl")

    monkeypatch.setattr(service, "MAX_RECORD_IMPORT_BYTES", 1024)
    monkeypatch.setattr(service, "MAX_RECORD_IMPORT_LINE_BYTES", 16)
    with pytest.raises(service.RecordImportLimitError, match="line 1"):
        service.parse_records(b'{"value":"' + b"x" * 20 + b'"}', "evidence.jsonl")

    monkeypatch.setattr(service, "MAX_RECORD_IMPORT_LINE_BYTES", 1024)
    monkeypatch.setattr(service, "MAX_RECORD_IMPORT_RECORDS", 1)
    with pytest.raises(service.RecordImportLimitError, match="record count"):
        service.parse_records(b"{}\n{}\n", "evidence.jsonl")

    monkeypatch.setattr(service, "MAX_RECORD_IMPORT_RECORDS", 10)
    monkeypatch.setattr(service, "MAX_RECORD_IMPORT_JSON_DEPTH", 2)
    with pytest.raises(service.RecordImportLimitError, match="nesting"):
        service.parse_records(b'{"a":{"b":{"c":1}}}', "evidence.json")


@pytest.mark.parametrize(
    ("filename", "payload"),
    [
        ("evidence.json", b"[" + b",".join([b"{}"] * 10_001) + b"]"),
        ("evidence.jsonl", b"{}\n" * 10_001),
        ("evidence.csv", b"value\n" + b"x\n" * 10_001),
    ],
    ids=("json-array", "jsonl", "csv"),
)
def test_parse_records_stops_incrementally_at_record_10001(filename, payload):
    from src.job_data_service import RecordImportLimitError, parse_records

    with pytest.raises(RecordImportLimitError, match="record count"):
        parse_records(payload, filename)


def test_parse_records_rejects_depth_1100_before_decoding(monkeypatch):
    import src.job_data_service as service

    def decoder_must_not_run(*args, **kwargs):
        raise AssertionError("deep JSON reached the decoder")

    monkeypatch.setattr(service, "decode_json_array_incrementally", decoder_must_not_run)

    payload = ("[" * 1_100 + "0" + "]" * 1_100).encode()
    with pytest.raises(service.RecordImportLimitError, match="nesting"):
        service.parse_records(payload, "evidence.json")


def test_json_array_import_uses_incremental_raw_decode(monkeypatch):
    import src.job_data_service as service

    real_loads = service.json.loads

    def reject_whole_array(value, *args, **kwargs):
        if isinstance(value, str) and value.lstrip().startswith("["):
            raise AssertionError("whole JSON array was materialized")
        return real_loads(value, *args, **kwargs)

    monkeypatch.setattr(service.json, "loads", reject_whole_array)

    assert service.parse_records(b'[{"id":1},{"id":2}]', "evidence.json") == [
        {"id": 1},
        {"id": 2},
    ]


def test_prepare_job_record_normalizes_and_extracts_skills():
    from src.job_data_service import prepare_job_record

    prepared = prepare_job_record(_record())
    assert prepared["job_title_normalized"] == "Java开发工程师"
    assert len(prepared["content_hash"]) == 64
    assert len(prepared["simhash"]) == 16
    assert prepared["source_score"] >= 0.9
    skills = {item["name"]: item for item in prepared["skills"]}
    assert {"Java", "Spring Boot", "MySQL", "Redis", "Docker", "Kubernetes"}.issubset(skills)
    assert skills["Kubernetes"]["requirement_type"] == "preferred"
    assert skills["Java"]["evidence_text"]


def test_simhash_detects_near_duplicate_descriptions():
    from src.job_data_service import hamming_distance, simhash64

    first = "负责Java Spring Boot微服务开发，熟悉MySQL Redis Docker"
    second = "负责 Java、Spring Boot 微服务开发；熟悉 MySQL、Redis 和 Docker。"
    assert hamming_distance(simhash64(first), simhash64(second)) <= 8


@pytest.mark.asyncio
async def test_import_marks_exact_duplicate_and_persists_skill_evidence():
    from sqlalchemy import func, select
    from model_class.job_competency import JobPosting, JobPostingSkill
    from src.job_data_service import import_job_records

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    duplicate = _record("A-JD-0002")
    duplicate["source_url"] = "https://another.example.com/2"
    async with Session() as session:
        result = await import_job_records(session, [_record(), duplicate])
        assert result["imported"] == 2
        assert result["duplicates"] == 1
        duplicate_row = await session.scalar(
            select(JobPosting).where(JobPosting.record_id == "A-JD-0002")
        )
        assert duplicate_row.duplicate_of_id is not None
        skill_count = await session.scalar(select(func.count()).select_from(JobPostingSkill))
        assert skill_count >= 6

    await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_direct_import_cannot_self_assert_provenance_or_date_trust():
    from sqlalchemy import select
    from model_class.job_competency import JobPosting
    from src.job_data_service import import_job_records

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    record = _record("SPOOFED-PROVENANCE")
    record.update(
        {
            "source_id": "invented-source",
            "source_type": "invented_official",
            "source_domain": "trusted.example",
            "source_url": "HTTPS://Jobs.Example.COM/roles/1",
            "published_at_evidence": "invented structured field",
            "published_at_confidence": 1.0,
            "published_at_trusted": True,
            "parser_name": "invented-parser",
            "parser_version": "999",
            "collection_method": "invented-method",
        }
    )

    async with Session() as session:
        result = await import_job_records(session, [record])
        posting = await session.scalar(select(JobPosting))

    await engine.dispose()
    assert result["imported"] == 1
    assert posting.source_id is None
    assert posting.source_type == "unknown"
    assert posting.source_domain == "jobs.example.com"
    assert posting.provenance_status == "unverified"
    assert posting.published_at_trusted is False
    assert posting.source_score == 0.0
    assert posting.status == "review"
    assert posting.gate_status == "review"


@pytest.mark.asyncio
async def test_import_marks_simhash_near_duplicate():
    from sqlalchemy import select
    from model_class.job_competency import JobPosting
    from src.job_data_service import import_job_records

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    first = _record("A-JD-0101", "负责Java Spring Boot微服务开发，熟悉MySQL Redis Docker")
    second = _record("A-JD-0102", "负责Java Spring Boot微服务后端开发，熟悉MySQL Redis Docker")
    second["source_url"] = "https://mirror.example.com/101"
    async with Session() as session:
        result = await import_job_records(session, [first, second])
        row = await session.scalar(select(JobPosting).where(JobPosting.record_id == "A-JD-0102"))
    await engine.dispose()
    assert result["duplicates"] == 1
    assert row.status == "duplicate"
    assert row.duplicate_of_id is not None
