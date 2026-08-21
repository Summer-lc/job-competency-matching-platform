from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from model_class.base import Base


def _now() -> datetime:
    return datetime.now()


class JobPosting(Base):
    __tablename__ = "job_posting"
    __table_args__ = (
        Index("idx_job_posting_family_date", "job_family_id", "published_at"),
        Index("idx_job_posting_hash", "content_hash"),
        Index("idx_job_posting_source_domain", "source_domain"),
        Index(
            "idx_job_posting_source_record", "source_id", "source_record_id"
        ),
        Index(
            "idx_job_posting_trusted_published_at",
            "published_at_trusted",
            "published_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    record_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    job_family_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    job_title_raw: Mapped[str] = mapped_column(String(255), nullable=False)
    job_title_normalized: Mapped[str] = mapped_column(String(255), nullable=False)
    company_name: Mapped[str] = mapped_column(String(255), nullable=False, default="unknown")
    industry: Mapped[Optional[str]] = mapped_column(String(255))
    region: Mapped[Optional[str]] = mapped_column(String(100))
    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False, default="public")
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[Optional[str]] = mapped_column(String(100))
    source_domain: Mapped[Optional[str]] = mapped_column(String(255))
    provenance_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="unverified", server_default=text("'unverified'")
    )
    source_record_id: Mapped[Optional[str]] = mapped_column(String(255))
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    published_at_evidence: Mapped[Optional[str]] = mapped_column(Text)
    published_at_confidence: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default=text("0")
    )
    published_at_trusted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    collected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    first_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    experience_requirement: Mapped[Optional[str]] = mapped_column(String(255))
    education_requirement: Mapped[Optional[str]] = mapped_column(String(100))
    salary_range: Mapped[Optional[str]] = mapped_column(String(100))
    job_description_raw: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    simhash: Mapped[str] = mapped_column(String(16), nullable=False)
    snapshot_hash: Mapped[Optional[str]] = mapped_column(String(64))
    parser_name: Mapped[Optional[str]] = mapped_column(String(100))
    parser_version: Mapped[Optional[str]] = mapped_column(String(50))
    collection_method: Mapped[Optional[str]] = mapped_column(String(50))
    observation_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    source_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    quality_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    duplicate_of_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("job_posting.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="valid")
    machine_level: Mapped[str] = mapped_column(
        String(30), nullable=False, default="unspecified"
    )
    machine_level_confidence: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )
    machine_level_evidence_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}"
    )
    manual_level: Mapped[Optional[str]] = mapped_column(String(30))
    manual_level_review_json: Mapped[Optional[str]] = mapped_column(Text)
    gate_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="review"
    )
    gate_issue_codes_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]"
    )
    gate_rule_version: Mapped[Optional[str]] = mapped_column(String(50))
    gated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    raw_payload: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class Skill(Base):
    __tablename__ = "skill"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False, default="general")
    aliases_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class JobPostingSkill(Base):
    __tablename__ = "job_posting_skill"
    __table_args__ = (
        UniqueConstraint("job_posting_id", "skill_id", "requirement_type", name="uq_posting_skill"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_posting_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("job_posting.id", ondelete="CASCADE"), nullable=False
    )
    skill_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("skill.id", ondelete="CASCADE"), nullable=False
    )
    requirement_type: Mapped[str] = mapped_column(String(30), nullable=False, default="required")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.8)
    evidence_text: Mapped[str] = mapped_column(Text, nullable=False)


class JobProfile(Base):
    __tablename__ = "job_profile"
    __table_args__ = (
        UniqueConstraint("family_code", "version", name="uq_profile_family_version"),
        Index("idx_profile_status", "status", "review_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    family_code: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    responsibilities_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    industry_scenarios_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="existing")
    level: Mapped[str] = mapped_column(String(30), nullable=False, default="all")
    tech_stack: Mapped[str] = mapped_column(String(80), nullable=False, default="general")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    review_status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    valid_from: Mapped[Optional[datetime]] = mapped_column(DateTime)
    valid_to: Mapped[Optional[datetime]] = mapped_column(DateTime)
    profile_kind: Mapped[str] = mapped_column(
        String(30), nullable=False, default="legacy"
    )
    period_key: Mapped[Optional[str]] = mapped_column(String(10))
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sample_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="insufficient"
    )
    input_signature: Mapped[Optional[str]] = mapped_column(String(64))
    pipeline_run_id: Mapped[Optional[int]] = mapped_column(Integer)
    generation_key: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    derivation_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="active"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)


