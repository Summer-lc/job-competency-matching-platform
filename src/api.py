from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from sqlalchemy import delete, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.DB_config import ASYNC_DATABASE_URL, get_db, init_db
from model_class.job_competency import (
    EvaluationRun,
    EvidenceRecord,
    JobPosting,
    JobPostingSkill,
    JobProfile,
    JobProfileSkill,
    MatchRecord,
    ResumeRecord,
    ReviewItem,
    Skill,
)
from model_class.knowledge_base import EvolutionEvent, ImportBatch, RawJobRecord
from schemes.job_competency import (
    EvidenceInput,
    EvidenceQuestionRequest,
    KnowledgeAnswerRequest,
    HardMetricsRunRequest,
    JobLevelReviewRequest,
    JobProfileUpdate,
    JobRecommendationRequest,
    MatchRequest,
    ReviewDecisionRequest,
)
from src.acceptance_service import acceptance_summary
from src.app_config import DEEPSEEK_API_KEY
from src.evaluation_service import (
    parse_benchmark_records,
    readiness_from_results,
    run_benchmark,
)
from src.evidence_qa_service import NoEvidenceError, answer_knowledge_question
from src.evolution_service import family_evolution_payload
from src.hard_metrics_pipeline import (
    persist_manual_level_review,
    pipeline_run_history,
    quality_distribution,
)
from src.job_analysis_service import family_evolution, graph_data, rebuild_analysis
from src.job_data_service import (
    MAX_RECORD_IMPORT_BYTES,
    RecordImportLimitError,
    SOURCE_SCORES,
    parse_records,
)
from src.import_service import MAX_IMPORT_BYTES, ImportLimitError, import_job_file
from src.knowledge_service import search_knowledge, update_knowledge_chunks
from src.job_recommendation_service import profile_matching_payload, recommend_jobs
from src.matching_service import match_resume_to_job
from src.resume_enrichment_service import enrich_resume_profile
from src.resume_service import parse_resume_bytes
from src.quarterly_profile_service import list_quarterly_profiles
from src.schema_migration import backup_sqlite_database


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKUP_DIR = PROJECT_ROOT / "data" / "backups"
MAX_EVIDENCE_IMPORT_BYTES = MAX_RECORD_IMPORT_BYTES


def _json_load(value: str | None, fallback):
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _parse_datetime(value: str | None) -> datetime | None:
    if not value or value == "unknown":
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


async def _profile_payload(db: AsyncSession, profile: JobProfile) -> dict:
    payload = await profile_matching_payload(db, profile)
    payload.update(
        {
        "status": profile.status,
        "valid_from": profile.valid_from.isoformat() if profile.valid_from else None,
        "valid_to": profile.valid_to.isoformat() if profile.valid_to else None,
        "derivation_status": profile.derivation_status,
        }
    )
    return payload


