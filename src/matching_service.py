from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date

from src.learning_path_service import build_learning_path
from src.skill_ontology import PROFICIENCY_RANK, normalize_skill, skill_relationship


SCORING_VERSION = "evidence-match-v2"
MATCHING_WEIGHTS = {
    "required_skill_coverage": 30,
    "skill_proficiency": 15,
    "experience_level": 15,
    "project_evidence": 15,
    "skill_recency": 10,
    "preferred_skill_coverage": 5,
    "responsibility_scenario": 10,
}
WEIGHTS = MATCHING_WEIGHTS
LEVEL_REQUIRED_YEARS = {
    "junior": 0.0,
    "mid": 3.0,
    "senior": 5.0,
    "expert": 8.0,
    "unspecified": 0.0,
    "all": 0.0,
}
RELATED_SKILL_CREDIT = 0.4
CORE_PREVALENCE = 0.65


def _skill_name(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("name", "")).strip()
    return str(value or "").strip()


def _canonical_name(value: object) -> str:
    return str(normalize_skill(_skill_name(value)).get("name") or "")


def _skill_records(values: Iterable | None) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for value in values or []:
        raw = value if isinstance(value, dict) else {"name": value}
        name = _canonical_name(raw)
        if not name:
            continue
        current = result.get(name)
        item = dict(raw)
        item["name"] = name
        if current is None:
            result[name] = item
            continue
        current_sources = set(current.get("evidence_sources") or [])
        current_sources.update(item.get("evidence_sources") or [])
        current["evidence_sources"] = sorted(current_sources)
        current_rank = PROFICIENCY_RANK.get(str(current.get("proficiency", "aware")), 1)
        item_rank = PROFICIENCY_RANK.get(str(item.get("proficiency", "aware")), 1)
        if item_rank > current_rank:
            current["proficiency"] = item.get("proficiency")
    return result


def _target_skills(job_profile: dict) -> tuple[dict[str, dict], dict[str, dict]]:
    required = _skill_records(job_profile.get("required_skills", []))
    preferred = _skill_records(job_profile.get("preferred_skills", []))
    for raw in job_profile.get("skills", []) or []:
        if not isinstance(raw, dict):
            continue
        name = _canonical_name(raw)
        if not name:
            continue
        target = required if raw.get("requirement_type", "required") == "required" else preferred
        target.setdefault(name, {"name": name}).update(raw)
        target[name]["name"] = name
    return required, preferred


def _related_candidate(target: str, resume_skills: dict[str, dict]) -> str | None:
    candidates = [
        name
        for name in resume_skills
        if skill_relationship(name, target) == "related"
    ]
    return sorted(candidates, key=str.casefold)[0] if candidates else None


def _dimension(
    ratio: float,
    key: str,
    *,
    evidence: list[str] | None = None,
    gaps: list[str] | None = None,
    explanation: str,
) -> dict:
    bounded = min(max(float(ratio), 0.0), 1.0)
    return {
        "score": round(bounded * MATCHING_WEIGHTS[key], 2),
        "max_score": MATCHING_WEIGHTS[key],
        "ratio": round(bounded, 4),
        "evidence": evidence or [],
        "gaps": gaps or [],
        "explanation": explanation,
    }


def _coverage_dimension(
    targets: dict[str, dict],
    resume_skills: dict[str, dict],
    key: str,
) -> tuple[dict, list[str], list[str], list[dict]]:
    if not targets:
        return (
            _dimension(1.0, key, explanation="目标岗位未设置该类技能要求。"),
            [],
            [],
            [],
        )
    points = 0.0
    matched: list[str] = []
    missing: list[str] = []
    transferable: list[dict] = []
    evidence: list[str] = []
    for target in sorted(targets, key=str.casefold):
        if target in resume_skills:
            points += 1.0
            matched.append(target)
            evidence.append(f"{target}：规范技能精确匹配")
            continue
        related = _related_candidate(target, resume_skills)
        missing.append(target)
        if related:
            points += RELATED_SKILL_CREDIT
            transferable.append(
                {
                    "candidate_skill": related,
                    "target_skill": target,
                    "relationship": "related",
                }
            )
            evidence.append(f"{related}可向{target}迁移，按部分能力计分")
    ratio = points / len(targets)
    return (
        _dimension(
            ratio,
            key,
            evidence=evidence,
            gaps=missing,
            explanation="精确技能完整计分，相关技能按40%计迁移分。",
        ),
        matched,
        missing,
        transferable,
    )


