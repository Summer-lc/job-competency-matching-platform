from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Collection

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from model_class.job_competency import (
    JobPosting,
    JobPostingSkill,
    JobProfile,
    JobProfileSkill,
    Skill,
)
from model_class.knowledge_base import EvolutionEvent, EvolutionEvidence
from src.competition_rules import (
    are_adjacent_quarters,
    classify_skill_change,
    quarter_key,
)


EVOLUTION_RULE_VERSION = "competition-evolution-v1"
FORMAL_MIN_SAMPLE_COUNT = 20


async def _profile_skills(
    db: AsyncSession, profile_id: int
) -> dict[int, dict[str, object]]:
    rows = (
        await db.execute(
            select(JobProfileSkill, Skill)
            .join(Skill, Skill.id == JobProfileSkill.skill_id)
            .where(JobProfileSkill.job_profile_id == profile_id)
        )
    ).all()
    return {
        skill.id: {
            "skill_id": skill.id,
            "name": skill.name,
            "requirement_type": link.requirement_type,
            "prevalence": float(link.prevalence),
            "evidence_count": int(link.evidence_count),
        }
        for link, skill in rows
    }


def _effective_level(posting: JobPosting) -> str:
    return posting.manual_level or posting.machine_level or "unspecified"


async def _posting_evidence(
    db: AsyncSession,
    profile: JobProfile,
    skill_id: int,
) -> list[tuple[JobPosting, JobPostingSkill]]:
    rows = (
        await db.execute(
            select(JobPosting, JobPostingSkill)
            .join(
                JobPostingSkill,
                JobPostingSkill.job_posting_id == JobPosting.id,
            )
            .where(
                JobPosting.job_family_id == profile.family_code,
                JobPosting.gate_status == "valid",
                JobPosting.duplicate_of_id.is_(None),
                JobPosting.published_at.is_not(None),
                JobPosting.published_at_trusted.is_(True),
                JobPosting.provenance_status == "approved",
                JobPostingSkill.skill_id == skill_id,
            )
            .order_by(JobPosting.published_at, JobPosting.id)
        )
    ).all()
    return [
        (posting, link)
        for posting, link in rows
        if profile.period_key
        and quarter_key(posting.published_at) == profile.period_key
        and _effective_level(posting) == profile.level
    ]


