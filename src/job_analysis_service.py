from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from typing import Dict, Iterable, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from model_class.job_competency import (
    EvidenceRecord,
    JobPosting,
    JobPostingSkill,
    JobProfile,
    JobProfileSkill,
    ReviewItem,
    Skill,
)
from model_class.knowledge_base import (
    EvidenceSnippet,
    EvolutionEvent,
    IndustryScenario,
    JobProfileResponsibility,
    JobProfileScenario,
    JobProfileSnapshot,
    Responsibility,
)
from src.competition_rules import SKILL_EVIDENCE_RULE_VERSION, aggregate_skill_evidence
from src.job_data_service import JOB_FAMILY_NAMES


EMERGING_FAMILIES = {
    "AI_AGENT_ENGINEER",
    "LLM_APPLICATION_ENGINEER",
    "RAG_ENGINEER",
    "PROMPT_ENGINEER",
    "MULTIMODAL_ENGINEER",
    "DIGITAL_TWIN_ENGINEER",
}

TECH_STACKS = {
    "JAVA_DEVELOPER": "software",
    "PYTHON_BACKEND": "software",
    "GO_DEVELOPER": "software",
    "FRONTEND_DEVELOPER": "software",
    "DEVOPS_ENGINEER": "cloud_native",
    "SRE_ENGINEER": "cloud_native",
    "CLOUD_NATIVE_ENGINEER": "cloud_native",
    "AI_AGENT_ENGINEER": "artificial_intelligence",
    "LLM_APPLICATION_ENGINEER": "artificial_intelligence",
    "RAG_ENGINEER": "artificial_intelligence",
    "MLOPS_ENGINEER": "artificial_intelligence",
    "MULTIMODAL_ENGINEER": "artificial_intelligence",
    "PROMPT_ENGINEER": "artificial_intelligence",
    "AI_SOLUTION_ENGINEER": "artificial_intelligence",
    "BIG_DATA_DEVELOPER": "big_data",
    "DATA_GOVERNANCE_ENGINEER": "big_data",
    "DATA_ENGINEER": "big_data",
    "IOT_ENGINEER": "iot",
    "EDGE_COMPUTING_ENGINEER": "iot",
    "CYBERSECURITY_ENGINEER": "security",
    "DIGITAL_TWIN_ENGINEER": "intelligent_system",
    "ROBOTICS_ENGINEER": "intelligent_system",
}


def emerging_job_score(
    *,
    current_count: int,
    previous_count: int,
    source_count: int,
    novelty: float,
    persistence: float,
) -> float:
    if current_count <= 0:
        return 0.0
    growth = max(0.0, (current_count - previous_count) / max(previous_count, 1))
    growth_score = min(growth / 2.0, 1.0)
    source_score = min(source_count / 5.0, 1.0)
    volume_score = min(current_count / 30.0, 1.0)
    score = (
        0.35 * growth_score
        + 0.20 * source_score
        + 0.25 * max(0.0, min(novelty, 1.0))
        + 0.10 * max(0.0, min(persistence, 1.0))
        + 0.10 * volume_score
    )
    return round(score, 4)


def aggregate_skill_prevalence(
    rows: Iterable[dict], *, start_year: int, end_year: int
) -> Dict[str, float]:
    selected = [
        row
        for row in rows
        if row.get("published_at")
        and row.get("published_at_trusted") is True
        and start_year <= row["published_at"].year <= end_year
    ]
    if not selected:
        return {}
    counts: Counter[str] = Counter()
    for row in selected:
        counts.update(set(row.get("skills") or []))
    return {
        skill: round(count / len(selected), 4)
        for skill, count in counts.items()
    }


def compare_skill_windows(
    baseline: Dict[str, float], current: Dict[str, float]
) -> dict:
    result = {"added": [], "removed": [], "changed": [], "unchanged": []}
    for skill in sorted(set(baseline) | set(current)):
        old = float(baseline.get(skill, 0.0))
        new = float(current.get(skill, 0.0))
        item = {
            "skill": skill,
            "baseline_prevalence": round(old, 4),
            "current_prevalence": round(new, 4),
            "delta": round(new - old, 4),
        }
        if old < 0.2 and new >= 0.3:
            result["added"].append(item)
        elif old >= 0.3 and new < 0.15:
            result["removed"].append(item)
        elif old >= 0.15 and new >= 0.15 and abs(new - old) >= 0.2:
            result["changed"].append(item)
        else:
            result["unchanged"].append(item)
    return result


