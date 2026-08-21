from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from model_class.base import Base


def _now() -> datetime:
    return datetime.now()


class JobSource(Base):
    __tablename__ = "job_source"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    market_scope: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="pending_review",
        server_default=text("'pending_review'"),
    )
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_paths_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default=text("'[]'")
    )
    collection_mode: Mapped[str] = mapped_column(String(50), nullable=False)
    compliance_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="pending_review"
    )
    compliance_note: Mapped[Optional[str]] = mapped_column(Text)
    rate_limit_seconds: Mapped[float] = mapped_column(
        Float, nullable=False, default=1.0, server_default=text("1")
    )
    max_pages_per_run: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    max_records_per_run: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    parser_name: Mapped[str] = mapped_column(String(100), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(50), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now, onupdate=_now
    )


class CollectionRun(Base):
    __tablename__ = "collection_run"
    __table_args__ = (Index("idx_collection_run_status", "status", "started_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    source_ids_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default=text("'[]'")
    )
    mode: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="running"
    )
    staging_dir: Mapped[str] = mapped_column(String(500), nullable=False)
    fetched_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    parsed_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    valid_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    review_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    quarantined_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    duplicate_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    imported_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    summary_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default=text("'{}'")
    )
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class CollectionSnapshot(Base):
    __tablename__ = "collection_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "collection_run_id",
            "job_source_id",
            "source_record_id",
            name="uq_collection_snapshot_run_source_record",
        ),
        Index(
            "idx_collection_snapshot_run_status",
            "collection_run_id",
            "parse_status",
        ),
        Index(
            "idx_collection_snapshot_source_record",
            "job_source_id",
            "source_record_id",
        ),
        Index("idx_collection_snapshot_content_hash", "content_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    collection_run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("collection_run.id", ondelete="CASCADE"), nullable=False
    )
    job_source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("job_source.id", ondelete="CASCADE"), nullable=False
    )
    source_record_id: Mapped[Optional[str]] = mapped_column(String(255))
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    response_status: Mapped[Optional[int]] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(500), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    parser_version: Mapped[Optional[str]] = mapped_column(String(50))
    parse_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="pending"
    )
    parse_error: Mapped[Optional[str]] = mapped_column(Text)


