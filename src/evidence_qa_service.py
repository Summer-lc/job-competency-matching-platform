from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from model_class.job_competency import EvidenceRecord
from src.knowledge_service import lexical_score, lexical_tokens, search_knowledge


ModelInvoker = Callable[[str, str | None], Awaitable[str]]


class NoEvidenceError(ValueError):
    """Raised when the knowledge base cannot support an answer."""


def _dedupe_key(item: dict) -> tuple[str, str]:
    normalized = re.sub(r"\s+", "", item["text"]).lower()
    return normalized, item.get("source_url") or ""


def _relevant_excerpt(question: str, text: str, max_chars: int = 800) -> str:
    if len(text) <= max_chars:
        return text
    lowered = text.lower()
    terms = [term for term in lexical_tokens(question) if term in lowered]
    positions = {lowered.find(term) for term in terms}
    positions.discard(-1)
    if not positions:
        positions = {0}
    best_start = 0
    best_score = -1
    body_length = max_chars - 2
    for position in positions:
        start = max(0, min(position - body_length // 3, len(text) - body_length))
        window = lowered[start : start + body_length]
        score = sum(len(term) for term in terms if term in window)
        if score > best_score or (score == best_score and start < best_start):
            best_start = start
            best_score = score
    prefix = "…" if best_start else ""
    end = best_start + body_length
    suffix = "…" if end < len(text) else ""
    return prefix + text[best_start:end] + suffix


async def gather_answer_evidence(
    db: AsyncSession,
    question: str,
    *,
    family_code: str | None = None,
    limit: int = 6,
) -> list[dict]:
    capped_limit = max(3, min(limit, 12))
    knowledge = await search_knowledge(
        db,
        question,
        family_code=family_code,
        limit=min(capped_limit * 3, 100),
    )
    candidates: list[dict] = []
    for item in knowledge["items"]:
        quality_score = float(item.get("quality_score") or 0.0)
        candidates.append(
            {
                "source_kind": "jd",
                "evidence_type": "job_description",
                "family_code": item["family_code"],
                "title": item["text"].splitlines()[0][:120],
                "organization": item.get("company_name") or item.get("source_name") or "未知来源",
                "record_id": item.get("record_id"),
                "review_status": item.get("review_status") or "unknown",
                "text": _relevant_excerpt(question, item["text"]),
                "source_url": item.get("source_url"),
                "score": float(item["score"]) + quality_score * 0.2,
                "stable_key": item["chunk_id"],
            }
        )

    statement = select(EvidenceRecord)
    if family_code:
        statement = statement.where(EvidenceRecord.job_family_id == family_code)
    external_records = list((await db.execute(statement)).scalars().all())
    for record in external_records:
        searchable_text = "\n".join(
            value
            for value in [record.title, record.related_skill, record.evidence_summary]
            if value
        )
        relevance = lexical_score(question, searchable_text)
        if relevance <= 0.0:
            continue
        candidates.append(
            {
                "source_kind": "external",
                "evidence_type": record.evidence_type,
                "family_code": record.job_family_id,
                "title": record.title,
                "organization": record.publisher,
                "record_id": record.evidence_id,
                "review_status": "verified",
                "text": _relevant_excerpt(question, record.evidence_summary),
                "source_url": record.source_url,
                "score": relevance + float(record.source_score) * 0.35,
                "stable_key": f"external:{record.evidence_id}",
            }
        )

    candidates.sort(
        key=lambda item: (
            -item["score"],
            0 if item["source_kind"] == "external" else 1,
            item["stable_key"],
        )
    )
    unique: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in candidates:
        key = _dedupe_key(item)
        if key in seen:
            continue
        seen.add(key)
        item = {key: value for key, value in item.items() if key != "stable_key"}
        item["score"] = round(item["score"], 6)
        unique.append(item)
        if len(unique) >= capped_limit:
            break

    for index, item in enumerate(unique, start=1):
        item["citation_id"] = f"K{index}"
    return unique


def build_grounded_prompt(question: str, evidence: list[dict]) -> str:
    context = "\n".join(
        f"[{item['citation_id']}] {item['title']} | {item['organization']} | {item['text']}"
        for item in evidence
    )
    return (
        "你是岗位能力分析助手。只能依据下方编号证据回答，不得补充证据之外的常识。"
        "每个主要结论必须引用至少一个[K编号]，不得引用不存在的编号。"
        "如果证据冲突，要明确指出来源差异；如果证据不足，要直接说明不足。"
        "请使用简洁中文。\n\n"
        f"问题：{question}\n\n证据：\n{context}"
    )


def validate_citations(answer: str, evidence: list[dict]) -> bool:
    if not answer or not answer.strip():
        return False
    cited = {f"K{number}" for number in re.findall(r"\[K(\d+)\]", answer)}
    allowed = {item["citation_id"] for item in evidence}
    return bool(cited) and cited.issubset(allowed)


def build_extractive_answer(evidence: list[dict]) -> str:
    lines = ["根据当前知识库，可追溯到以下证据："]
    for item in evidence[:3]:
        excerpt = re.sub(r"\s+", " ", item["text"]).strip()[:180]
        lines.append(f"- [{item['citation_id']}] {item['title']}：{excerpt}")
    return "\n".join(lines)


async def _invoke_deepseek(prompt: str, model: str | None) -> str:
    from src.llm import get_llm

    response = await asyncio.to_thread(get_llm(model).invoke, prompt)
    return str(response.content)


async def answer_knowledge_question(
    db: AsyncSession,
    question: str,
    *,
    family_code: str | None = None,
    limit: int = 6,
    model: str | None = None,
    model_invoker: ModelInvoker | None = None,
) -> dict:
    evidence = await gather_answer_evidence(
        db,
        question,
        family_code=family_code,
        limit=limit,
    )
    if not evidence:
        raise NoEvidenceError("当前知识库没有足够证据支持回答")

    prompt = build_grounded_prompt(question, evidence)
    invoker = model_invoker or _invoke_deepseek
    warning = None
    try:
        answer = await invoker(prompt, model)
    except Exception:
        answer = build_extractive_answer(evidence)
        mode = "extractive_fallback"
        warning = "模型暂时不可用，已返回可追溯的证据摘要"
    else:
        if validate_citations(answer, evidence):
            mode = "grounded_llm"
        else:
            answer = build_extractive_answer(evidence)
            mode = "extractive_fallback"
            warning = "模型回答未通过引用校验，已返回证据摘要"

    return {
        "answer": answer,
        "mode": mode,
        "family_code": family_code,
        "citations_valid": validate_citations(answer, evidence),
        "evidence": evidence,
        "warning": warning,
    }