def _proficiency_dimension(
    required: dict[str, dict], resume_skills: dict[str, dict]
) -> dict:
    if not required:
        return _dimension(1.0, "skill_proficiency", explanation="目标岗位未设置熟练度要求。")
    ratios = []
    evidence = []
    gaps = []
    for name, target in sorted(required.items()):
        candidate = resume_skills.get(name)
        target_level = str(target.get("proficiency_level") or target.get("proficiency") or "working")
        target_rank = PROFICIENCY_RANK.get(target_level, PROFICIENCY_RANK["working"])
        if candidate is None:
            ratios.append(0.0)
            gaps.append(f"{name}缺少熟练度证据")
            continue
        candidate_level = str(candidate.get("proficiency") or "working")
        candidate_rank = PROFICIENCY_RANK.get(candidate_level, PROFICIENCY_RANK["working"])
        ratio = min(candidate_rank / target_rank, 1.0)
        ratios.append(ratio)
        evidence.append(f"{name}：候选人{candidate_level}，岗位要求{target_level}")
        if ratio < 1:
            gaps.append(f"{name}熟练度需由{candidate_level}提升至{target_level}")
    return _dimension(
        sum(ratios) / len(ratios),
        "skill_proficiency",
        evidence=evidence,
        gaps=gaps,
        explanation="按必备技能的证据熟练度与岗位要求逐项比较。",
    )


def required_years_for_profile(job_profile: dict) -> float:
    try:
        explicit = max(float(job_profile.get("required_years") or 0), 0.0)
    except (TypeError, ValueError):
        explicit = 0.0
    if explicit:
        return explicit
    return LEVEL_REQUIRED_YEARS.get(str(job_profile.get("level") or "unspecified"), 0.0)


def _experience_dimension(resume: dict, job_profile: dict) -> tuple[dict, float]:
    required_years = required_years_for_profile(job_profile)
    try:
        experience_years = max(float(resume.get("experience_years") or 0), 0.0)
    except (TypeError, ValueError):
        experience_years = 0.0
    ratio = min(experience_years / required_years, 1.0) if required_years else 1.0
    gaps = [] if ratio >= 1 else [f"岗位通常需要约{required_years:g}年相关经验，当前证据约{experience_years:g}年"]
    return (
        _dimension(
            ratio,
            "experience_level",
            evidence=[f"简历有效工作经验约{experience_years:g}年"],
            gaps=gaps,
            explanation="优先使用岗位明确年限，否则按岗位层级映射经验要求。",
        ),
        required_years,
    )


def _project_skill_names(resume: dict) -> set[str]:
    names = set()
    for project in resume.get("project_experiences", []) or []:
        for skill in project.get("skills", []) or []:
            name = _canonical_name(skill)
            if name:
                names.add(name)
    return names


def _project_dimension(
    resume: dict, required: dict[str, dict], resume_skills: dict[str, dict]
) -> dict:
    if not required:
        return _dimension(1.0, "project_evidence", explanation="目标岗位未设置必备技能。")
    project_skills = _project_skill_names(resume)
    structured_projects = resume.get("project_experiences", []) or []
    legacy_projects = resume.get("projects", []) or []
    points = []
    evidence = []
    gaps = []
    for name in sorted(required, key=str.casefold):
        record = resume_skills.get(name)
        sources = set(record.get("evidence_sources") or []) if record else set()
        if name in project_skills or "project" in sources:
            points.append(1.0)
            evidence.append(f"{name}具有项目使用证据")
        elif "work" in sources:
            points.append(0.6)
            evidence.append(f"{name}具有工作使用证据，但缺少项目成果")
        elif record and legacy_projects and not structured_projects:
            points.append(0.6)
            evidence.append(f"{name}存在旧版项目经历，证据粒度有限")
        else:
            points.append(0.0)
            gaps.append(f"{name}缺少可验证项目证据")
    achievements = [
        achievement
        for project in structured_projects
        if {
            _canonical_name(skill)
            for skill in project.get("skills", []) or []
            if _canonical_name(skill)
        }
        & set(required)
        for achievement in project.get("achievements", []) or []
    ]
    ratio = sum(points) / len(points)
    if achievements:
        ratio = min(1.0, ratio + 0.1)
        evidence.append(f"包含{len(achievements)}条量化项目成果")
    return _dimension(
        ratio,
        "project_evidence",
        evidence=evidence,
        gaps=gaps,
        explanation="项目实际使用证据高于技能清单自述，量化成果提供小幅增强。",
    )