class DataRepairAudit(Base):
    __tablename__ = "data_repair_audit"
    __table_args__ = (
        Index("idx_repair_audit_run", "repair_run_id", "created_at"),
        Index("idx_repair_audit_posting", "job_posting_id", "field_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repair_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    job_posting_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("job_posting.id", ondelete="CASCADE"), nullable=False
    )
    field_name: Mapped[str] = mapped_column(String(80), nullable=False)
    before_json: Mapped[str] = mapped_column(Text, nullable=False)
    after_json: Mapped[str] = mapped_column(Text, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(80), nullable=False)
    applied: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class ImportBatch(Base):
    __tablename__ = "import_batch"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="processing")
    raw_lines: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parsed_lines: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    imported: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quarantined: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    review_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revised: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    affected_families_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    graph_sync_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="pending"
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    summary_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class RawJobRecord(Base):
    __tablename__ = "raw_job_record"
    __table_args__ = (
        UniqueConstraint("import_batch_id", "line_number", name="uq_raw_batch_line"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    import_batch_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("import_batch.id", ondelete="CASCADE"), nullable=False
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    raw_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parsed_json: Mapped[Optional[str]] = mapped_column(Text)
    job_posting_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("job_posting.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    error_code: Mapped[Optional[str]] = mapped_column(String(80))
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class JobPostingRevision(Base):
    __tablename__ = "job_posting_revision"
    __table_args__ = (
        UniqueConstraint("job_posting_id", "revision_no", name="uq_posting_revision"),
        Index(
            "uq_posting_revision_observation",
            "job_posting_id",
            "payload_hash",
            "observation_identity",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_posting_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("job_posting.id", ondelete="CASCADE"), nullable=False
    )
    import_batch_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("import_batch.id", ondelete="CASCADE"), nullable=False
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    observation_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    observation_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_payload: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class QualityIssue(Base):
    __tablename__ = "quality_issue"
    __table_args__ = (Index("idx_quality_issue_status", "severity", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    raw_record_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("raw_job_record.id", ondelete="CASCADE")
    )
    job_posting_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("job_posting.id", ondelete="CASCADE")
    )
    code: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str] = mapped_column(String(30), nullable=False)
    field_name: Mapped[Optional[str]] = mapped_column(String(80))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class Responsibility(Base):
    __tablename__ = "responsibility"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(500), unique=True, nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False, default="general")
    aliases_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class IndustryScenario(Base):
    __tablename__ = "industry_scenario"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    aliases_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class EvidenceSnippet(Base):
    __tablename__ = "evidence_snippet"
    __table_args__ = (
        Index("idx_evidence_entity", "entity_type", "entity_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    evidence_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    job_posting_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("job_posting.id", ondelete="CASCADE"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_key: Mapped[str] = mapped_column(String(500), nullable=False)
    evidence_text: Mapped[str] = mapped_column(Text, nullable=False)
    start_offset: Mapped[Optional[int]] = mapped_column(Integer)
    end_offset: Mapped[Optional[int]] = mapped_column(Integer)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    review_status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class JobProfileResponsibility(Base):
    __tablename__ = "job_profile_responsibility"
    __table_args__ = (
        UniqueConstraint("job_profile_id", "responsibility_id", name="uq_profile_responsibility"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("job_profile.id", ondelete="CASCADE"), nullable=False
    )
    responsibility_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("responsibility.id", ondelete="CASCADE"), nullable=False
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prevalence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    first_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    review_status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")


class JobProfileScenario(Base):
    __tablename__ = "job_profile_scenario"
    __table_args__ = (
        UniqueConstraint("job_profile_id", "scenario_id", name="uq_profile_scenario"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("job_profile.id", ondelete="CASCADE"), nullable=False
    )
    scenario_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("industry_scenario.id", ondelete="CASCADE"), nullable=False
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prevalence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    first_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    review_status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunk"
    __table_args__ = (Index("idx_chunk_family_status", "family_code", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chunk_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_entity_id: Mapped[str] = mapped_column(String(80), nullable=False)
    job_posting_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("job_posting.id", ondelete="CASCADE")
    )
    job_profile_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("job_profile.id", ondelete="CASCADE")
    )
    family_code: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[Optional[str]] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    embedding_json: Mapped[Optional[str]] = mapped_column(Text)
    embedding_model: Mapped[Optional[str]] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class JobProfileSnapshot(Base):
    __tablename__ = "job_profile_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("job_profile.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    content_signature: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    posting_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    data_cutoff: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class EvolutionEvent(Base):
    __tablename__ = "evolution_event"
    __table_args__ = (Index("idx_evolution_family", "family_code", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    family_code: Mapped[str] = mapped_column(String(80), nullable=False)
    previous_profile_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("job_profile.id", ondelete="SET NULL")
    )
    current_profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("job_profile.id", ondelete="CASCADE"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_key: Mapped[str] = mapped_column(String(500), nullable=False)
    change_type: Mapped[str] = mapped_column(String(30), nullable=False)
    before_json: Mapped[Optional[str]] = mapped_column(Text)
    after_json: Mapped[Optional[str]] = mapped_column(Text)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    previous_period: Mapped[Optional[str]] = mapped_column(String(10))
    current_period: Mapped[Optional[str]] = mapped_column(String(10))
    before_rate: Mapped[Optional[float]] = mapped_column(Float)
    after_rate: Mapped[Optional[float]] = mapped_column(Float)
    change_delta: Mapped[Optional[float]] = mapped_column(Float)
    event_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="legacy"
    )
    pipeline_run_id: Mapped[Optional[int]] = mapped_column(Integer)
    generation_key: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class PipelineRun(Base):
    __tablename__ = "pipeline_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    mode: Mapped[str] = mapped_column(String(30), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(80), nullable=False)
    family_codes_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="running")
    input_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    result_signature: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    error_summary: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class EvolutionEvidence(Base):
    __tablename__ = "evolution_evidence"
    __table_args__ = (
        UniqueConstraint(
            "evolution_event_id",
            "job_posting_id",
            "period_role",
            name="uq_evolution_posting_role",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    evolution_event_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("evolution_event.id", ondelete="CASCADE"), nullable=False
    )
    job_posting_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("job_posting.id", ondelete="CASCADE"), nullable=False
    )
    period_role: Mapped[str] = mapped_column(String(20), nullable=False)
    evidence_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class AcceptanceSnapshot(Base):
    __tablename__ = "acceptance_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    minimum_json: Mapped[str] = mapped_column(Text, nullable=False)
    internal_json: Mapped[str] = mapped_column(Text, nullable=False)
    overall_status: Mapped[str] = mapped_column(String(30), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
