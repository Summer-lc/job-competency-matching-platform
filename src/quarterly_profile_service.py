from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Collection, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from model_class.job_competency import (
    JobPosting,
    JobPostingSkill,
    JobProfile,
    JobProfileSkill,
    Skill,
)
from model_class.knowledge_base import (
    EvidenceSnippet,
    IndustryScenario,
    JobProfileResponsibility,
    JobProfileScenario,
    JobProfileSnapshot,
    Responsibility,
)
from src.competition_rules import (
    SKILL_EVIDENCE_RULE_VERSION,
    aggregate_skill_evidence,
    quarter_key,
)
from src.job_data_service import JOB_FAMILY_NAMES


PROFILE_RULE_VERSION = "competition-profile-v3"
EMERGING_FAMILIES = {
    "AI_AGENT_ENGINEER",
    "LLM_APPLICATION_ENGINEER",
    "RAG_ENGINEER",
    "MLOPS_ENGINEER",
    "MULTIMODAL_ENGINEER",
    "PROMPT_ENGINEER",
    "DIGITAL_TWIN_ENGINEER",
}
FAMILY_TECH_STACK = {
    "JAVA_DEVELOPER": "backend",
    "PYTHON_BACKEND": "backend",
    "GO_DEVELOPER": "backend",
    "FRONTEND_DEVELOPER": "frontend",
    "DEVOPS_ENGINEER": "cloud_native",
    "SRE_ENGINEER": "cloud_native",
    "CLOUD_NATIVE_ENGINEER": "cloud_native",
    "AI_AGENT_ENGINEER": "ai",
    "LLM_APPLICATION_ENGINEER": "ai",
    "RAG_ENGINEER": "ai",
    "MLOPS_ENGINEER": "ai",
    "MULTIMODAL_ENGINEER": "ai",
    "PROMPT_ENGINEER": "ai",
    "AI_SOLUTION_ENGINEER": "ai",
    "BIG_DATA_DEVELOPER": "big_data",
    "DATA_GOVERNANCE_ENGINEER": "big_data",
    "DATA_ENGINEER": "big_data",
    "IOT_ENGINEER": "iot",
    "EDGE_COMPUTING_ENGINEER": "iot",
    "CYBERSECURITY_ENGINEER": "security",
    "DIGITAL_TWIN_ENGINEER": "intelligent_system",
    "ROBOTICS_ENGINEER": "intelligent_system",
}


@dataclass(frozen=True, order=True)
class ProfileSlice:
    family_code: str
    tech_stack: str
    level: str
    period_key: str