def _recent_skill_names(resume: dict) -> set[str]:
    declared = {_canonical_name(item) for item in resume.get("recent_skills", []) or []}
    declared.discard("")
    if declared and resume.get("recency_mode") != "undated_fallback":
        return declared
    dated: list[tuple[str, int]] = []
    for raw in resume.get("skills", []) or []:
        if not isinstance(raw, dict) or not raw.get("last_used_at"):
            continue
        match = re.match(r"(\d{4})-(\d{2})", str(raw["last_used_at"]))
        if match:
            dated.append((_canonical_name(raw), int(match.group(1)) * 12 + int(match.group(2))))
    if not dated:
        return set()
    reference = str(resume.get("reference_date") or date.today().isoformat())
    match = re.match(r"(\d{4})-(\d{2})", reference)
    reference_month = (
        int(match.group(1)) * 12 + int(match.group(2))
        if match
        else date.today().year * 12 + date.today().month
    )
    return {
        name
        for name, value in dated
        if name and 0 <= reference_month - value <= 24
    }


def _recency_dimension(resume: dict, required: dict[str, dict]) -> dict:
    if not required:
        return _dimension(1.0, "skill_recency", explanation="目标岗位未设置必备技能。")
    recent = _recent_skill_names(resume)
    matched = sorted(set(required) & recent, key=str.casefold)
    gaps = sorted(set(required) - recent, key=str.casefold)
    return _dimension(
        len(matched) / len(required),
        "skill_recency",
        evidence=[f"{name}具有近期使用证据" for name in matched],
        gaps=[f"{name}缺少近期使用证据" for name in gaps],
        explanation="以简历时间线中的近两年工作或项目使用证据为准。",
    )


def _normalized_text(value: object) -> str:
    return re.sub(r"[\s\W_]+", "", str(value or "").casefold(), flags=re.UNICODE)


def _responsibility_scenario_dimension(resume: dict, job_profile: dict) -> dict:
    targets = [
        str(item).strip()
        for item in (job_profile.get("responsibilities", []) or [])
        + (job_profile.get("industry_scenarios", []) or [])
        if str(item).strip()
    ]
    if not targets:
        return _dimension(1.0, "responsibility_scenario", explanation="岗位未设置职责或场景要求。")
    candidate_values = []
    for key in ("work_experiences", "project_experiences"):
        for item in resume.get(key, []) or []:
            candidate_values.extend(item.get("responsibilities", []) or [])
            if item.get("industry_scenario"):
                candidate_values.append(item["industry_scenario"])
            if item.get("evidence_text"):
                candidate_values.append(item["evidence_text"])
    candidate_text = _normalized_text(" ".join(map(str, candidate_values)))
    matched = [item for item in targets if _normalized_text(item) in candidate_text]
    gaps = [item for item in targets if item not in matched]
    return _dimension(
        len(matched) / len(targets),
        "responsibility_scenario",
        evidence=[f"职责或场景命中：{item}" for item in matched],
        gaps=[f"缺少职责或场景证据：{item}" for item in gaps],
        explanation="比较项目和工作经历中的职责及行业场景证据。",
    )


