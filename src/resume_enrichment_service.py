from __future__ import annotations

import json
from copy import deepcopy
from typing import Callable

from src.skill_ontology import PROFICIENCY_RANK, SKILL_CATALOG, normalize_skill
from src.structured_extraction import parse_llm_json


ENRICHMENT_VERSION = "resume-enrichment-v1"
ALLOWED_PROFICIENCY = frozenset(PROFICIENCY_RANK)
MIN_MODEL_CONFIDENCE = 0.5


def _clamped_confidence(value: object, default: float = 0.75) -> float:
    try:
        return round(min(max(float(value), 0.0), 1.0), 4)
    except (TypeError, ValueError):
        return default


def _verbatim_evidence(item: dict, source_text: str) -> tuple[str | None, str | None]:
    evidence = str(item.get("evidence", "")).strip()
    if not evidence or evidence not in source_text:
        return None, "evidence_not_found"
    return evidence, None


def _skill_supported_by_evidence(raw_name: str, canonical_name: str, evidence: str) -> bool:
    category_and_aliases = SKILL_CATALOG.get(canonical_name)
    aliases = category_and_aliases[1] if category_and_aliases else ()
    candidates = {raw_name, canonical_name, *aliases}
    folded = evidence.casefold().replace(" ", "")
    return any(value and value.casefold().replace(" ", "") in folded for value in candidates)


def validate_resume_enrichment(payload: dict, source_text: str) -> dict:
    accepted: dict[str, list[dict]] = {"skills": [], "achievements": []}
    rejected: list[dict] = []

    for raw in payload.get("skills", []) if isinstance(payload.get("skills"), list) else []:
        item = raw if isinstance(raw, dict) else {}
        evidence, reason = _verbatim_evidence(item, source_text)
        raw_name = str(item.get("name", "")).strip()
        normalized = normalize_skill(raw_name)
        if not normalized["name"]:
            reason = reason or "missing_name"
        confidence = _clamped_confidence(item.get("confidence"))
        if confidence < MIN_MODEL_CONFIDENCE:
            reason = reason or "confidence_below_threshold"
        if (
            not reason
            and evidence
            and not _skill_supported_by_evidence(raw_name, normalized["name"], evidence)
        ):
            reason = "skill_not_supported_by_evidence"
        if reason:
            rejected.append({"kind": "skill", **item, "reason": reason})
            continue
        proficiency = str(item.get("proficiency", "working")).lower()
        if proficiency not in ALLOWED_PROFICIENCY:
            proficiency = "working"
        accepted["skills"].append(
            {
                "name": normalized["name"],
                "alias": raw_name,
                "category": normalized["category"],
                "proficiency": proficiency,
                "confidence": confidence,
                "evidence": evidence,
            }
        )

    values = payload.get("achievements", [])
    for raw in values if isinstance(values, list) else []:
        item = raw if isinstance(raw, dict) else {}
        evidence, reason = _verbatim_evidence(item, source_text)
        text = str(item.get("text", "")).strip()
        if not text:
            reason = reason or "missing_text"
        confidence = _clamped_confidence(item.get("confidence"))
        if confidence < MIN_MODEL_CONFIDENCE:
            reason = reason or "confidence_below_threshold"
        if not reason and (text not in source_text or not evidence or text not in evidence):
            reason = "achievement_not_verbatim"
        if reason:
            rejected.append({"kind": "achievement", **item, "reason": reason})
            continue
        accepted["achievements"].append(
            {
                "text": text,
                "evidence": evidence,
                "confidence": confidence,
            }
        )

    return {**accepted, "rejected": rejected}


def _merge_skill(existing: dict, addition: dict) -> None:
    aliases = set(existing.get("aliases") or [])
    if addition["alias"]:
        aliases.add(addition["alias"])
    existing["aliases"] = sorted(aliases, key=str.casefold)
    current = str(existing.get("proficiency", "aware"))
    if PROFICIENCY_RANK.get(addition["proficiency"], 1) > PROFICIENCY_RANK.get(current, 1):
        existing["proficiency"] = addition["proficiency"]
    evidence = {
        "text": addition["evidence"],
        "source": "model_enrichment",
        "strength": addition["confidence"],
        "used_at": None,
    }
    evidence_items = existing.setdefault("evidence", [])
    if evidence not in evidence_items:
        evidence_items.append(evidence)
    sources = existing.setdefault("evidence_sources", [])
    if "model_enrichment" not in sources:
        sources.append("model_enrichment")
    existing["confidence"] = max(
        _clamped_confidence(existing.get("confidence")), addition["confidence"]
    )