async def _posting_rows(db: AsyncSession, family_code: str | None = None) -> List[dict]:
    filters = (
        JobPosting.gate_status == "valid",
        JobPosting.duplicate_of_id.is_(None),
    )
    query = select(
        JobPosting.id,
        JobPosting.job_family_id,
        JobPosting.job_title_normalized,
        JobPosting.published_at,
        JobPosting.published_at_trusted,
        JobPosting.first_seen_at,
        JobPosting.last_seen_at,
        JobPosting.source_name,
        JobPosting.source_type,
        JobPosting.source_domain,
        JobPosting.source_url,
        JobPosting.source_score,
        JobPosting.provenance_status,
        JobPosting.company_name,
        JobPosting.industry,
    ).where(*filters)
    if family_code:
        query = query.where(JobPosting.job_family_id == family_code)
    result = await db.execute(query.order_by(JobPosting.id))
    grouped: Dict[int, dict] = {}
    for posting in result.all():
        grouped[posting.id] = {
            "id": posting.id,
            "family_code": posting.job_family_id,
            "title": posting.job_title_normalized,
            "published_at": posting.published_at,
            "published_at_trusted": posting.published_at_trusted,
            "first_seen_at": posting.first_seen_at,
            "last_seen_at": posting.last_seen_at,
            "source_name": posting.source_name,
            "source_type": posting.source_type,
            "source_domain": posting.source_domain,
            "source_url": posting.source_url,
            "source_score": posting.source_score,
            "provenance_status": posting.provenance_status,
            "company_name": posting.company_name,
            "industry": posting.industry,
            "skills": [],
            "skill_links": [],
            "responsibilities": [],
        }

    skill_query = (
        select(
            JobPosting.id,
            Skill.id,
            Skill.name,
            Skill.category,
            JobPostingSkill.requirement_type,
            JobPostingSkill.confidence,
            JobPostingSkill.evidence_text,
        )
        .join(JobPostingSkill, JobPostingSkill.job_posting_id == JobPosting.id)
        .join(Skill, Skill.id == JobPostingSkill.skill_id)
        .where(*filters)
    )
    responsibility_query = (
        select(JobPosting.id, EvidenceSnippet.entity_key)
        .join(EvidenceSnippet, EvidenceSnippet.job_posting_id == JobPosting.id)
        .where(*filters, EvidenceSnippet.entity_type == "responsibility")
    )
    if family_code:
        skill_query = skill_query.where(JobPosting.job_family_id == family_code)
        responsibility_query = responsibility_query.where(
            JobPosting.job_family_id == family_code
        )
    skill_rows = (
        await db.execute(skill_query.order_by(JobPosting.id, Skill.name, Skill.id))
    ).all()
    for (
        posting_id,
        skill_id,
        name,
        category,
        requirement,
        confidence,
        evidence,
    ) in skill_rows:
        row = grouped[posting_id]
        row["skills"].append(name)
        row["skill_links"].append(
            {
                "id": skill_id,
                "name": name,
                "category": category,
                "requirement_type": requirement,
                "link_confidence": confidence,
                "evidence_text": evidence,
            }
        )
    for posting_id, responsibility in (
        await db.execute(
            responsibility_query.order_by(
                JobPosting.id, EvidenceSnippet.entity_key, EvidenceSnippet.id
            )
        )
    ).all():
        grouped[posting_id]["responsibilities"].append(responsibility)
    return list(grouped.values())


def _responsibilities(rows: List[dict]) -> List[str]:
    candidates: List[str] = []
    for row in rows:
        for line in row["description"].replace("；", "\n").replace("。", "\n").splitlines():
            line = line.strip(" -•\t")
            if 8 <= len(line) <= 120 and any(marker in line for marker in ("负责", "职责", "参与", "建设", "开发")):
                if line not in candidates:
                    candidates.append(line)
            if len(candidates) >= 8:
                return candidates
    return candidates