class JobProfileSkill(Base):
    __tablename__ = "job_profile_skill"
    __table_args__ = (
        UniqueConstraint("job_profile_id", "skill_id", "requirement_type", name="uq_profile_skill"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("job_profile.id", ondelete="CASCADE"), nullable=False
    )
    skill_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("skill.id", ondelete="CASCADE"), nullable=False
    )
    requirement_type: Mapped[str] = mapped_column(String(30), nullable=False, default="required")
    proficiency_level: Mapped[str] = mapped_column(String(30), nullable=False, default="working")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prevalence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    source_type_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    source_domain_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    company_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    required_ratio: Mapped[float] = mapped_column(
        Float, nullable=False, default=-1.0, server_default=text("-1")
    )
    preferred_ratio: Mapped[float] = mapped_column(
        Float, nullable=False, default=-1.0, server_default=text("-1")
    )
    ratio_evidence_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="unknown", server_default=text("'unknown'")
    )
    first_published_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_published_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    cross_source_status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="single_source",
        server_default=text("'single_source'"),
    )


class EvidenceRecord(Base):
    __tablename__ = "evidence_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    evidence_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    job_family_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    evidence_type: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    publisher: Mapped[str] = mapped_column(String(255), nullable=False)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    related_skill: Mapped[Optional[str]] = mapped_column(String(150))
    evidence_summary: Mapped[str] = mapped_column(Text, nullable=False)
    source_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.7)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class ResumeRecord(Base):
    __tablename__ = "resume_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    parsed_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class MatchRecord(Base):
    __tablename__ = "match_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    resume_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("resume_record.id", ondelete="CASCADE"), nullable=False
    )
    job_profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("job_profile.id", ondelete="CASCADE"), nullable=False
    )
    total_score: Mapped[float] = mapped_column(Float, nullable=False)
    dimension_scores_json: Mapped[str] = mapped_column(Text, nullable=False)
    gap_json: Mapped[str] = mapped_column(Text, nullable=False)
    recommendations_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class RecommendationRun(Base):
    __tablename__ = "recommendation_run"
    __table_args__ = (
        Index("idx_recommendation_resume_created", "resume_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    resume_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("resume_record.id", ondelete="CASCADE"), nullable=False
    )
    scoring_version: Mapped[str] = mapped_column(String(80), nullable=False)
    input_signature: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    filters_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="running")
    result_signature: Mapped[Optional[str]] = mapped_column(String(64))
    error_summary: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class RecommendationResult(Base):
    __tablename__ = "recommendation_result"
    __table_args__ = (
        UniqueConstraint(
            "recommendation_run_id", "rank", name="uq_recommendation_run_rank"
        ),
        UniqueConstraint(
            "recommendation_run_id",
            "job_profile_id",
            name="uq_recommendation_run_profile",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recommendation_run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("recommendation_run.id", ondelete="CASCADE"), nullable=False
    )
    job_profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("job_profile.id", ondelete="CASCADE"), nullable=False
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    total_score: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[str] = mapped_column(String(30), nullable=False)
    result_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class ReviewItem(Base):
    __tablename__ = "review_item"
    __table_args__ = (Index("idx_review_status", "status", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    reviewer_note: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class EvaluationRun(Base):
    __tablename__ = "evaluation_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    metric_name: Mapped[str] = mapped_column(String(80), nullable=False)
    dataset_name: Mapped[str] = mapped_column(String(255), nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    precision: Mapped[Optional[float]] = mapped_column(Float)
    recall: Mapped[Optional[float]] = mapped_column(Float)
    f1: Mapped[Optional[float]] = mapped_column(Float)
    accuracy: Mapped[Optional[float]] = mapped_column(Float)
    details_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
