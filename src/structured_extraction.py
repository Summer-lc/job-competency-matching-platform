from __future__ import annotations

import json
import re


def _normalized(value: str) -> str:
    return re.sub(r"[\s\W_]+", "", (value or "").lower(), flags=re.UNICODE)


def _string_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def validate_grounded_extraction(payload: dict, source_text: str) -> dict:
    source = _normalized(source_text)
    accepted = {"required_skills": [], "preferred_skills": []}
    rejected = []
    total = 0
    for field in accepted:
        values = payload.get(field) if isinstance(payload.get(field), list) else []
        for raw in values:
            total += 1
            item = raw if isinstance(raw, dict) else {"name": str(raw), "evidence": ""}
            name = str(item.get("name", "")).strip()
            evidence = str(item.get("evidence", "")).strip()
            if not name:
                continue
            if not evidence or _normalized(evidence) not in source:
                rejected.append({"name": name, "reason": "evidence_not_found", "evidence": evidence})
                continue
            accepted[field].append(
                {
                    "name": name,
                    "evidence": evidence,
                    "confidence": min(max(float(item.get("confidence", 0.85)), 0.0), 1.0),
                }
            )
    return {
        "responsibilities": _string_list(payload.get("responsibilities")),
        "required_skills": accepted["required_skills"],
        "preferred_skills": accepted["preferred_skills"],
        "industry_scenarios": _string_list(payload.get("industry_scenarios")),
        "rejected_skills": rejected,
        "hallucination_risk": round(len(rejected) / max(total, 1), 4),
        "grounded": not rejected,
    }


def parse_llm_json(raw: str) -> dict:
    text = (raw or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    if not candidate.startswith("{"):
        match = re.search(r"\{.*\}", candidate, flags=re.DOTALL)
        if not match:
            raise ValueError("大模型未返回JSON对象")
        candidate = match.group(0)
    value = json.loads(candidate)
    if not isinstance(value, dict):
        raise ValueError("大模型结果必须是JSON对象")
    return value


def extract_job_with_llm(source_text: str, model: str | None = None) -> dict:
    from src.llm import get_llm

    prompt = f"""你是岗位JD结构化抽取器。只能抽取原文明确出现的信息，不得补充常识。
返回严格JSON：
{{
  "responsibilities": ["原文职责"],
  "required_skills": [{{"name":"技能", "evidence":"原文连续证据", "confidence":0.0}}],
  "preferred_skills": [{{"name":"技能", "evidence":"原文连续证据", "confidence":0.0}}],
  "industry_scenarios": ["原文场景"]
}}
每个技能的evidence必须是JD原文中的连续片段。

JD原文：
{source_text}
"""
    response = get_llm(model).invoke(prompt)
    raw = response.content if hasattr(response, "content") else str(response)
    return validate_grounded_extraction(parse_llm_json(raw), source_text)