def _event_key(
    previous_profile_id: int,
    current_profile_id: int,
    skill_id: int,
    change_type: str,
) -> str:
    raw = "|".join(
        (
            EVOLUTION_RULE_VERSION,
            str(previous_profile_id),
            str(current_profile_id),
            "skill",
            str(skill_id),
            change_type,
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _formal_profile_eligible(profile: JobProfile | None) -> bool:
    return bool(
        profile is not None
        and profile.profile_kind == "quarterly"
        and profile.derivation_status == "active"
        and profile.sample_status == "ready"
        and profile.sample_count >= FORMAL_MIN_SAMPLE_COUNT
        and profile.period_key
    )


async def reconcile_formal_evolution_events(db: AsyncSession) -> int:
    previous_profile = aliased(JobProfile)
    current_profile = aliased(JobProfile)
    rows = (
        await db.execute(
            select(EvolutionEvent, previous_profile, current_profile)
            .outerjoin(
                previous_profile,
                previous_profile.id == EvolutionEvent.previous_profile_id,
            )
            .outerjoin(
                current_profile,
                current_profile.id == EvolutionEvent.current_profile_id,
            )
            .where(EvolutionEvent.event_status == "formal")
        )
    ).all()
    superseded = 0
    for event, previous, current in rows:
        if _formal_profile_eligible(previous) and _formal_profile_eligible(current):
            continue
        event.event_status = "superseded"
        superseded += 1
    if superseded:
        await db.flush()
    return superseded


async def rebuild_evolution(
    db: AsyncSession,
    previous_profile_id: int,
    current_profile_id: int,
    *,
    pipeline_run_id: int,
) -> list[EvolutionEvent]:
    previous = await db.get(JobProfile, previous_profile_id)
    current = await db.get(JobProfile, current_profile_id)
    if previous is None or current is None:
        return []
    if (
        previous.family_code != current.family_code
        or previous.tech_stack != current.tech_stack
        or previous.level != current.level
        or not _formal_profile_eligible(previous)
        or not _formal_profile_eligible(current)
        or not are_adjacent_quarters(previous.period_key, current.period_key)
    ):
        return []

    before_skills = await _profile_skills(db, previous.id)
    after_skills = await _profile_skills(db, current.id)
    events: list[EvolutionEvent] = []
    active_keys: set[str] = set()
    for skill_id in sorted(set(before_skills) | set(after_skills)):
        before = before_skills.get(skill_id)
        after = after_skills.get(skill_id)
        decision = classify_skill_change(
            float(before["prevalence"]) if before else 0.0,
            float(after["prevalence"]) if after else 0.0,
            before_requirement=str(before["requirement_type"]) if before else None,
            after_requirement=str(after["requirement_type"]) if after else None,
            before_evidence=int(before["evidence_count"]) if before else 0,
            after_evidence=int(after["evidence_count"]) if after else 0,
        )
        if decision.change_type is None:
            continue
        before_evidence = await _posting_evidence(db, previous, skill_id)
        after_evidence = await _posting_evidence(db, current, skill_id)
        changed_side_count = (
            len(before_evidence)
            if decision.change_type == "removed"
            else len(after_evidence)
        )
        if changed_side_count < 3:
            continue
        generation_key = _event_key(
            previous.id, current.id, skill_id, decision.change_type
        )
        active_keys.add(generation_key)
        event = await db.scalar(
            select(EvolutionEvent).where(
                EvolutionEvent.generation_key == generation_key
            )
        )
        name = str((after or before)["name"])
        if event is None:
            event = EvolutionEvent(
                family_code=current.family_code,
                previous_profile_id=previous.id,
                current_profile_id=current.id,
                entity_type="skill",
                entity_key=name,
                change_type=decision.change_type,
                generation_key=generation_key,
            )
            db.add(event)
            await db.flush()
        event.before_json = json.dumps(before, ensure_ascii=False, sort_keys=True) if before else None
        event.after_json = json.dumps(after, ensure_ascii=False, sort_keys=True) if after else None
        event.evidence_count = changed_side_count
        event.previous_period = previous.period_key
        event.current_period = current.period_key
        event.before_rate = float(before["prevalence"]) if before else 0.0
        event.after_rate = float(after["prevalence"]) if after else 0.0
        event.change_delta = decision.delta
        event.event_status = "formal"
        event.pipeline_run_id = pipeline_run_id
        await db.execute(
            delete(EvolutionEvidence).where(
                EvolutionEvidence.evolution_event_id == event.id
            )
        )
        evidence_groups = []
        if before_evidence:
            evidence_groups.append(("before", before_evidence))
        if after_evidence:
            evidence_groups.append(("after", after_evidence))
        for period_role, rows in evidence_groups:
            for posting, link in rows:
                db.add(
                    EvolutionEvidence(
                        evolution_event_id=event.id,
                        job_posting_id=posting.id,
                        period_role=period_role,
                        evidence_text=link.evidence_text,
                    )
                )
        events.append(event)

    previous_events = list(
        (
            await db.execute(
                select(EvolutionEvent).where(
                    EvolutionEvent.previous_profile_id == previous.id,
                    EvolutionEvent.current_profile_id == current.id,
                    EvolutionEvent.event_status == "formal",
                )
            )
        ).scalars()
    )
    for old_event in previous_events:
        if old_event.generation_key not in active_keys:
            old_event.event_status = "superseded"
    await db.flush()
    return sorted(events, key=lambda item: (item.entity_key, item.change_type))


async def rebuild_all_adjacent_evolution(
    db: AsyncSession,
    *,
    pipeline_run_id: int,
    family_codes: Collection[str] | None = None,
) -> dict[str, int]:
    superseded = await reconcile_formal_evolution_events(db)
    query = select(JobProfile).where(
        JobProfile.profile_kind == "quarterly",
        JobProfile.derivation_status == "active",
        JobProfile.sample_status == "ready",
        JobProfile.sample_count >= FORMAL_MIN_SAMPLE_COUNT,
    )
    if family_codes is not None:
        query = query.where(JobProfile.family_code.in_(family_codes))
    profiles = list((await db.execute(query)).scalars())
    groups: dict[tuple[str, str, str], list[JobProfile]] = defaultdict(list)
    for profile in profiles:
        groups[(profile.family_code, profile.tech_stack, profile.level)].append(profile)
    compared = 0
    event_count = 0
    for group in groups.values():
        ordered = sorted(group, key=lambda item: item.period_key or "")
        for previous, current in zip(ordered, ordered[1:]):
            if not are_adjacent_quarters(previous.period_key, current.period_key):
                continue
            compared += 1
            event_count += len(
                await rebuild_evolution(
                    db,
                    previous.id,
                    current.id,
                    pipeline_run_id=pipeline_run_id,
                )
            )
    return {
        "pairs_compared": compared,
        "formal_events": event_count,
        "events_superseded": superseded,
    }


async def family_evolution_payload(
    db: AsyncSession,
    family_code: str,
    *,
    level: str | None = None,
    current_period: str | None = None,
) -> dict[str, object]:
    rows = list(
        (
            await db.execute(
                select(EvolutionEvent, JobProfile)
                .join(JobProfile, JobProfile.id == EvolutionEvent.current_profile_id)
                .where(
                    EvolutionEvent.family_code == family_code,
                    EvolutionEvent.event_status == "formal",
                )
                .order_by(
                    EvolutionEvent.current_period.desc(),
                    EvolutionEvent.entity_key,
                )
            )
        ).all()
    )
    if level:
        rows = [(event, profile) for event, profile in rows if profile.level == level]
    selected_period = current_period or next(
        (event.current_period for event, _ in rows if event.current_period), None
    )
    if selected_period:
        rows = [
            (event, profile)
            for event, profile in rows
            if event.current_period == selected_period
        ]
    previous_period = next(
        (event.previous_period for event, _ in rows if event.previous_period), None
    )
    grouped: dict[str, list[dict[str, object]]] = {
        "added": [],
        "removed": [],
        "changed": [],
    }
    evidence_payload: list[dict[str, object]] = []
    sources: set[str] = set()
    for event, profile in rows:
        item = {
            "event_id": event.id,
            "skill": event.entity_key,
            "baseline_prevalence": float(event.before_rate or 0.0),
            "current_prevalence": float(event.after_rate or 0.0),
            "delta": float(event.change_delta or 0.0),
            "evidence_count": event.evidence_count,
            "previous_period": event.previous_period,
            "current_period": event.current_period,
            "level": profile.level,
            "sample_status": profile.sample_status,
        }
        key = "changed" if event.change_type == "modified" else event.change_type
        grouped[key].append(item)
        evidence_rows = (
            await db.execute(
                select(EvolutionEvidence, JobPosting)
                .join(JobPosting, JobPosting.id == EvolutionEvidence.job_posting_id)
                .where(EvolutionEvidence.evolution_event_id == event.id)
                .order_by(EvolutionEvidence.period_role, JobPosting.published_at, JobPosting.id)
            )
        ).all()
        for link, posting in evidence_rows:
            sources.add(posting.source_url)
            evidence_payload.append(
                {
                    "event_id": event.id,
                    "skill": event.entity_key,
                    "period_role": link.period_role,
                    "record_id": posting.record_id,
                    "job_title": posting.job_title_normalized,
                    "company_name": posting.company_name,
                    "published_at": posting.published_at.isoformat()
                    if posting.published_at
                    else None,
                    "source_url": posting.source_url,
                    "evidence_text": link.evidence_text,
                }
            )
    return {
        "family_code": family_code,
        "level": level,
        "previous_period": previous_period,
        "current_period": selected_period,
        **grouped,
        "evidence": evidence_payload,
        "sources": sorted(sources),
    }