async def _latest_profiles(db: AsyncSession) -> list[JobProfile]:
    profiles = list(
        (await db.execute(select(JobProfile).order_by(JobProfile.family_code, JobProfile.version.desc())))
        .scalars()
        .all()
    )
    latest = {}
    for profile in profiles:
        selected = latest.get(profile.family_code)
        if selected is None or (
            profile.profile_kind == "quarterly"
            and profile.derivation_status == "active"
            and selected.profile_kind != "quarterly"
        ):
            latest[profile.family_code] = profile
    return list(latest.values())


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="岗位能力图谱与动态演化分析平台",
        description="多源异构数据驱动的岗位能力图谱与动态演化分析平台",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "job-competency-platform", "version": "1.0.0"}

    @app.get("/", response_class=HTMLResponse)
    async def home():
        path = PROJECT_ROOT / "index.html"
        if not path.exists():
            return "<h1>岗位能力图谱与动态演化分析平台</h1>"
        return path.read_text(encoding="utf-8")

    async def read_bounded_upload(
        file: UploadFile, *, limit: int, label: str
    ) -> bytes:
        content_length = file.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = None
            if declared is not None and declared > limit:
                raise HTTPException(
                    status_code=413, detail=f"{label} exceeds byte limit"
                )
        chunks: list[bytes] = []
        received = 0
        while True:
            chunk = await file.read(min(64 * 1024, limit - received + 1))
            if not chunk:
                return b"".join(chunks)
            received += len(chunk)
            if received > limit:
                raise HTTPException(status_code=413, detail=f"{label} exceeds byte limit")
            chunks.append(chunk)

    async def read_import_upload(file: UploadFile) -> bytes:
        return await read_bounded_upload(
            file, limit=MAX_IMPORT_BYTES, label="import file"
        )

    @app.post("/api/data/import")
    async def import_data(
        file: UploadFile = File(...), db: AsyncSession = Depends(get_db)
    ):
        try:
            result = await import_job_file(
                db, await read_import_upload(file), file.filename or "jobs.jsonl"
            )
        except ImportLimitError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        affected = set(result.get("affected_families") or [])
        if affected and not result.get("idempotent"):
            result["analysis"] = await rebuild_analysis(db, family_codes=affected)
            result["knowledge"] = await update_knowledge_chunks(db, affected)
        else:
            result["analysis"] = {
                "profiles_created": 0,
                "review_items_created": 0,
                "families": 0,
                "unchanged_families": sorted(affected),
            }
            result["knowledge"] = {"created": 0, "updated": 0, "families": []}
        return result

    @app.get("/api/data/import-batches")
    async def import_batches(db: AsyncSession = Depends(get_db)):
        rows = list(
            (
                await db.execute(
                    select(ImportBatch).order_by(ImportBatch.created_at.desc(), ImportBatch.id.desc())
                )
            )
            .scalars()
            .all()
        )
        return {
            "items": [
                {
                    "batch_id": row.batch_id,
                    "filename": row.filename,
                    "status": row.status,
                    "raw_lines": row.raw_lines,
                    "parsed_lines": row.parsed_lines,
                    "imported": row.imported,
                    "revised": row.revised,
                    "review": row.review_count,
                    "duplicates": row.duplicates,
                    "quarantined": row.quarantined,
                    "affected_families": _json_load(row.affected_families_json, []),
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ],
            "total": len(rows),
        }

    @app.get("/api/data/quarantine")
    async def quarantined_records(
        limit: int = Query(100, ge=1, le=500), db: AsyncSession = Depends(get_db)
    ):
        rows = list(
            (
                await db.execute(
                    select(RawJobRecord)
                    .where(RawJobRecord.status == "quarantined")
                    .order_by(RawJobRecord.id.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        total = await db.scalar(
            select(func.count()).select_from(RawJobRecord).where(
                RawJobRecord.status == "quarantined"
            )
        ) or 0
        return {
            "items": [
                {
                    "id": row.id,
                    "line_number": row.line_number,
                    "error_code": row.error_code,
                    "error_message": row.error_message,
                    "raw_text": row.raw_text,
                }
                for row in rows
            ],
            "total": total,
        }

    @app.get("/api/knowledge/search")
    async def knowledge_search(
        q: str = Query(..., min_length=1),
        family_code: str | None = None,
        limit: int = Query(10, ge=1, le=100),
        db: AsyncSession = Depends(get_db),
    ):
        return await search_knowledge(
            db, q, family_code=family_code, limit=limit
        )

    @app.post("/api/knowledge/answer")
    async def knowledge_answer(
        request: KnowledgeAnswerRequest,
        db: AsyncSession = Depends(get_db),
    ):
        try:
            return await answer_knowledge_question(
                db,
                request.question,
                family_code=request.family_code,
                limit=request.limit,
                model=request.model,
            )
        except NoEvidenceError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/data/evidence/import")
    async def import_evidence(
        file: UploadFile = File(...), db: AsyncSession = Depends(get_db)
    ):
        try:
            raw = await read_bounded_upload(
                file,
                limit=MAX_EVIDENCE_IMPORT_BYTES,
                label="evidence import file",
            )
            records = parse_records(raw, file.filename or "evidence.jsonl")
        except RecordImportLimitError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        imported = 0
        skipped = 0
        errors = []
        for index, raw in enumerate(records, start=1):
            try:
                item = EvidenceInput.model_validate(raw)
                if await db.scalar(select(EvidenceRecord).where(EvidenceRecord.evidence_id == item.evidence_id)):
                    skipped += 1
                    continue
                db.add(
                    EvidenceRecord(
                        evidence_id=item.evidence_id,
                        job_family_id=item.job_family_id,
                        evidence_type=item.evidence_type,
                        title=item.title,
                        publisher=item.publisher,
                        published_at=_parse_datetime(item.published_at),
                        source_url=item.source_url,
                        related_skill=item.related_skill,
                        evidence_summary=item.evidence_summary,
                        source_score=SOURCE_SCORES.get(item.evidence_type, 0.75),
                    )
                )
                imported += 1
            except Exception as exc:
                errors.append({"row": index, "message": str(exc)})
        await db.commit()
        return {"received": len(records), "imported": imported, "skipped": skipped, "errors": errors}

    @app.get("/api/data/stats")
    async def data_stats(db: AsyncSession = Depends(get_db)):
        postings = await db.scalar(select(func.count()).select_from(JobPosting)) or 0
        valid = await db.scalar(
            select(func.count()).select_from(JobPosting).where(JobPosting.status == "valid")
        ) or 0
        duplicates = await db.scalar(
            select(func.count()).select_from(JobPosting).where(JobPosting.status == "duplicate")
        ) or 0
        sources = await db.scalar(select(func.count(distinct(JobPosting.source_name)))) or 0
        skills = await db.scalar(select(func.count()).select_from(Skill)) or 0
        evidence = await db.scalar(select(func.count()).select_from(EvidenceRecord)) or 0
        profiles = await db.scalar(select(func.count(distinct(JobProfile.family_code)))) or 0
        pending = await db.scalar(
            select(func.count()).select_from(ReviewItem).where(ReviewItem.status == "pending")
        ) or 0
        avg_quality = await db.scalar(select(func.avg(JobPosting.quality_score))) or 0.0
        return {
            "job_postings": postings,
            "valid_postings": valid,
            "duplicates": duplicates,
            "duplicate_rate": round(duplicates / postings, 4) if postings else 0.0,
            "sources": sources,
            "skills": skills,
            "evidence_records": evidence,
            "job_profiles": profiles,
            "pending_reviews": pending,
            "average_quality_score": round(float(avg_quality), 4),
        }

    @app.get("/api/jobs")
    async def jobs(
        status: str | None = None,
        tech_stack: str | None = None,
        db: AsyncSession = Depends(get_db),
    ):
        profiles = await _latest_profiles(db)
        if status:
            profiles = [profile for profile in profiles if profile.status == status]
        if tech_stack:
            profiles = [profile for profile in profiles if profile.tech_stack == tech_stack]
        return {"items": [await _profile_payload(db, profile) for profile in profiles], "total": len(profiles)}

    @app.get("/api/jobs/{profile_id}")
    async def job_detail(profile_id: int, db: AsyncSession = Depends(get_db)):
        profile = await db.get(JobProfile, profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="岗位画像不存在")
        return await _profile_payload(db, profile)

    @app.put("/api/jobs/{profile_id}")
    async def update_job(
        profile_id: int,
        request: JobProfileUpdate,
        db: AsyncSession = Depends(get_db),
    ):
        profile = await db.get(JobProfile, profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="岗位画像不存在")
        values = request.model_dump(exclude_unset=True)
        for field in ("name", "description", "status", "level", "tech_stack"):
            if field in values:
                setattr(profile, field, values[field])
        if "responsibilities" in values:
            profile.responsibilities_json = json.dumps(values["responsibilities"], ensure_ascii=False)
        if "industry_scenarios" in values:
            profile.industry_scenarios_json = json.dumps(values["industry_scenarios"], ensure_ascii=False)
        if "required_skills" in values or "preferred_skills" in values:
            await db.execute(delete(JobProfileSkill).where(JobProfileSkill.job_profile_id == profile.id))
            for requirement_type, names in (
                ("required", values.get("required_skills", [])),
                ("preferred", values.get("preferred_skills", [])),
            ):
                for name in names:
                    skill = await db.scalar(select(Skill).where(Skill.name == name))
                    if skill is None:
                        skill = Skill(name=name, category="manual", aliases_json="[]")
                        db.add(skill)
                        await db.flush()
                    db.add(
                        JobProfileSkill(
                            job_profile_id=profile.id,
                            skill_id=skill.id,
                            requirement_type=requirement_type,
                            proficiency_level="working",
                            confidence=1.0,
                            evidence_count=0,
                            prevalence=0.0,
                        )
                    )
        profile.review_status = "approved"
        await db.commit()
        await db.refresh(profile)
        return await _profile_payload(db, profile)

    @app.post("/api/analysis/rebuild")
    async def rebuild(db: AsyncSession = Depends(get_db)):
        return await rebuild_analysis(db)

    @app.post("/api/hard-metrics/rebuild")
    async def rebuild_hard_metrics(
        request: HardMetricsRunRequest,
        confirm: bool = False,
        db: AsyncSession = Depends(get_db),
    ):
        if request.mode == "full" and not confirm:
            raise HTTPException(status_code=400, detail="全量重算需要明确确认")
        backup = (
            backup_sqlite_database(ASYNC_DATABASE_URL, BACKUP_DIR)
            if request.mode == "full"
            else None
        )
        from src.hard_metrics_pipeline import run_hard_metrics_pipeline

        result = await run_hard_metrics_pipeline(
            db,
            mode=request.mode,
            family_codes=set(request.family_codes or []) or None,
        )
        return {**result, "backup_path": str(backup) if backup else None}

    @app.get("/api/hard-metrics/runs")
    async def hard_metric_runs(db: AsyncSession = Depends(get_db)):
        return await pipeline_run_history(db)

    @app.get("/api/hard-metrics/quality")
    async def hard_metric_quality(db: AsyncSession = Depends(get_db)):
        return await quality_distribution(db)

    @app.put("/api/hard-metrics/levels/{posting_id}")
    async def review_posting_level(
        posting_id: int,
        request: JobLevelReviewRequest,
        db: AsyncSession = Depends(get_db),
    ):
        try:
            payload = await persist_manual_level_review(
                db, posting_id, **request.model_dump()
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        await db.commit()
        return payload

    @app.get("/api/analysis/quarterly-profiles")
    async def quarterly_profiles(
        family_code: str | None = None,
        tech_stack: str | None = None,
        level: str | None = None,
        period_key: str | None = None,
        db: AsyncSession = Depends(get_db),
    ):
        return await list_quarterly_profiles(
            db,
            family_code=family_code,
            tech_stack=tech_stack,
            level=level,
            period_key=period_key,
        )

    @app.get("/api/acceptance/summary")
    async def acceptance(db: AsyncSession = Depends(get_db)):
        return await acceptance_summary(db, persist=False)

    @app.post("/api/extraction/jobs/{posting_id}")
    async def extract_job_with_model(
        posting_id: int,
        model: str | None = None,
        db: AsyncSession = Depends(get_db),
    ):
        posting = await db.get(JobPosting, posting_id)
        if posting is None:
            raise HTTPException(status_code=404, detail="岗位JD不存在")
        try:
            from src.structured_extraction import extract_job_with_llm

            extracted = await asyncio.to_thread(
                extract_job_with_llm, posting.job_description_raw, model
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"大模型抽取失败: {exc}") from exc
        for requirement_type in ("required_skills", "preferred_skills"):
            for item in extracted[requirement_type]:
                skill = await db.scalar(select(Skill).where(Skill.name == item["name"]))
                if skill is None:
                    skill = Skill(name=item["name"], category="llm_extracted", aliases_json="[]")
                    db.add(skill)
                    await db.flush()
                link = await db.scalar(
                    select(JobPostingSkill).where(
                        JobPostingSkill.job_posting_id == posting.id,
                        JobPostingSkill.skill_id == skill.id,
                        JobPostingSkill.requirement_type == (
                            "required" if requirement_type == "required_skills" else "preferred"
                        ),
                    )
                )
                if link is None:
                    db.add(
                        JobPostingSkill(
                            job_posting_id=posting.id,
                            skill_id=skill.id,
                            requirement_type="required" if requirement_type == "required_skills" else "preferred",
                            confidence=item["confidence"],
                            evidence_text=item["evidence"],
                        )
                    )
        if extracted["rejected_skills"]:
            db.add(
                ReviewItem(
                    entity_type="job_posting",
                    entity_id=posting.id,
                    reason="大模型抽取包含无法在原文定位的技能",
                    payload_json=json.dumps(extracted, ensure_ascii=False),
                )
            )
        await db.commit()
        return extracted

    @app.get("/api/analysis/emerging")
    async def emerging(db: AsyncSession = Depends(get_db)):
        profiles = [profile for profile in await _latest_profiles(db) if profile.status == "emerging"]
        return {"items": [await _profile_payload(db, profile) for profile in profiles], "total": len(profiles)}

    @app.get("/api/analysis/evolution/{family_code}")
    async def evolution(
        family_code: str,
        level: str | None = None,
        current_period: str | None = None,
        db: AsyncSession = Depends(get_db),
    ):
        quarterly = await family_evolution_payload(
            db, family_code, level=level, current_period=current_period
        )
        if quarterly["previous_period"] or quarterly["current_period"]:
            return quarterly
        return await family_evolution(db, family_code)

    @app.get("/api/graph")
    async def graph(
        tech_stack: str | None = Query(None),
        level: str | None = Query(None),
        family_code: str | None = Query(None),
        version: int | None = Query(None),
        scope: str = Query("draft", pattern="^(draft|published)$"),
        include_evidence: bool = Query(False),
        db: AsyncSession = Depends(get_db),
    ):
        return await graph_data(
            db,
            tech_stack=tech_stack,
            level=level,
            family_code=family_code,
            version=version,
            scope=scope,
            include_evidence=include_evidence,
        )

    @app.get("/api/graph/versions/{family_code}")
    async def graph_versions(family_code: str, db: AsyncSession = Depends(get_db)):
        profiles = list(
            (
                await db.execute(
                    select(JobProfile)
                    .where(JobProfile.family_code == family_code)
                    .order_by(JobProfile.version.desc())
                )
            )
            .scalars()
            .all()
        )
        events = list(
            (
                await db.execute(
                    select(EvolutionEvent)
                    .where(EvolutionEvent.family_code == family_code)
                    .order_by(EvolutionEvent.created_at.desc(), EvolutionEvent.id.desc())
                )
            )
            .scalars()
            .all()
        )
        return {
            "family_code": family_code,
            "items": [await _profile_payload(db, profile) for profile in profiles],
            "events": [
                {
                    "entity_type": event.entity_type,
                    "entity_key": event.entity_key,
                    "change_type": event.change_type,
                    "previous_profile_id": event.previous_profile_id,
                    "current_profile_id": event.current_profile_id,
                }
                for event in events
            ],
            "total": len(profiles),
        }

    @app.post("/api/graph/sync")
    async def sync_graph(db: AsyncSession = Depends(get_db)):
        graph = await graph_data(db)
        try:
            from src.job_graph_sync import sync_graph_to_neo4j

            result = await asyncio.to_thread(sync_graph_to_neo4j, graph)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Neo4j同步失败: {exc}") from exc
        return {"status": "synced", **result}

    @app.post("/api/resumes/parse")
    async def parse_resume(
        file: UploadFile = File(...),
        enrich: bool = Query(False),
        model: str | None = Query(None),
        db: AsyncSession = Depends(get_db),
    ):
        try:
            parsed = parse_resume_bytes(await file.read(), file.filename or "resume.txt")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if enrich and DEEPSEEK_API_KEY:
            metadata = {
                key: parsed[key] for key in ("filename", "content_hash", "raw_text")
            }
            profile = {
                key: value for key, value in parsed.items() if key not in metadata
            }
            profile = await asyncio.to_thread(
                enrich_resume_profile,
                parsed["raw_text"],
                profile,
                model=model,
            )
            parsed = {**profile, **metadata}
        record = ResumeRecord(
            filename=parsed["filename"],
            content_hash=parsed["content_hash"],
            raw_text=parsed["raw_text"],
            parsed_json=json.dumps(
                {key: value for key, value in parsed.items() if key not in {"raw_text", "filename", "content_hash"}},
                ensure_ascii=False,
            ),
        )
        db.add(record)
        await db.commit()
        await db.refresh(record)
        return {"resume_id": record.id, **parsed}

    @app.post("/api/matches/recommend")
    async def recommend_matches(
        request: JobRecommendationRequest,
        db: AsyncSession = Depends(get_db),
    ):
        try:
            return await recommend_jobs(
                db,
                resume_id=request.resume_id,
                limit=request.limit,
                levels=request.levels,
                family_codes=request.family_codes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/matches")
    async def create_match(request: MatchRequest, db: AsyncSession = Depends(get_db)):
        resume = await db.get(ResumeRecord, request.resume_id)
        profile = await db.get(JobProfile, request.job_profile_id)
        if resume is None or profile is None:
            raise HTTPException(status_code=404, detail="简历或岗位画像不存在")
        parsed_resume = _json_load(resume.parsed_json, {})
        profile_payload = await _profile_payload(db, profile)
        result = match_resume_to_job(parsed_resume, profile_payload)
        match = MatchRecord(
            resume_id=resume.id,
            job_profile_id=profile.id,
            total_score=result["total_score"],
            dimension_scores_json=json.dumps(result["dimension_scores"], ensure_ascii=False),
            gap_json=json.dumps(
                {
                    "missing_required_skills": result["missing_required_skills"],
                    "missing_preferred_skills": result["missing_preferred_skills"],
                },
                ensure_ascii=False,
            ),
            recommendations_json=json.dumps(
                {
                    "recommendations": result["recommendations"],
                    "learning_path": result["learning_path"],
                    "result": result,
                },
                ensure_ascii=False,
            ),
        )
        db.add(match)
        await db.commit()
        await db.refresh(match)
        return {"match_id": match.id, **result}

    @app.get("/api/matches/{match_id}")
    async def match_detail(match_id: int, db: AsyncSession = Depends(get_db)):
        match = await db.get(MatchRecord, match_id)
        if match is None:
            raise HTTPException(status_code=404, detail="匹配记录不存在")
        recommendations = _json_load(match.recommendations_json, {})
        saved_result = recommendations.get("result")
        if isinstance(saved_result, dict):
            return {"match_id": match.id, **saved_result}
        dimensions = _json_load(match.dimension_scores_json, {})
        return {
            "match_id": match.id,
            "total_score": match.total_score,
            "dimension_scores": dimensions,
            "dimensions": {
                key: {"score": value} for key, value in dimensions.items()
            },
            **_json_load(match.gap_json, {}),
            **recommendations,
        }

    @app.get("/api/reviews")
    async def reviews(
        status: str = "pending", db: AsyncSession = Depends(get_db)
    ):
        query = select(ReviewItem).order_by(ReviewItem.created_at.desc())
        if status != "all":
            query = query.where(ReviewItem.status == status)
        items = list((await db.execute(query)).scalars().all())
        return {
            "items": [
                {
                    "id": item.id,
                    "entity_type": item.entity_type,
                    "entity_id": item.entity_id,
                    "reason": item.reason,
                    "payload": _json_load(item.payload_json, {}),
                    "status": item.status,
                    "reviewer_note": item.reviewer_note,
                    "created_at": item.created_at.isoformat(),
                }
                for item in items
            ],
            "total": len(items),
        }

    @app.post("/api/reviews/{review_id}/decision")
    async def review_decision(
        review_id: int,
        request: ReviewDecisionRequest,
        db: AsyncSession = Depends(get_db),
    ):
        item = await db.get(ReviewItem, review_id)
        if item is None:
            raise HTTPException(status_code=404, detail="审核项不存在")
        item.status = request.decision
        item.reviewer_note = request.reviewer_note
        item.reviewed_at = datetime.now()
        if request.replacement_payload is not None:
            item.payload_json = json.dumps(request.replacement_payload, ensure_ascii=False)
        if item.entity_type == "job_profile":
            profile = await db.get(JobProfile, item.entity_id)
            if profile:
                profile.review_status = request.decision
        await db.commit()
        return {"id": item.id, "status": item.status, "reviewer_note": item.reviewer_note}

    @app.post("/api/evaluation/run")
    async def run_evaluation(
        file: UploadFile = File(...), db: AsyncSession = Depends(get_db)
    ):
        dataset_name = file.filename or "benchmark.jsonl"
        try:
            records = parse_benchmark_records(await file.read(), dataset_name)
            report = run_benchmark(records)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        evaluation_batch_id = str(uuid.uuid4())
        for result in report["results"]:
            details = {
                **result["details"],
                "evaluation_batch_id": evaluation_batch_id,
            }
            db.add(
                EvaluationRun(
                    metric_name=result["metric_name"],
                    dataset_name=dataset_name,
                    sample_count=result["sample_count"],
                    precision=result["precision"],
                    recall=result["recall"],
                    f1=result["f1"],
                    accuracy=result["accuracy"],
                    details_json=json.dumps(details, ensure_ascii=False),
                )
            )
        await db.commit()
        return {"dataset_name": dataset_name, **report}

    @app.get("/api/evaluation/summary")
    async def evaluation_summary(db: AsyncSession = Depends(get_db)):
        runs = list((await db.execute(select(EvaluationRun).order_by(EvaluationRun.created_at.desc()))).scalars().all())
        run_payloads = [
            {
                "id": run.id,
                "metric_name": run.metric_name,
                "dataset_name": run.dataset_name,
                "sample_count": run.sample_count,
                "precision": run.precision,
                "recall": run.recall,
                "f1": run.f1,
                "accuracy": run.accuracy,
                "details": _json_load(run.details_json, {}),
                "created_at": run.created_at.isoformat(),
            }
            for run in runs
        ]
        latest = {}
        latest_batch_id = (
            run_payloads[0].get("details", {}).get("evaluation_batch_id")
            if run_payloads
            else None
        )
        active_runs = (
            [
                payload
                for payload in run_payloads
                if payload.get("details", {}).get("evaluation_batch_id")
                == latest_batch_id
            ]
            if latest_batch_id
            else run_payloads
        )
        for payload in active_runs:
            latest.setdefault(payload["metric_name"], payload)
        return {
            "targets": {
                "jd_parsing_accuracy": 0.9,
                "resume_extraction_accuracy": 0.9,
                "matching_accuracy": 0.9,
                "unit_test_coverage": 0.6,
                "minimum_jd_test_cases": 100,
            },
            "latest": latest,
            "readiness": readiness_from_results(list(latest.values())),
            "runs": run_payloads,
        }

    @app.post("/api/assistant/explain")
    async def evidence_explain(
        request: EvidenceQuestionRequest, db: AsyncSession = Depends(get_db)
    ):
        evidence = list(
            (
                await db.execute(
                    select(EvidenceRecord)
                    .where(EvidenceRecord.job_family_id == request.job_family_id)
                    .order_by(EvidenceRecord.source_score.desc())
                    .limit(12)
                )
            )
            .scalars()
            .all()
        )
        if not evidence:
            raise HTTPException(status_code=422, detail="没有可用于回答的证据，系统拒绝生成")
        context = "\n".join(
            f"[E{item.id}] {item.title} | {item.publisher} | {item.evidence_summary} | {item.source_url}"
            for item in evidence
        )
        prompt = (
            "你是岗位能力分析专家。只能依据下方证据回答；每个结论必须引用[E编号]。"
            "证据不足时明确回答证据不足，不得补充常识猜测。\n\n"
            f"问题：{request.question}\n\n证据：\n{context}"
        )
        try:
            from src.llm import get_llm

            answer = get_llm(request.model).invoke(prompt).content
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"大模型不可用: {exc}") from exc
        return {
            "answer": answer,
            "evidence": [
                {"id": f"E{item.id}", "title": item.title, "source_url": item.source_url}
                for item in evidence
            ],
            "hallucination_control": "evidence_required",
        }

    return app


app = create_app()
