from __future__ import annotations

import json
import hashlib
from collections import defaultdict
from datetime import datetime
from typing import Collection
from uuid import uuid4

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from model_class.job_competency import (
    JobPosting,
    JobPostingSkill,
    JobProfile,
    JobProfileSkill,
)
from model_class.knowledge_base import EvidenceSnippet, EvolutionEvent, PipelineRun
from src.competition_rules import (
    GATE_RULE_VERSION,
    assess_gate,
    classify_seniority,
)
from src.job_data_service import hamming_distance


def _posting_completeness(posting: JobPosting) -> int:
    return sum(
        bool(getattr(posting, field))
        for field in (
            "company_name",
            "industry",
            "region",
            "published_at",
            "experience_requirement",
            "education_requirement",
            "salary_range",
            "source_url",
        )
    )


def _master_rank(posting: JobPosting) -> tuple[float, int, float, int]:
    published = posting.published_at.timestamp() if posting.published_at else 0.0
    return (
        float(posting.source_score or 0.0),
        _posting_completeness(posting),
        published,
        -posting.id,
    )


async def rebuild_duplicate_groups(
    db: AsyncSession, *, family_codes: Collection[str] | None = None
) -> dict[str, int]:
    query = select(JobPosting).order_by(JobPosting.job_family_id, JobPosting.id)
    if family_codes is not None:
        query = query.where(JobPosting.job_family_id.in_(family_codes))
    postings = list((await db.execute(query)).scalars())
    families: dict[str, list[JobPosting]] = defaultdict(list)
    for posting in postings:
        posting.duplicate_of_id = None
        families[posting.job_family_id].append(posting)

    groups = 0
    duplicates = 0
    for members in families.values():
        parent = list(range(len(members)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(first: int, second: int) -> None:
            first_root = find(first)
            second_root = find(second)
            if first_root != second_root:
                parent[second_root] = first_root

        for first_index, first in enumerate(members):
            for second_index in range(first_index + 1, len(members)):
                second = members[second_index]
                same_hash = bool(first.content_hash) and first.content_hash == second.content_hash
                near = False
                try:
                    near = hamming_distance(first.simhash, second.simhash) <= 8
                except (TypeError, ValueError):
                    near = False
                if same_hash or near:
                    union(first_index, second_index)

        clusters: dict[int, list[JobPosting]] = defaultdict(list)
        for index, posting in enumerate(members):
            clusters[find(index)].append(posting)
        for cluster in clusters.values():
            if len(cluster) < 2:
                continue
            groups += 1
            master = max(cluster, key=_master_rank)
            for posting in cluster:
                if posting.id == master.id:
                    posting.duplicate_of_id = None
                    continue
                posting.duplicate_of_id = master.id
                duplicates += 1
    await db.flush()
    return {"groups": groups, "duplicates": duplicates}


async def _capability_posting_ids(db: AsyncSession) -> set[int]:
    skill_ids = set(
        (
            await db.execute(select(distinct(JobPostingSkill.job_posting_id)))
        ).scalars()
    )
    snippet_ids = set(
        (
            await db.execute(select(distinct(EvidenceSnippet.job_posting_id)))
        ).scalars()
    )
    return skill_ids | snippet_ids


def posting_gate_payload(
    posting: JobPosting, *, has_capability_evidence: bool
) -> dict[str, object]:
    return {
        "record_id": posting.record_id,
        "job_family_id": posting.job_family_id,
        "job_title_raw": posting.job_title_raw,
        "source_name": posting.source_name,
        "source_type": posting.source_type,
        "source_url": posting.source_url,
        "provenance_status": posting.provenance_status,
        "published_at": posting.published_at,
        "collected_at": posting.collected_at,
        "job_description_raw": posting.job_description_raw,
        "quality_score": posting.quality_score,
        "duplicate_of_id": posting.duplicate_of_id,
        "has_capability_evidence": has_capability_evidence,
    }


async def reclassify_postings(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    posting_ids: Collection[int] | None = None,
    family_codes: Collection[str] | None = None,
) -> dict[str, int]:
    effective_now = now or datetime.now()
    query = select(JobPosting).order_by(JobPosting.id)
    if posting_ids is not None:
        query = query.where(JobPosting.id.in_(posting_ids))
    if family_codes is not None:
        query = query.where(JobPosting.job_family_id.in_(family_codes))
    postings = list((await db.execute(query)).scalars())
    evidence_ids = await _capability_posting_ids(db)
    counts = {
        "processed": 0,
        "valid": 0,
        "review": 0,
        "quarantined": 0,
        "duplicate": 0,
    }
    for posting in postings:
        gate = assess_gate(
            posting_gate_payload(
                posting, has_capability_evidence=posting.id in evidence_ids
            ),
            now=effective_now,
        )
        level = classify_seniority(
            posting.job_title_raw,
            posting.experience_requirement,
            posting.job_description_raw,
        )
        posting.gate_status = gate.status
        posting.gate_issue_codes_json = json.dumps(
            list(gate.issue_codes), ensure_ascii=False, sort_keys=True
        )
        posting.gate_rule_version = GATE_RULE_VERSION
        posting.gated_at = effective_now
        posting.machine_level = level.level
        posting.machine_level_confidence = level.confidence
        posting.machine_level_evidence_json = json.dumps(
            level.evidence, ensure_ascii=False, sort_keys=True
        )
        counts["processed"] += 1
        counts[gate.status] += 1
    await db.flush()
    return counts


async def persist_manual_level_review(
    db: AsyncSession,
    posting_id: int,
    *,
    level: str,
    reviewer: str,
    note: str,
    reviewed_at: datetime | None = None,
) -> dict[str, object]:
    posting = await db.get(JobPosting, posting_id)
    if posting is None:
        raise LookupError("岗位JD不存在")
    review = {
        "reviewer": reviewer,
        "note": note,
        "reviewed_at": (reviewed_at or datetime.now()).isoformat(),
        "machine_level": posting.machine_level,
    }
    posting.manual_level = level
    posting.manual_level_review_json = json.dumps(
        review, ensure_ascii=False, sort_keys=True
    )
    await db.flush()
    return {
        "posting_id": posting.id,
        "machine_level": posting.machine_level,
        "manual_level": posting.manual_level,
        "effective_level": posting.manual_level or posting.machine_level,
        "review": review,
    }


async def quality_distribution(db: AsyncSession) -> dict[str, object]:
    total = int(await db.scalar(select(func.count()).select_from(JobPosting)) or 0)
    status_rows = (
        await db.execute(
            select(JobPosting.gate_status, func.count())
            .group_by(JobPosting.gate_status)
            .order_by(JobPosting.gate_status)
        )
    ).all()
    level_rows = (
        await db.execute(
            select(JobPosting.machine_level, func.count())
            .group_by(JobPosting.machine_level)
            .order_by(JobPosting.machine_level)
        )
    ).all()
    return {
        "total": total,
        "gate_status": {str(name): int(count) for name, count in status_rows},
        "machine_levels": {str(name): int(count) for name, count in level_rows},
    }


async def pipeline_run_history(
    db: AsyncSession, *, limit: int = 20
) -> dict[str, object]:
    rows = list(
        (
            await db.execute(
                select(PipelineRun)
                .order_by(PipelineRun.started_at.desc(), PipelineRun.id.desc())
                .limit(limit)
            )
        ).scalars()
    )
    return {
        "items": [
            {
                "id": row.id,
                "run_id": row.run_id,
                "mode": row.mode,
                "status": row.status,
                "input_count": row.input_count,
                "result": json.loads(row.result_json or "{}"),
                "error_summary": row.error_summary,
                "started_at": row.started_at.isoformat(),
                "completed_at": row.completed_at.isoformat()
                if row.completed_at
                else None,
            }
            for row in rows
        ],
        "total": len(rows),
    }


async def _create_pipeline_run(
    db: AsyncSession,
    *,
    mode: str,
    family_codes: Collection[str] | None,
) -> PipelineRun:
    query = select(func.count()).select_from(JobPosting)
    if family_codes is not None:
        query = query.where(JobPosting.job_family_id.in_(family_codes))
    run = PipelineRun(
        run_id=str(uuid4()),
        mode=mode,
        rule_version="competition-hard-metrics-v1",
        family_codes_json=json.dumps(sorted(family_codes or []), ensure_ascii=False),
        status="running",
        input_count=int(await db.scalar(query) or 0),
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def _state_signature(
    db: AsyncSession, acceptance: dict[str, object]
) -> str:
    quality = await quality_distribution(db)
    profiles = list(
        (
            await db.execute(
                select(JobProfile).where(
                    JobProfile.profile_kind == "quarterly",
                    JobProfile.derivation_status == "active",
                )
            )
        ).scalars()
    )
    events = list(
        (
            await db.execute(
                select(EvolutionEvent).where(
                    EvolutionEvent.event_status == "formal"
                )
            )
        ).scalars()
    )
    profile_skills = list(
        (
            await db.execute(
                select(JobProfileSkill)
                .join(JobProfile, JobProfile.id == JobProfileSkill.job_profile_id)
                .where(
                    JobProfile.profile_kind == "quarterly",
                    JobProfile.derivation_status == "active",
                )
            )
        ).scalars()
    )
    state = {
        "quality": quality,
        "profiles": sorted(
            (item.generation_key, item.input_signature, item.sample_status)
            for item in profiles
        ),
        "events": sorted(
            (
                item.generation_key,
                item.before_rate,
                item.after_rate,
                item.evidence_count,
            )
            for item in events
        ),
        "profile_skills": sorted(
            (
                item.job_profile_id,
                item.skill_id,
                item.requirement_type,
                item.confidence,
                item.evidence_count,
                item.prevalence,
                item.source_type_count,
                item.source_domain_count,
                item.company_count,
                item.required_ratio,
                item.preferred_ratio,
                item.ratio_evidence_status,
                item.first_published_at.isoformat()
                if item.first_published_at
                else None,
                item.last_published_at.isoformat() if item.last_published_at else None,
                item.cross_source_status,
            )
            for item in profile_skills
        ),
        "acceptance": acceptance,
    }
    canonical = json.dumps(
        state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _complete_pipeline_run(
    db: AsyncSession,
    run_id: int,
    result: dict[str, object],
    result_signature: str,
) -> None:
    run = await db.get(PipelineRun, run_id)
    if run is None:
        raise RuntimeError("流水线运行记录不存在")
    run.status = "completed"
    run.result_json = json.dumps(result, ensure_ascii=False, sort_keys=True)
    run.result_signature = result_signature
    run.completed_at = datetime.now()
    await db.commit()


async def _fail_pipeline_run(
    db: AsyncSession, run_id: int, error: Exception
) -> None:
    await db.rollback()
    run = await db.get(PipelineRun, run_id)
    if run is None:
        return
    run.status = "failed"
    run.error_summary = f"{type(error).__name__}: {error}"
    run.completed_at = datetime.now()
    await db.commit()


async def run_hard_metrics_pipeline(
    db: AsyncSession,
    *,
    mode: str,
    now: datetime | None = None,
    family_codes: Collection[str] | None = None,
) -> dict[str, object]:
    if mode not in {"full", "incremental"}:
        raise ValueError("mode必须是full或incremental")
    if family_codes is None:
        family_codes = set(
            (
                await db.execute(select(distinct(JobPosting.job_family_id)))
            ).scalars()
        )
    else:
        family_codes = set(family_codes)
    run = await _create_pipeline_run(
        db, mode=mode, family_codes=family_codes
    )
    try:
        from src.acceptance_service import acceptance_summary
        from src.evolution_service import rebuild_all_adjacent_evolution
        from src.knowledge_service import update_knowledge_chunks
        from src.quarterly_profile_service import rebuild_quarterly_profiles

        duplicate_summary = await rebuild_duplicate_groups(
            db, family_codes=family_codes
        )
        gate_summary = await reclassify_postings(
            db, now=now, family_codes=family_codes
        )
        profile_summary = await rebuild_quarterly_profiles(
            db, pipeline_run_id=run.id, family_codes=family_codes
        )
        evolution_summary = await rebuild_all_adjacent_evolution(
            db, pipeline_run_id=run.id, family_codes=family_codes
        )
        knowledge_summary = await update_knowledge_chunks(db, set(family_codes))
        acceptance = await acceptance_summary(db, persist=True)
        signature = await _state_signature(db, acceptance)
        result: dict[str, object] = {
            "status": "completed",
            "run_id": run.run_id,
            "duplicate_summary": duplicate_summary,
            "gate_summary": gate_summary,
            "profile_summary": profile_summary,
            "evolution_summary": evolution_summary,
            "knowledge_summary": knowledge_summary,
            "acceptance": acceptance,
            "result_signature": signature,
        }
        await _complete_pipeline_run(db, run.id, result, signature)
        return result
    except Exception as exc:
        await _fail_pipeline_run(db, run.id, exc)
        raise
