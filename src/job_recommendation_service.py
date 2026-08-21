from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Collection
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from model_class.job_competency import (
    EvidenceRecord,
    JobProfile,
    JobProfileSkill,
    RecommendationResult,
    RecommendationRun,
    ResumeRecord,
    Skill,
)
from src.matching_service import (
    SCORING_VERSION,
    match_resume_to_job,
    required_years_for_profile,
)


RECOMMENDATION_VERSION = "job-recommendation-v1"
CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3}


def _json_load(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


async def profile_matching_payload(db: AsyncSession, profile: JobProfile) -> dict:
    rows = (
        await db.execute(
            select(JobProfileSkill, Skill)
            .join(Skill, Skill.id == JobProfileSkill.skill_id)
            .where(JobProfileSkill.job_profile_id == profile.id)
            .order_by(JobProfileSkill.requirement_type, Skill.name)
        )
    ).all()
    required = []
    preferred = []
    details = []
    for link, skill in rows:
        item = {
            "name": skill.name,
            "category": skill.category,
            "requirement_type": link.requirement_type,
            "proficiency_level": link.proficiency_level,
            "confidence": float(link.confidence),
            "evidence_count": int(link.evidence_count),
            "prevalence": float(link.prevalence),
        }
        details.append(item)
        (required if link.requirement_type == "required" else preferred).append(skill.name)
    evidence_rows = list(
        (
            await db.execute(
                select(EvidenceRecord)
                .where(EvidenceRecord.job_family_id == profile.family_code)
                .order_by(EvidenceRecord.source_score.desc(), EvidenceRecord.evidence_id)
            )
        ).scalars()
    )
    payload = {
        "id": profile.id,
        "family_code": profile.family_code,
        "name": profile.name,
        "description": profile.description,
        "level": profile.level,
        "tech_stack": profile.tech_stack,
        "version": profile.version,
        "confidence": float(profile.confidence),
        "review_status": profile.review_status,
        "profile_kind": profile.profile_kind,
        "period_key": profile.period_key,
        "sample_count": profile.sample_count,
        "sample_status": profile.sample_status,
        "input_signature": profile.input_signature,
        "responsibilities": _json_load(profile.responsibilities_json, []),
        "industry_scenarios": _json_load(profile.industry_scenarios_json, []),
        "required_skills": required,
        "preferred_skills": preferred,
        "skills": details,
        "evidence_records": [
            {
                "title": item.title,
                "publisher": item.publisher,
                "source_url": item.source_url,
                "related_skill": item.related_skill,
            }
            for item in evidence_rows
            if item.source_url and item.related_skill
        ],
    }
    payload["required_years"] = required_years_for_profile(payload)
    return payload


async def load_candidate_profiles(
    db: AsyncSession,
    *,
    levels: Collection[str] | None = None,
    family_codes: Collection[str] | None = None,
) -> list[tuple[JobProfile, dict]]:
    query = select(JobProfile).where(
        JobProfile.derivation_status == "active",
        JobProfile.review_status != "rejected",
    )
    if levels:
        query = query.where(JobProfile.level.in_(set(levels)))
    if family_codes:
        query = query.where(JobProfile.family_code.in_(set(family_codes)))
    profiles = list(
        (
            await db.execute(
                query.order_by(
                    JobProfile.family_code,
                    JobProfile.version.desc(),
                    JobProfile.id.desc(),
                )
            )
        ).scalars()
    )
    by_family: dict[str, list[JobProfile]] = {}
    for profile in profiles:
        by_family.setdefault(profile.family_code, []).append(profile)

    selected: list[JobProfile] = []
    for family in sorted(by_family):
        members = by_family[family]
        quarterly = [item for item in members if item.profile_kind == "quarterly"]
        if quarterly:
            selected.extend(quarterly)
            continue
        latest_by_level: dict[str, JobProfile] = {}
        for item in members:
            latest_by_level.setdefault(item.level, item)
        selected.extend(latest_by_level.values())

    candidates = []
    for profile in selected:
        payload = await profile_matching_payload(db, profile)
        if not payload["required_skills"] and not payload["preferred_skills"]:
            continue
        candidates.append((profile, payload))
    return candidates


def _candidate_signature_payload(candidates: list[tuple[JobProfile, dict]]) -> list[dict]:
    return [
        {
            "profile_id": profile.id,
            "family_code": profile.family_code,
            "version": profile.version,
            "input_signature": profile.input_signature,
            "payload": payload,
        }
        for profile, payload in sorted(candidates, key=lambda item: item[0].id)
    ]


def recommendation_input_signature(
    resume: ResumeRecord,
    candidates: list[tuple[JobProfile, dict]],
    filters: dict,
) -> str:
    state = {
        "resume_id": resume.id,
        "resume_hash": resume.content_hash,
        "resume_profile": _json_load(resume.parsed_json, {}),
        "scoring_version": SCORING_VERSION,
        "recommendation_version": RECOMMENDATION_VERSION,
        "filters": filters,
        "candidates": _candidate_signature_payload(candidates),
    }
    canonical = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _result_signature(items: list[dict]) -> str:
    canonical = json.dumps(
        [
            {
                "profile_id": item["profile_id"],
                "rank": item["rank"],
                "total_score": item["total_score"],
                "confidence": item["confidence"],
            }
            for item in items
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def rank_job_payloads(
    resume: dict,
    candidates: Collection[dict],
    limit: int = 5,
) -> list[dict]:
    """Rank in-memory job payloads with the same policy used by persisted runs."""
    scored = []
    for candidate in candidates:
        payload = dict(candidate)
        profile_id = payload.get("id", payload.get("profile_id"))
        family_code = str(
            payload.get("family_code") or profile_id or payload.get("name") or "unknown"
        )
        sample_count = int(payload.get("sample_count") or 0)
        evidence_count = sum(
            int(item.get("evidence_count") or 0)
            for item in payload.get("skills", [])
            if isinstance(item, dict)
        )
        sample_status = str(payload.get("sample_status") or "ready")
        match = match_resume_to_job(resume, payload)
        notes = list(match["confidence_reasons"])
        if sample_status != "ready" and "岗位画像样本尚未达到稳定状态" not in notes:
            notes.append("岗位画像样本尚未达到稳定状态")
        scored.append(
            {
                "profile_id": profile_id,
                "family_code": family_code,
                "job_name": str(payload.get("name") or family_code),
                "level": payload.get("level"),
                "period_key": payload.get("period_key"),
                "profile_kind": payload.get("profile_kind"),
                "sample_count": sample_count,
                "evidence_count": evidence_count,
                "sample_status": sample_status,
                "total_score": match["total_score"],
                "match_band": match["match_band"],
                "confidence": match["confidence"],
                "confidence_notes": notes,
                "positive_factors": match["positive_factors"],
                "negative_factors": match["negative_factors"],
                "missing_required_skills": match["missing_required_skills"],
                "match": match,
            }
        )
    scored.sort(
        key=lambda item: (
            -item["total_score"],
            -CONFIDENCE_RANK.get(item["confidence"], 0),
            -item["evidence_count"],
            -item["sample_count"],
            item["job_name"].casefold(),
            item["family_code"],
            str(item["profile_id"]),
        )
    )
    deduplicated = []
    seen_families = set()
    for item in scored:
        if item["family_code"] in seen_families:
            continue
        seen_families.add(item["family_code"])
        deduplicated.append(item)
        if len(deduplicated) >= min(max(int(limit), 1), 10):
            break
    for rank, item in enumerate(deduplicated, start=1):
        item["rank"] = rank
    return deduplicated


def _rank_candidates(
    resume: dict,
    candidates: list[tuple[JobProfile, dict]],
    limit: int,
) -> list[dict]:
    return rank_job_payloads(resume, [payload for _, payload in candidates], limit)


async def _existing_response(
    db: AsyncSession, run: RecommendationRun
) -> dict:
    rows = list(
        (
            await db.execute(
                select(RecommendationResult)
                .where(RecommendationResult.recommendation_run_id == run.id)
                .order_by(RecommendationResult.rank)
            )
        ).scalars()
    )
    return {
        "items": [_json_load(item.result_json, {}) for item in rows],
        "reason": None,
        "result_signature": run.result_signature,
        "recommendation_run_id": run.run_id,
    }


async def recommend_jobs(
    db: AsyncSession,
    *,
    resume_id: int,
    limit: int = 5,
    levels: Collection[str] | None = None,
    family_codes: Collection[str] | None = None,
) -> dict:
    resume_record = await db.get(ResumeRecord, resume_id)
    if resume_record is None:
        raise ValueError("简历不存在")
    filters = {
        "limit": min(max(int(limit), 1), 10),
        "levels": sorted(set(levels or [])),
        "family_codes": sorted(set(family_codes or [])),
    }
    candidates = await load_candidate_profiles(
        db,
        levels=filters["levels"],
        family_codes=filters["family_codes"],
    )
    if not candidates:
        return {
            "items": [],
            "reason": "no_eligible_profiles",
            "result_signature": None,
            "recommendation_run_id": None,
        }

    signature = recommendation_input_signature(resume_record, candidates, filters)
    existing = await db.scalar(
        select(RecommendationRun).where(
            RecommendationRun.input_signature == signature,
            RecommendationRun.status == "completed",
        )
    )
    if existing is not None:
        return await _existing_response(db, existing)

    parsed_resume = _json_load(resume_record.parsed_json, {})
    ranked = _rank_candidates(parsed_resume, candidates, filters["limit"])
    result_signature = _result_signature(ranked)
    audit_filters = {
        **filters,
        "candidate_profile_ids": sorted(profile.id for profile, _ in candidates),
    }
    run = RecommendationRun(
        run_id=str(uuid.uuid4()),
        resume_id=resume_record.id,
        scoring_version=SCORING_VERSION,
        input_signature=signature,
        filters_json=json.dumps(audit_filters, ensure_ascii=False, sort_keys=True),
        status="completed",
        result_signature=result_signature,
        completed_at=datetime.now(),
    )
    db.add(run)
    await db.flush()
    for item in ranked:
        db.add(
            RecommendationResult(
                recommendation_run_id=run.id,
                job_profile_id=item["profile_id"],
                rank=item["rank"],
                total_score=item["total_score"],
                confidence=item["confidence"],
                result_json=json.dumps(item, ensure_ascii=False, sort_keys=True),
            )
        )
    await db.commit()
    return {
        "items": ranked,
        "reason": None,
        "result_signature": result_signature,
        "recommendation_run_id": run.run_id,
    }
