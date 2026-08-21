from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class JobPostingInput(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    record_id: str = Field(..., min_length=1, max_length=80)
    collector_id: Optional[str] = None
    job_family_id: str = Field(..., min_length=1, max_length=80)
    job_title_raw: str = Field(..., min_length=1, max_length=255)
    company_name: str = Field(..., min_length=1, max_length=255)
    industry: Optional[str] = None
    region: Optional[str] = None
    source_name: str = Field(..., min_length=1, max_length=255)
    source_type: str = "public_recruitment"
    source_url: str = Field(..., min_length=1)
    source_id: Optional[str] = None
    source_domain: Optional[str] = None
    source_record_id: Optional[str] = None
    published_at: Optional[str] = None
    published_at_evidence: Optional[str] = None
    published_at_confidence: float = 0.0
    published_at_trusted: bool = False
    collected_at: Optional[str] = None
    first_seen_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    snapshot_hash: Optional[str] = None
    parser_name: Optional[str] = None
    parser_version: Optional[str] = None
    collection_method: Optional[str] = None
    experience_requirement: Optional[str] = None
    education_requirement: Optional[str] = None
    salary_range: Optional[str] = None
    job_description_raw: str = Field(..., min_length=10)

class EvidenceInput(BaseModel):
    evidence_id: str
    job_family_id: str
    evidence_type: str
    title: str
    publisher: str
    published_at: Optional[str] = None
    source_url: str
    related_skill: Optional[str] = None
    evidence_summary: str


class MatchRequest(BaseModel):
    resume_id: int
    job_profile_id: int


class JobRecommendationRequest(BaseModel):
    resume_id: int
    limit: int = Field(5, ge=1, le=10)
    levels: Optional[List[str]] = None
    family_codes: Optional[List[str]] = None


class MatchByPayloadRequest(BaseModel):
    resume: Dict
    job_profile: Dict


class ReviewDecisionRequest(BaseModel):
    decision: str = Field(..., pattern="^(approved|rejected)$")
    reviewer_note: Optional[str] = None
    replacement_payload: Optional[Dict] = None


class JobProfileUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    responsibilities: Optional[List[str]] = None
    industry_scenarios: Optional[List[str]] = None
    status: Optional[str] = Field(None, pattern="^(emerging|existing)$")
    level: Optional[str] = None
    tech_stack: Optional[str] = None
    required_skills: Optional[List[str]] = None
    preferred_skills: Optional[List[str]] = None


class EvidenceQuestionRequest(BaseModel):
    question: str = Field(..., min_length=2)
    job_family_id: str
    model: Optional[str] = None


class KnowledgeAnswerRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=500)
    family_code: Optional[str] = Field(None, max_length=80)
    limit: int = Field(6, ge=3, le=12)
    model: Optional[str] = None


class HardMetricsRunRequest(BaseModel):
    mode: str = Field("incremental", pattern="^(full|incremental)$")
    family_codes: Optional[List[str]] = None


class JobLevelReviewRequest(BaseModel):
    level: str = Field(pattern="^(junior|mid|senior|expert|unspecified)$")
    reviewer: str = Field(min_length=1, max_length=100)
    note: str = Field(min_length=2, max_length=500)