def merge_resume_enrichment(profile: dict, accepted: dict) -> dict:
    merged = deepcopy(profile)
    existing_by_name = {
        str(item.get("name", "")): item for item in merged.setdefault("skills", [])
    }
    for addition in accepted.get("skills", []):
        existing = existing_by_name.get(addition["name"])
        if existing is not None:
            _merge_skill(existing, addition)
            continue
        item = {
            "name": addition["name"],
            "category": addition["category"],
            "confidence": addition["confidence"],
            "evidence_text": addition["evidence"],
            "aliases": [addition["alias"]] if addition["alias"] else [],
            "proficiency": addition["proficiency"],
            "last_used_at": None,
            "evidence_sources": ["model_enrichment"],
            "evidence": [
                {
                    "text": addition["evidence"],
                    "source": "model_enrichment",
                    "strength": addition["confidence"],
                    "used_at": None,
                }
            ],
        }
        merged["skills"].append(item)
        existing_by_name[addition["name"]] = item

    achievements = merged.setdefault("achievements", [])
    for item in accepted.get("achievements", []):
        if item not in achievements:
            achievements.append(item)
    merged["skills"] = sorted(merged["skills"], key=lambda item: item["name"].casefold())
    merged["evidence_count"] = (
        sum(len(item.get("evidence", [])) for item in merged["skills"])
        + len(merged.get("projects", []))
        + len(achievements)
    )
    merged["parser_mode"] = "hybrid"
    merged["enrichment_version"] = ENRICHMENT_VERSION
    merged["enrichment_rejected"] = accepted.get("rejected", [])
    return merged


def build_resume_prompt(source_text: str, profile: dict) -> str:
    rule_summary = json.dumps(
        {
            "skills": [item.get("name") for item in profile.get("skills", [])],
            "project_count": len(profile.get("project_experiences", [])),
        },
        ensure_ascii=False,
    )
    return f"""你是简历能力证据补充器。只能补充简历原文明确表达、规则尚未完整识别的信息。
返回严格JSON对象：
{{
  "skills": [{{"name":"技能原名", "proficiency":"aware|working|advanced|expert", "confidence":0.0, "evidence":"连续原文"}}],
  "achievements": [{{"text":"量化成果", "confidence":0.0, "evidence":"连续原文"}}]
}}
每个evidence必须是输入简历中的连续原文。不得推断或输出姓名、性别、年龄、籍贯等个人属性；不得评价候选人；不得生成匹配分数；不得补充常识或虚构经历。
规则结果摘要：{rule_summary}

简历原文：
{source_text}
"""


def _default_invoke(prompt: str, model: str | None) -> object:
    from src.llm import get_llm

    return get_llm(model).invoke(prompt)


def enrich_resume_profile(
    source_text: str,
    profile: dict,
    *,
    model: str | None = None,
    invoke: Callable[[str], object] | None = None,
) -> dict:
    prompt = build_resume_prompt(source_text, profile)
    try:
        response = invoke(prompt) if invoke else _default_invoke(prompt, model)
    except Exception:
        fallback = deepcopy(profile)
        fallback["parser_mode"] = "rules"
        warnings = fallback.setdefault("parse_warnings", [])
        if "model_unavailable" not in warnings:
            warnings.append("model_unavailable")
        return fallback

    try:
        raw = response.content if hasattr(response, "content") else str(response)
        accepted = validate_resume_enrichment(parse_llm_json(raw), source_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        fallback = deepcopy(profile)
        fallback["parser_mode"] = "rules"
        warnings = fallback.setdefault("parse_warnings", [])
        if "model_invalid_response" not in warnings:
            warnings.append("model_invalid_response")
        return fallback

    if not accepted["skills"] and not accepted["achievements"]:
        fallback = deepcopy(profile)
        fallback["parser_mode"] = "rules"
        fallback["enrichment_rejected"] = accepted["rejected"]
        warnings = fallback.setdefault("parse_warnings", [])
        if "model_no_grounded_fields" not in warnings:
            warnings.append("model_no_grounded_fields")
        return fallback
    return merge_resume_enrichment(profile, accepted)
