from __future__ import annotations

import json
import math
import re
from hashlib import sha256
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from model_class.job_competency import JobPosting
from model_class.knowledge_base import KnowledgeChunk


class EmbeddingProvider(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def embed_query(self, text: str) -> list[float]:
        raise NotImplementedError


def cosine_similarity(first: list[float], second: list[float]) -> float:
    if not first or len(first) != len(second):
        return 0.0
    norm_first = math.sqrt(sum(value * value for value in first))
    norm_second = math.sqrt(sum(value * value for value in second))
    if norm_first == 0.0 or norm_second == 0.0:
        return 0.0
    return sum(a * b for a, b in zip(first, second)) / (norm_first * norm_second)


_CHINESE_STOP_TOKENS = {
    "哪些",
    "什么",
    "需要",
    "要求",
    "负责",
    "相关",
    "能力",
    "岗位",
}


def lexical_tokens(text: str) -> list[str]:
    tokens: set[str] = set()
    for value in re.findall(r"[A-Za-z0-9+#./-]+|[\u4e00-\u9fff]{2,}", text):
        lowered = value.lower()
        if re.fullmatch(r"[\u4e00-\u9fff]+", lowered):
            for size in range(2, min(6, len(lowered)) + 1):
                for start in range(len(lowered) - size + 1):
                    token = lowered[start : start + size]
                    if token not in _CHINESE_STOP_TOKENS:
                        tokens.add(token)
        else:
            tokens.add(lowered)
    return sorted(tokens, key=lambda token: (-len(token), token))


def lexical_score(query: str, text: str) -> float:
    lowered = text.lower()
    return sum(
        1.0 + lowered.count(term) * 0.1
        for term in lexical_tokens(query)
        if term in lowered
    )


async def update_knowledge_chunks(
    db: AsyncSession,
    family_codes: set[str],
    embedder: EmbeddingProvider | None = None,
) -> dict:
    if not family_codes:
        return {"created": 0, "updated": 0, "families": []}
    postings = list(
        (
            await db.execute(
                select(JobPosting).where(
                    JobPosting.job_family_id.in_(family_codes),
                    JobPosting.status.in_(["valid", "review"]),
                    JobPosting.duplicate_of_id.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    vectors = embedder.embed_documents(
        [posting.job_description_raw for posting in postings]
    ) if embedder and postings else [None] * len(postings)
    created = 0
    updated = 0
    for posting, vector in zip(postings, vectors):
        text = f"{posting.job_title_normalized}\n{posting.job_description_raw}"
        text_hash = sha256(text.encode("utf-8")).hexdigest()
        chunk_id = sha256(f"job_posting:{posting.id}:{text_hash}".encode("utf-8")).hexdigest()
        existing = await db.scalar(
            select(KnowledgeChunk).where(KnowledgeChunk.chunk_id == chunk_id)
        )
        metadata = {
            "record_id": posting.record_id,
            "company_name": posting.company_name,
            "source_name": posting.source_name,
            "published_at": posting.published_at.isoformat() if posting.published_at else None,
            "quality_score": posting.quality_score,
            "review_status": posting.status,
        }
        if existing is None:
            db.add(
                KnowledgeChunk(
                    chunk_id=chunk_id,
                    source_type="job_posting",
                    source_entity_id=str(posting.id),
                    job_posting_id=posting.id,
                    family_code=posting.job_family_id,
                    text=text,
                    text_hash=text_hash,
                    source_url=posting.source_url,
                    metadata_json=json.dumps(metadata, ensure_ascii=False),
                    embedding_json=json.dumps(vector) if vector is not None else None,
                    embedding_model=type(embedder).__name__ if embedder else None,
                )
            )
            created += 1
        else:
            existing.metadata_json = json.dumps(metadata, ensure_ascii=False)
            existing.source_url = posting.source_url
            if vector is not None:
                existing.embedding_json = json.dumps(vector)
                existing.embedding_model = type(embedder).__name__
            updated += 1
    await db.commit()
    return {"created": created, "updated": updated, "families": sorted(family_codes)}


async def search_knowledge(
    db: AsyncSession,
    query: str,
    *,
    family_code: str | None = None,
    limit: int = 10,
    embedder: EmbeddingProvider | None = None,
) -> dict:
    statement = select(KnowledgeChunk).where(KnowledgeChunk.status == "active")
    if family_code:
        statement = statement.where(KnowledgeChunk.family_code == family_code)
    chunks = list((await db.execute(statement)).scalars().all())
    query_vector = embedder.embed_query(query) if embedder else None
    scored = []
    for chunk in chunks:
        lexical = lexical_score(query, chunk.text)
        vector_score = 0.0
        if query_vector is not None and chunk.embedding_json:
            vector_score = cosine_similarity(query_vector, json.loads(chunk.embedding_json))
        if lexical <= 0.0 and vector_score <= 0.0:
            continue
        score = lexical + vector_score
        metadata = json.loads(chunk.metadata_json)
        scored.append(
            (
                score,
                {
                    "chunk_id": chunk.chunk_id,
                    "family_code": chunk.family_code,
                    "text": chunk.text,
                    "source_url": chunk.source_url,
                    "score": round(score, 6),
                    **metadata,
                },
            )
        )
    scored.sort(key=lambda item: (-item[0], item[1]["chunk_id"]))
    return {
        "mode": "hybrid" if embedder else "lexical",
        "query": query,
        "items": [item for _, item in scored[: max(1, min(limit, 100))]],
        "total": len(scored),
    }