def _apply_cap(dimensions: dict[str, dict], cap: float) -> float:
    raw_total = round(sum(item["score"] for item in dimensions.values()), 2)
    if raw_total <= cap:
        return raw_total
    factor = cap / raw_total
    for item in dimensions.values():
        item["raw_score"] = item["score"]
        item["raw_ratio"] = item["ratio"]
        item["score"] = round(item["score"] * factor, 2)
    adjusted = round(sum(item["score"] for item in dimensions.values()), 2)
    delta = round(cap - adjusted, 2)
    if delta:
        key = max(dimensions, key=lambda name: dimensions[name]["score"])
        dimensions[key]["score"] = round(dimensions[key]["score"] + delta, 2)
    for item in dimensions.values():
        item["ratio"] = round(item["score"] / item["max_score"], 4)
    return round(sum(item["score"] for item in dimensions.values()), 2)


def _confidence(
    resume: dict,
    job_profile: dict,
    required: dict[str, dict],
    resume_skills: dict[str, dict],
) -> tuple[str, list[str]]:
    strong = sum(
        bool(set(resume_skills.get(name, {}).get("evidence_sources") or []) & {"project", "work"})
        for name in required
    )
    reasons = []
    if not (resume.get("project_experiences") or resume.get("work_experiences")):
        reasons.append("简历缺少结构化工作或项目证据")
        return "low", reasons
    if job_profile.get("sample_status") not in {None, "ready"}:
        reasons.append("岗位画像样本尚未达到稳定状态")
    if float(job_profile.get("confidence") or 0) < 0.6:
        reasons.append("岗位画像置信度偏低")
    if reasons:
        return "medium", reasons
    if not required or strong / len(required) >= 0.5:
        return "high", ["岗位画像和简历能力均有较充分证据"]
    return "medium", ["部分必备技能缺少工作或项目证据"]


def _match_band(score: float) -> str:
    if score >= 80:
        return "high"
    if score >= 60:
        return "medium"
    return "low"