def _dimension_values(rows: Iterable[dict], key: str) -> set[str]:
    return {
        normalized
        for row in rows
        if row.get("provenance_status") == "approved"
        if (normalized := str(row.get(key) or "").strip().casefold())
        not in {"unknown", "未知"}
    }


def _skill_snapshot_payloads(
    skills: Iterable[dict[str, object]],
) -> list[dict[str, object]]:
    payloads = []
    for skill in skills:
        payload = dict(skill)
        for key in ("first_published_at", "last_published_at"):
            value = payload[key]
            payload[key] = value.isoformat() if isinstance(value, datetime) else None
        payloads.append(payload)
    return payloads


async def rebuild_analysis(
    db: AsyncSession, family_codes: set[str] | None = None
) -> dict:
    rows = await _posting_rows(db)
    if family_codes is not None:
        rows = [row for row in rows if row["family_code"] in family_codes]
    by_family: Dict[str, List[dict]] = defaultdict(list)
    for row in rows:
        by_family[row["family_code"]].append(row)

    profiles_created = 0
    review_items_created = 0
    unchanged_families = []
    for family_code, family_rows in by_family.items():
        dated = [
            row
            for row in family_rows
            if row["published_at"]
            and row["published_at_trusted"]
            and row["provenance_status"] == "approved"
        ]
        latest_year = max((row["published_at"].year for row in dated), default=datetime.now().year)
        current_count = sum(1 for row in dated if row["published_at"].year == latest_year)
        previous_count = sum(1 for row in dated if row["published_at"].year == latest_year - 1)
        source_type_count = len(_dimension_values(family_rows, "source_type"))
        source_domain_count = len(_dimension_values(family_rows, "source_domain"))
        company_count = len(_dimension_values(family_rows, "company_name"))
        source_count = max(source_type_count, source_domain_count)
        years = {row["published_at"].year for row in dated}
        novelty = 0.9 if family_code in EMERGING_FAMILIES else 0.3
        persistence = min(len(years) / 2.0, 1.0)
        score = emerging_job_score(
            current_count=current_count or len(family_rows),
            previous_count=previous_count,
            source_count=source_count,
            novelty=novelty,
            persistence=persistence,
        )
        status = "emerging" if score >= 0.60 and family_code in EMERGING_FAMILIES else "existing"
        average_source = sum(row["source_score"] for row in family_rows) / len(family_rows)
        confidence = round(0.55 * average_source + 0.45 * min(source_count / 5, 1.0), 4)
        previous_profile = await db.scalar(
            select(JobProfile)
            .where(
                JobProfile.family_code == family_code,
                JobProfile.profile_kind != "quarterly",
            )
            .order_by(JobProfile.version.desc())
            .limit(1)
        )
        version = (previous_profile.version if previous_profile else 0) + 1
        industry_counts = Counter(
            row["industry"] for row in family_rows if row["industry"] and row["industry"] != "unknown"
        )
        ordered_industries = sorted(
            industry_counts.items(),
            key=lambda item: (-item[1], item[0].casefold(), item[0]),
        )[:5]
        industries = [name for name, _ in ordered_industries]

        skill_evidence = []
        for row in family_rows:
            for link in row["skill_links"]:
                skill_evidence.append(
                    {
                        "posting_id": row["id"],
                        "skill_id": link["id"],
                        "name": link["name"],
                        "category": link["category"],
                        "requirement_type": link["requirement_type"],
                        "link_confidence": link["link_confidence"],
                        "source_score": row["source_score"],
                        "source_type": row["source_type"],
                        "source_domain": row["source_domain"],
                        "company_name": row["company_name"],
                        "published_at": row["published_at"],
                        "published_at_trusted": row["published_at_trusted"],
                        "provenance_status": row["provenance_status"],
                    }
                )
        skills_payload = aggregate_skill_evidence(
            skill_evidence, total_postings=len(family_rows)
        )

        responsibility_counts = Counter()
        for row in family_rows:
            responsibility_counts.update(set(row["responsibilities"]))
        ordered_responsibilities = sorted(
            responsibility_counts.items(),
            key=lambda item: (-item[1], item[0].casefold(), item[0]),
        )[:12]
        responsibilities_payload = [
            {
                "name": name,
                "evidence_count": count,
                "prevalence": round(count / len(family_rows), 4),
            }
            for name, count in ordered_responsibilities
        ]
        scenarios_payload = [
            {
                "name": name,
                "evidence_count": count,
                "prevalence": round(count / len(family_rows), 4),
            }
            for name, count in ordered_industries
        ]
        snapshot_payload = {
            "family_code": family_code,
            "status": status,
            "tech_stack": TECH_STACKS.get(family_code, "general"),
            "skills": _skill_snapshot_payloads(skills_payload),
            "responsibilities": responsibilities_payload,
            "scenarios": scenarios_payload,
            "posting_count": len(family_rows),
            "source_count": source_count,
            "source_type_count": source_type_count,
            "source_domain_count": source_domain_count,
            "company_count": company_count,
            "skill_evidence_rule_version": SKILL_EVIDENCE_RULE_VERSION,
            "data_cutoff": max((row["published_at"] for row in dated), default=None).isoformat()
            if dated
            else None,
            "temporal_basis": {
                "publication_time_field": "published_at",
                "publication_trust_required": True,
                "quarter_assignment": None,
                "observation_time_field": "first_seen_at",
                "observation_affects_profile": False,
            },
        }
        canonical = json.dumps(
            snapshot_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        signature = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if previous_profile is not None:
            previous_snapshot = await db.scalar(
                select(JobProfileSnapshot).where(
                    JobProfileSnapshot.job_profile_id == previous_profile.id
                )
            )
            if previous_snapshot and previous_snapshot.content_signature == signature:
                unchanged_families.append(family_code)
                continue

        profile = JobProfile(
            family_code=family_code,
            name=JOB_FAMILY_NAMES.get(family_code, family_rows[0]["title"]),
            description=f"基于{len(family_rows)}条有效JD和{source_count}个来源形成的岗位定义。",
            responsibilities_json=json.dumps(
                [item["name"] for item in responsibilities_payload], ensure_ascii=False
            ),
            industry_scenarios_json=json.dumps(industries, ensure_ascii=False),
            status=status,
            tech_stack=TECH_STACKS.get(family_code, "general"),
            version=version,
            confidence=confidence,
            review_status="pending" if status == "emerging" or confidence < 0.7 else "approved",
            valid_from=min((row["published_at"] for row in dated), default=None),
            valid_to=max((row["published_at"] for row in dated), default=None),
        )
        db.add(profile)
        await db.flush()

        for item in skills_payload:
            db.add(
                JobProfileSkill(
                    job_profile_id=profile.id,
                    skill_id=item["id"],
                    requirement_type=item["requirement_type"],
                    proficiency_level="advanced" if item["prevalence"] >= 0.65 else "working",
                    confidence=item["confidence"],
                    evidence_count=item["evidence_count"],
                    prevalence=item["prevalence"],
                    source_type_count=item["source_type_count"],
                    source_domain_count=item["source_domain_count"],
                    company_count=item["company_count"],
                    required_ratio=item["required_ratio"],
                    preferred_ratio=item["preferred_ratio"],
                    ratio_evidence_status=item["ratio_evidence_status"],
                    first_published_at=item["first_published_at"],
                    last_published_at=item["last_published_at"],
                    cross_source_status=item["cross_source_status"],
                )
            )
        first_seen = min(
            (row["first_seen_at"] for row in family_rows if row["first_seen_at"]),
            default=None,
        )
        last_seen = max(
            (
                row["last_seen_at"] or row["first_seen_at"]
                for row in family_rows
                if row["first_seen_at"]
            ),
            default=None,
        )
        responsibility_names = [item["name"] for item in responsibilities_payload]
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
        scenario_names = [item["name"] for item in scenarios_payload]
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
        for item in responsibilities_payload:
            entity = responsibility_entities[item["name"]]
            db.add(
                JobProfileResponsibility(
                    job_profile_id=profile.id,
                    responsibility_id=entity.id,
                    confidence=0.82,
                    evidence_count=item["evidence_count"],
                    prevalence=item["prevalence"],
                    first_seen_at=first_seen,
                    last_seen_at=last_seen,
                    review_status=profile.review_status,
                )
            )
        for item in scenarios_payload:
            entity = scenario_entities[item["name"]]
            db.add(
                JobProfileScenario(
                    job_profile_id=profile.id,
                    scenario_id=entity.id,
                    confidence=0.80,
                    evidence_count=item["evidence_count"],
                    prevalence=item["prevalence"],
                    first_seen_at=first_seen,
                    last_seen_at=last_seen,
                    review_status=profile.review_status,
                )
            )
        db.add(
            JobProfileSnapshot(
                job_profile_id=profile.id,
                content_signature=signature,
                payload_json=canonical,
                posting_count=len(family_rows),
                source_count=source_count,
                data_cutoff=profile.valid_to,
            )
        )

        if previous_profile is not None:
            previous_snapshot = await db.scalar(
                select(JobProfileSnapshot).where(
                    JobProfileSnapshot.job_profile_id == previous_profile.id
                )
            )
            previous_payload = json.loads(previous_snapshot.payload_json) if previous_snapshot else {}
            for entity_type, key in (
                ("skill", "skills"),
                ("responsibility", "responsibilities"),
                ("scenario", "scenarios"),
            ):
                old_items = {item["name"]: item for item in previous_payload.get(key, [])}
                new_items = {item["name"]: item for item in snapshot_payload.get(key, [])}
                for name in sorted(set(old_items) | set(new_items)):
                    if name not in old_items:
                        change_type = "added"
                    elif name not in new_items:
                        change_type = "removed"
                    elif old_items[name] != new_items[name]:
                        change_type = "changed"
                    else:
                        continue
                    db.add(
                        EvolutionEvent(
                            family_code=family_code,
                            previous_profile_id=previous_profile.id,
                            current_profile_id=profile.id,
                            entity_type=entity_type,
                            entity_key=name,
                            change_type=change_type,
                            before_json=(
                                json.dumps(old_items[name], ensure_ascii=False)
                                if name in old_items
                                else None
                            ),
                            after_json=(
                                json.dumps(new_items[name], ensure_ascii=False)
                                if name in new_items
                                else None
                            ),
                            evidence_count=int(new_items.get(name, {}).get("evidence_count", 0)),
                        )
                    )

        if profile.review_status == "pending":
            db.add(
                ReviewItem(
                    entity_type="job_profile",
                    entity_id=profile.id,
                    reason="候选新岗位需要人工确认" if status == "emerging" else "跨来源置信度不足",
                    payload_json=json.dumps(
                        {"family_code": family_code, "score": score, "source_count": source_count},
                        ensure_ascii=False,
                    ),
                )
            )
            review_items_created += 1
        profiles_created += 1

    await db.commit()
    return {
        "profiles_created": profiles_created,
        "review_items_created": review_items_created,
        "families": len(by_family),
        "unchanged_families": sorted(unchanged_families),
    }


async def family_evolution(db: AsyncSession, family_code: str) -> dict:
    rows = await _posting_rows(db, family_code)
    dated = [
        row
        for row in rows
        if row["published_at"]
        and row["published_at_trusted"]
        and row["provenance_status"] == "approved"
    ]
    if not dated:
        return {"family_code": family_code, "baseline": {}, "current": {}, **compare_skill_windows({}, {})}
    years = sorted({row["published_at"].year for row in dated})
    latest = years[-1]
    baseline_end = latest - 1
    baseline_start = years[0]
    baseline = aggregate_skill_prevalence(dated, start_year=baseline_start, end_year=baseline_end)
    current = aggregate_skill_prevalence(dated, start_year=latest, end_year=latest)
    changes = compare_skill_windows(baseline, current)
    return {
        "family_code": family_code,
        "baseline_window": [baseline_start, baseline_end],
        "current_window": [latest, latest],
        "baseline": baseline,
        "current": current,
        **changes,
        "sources": sorted({row["source_url"] for row in rows})[:50],
    }


async def graph_data(
    db: AsyncSession,
    *,
    tech_stack: str | None = None,
    level: str | None = None,
    family_code: str | None = None,
    version: int | None = None,
    scope: str = "draft",
    include_evidence: bool = False,
) -> dict:
    profile_query = select(JobProfile).order_by(JobProfile.family_code, JobProfile.version.desc())
    if tech_stack:
        profile_query = profile_query.where(JobProfile.tech_stack == tech_stack)
    if level:
        profile_query = profile_query.where(JobProfile.level.in_([level, "all"]))
    if family_code:
        profile_query = profile_query.where(JobProfile.family_code == family_code)
    if version is not None:
        profile_query = profile_query.where(JobProfile.version == version)
    if scope == "published":
        profile_query = profile_query.where(JobProfile.review_status == "approved")
    profiles = list((await db.execute(profile_query)).scalars().all())
    latest = {}
    for profile in profiles:
        key = (profile.family_code, profile.version if version is not None else None)
        latest.setdefault(key, profile)

    nodes = []
    edges = []
    node_ids = set()
    skill_ids = set()
    for profile in latest.values():
        family_id = f"family:{profile.family_code}"
        if family_id not in node_ids:
            nodes.append(
                {
                    "id": family_id,
                    "label": JOB_FAMILY_NAMES.get(profile.family_code, profile.family_code),
                    "type": "family",
                    "family_code": profile.family_code,
                }
            )
            node_ids.add(family_id)
        nodes.append(
            {
                "id": f"job:{profile.id}",
                "label": profile.name,
                "type": "job",
                "status": profile.status,
                "confidence": profile.confidence,
                "tech_stack": profile.tech_stack,
                "version": profile.version,
                "review_status": profile.review_status,
            }
        )
        node_ids.add(f"job:{profile.id}")
        edges.append(
            {
                "source": family_id,
                "target": f"job:{profile.id}",
                "type": "has_version",
                "version": profile.version,
            }
        )
        links = await db.execute(
            select(JobProfileSkill, Skill)
            .join(Skill, Skill.id == JobProfileSkill.skill_id)
            .where(JobProfileSkill.job_profile_id == profile.id)
        )
        for link, skill in links.all():
            if skill.id not in skill_ids:
                nodes.append(
                    {"id": f"skill:{skill.id}", "label": skill.name, "type": "skill", "category": skill.category}
                )
                skill_ids.add(skill.id)
                node_ids.add(f"skill:{skill.id}")
            edges.append(
                {
                    "source": f"job:{profile.id}",
                    "target": f"skill:{skill.id}",
                    "type": link.requirement_type,
                    "prevalence": link.prevalence,
                    "evidence_count": link.evidence_count,
                }
            )
        responsibility_links = await db.execute(
            select(JobProfileResponsibility, Responsibility)
            .join(
                Responsibility,
                Responsibility.id == JobProfileResponsibility.responsibility_id,
            )
            .where(JobProfileResponsibility.job_profile_id == profile.id)
        )
        for link, responsibility in responsibility_links.all():
            node_id = f"responsibility:{responsibility.id}"
            if node_id not in node_ids:
                nodes.append(
                    {
                        "id": node_id,
                        "label": responsibility.name,
                        "type": "responsibility",
                    }
                )
                node_ids.add(node_id)
            edges.append(
                {
                    "source": f"job:{profile.id}",
                    "target": node_id,
                    "type": "has_responsibility",
                    "prevalence": link.prevalence,
                    "evidence_count": link.evidence_count,
                }
            )
        scenario_links = await db.execute(
            select(JobProfileScenario, IndustryScenario)
            .join(IndustryScenario, IndustryScenario.id == JobProfileScenario.scenario_id)
            .where(JobProfileScenario.job_profile_id == profile.id)
        )
        for link, scenario in scenario_links.all():
            node_id = f"scenario:{scenario.id}"
            if node_id not in node_ids:
                nodes.append({"id": node_id, "label": scenario.name, "type": "scenario"})
                node_ids.add(node_id)
            edges.append(
                {
                    "source": f"job:{profile.id}",
                    "target": node_id,
                    "type": "applies_to",
                    "prevalence": link.prevalence,
                    "evidence_count": link.evidence_count,
                }
            )
        if include_evidence:
            snippets = await db.execute(
                select(EvidenceSnippet, JobPosting)
                .join(JobPosting, JobPosting.id == EvidenceSnippet.job_posting_id)
                .where(
                    JobPosting.job_family_id == profile.family_code,
                    JobPosting.status.in_(["valid", "review"]),
                    JobPosting.duplicate_of_id.is_(None),
                )
                .order_by(EvidenceSnippet.confidence.desc(), EvidenceSnippet.id)
                .limit(80)
            )
            skill_nodes_by_name = {
                node["label"]: node["id"] for node in nodes if node["type"] == "skill"
            }
            responsibility_nodes_by_name = {
                node["label"]: node["id"]
                for node in nodes
                if node["type"] == "responsibility"
            }
            for snippet, posting in snippets.all():
                evidence_id = f"evidence:{snippet.id}"
                if evidence_id not in node_ids:
                    nodes.append(
                        {
                            "id": evidence_id,
                            "label": snippet.evidence_text[:60],
                            "type": "evidence",
                            "record_id": posting.record_id,
                            "source_url": posting.source_url,
                            "evidence_text": snippet.evidence_text,
                            "confidence": snippet.confidence,
                            "review_status": snippet.review_status,
                        }
                    )
                    node_ids.add(evidence_id)
                target_id = (
                    skill_nodes_by_name.get(snippet.entity_key)
                    if snippet.entity_type == "skill"
                    else responsibility_nodes_by_name.get(snippet.entity_key)
                )
                if target_id:
                    edges.append(
                        {"source": target_id, "target": evidence_id, "type": "supported_by"}
                    )

    if include_evidence:
        evidence_query = select(EvidenceRecord).order_by(
            EvidenceRecord.source_score.desc(),
            EvidenceRecord.evidence_id,
        )
        if family_code:
            evidence_query = evidence_query.where(
                EvidenceRecord.job_family_id == family_code
            )
        elif tech_stack or level or version is not None or scope == "published":
            visible_families = {
                str(node["family_code"])
                for node in nodes
                if node["type"] == "family"
            }
            evidence_query = evidence_query.where(
                EvidenceRecord.job_family_id.in_(visible_families)
            )

        external_evidence = await db.execute(evidence_query)
        for evidence in external_evidence.scalars().all():
            family_id = f"family:{evidence.job_family_id}"
            if family_id not in node_ids:
                nodes.append(
                    {
                        "id": family_id,
                        "label": JOB_FAMILY_NAMES.get(
                            evidence.job_family_id, evidence.job_family_id
                        ),
                        "type": "family",
                        "family_code": evidence.job_family_id,
                    }
                )
                node_ids.add(family_id)

            evidence_id = f"evidence:external:{evidence.evidence_id}"
            if evidence_id not in node_ids:
                nodes.append(
                    {
                        "id": evidence_id,
                        "label": evidence.title,
                        "type": "evidence",
                        "evidence_kind": "external_standard",
                        "evidence_id": evidence.evidence_id,
                        "family_code": evidence.job_family_id,
                        "evidence_type": evidence.evidence_type,
                        "publisher": evidence.publisher,
                        "published_at": (
                            evidence.published_at.isoformat()
                            if evidence.published_at
                            else None
                        ),
                        "source_url": evidence.source_url,
                        "related_skill": evidence.related_skill,
                        "evidence_summary": evidence.evidence_summary,
                        "source_score": evidence.source_score,
                    }
                )
                node_ids.add(evidence_id)
            edges.append(
                {
                    "source": family_id,
                    "target": evidence_id,
                    "type": "supported_by",
                }
            )
    return {"nodes": nodes, "edges": edges}