def profile_generation_key(
    slice_: ProfileSlice, rule_version: str = PROFILE_RULE_VERSION
) -> str:
    raw = "|".join(
        (
            rule_version,
            slice_.family_code,
            slice_.tech_stack,
            slice_.level,
            slice_.period_key,
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def input_signature(
    db: AsyncSession,
    postings: Sequence[JobPosting],
    *,
    skill_rows: Sequence[dict[str, object]] | None = None,
    responsibility_rows: Sequence[dict[str, object]] | None = None,
) -> str:
    await db.flush()
    posting_ids = [posting.id for posting in postings]
    record_ids = {posting.id: posting.record_id for posting in postings}
    if skill_rows is None or responsibility_rows is None:
        loaded_skills, loaded_responsibilities = await _profile_input_rows(
            db, posting_ids
        )
        skill_rows = loaded_skills if skill_rows is None else skill_rows
        responsibility_rows = (
            loaded_responsibilities
            if responsibility_rows is None
            else responsibility_rows
        )
    skill_inputs = [
        {
            "record_id": record_ids[int(row["posting_id"])],
            "skill_id": row["skill_id"],
            "skill_name": row["name"],
            "skill_category": row["category"],
            "requirement_type": row["requirement_type"],
            "confidence": float(row["link_confidence"]),
            "evidence_text": row["evidence_text"],
        }
        for row in skill_rows
    ]
    responsibility_inputs = [
        {
            "record_id": record_ids[int(row["posting_id"])],
            "entity_key": row["entity_key"],
            "evidence_text": row["evidence_text"],
            "text_hash": row["text_hash"],
            "confidence": float(row["confidence"]),
            "review_status": row["review_status"],
        }
        for row in responsibility_rows
    ]
    skill_inputs.sort(
        key=lambda item: (
            str(item["record_id"]),
            str(item["skill_name"]).casefold(),
            str(item["skill_name"]),
            str(item["skill_id"]),
            str(item["requirement_type"]),
            float(item["confidence"]),
        )
    )
    responsibility_inputs.sort(
        key=lambda item: (
            str(item["record_id"]),
            str(item["entity_key"]).casefold(),
            str(item["entity_key"]),
            str(item["text_hash"]),
            str(item["evidence_text"]),
        )
    )
    posting_inputs = [
        {
            "record_id": posting.record_id,
            "content_hash": posting.content_hash,
            "published_at": posting.published_at.isoformat()
            if posting.published_at
            else None,
            "published_at_evidence": posting.published_at_evidence,
            "published_at_confidence": float(posting.published_at_confidence),
            "published_at_trusted": bool(posting.published_at_trusted),
            "source_id": posting.source_id,
            "source_record_id": posting.source_record_id,
            "source_type": str(posting.source_type or "").strip().casefold(),
            "source_domain": str(posting.source_domain or "").strip().casefold(),
            "source_url": posting.source_url,
            "provenance_status": posting.provenance_status,
            "source_score": float(posting.source_score),
            "quality_score": float(posting.quality_score),
            "company_name": str(posting.company_name or "").strip().casefold(),
            "industry": str(posting.industry or "").strip().casefold(),
            "snapshot_hash": posting.snapshot_hash,
            "parser_name": posting.parser_name,
            "parser_version": posting.parser_version,
            "collection_method": posting.collection_method,
            "manual_level": posting.manual_level,
            "machine_level": posting.machine_level,
        }
        for posting in sorted(postings, key=lambda item: item.record_id)
    ]
    raw = json.dumps(
        {
            "postings": posting_inputs,
            "skills": skill_inputs,
            "responsibilities": responsibility_inputs,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _effective_level(posting: JobPosting) -> str:
    return posting.manual_level or posting.machine_level or "unspecified"


def _sample_status(count: int) -> str:
    if count < 10:
        return "insufficient"
    if count < 20:
        return "low_sample"
    return "ready"


async def _profile_input_rows(
    db: AsyncSession, posting_ids: Sequence[int]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    skill_rows = [
        {
            "posting_id": row[0],
            "skill_id": row[1],
            "name": row[2],
            "category": row[3],
            "requirement_type": row[4],
            "link_confidence": row[5],
            "evidence_text": row[6],
            "source_score": row[7],
            "source_type": row[8],
            "source_domain": row[9],
            "provenance_status": row[10],
            "company_name": row[11],
            "published_at": row[12],
            "published_at_trusted": row[13],
        }
        for row in (
            await db.execute(
            select(
                JobPostingSkill.job_posting_id,
                Skill.id,
                Skill.name,
                Skill.category,
                JobPostingSkill.requirement_type,
                JobPostingSkill.confidence,
                JobPostingSkill.evidence_text,
                JobPosting.source_score,
                JobPosting.source_type,
                JobPosting.source_domain,
                JobPosting.provenance_status,
                JobPosting.company_name,
                JobPosting.published_at,
                JobPosting.published_at_trusted,
            )
            .join(Skill, Skill.id == JobPostingSkill.skill_id)
            .join(JobPosting, JobPosting.id == JobPostingSkill.job_posting_id)
            .where(JobPostingSkill.job_posting_id.in_(posting_ids))
            .order_by(JobPostingSkill.job_posting_id, Skill.name, Skill.id)
            )
        ).all()
    ]
    responsibility_rows = [
        {
            "posting_id": row[0],
            "entity_key": row[1],
            "evidence_text": row[2],
            "text_hash": row[3],
            "confidence": row[4],
            "review_status": row[5],
        }
        for row in (
            await db.execute(
                select(
                    EvidenceSnippet.job_posting_id,
                    EvidenceSnippet.entity_key,
                    EvidenceSnippet.evidence_text,
                    EvidenceSnippet.text_hash,
                    EvidenceSnippet.confidence,
                    EvidenceSnippet.review_status,
                )
                .where(
                    EvidenceSnippet.job_posting_id.in_(posting_ids),
                    EvidenceSnippet.entity_type == "responsibility",
                )
                .order_by(
                    EvidenceSnippet.job_posting_id,
                    EvidenceSnippet.entity_key,
                    EvidenceSnippet.id,
                )
            )
        ).all()
    ]
    return skill_rows, responsibility_rows


def _skill_payloads(
    postings: Sequence[JobPosting], rows: Sequence[dict[str, object]]
) -> list[dict[str, object]]:
    return aggregate_skill_evidence(rows, total_postings=len(postings))


def _dimension_count(postings: Sequence[JobPosting], field: str) -> int:
    return len(
        {
            normalized
            for posting in postings
            if posting.provenance_status == "approved"
            if (normalized := str(getattr(posting, field) or "").strip().casefold())
            not in {"unknown", "未知"}
        }
    )


def _skill_snapshot_payloads(
    skills: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    payloads = []
    for skill in skills:
        payload = dict(skill)
        for field in ("first_published_at", "last_published_at"):
            value = payload[field]
            payload[field] = value.isoformat() if hasattr(value, "isoformat") else None
        payloads.append(payload)
    return payloads


def _supersede_profile(profile: JobProfile) -> None:
    profile.generation_key = hashlib.sha256(
        (
            f"{profile.generation_key}|superseded|{profile.id}|"
            f"{profile.input_signature}"
        ).encode("utf-8")
    ).hexdigest()
    profile.derivation_status = "superseded"


def _observation_range(
    postings: Sequence[JobPosting],
) -> tuple[object | None, object | None]:
    first_seen = min(
        (item.first_seen_at for item in postings if item.first_seen_at), default=None
    )
    last_seen = max(
        (
            item.last_seen_at or item.first_seen_at
            for item in postings
            if item.first_seen_at
        ),
        default=None,
    )
    return first_seen, last_seen


def _responsibility_payloads(
    postings: Sequence[JobPosting], rows: Sequence[dict[str, object]]
) -> list[dict[str, object]]:
    evidence: dict[str, set[int]] = defaultdict(set)
    for row in rows:
        evidence[str(row["entity_key"])].add(int(row["posting_id"]))
    return [
        {
            "name": name,
            "evidence_count": len(posting_ids),
            "prevalence": round(len(posting_ids) / len(postings), 4),
        }
        for name, posting_ids in sorted(
            evidence.items(),
            key=lambda item: (-len(item[1]), item[0].casefold(), item[0]),
        )[:12]
    ]


def _scenario_payloads(postings: Sequence[JobPosting]) -> list[dict[str, object]]:
    counts = Counter(
        posting.industry
        for posting in postings
        if posting.industry and posting.industry != "unknown"
    )
    return [
        {
            "name": name,
            "evidence_count": count,
            "prevalence": round(count / len(postings), 4),
        }
        for name, count in sorted(
            counts.items(),
            key=lambda item: (-item[1], item[0].casefold(), item[0]),
        )[:5]
    ]


async def _replace_profile_children(
    db: AsyncSession,
    profile: JobProfile,
    postings: Sequence[JobPosting],
    skills: list[dict[str, object]],
    responsibilities: list[dict[str, object]],
    scenarios: list[dict[str, object]],
) -> None:
    await db.execute(
        delete(JobProfileSkill).where(JobProfileSkill.job_profile_id == profile.id)
    )
    await db.execute(
        delete(JobProfileResponsibility).where(
            JobProfileResponsibility.job_profile_id == profile.id
        )
    )
    await db.execute(
        delete(JobProfileScenario).where(JobProfileScenario.job_profile_id == profile.id)
    )
    first_seen, last_seen = _observation_range(postings)
    for item in skills:
        db.add(
            JobProfileSkill(
                job_profile_id=profile.id,
                skill_id=int(item["id"]),
                requirement_type=str(item["requirement_type"]),
                proficiency_level=(
                    "advanced" if float(item["prevalence"]) >= 0.65 else "working"
                ),
                confidence=float(item["confidence"]),
                evidence_count=int(item["evidence_count"]),
                prevalence=float(item["prevalence"]),
                source_type_count=int(item["source_type_count"]),
                source_domain_count=int(item["source_domain_count"]),
                company_count=int(item["company_count"]),
                required_ratio=float(item["required_ratio"]),
                preferred_ratio=float(item["preferred_ratio"]),
                ratio_evidence_status=str(item["ratio_evidence_status"]),
                first_published_at=item["first_published_at"],
                last_published_at=item["last_published_at"],
                cross_source_status=str(item["cross_source_status"]),
            )
        )
    responsibility_names = [str(item["name"]) for item in responsibilities]
    responsibility_entities = {
        item.name: item
        for item in (
            (
                await db.execute(
                    select(Responsibility).where(
                        Responsibility.name.in_(responsibility_names)
                    )
                )
            ).scalars()
            if responsibility_names
            else []
        )
    }
    for name in responsibility_names:
        if name not in responsibility_entities:
            responsibility_entities[name] = Responsibility(name=name)
            db.add(responsibility_entities[name])
    scenario_names = [str(item["name"]) for item in scenarios]
    scenario_entities = {
        item.name: item
        for item in (
            (
                await db.execute(
                    select(IndustryScenario).where(
                        IndustryScenario.name.in_(scenario_names)
                    )
                )
            ).scalars()
            if scenario_names
            else []
        )
    }
    for name in scenario_names:
        if name not in scenario_entities:
            scenario_entities[name] = IndustryScenario(name=name)
            db.add(scenario_entities[name])
    if responsibility_names or scenario_names:
        await db.flush()
    for item in responsibilities:
        entity = responsibility_entities[str(item["name"])]
        db.add(
            JobProfileResponsibility(
                job_profile_id=profile.id,
                responsibility_id=entity.id,
                confidence=0.82,
                evidence_count=int(item["evidence_count"]),
                prevalence=float(item["prevalence"]),
                first_seen_at=first_seen,
                last_seen_at=last_seen,
                review_status=profile.review_status,
            )
        )
    for item in scenarios:
        entity = scenario_entities[str(item["name"])]
        db.add(
            JobProfileScenario(
                job_profile_id=profile.id,
                scenario_id=entity.id,
                confidence=0.80,
                evidence_count=int(item["evidence_count"]),
                prevalence=float(item["prevalence"]),
                first_seen_at=first_seen,
                last_seen_at=last_seen,
                review_status=profile.review_status,
            )
        )


async def rebuild_quarterly_profiles(
    db: AsyncSession,
    *,
    pipeline_run_id: int,
    family_codes: Collection[str] | None = None,
) -> dict[str, object]:
    query = (
        select(JobPosting)
        .options(
            load_only(
                JobPosting.id,
                JobPosting.record_id,
                JobPosting.job_family_id,
                JobPosting.company_name,
                JobPosting.industry,
                JobPosting.source_id,
                JobPosting.source_record_id,
                JobPosting.source_type,
                JobPosting.source_domain,
                JobPosting.source_url,
                JobPosting.provenance_status,
                JobPosting.published_at,
                JobPosting.published_at_evidence,
                JobPosting.published_at_confidence,
                JobPosting.published_at_trusted,
                JobPosting.first_seen_at,
                JobPosting.last_seen_at,
                JobPosting.snapshot_hash,
                JobPosting.parser_name,
                JobPosting.parser_version,
                JobPosting.collection_method,
                JobPosting.content_hash,
                JobPosting.source_score,
                JobPosting.quality_score,
                JobPosting.machine_level,
                JobPosting.manual_level,
            )
        )
        .where(
            JobPosting.gate_status == "valid",
            JobPosting.duplicate_of_id.is_(None),
            JobPosting.provenance_status == "approved",
            JobPosting.published_at.is_not(None),
            JobPosting.published_at_trusted.is_(True),
        )
    )
    if family_codes is not None:
        query = query.where(JobPosting.job_family_id.in_(family_codes))
    postings = list((await db.execute(query.order_by(JobPosting.id))).scalars())
    grouped: dict[ProfileSlice, list[JobPosting]] = defaultdict(list)
    for posting in postings:
        slice_ = ProfileSlice(
            family_code=posting.job_family_id,
            tech_stack=FAMILY_TECH_STACK.get(posting.job_family_id, "general"),
            level=_effective_level(posting),
            period_key=quarter_key(posting.published_at),
        )
        grouped[slice_].append(posting)

    summary: dict[str, object] = {
        "profiles_created": 0,
        "profiles_updated": 0,
        "profiles_superseded": 0,
        "insufficient": 0,
        "low_sample": 0,
        "ready": 0,
        "slices": len(grouped),
    }
    eligible_posting_ids = [
        posting.id
        for members in grouped.values()
        if _sample_status(len(members)) != "insufficient"
        for posting in members
    ]
    if eligible_posting_ids:
        all_skill_rows, all_responsibility_rows = await _profile_input_rows(
            db, eligible_posting_ids
        )
    else:
        all_skill_rows, all_responsibility_rows = [], []
    active_profile_query = select(JobProfile).where(
        JobProfile.profile_kind == "quarterly",
        JobProfile.derivation_status == "active",
    )
    if family_codes is not None:
        active_profile_query = active_profile_query.where(
            JobProfile.family_code.in_(family_codes)
        )
    active_profiles = list(
        (
            await db.execute(active_profile_query.order_by(JobProfile.id))
        ).scalars()
    )
    active_generation_keys: set[str] = set()
    for slice_, members in sorted(grouped.items()):
        status = _sample_status(len(members))
        if status == "insufficient":
            summary["insufficient"] += 1
            continue
        member_ids = {item.id for item in members}
        skill_rows = [
            row
            for row in all_skill_rows
            if int(row["posting_id"]) in member_ids
        ]
        responsibility_rows = [
            row
            for row in all_responsibility_rows
            if int(row["posting_id"]) in member_ids
        ]
        signature = await input_signature(
            db,
            members,
            skill_rows=skill_rows,
            responsibility_rows=responsibility_rows,
        )
        generation_key = profile_generation_key(slice_)
        active_generation_keys.add(generation_key)
        profile = next(
            (
                item
                for item in active_profiles
                if item.generation_key == generation_key
                and item.derivation_status == "active"
            ),
            None,
        )
        if profile is not None and profile.input_signature not in (None, signature):
            _supersede_profile(profile)
            summary["profiles_superseded"] += 1
            profile = None
        if profile is None:
            latest_version = await db.scalar(
                select(func.max(JobProfile.version)).where(
                    JobProfile.family_code == slice_.family_code
                )
            )
            profile = JobProfile(
                family_code=slice_.family_code,
                name=JOB_FAMILY_NAMES.get(slice_.family_code, slice_.family_code),
                status=(
                    "emerging"
                    if slice_.family_code in EMERGING_FAMILIES
                    else "existing"
                ),
                level=slice_.level,
                tech_stack=slice_.tech_stack,
                version=int(latest_version or 0) + 1,
                review_status="pending",
                profile_kind="quarterly",
                period_key=slice_.period_key,
                generation_key=generation_key,
                derivation_status="active",
            )
            db.add(profile)
            await db.flush()
            active_profiles.append(profile)
            summary["profiles_created"] += 1
        else:
            summary["profiles_updated"] += 1

        obsolete = [
            item
            for item in active_profiles
            if item is not profile
            and item.derivation_status == "active"
            and item.family_code == slice_.family_code
            and item.tech_stack == slice_.tech_stack
            and item.level == slice_.level
            and item.period_key == slice_.period_key
        ]
        for old_profile in obsolete:
            _supersede_profile(old_profile)
            summary["profiles_superseded"] += 1

        skills = _skill_payloads(members, skill_rows)
        responsibilities = _responsibility_payloads(
            members, responsibility_rows
        )
        scenarios = _scenario_payloads(members)
        average_quality = sum(float(item.quality_score) for item in members) / len(members)
        source_type_count = _dimension_count(members, "source_type")
        source_domain_count = _dimension_count(members, "source_domain")
        company_count = _dimension_count(members, "company_name")
        profile.description = (
            f"基于{len(members)}条有效去重JD形成的{slice_.period_key}岗位画像。"
        )
        profile.responsibilities_json = json.dumps(
            [item["name"] for item in responsibilities], ensure_ascii=False
        )
        profile.industry_scenarios_json = json.dumps(
            [item["name"] for item in scenarios], ensure_ascii=False
        )
        profile.confidence = round(average_quality, 4)
        profile.valid_from = min(item.published_at for item in members if item.published_at)
        profile.valid_to = max(item.published_at for item in members if item.published_at)
        profile.sample_count = len(members)
        profile.sample_status = status
        profile.input_signature = signature
        profile.pipeline_run_id = pipeline_run_id
        profile.derivation_status = "active"
        await _replace_profile_children(
            db, profile, members, skills, responsibilities, scenarios
        )
        snapshot_payload = {
            "family_code": slice_.family_code,
            "tech_stack": slice_.tech_stack,
            "level": slice_.level,
            "period_key": slice_.period_key,
            "sample_count": len(members),
            "sample_status": status,
            "skills": _skill_snapshot_payloads(skills),
            "responsibilities": responsibilities,
            "scenarios": scenarios,
            "source_type_count": source_type_count,
            "source_domain_count": source_domain_count,
            "company_count": company_count,
            "profile_rule_version": PROFILE_RULE_VERSION,
            "skill_evidence_rule_version": SKILL_EVIDENCE_RULE_VERSION,
            "temporal_basis": {
                "publication_time_field": "published_at",
                "publication_trust_required": True,
                "quarter_assignment": "published_at",
                "observation_time_field": "first_seen_at",
                "observation_affects_profile": False,
            },
        }
        canonical = json.dumps(
            snapshot_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        snapshot_signature = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        snapshot = await db.scalar(
            select(JobProfileSnapshot).where(
                JobProfileSnapshot.job_profile_id == profile.id
            )
        )
        if snapshot is None:
            snapshot = JobProfileSnapshot(
                job_profile_id=profile.id,
            )
            db.add(snapshot)
        snapshot.content_signature = snapshot_signature
        snapshot.payload_json = canonical
        snapshot.posting_count = len(members)
        snapshot.source_count = max(source_type_count, source_domain_count)
        snapshot.data_cutoff = profile.valid_to
        summary[status] += 1
    for stale_profile in active_profiles:
        if stale_profile.derivation_status != "active":
            continue
        if stale_profile.generation_key in active_generation_keys:
            continue
        _supersede_profile(stale_profile)
        summary["profiles_superseded"] += 1
    from src.evolution_service import reconcile_formal_evolution_events

    await reconcile_formal_evolution_events(db)
    await db.flush()
    return summary


async def list_quarterly_profiles(
    db: AsyncSession,
    *,
    family_code: str | None = None,
    tech_stack: str | None = None,
    level: str | None = None,
    period_key: str | None = None,
) -> dict[str, object]:
    query = select(JobProfile).where(
        JobProfile.profile_kind == "quarterly",
        JobProfile.derivation_status == "active",
    )
    for column, value in (
        (JobProfile.family_code, family_code),
        (JobProfile.tech_stack, tech_stack),
        (JobProfile.level, level),
        (JobProfile.period_key, period_key),
    ):
        if value:
            query = query.where(column == value)
    rows = list(
        (
            await db.execute(
                query.order_by(
                    JobProfile.family_code,
                    JobProfile.period_key.desc(),
                    JobProfile.level,
                )
            )
        ).scalars()
    )
    return {
        "items": [
            {
                "id": row.id,
                "family_code": row.family_code,
                "name": row.name,
                "status": row.status,
                "tech_stack": row.tech_stack,
                "level": row.level,
                "period_key": row.period_key,
                "sample_count": row.sample_count,
                "sample_status": row.sample_status,
                "confidence": row.confidence,
                "review_status": row.review_status,
                "version": row.version,
            }
            for row in rows
        ],
        "total": len(rows),
    }