def match_resume_to_job(resume: dict, job_profile: dict) -> dict:
    resume_skills = _skill_records(resume.get("skills", []))
    required, preferred = _target_skills(job_profile)

    required_dimension, matched_required, missing_required, transferable = _coverage_dimension(
        required, resume_skills, "required_skill_coverage"
    )
    preferred_dimension, matched_preferred, missing_preferred, preferred_transfer = _coverage_dimension(
        preferred, resume_skills, "preferred_skill_coverage"
    )
    transferable.extend(preferred_transfer)
    experience_dimension, required_years = _experience_dimension(resume, job_profile)
    dimensions = {
        "required_skill_coverage": required_dimension,
        "skill_proficiency": _proficiency_dimension(required, resume_skills),
        "experience_level": experience_dimension,
        "project_evidence": _project_dimension(resume, required, resume_skills),
        "skill_recency": _recency_dimension(resume, required),
        "preferred_skill_coverage": preferred_dimension,
        "responsibility_scenario": _responsibility_scenario_dimension(resume, job_profile),
    }

    exact_required_ratio = len(matched_required) / len(required) if required else 1.0
    core_missing = [
        name
        for name in missing_required
        if float(required[name].get("prevalence") or 0) >= CORE_PREVALENCE
    ]
    caps = []
    cap = 100.0
    has_profile_signal = bool(
        required
        or preferred
        or job_profile.get("responsibilities")
        or job_profile.get("industry_scenarios")
        or required_years
    )
    if not has_profile_signal:
        cap = 0.0
        caps.append("insufficient_job_profile")
    if core_missing:
        cap = min(cap, 79.0)
        caps.append("missing_core_required_skill")
    if exact_required_ratio < 0.5:
        cap = min(cap, 59.0)
        caps.append("required_coverage_below_half")
    total_score = _apply_cap(dimensions, cap)
    confidence, confidence_reasons = _confidence(
        resume, job_profile, required, resume_skills
    )

    positive_factors = []
    if matched_required:
        positive_factors.append(f"已覆盖必备技能：{'、'.join(matched_required)}")
    if dimensions["project_evidence"]["ratio"] >= 0.6:
        positive_factors.append("具备与岗位技能相关的项目或工作证据")
    negative_factors = []
    if missing_required:
        negative_factors.append(f"必备技能缺口：{'、'.join(missing_required)}")
    negative_factors.extend(dimensions["experience_level"]["gaps"])

    learning_gaps = []
    required_uplift = 70 / len(required) if required else 0
    preferred_uplift = 5 / len(preferred) if preferred else 0
    for skill in missing_required:
        prevalence = float(required[skill].get("prevalence") or 0)
        learning_gaps.append(
            {
                "skill": skill,
                "priority": "core" if prevalence >= CORE_PREVALENCE else "required",
                "reason": "目标岗位核心必备技能" if prevalence >= CORE_PREVALENCE else "目标岗位必备技能缺口",
                "max_uplift": required_uplift,
            }
        )
    proficiency_uplift = 15 / len(required) if required else 0
    for skill, target in sorted(required.items()):
        candidate = resume_skills.get(skill)
        if candidate is None:
            continue
        target_level = str(
            target.get("proficiency_level") or target.get("proficiency") or "working"
        )
        candidate_level = str(candidate.get("proficiency") or "working")
        target_rank = PROFICIENCY_RANK.get(target_level, PROFICIENCY_RANK["working"])
        candidate_rank = PROFICIENCY_RANK.get(
            candidate_level, PROFICIENCY_RANK["working"]
        )
        if candidate_rank >= target_rank:
            continue
        prevalence = float(target.get("prevalence") or 0)
        learning_gaps.append(
            {
                "skill": skill,
                "priority": "core" if prevalence >= CORE_PREVALENCE else "required",
                "reason": f"熟练度需由{candidate_level}提升至{target_level}",
                "max_uplift": proficiency_uplift
                * (1 - candidate_rank / target_rank),
                "allow_owned": True,
            }
        )
    for skill in missing_preferred:
        learning_gaps.append(
            {
                "skill": skill,
                "priority": "preferred",
                "reason": "目标岗位加分技能缺口",
                "max_uplift": preferred_uplift,
            }
        )
    learning_plan = build_learning_path(
        learning_gaps,
        resume_skills.keys(),
        total_score,
        evidence_records=job_profile.get("evidence_records", []),
    )
    learning_path = [
        {
            "order": index,
            "skill": item["skill"],
            "priority": "high" if item["priority"] in {"core", "required"} else "medium",
            "reason": item["reason"],
            "suggestion": f"学习{item['skill']}并完成一个可验证项目，形成真实能力证据。",
        }
        for index, item in enumerate(learning_gaps, start=1)
    ]

    recommendations = []
    if missing_required:
        recommendations.append(f"优先补齐必备技能：{'、'.join(missing_required)}。")
    if dimensions["experience_level"]["ratio"] < 1:
        recommendations.append(f"目标岗位通常需要约{required_years:g}年相关经验，建议补充可验证项目。")
    if dimensions["project_evidence"]["ratio"] == 0:
        recommendations.append("补充项目职责、实际技术栈和量化成果证据。")
    if not recommendations:
        recommendations.append("核心能力与目标岗位较为匹配，可继续强化行业场景和工程化能力。")

    return {
        "job_name": job_profile.get("name", "目标岗位"),
        "total_score": total_score,
        "match_band": _match_band(total_score),
        "confidence": confidence,
        "confidence_reasons": confidence_reasons,
        "scoring_version": SCORING_VERSION,
        "dimension_scores": {
            name: item["score"] for name, item in dimensions.items()
        },
        "dimensions": dimensions,
        "score_caps": caps,
        "positive_factors": positive_factors,
        "negative_factors": negative_factors,
        "transferable_skills": sorted(
            transferable,
            key=lambda item: (item["target_skill"].casefold(), item["candidate_skill"].casefold()),
        ),
        "matched_required_skills": matched_required,
        "matched_preferred_skills": matched_preferred,
        "missing_required_skills": missing_required,
        "missing_preferred_skills": missing_preferred,
        "learning_path": learning_path,
        "learning_plan": learning_plan,
        "recommendations": recommendations,
        "score_explanation": "总分由必备技能30%、熟练度15%、经验层级15%、项目证据15%、技能时效10%、加分技能5%、职责场景10%构成。",
    }
