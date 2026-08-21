from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

from coverage import Coverage
from coverage.exceptions import CoverageException
from sqlalchemy import Integer, and_, case, cast, distinct, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from model_class.job_competency import (
    EvaluationRun,
    EvidenceRecord,
    JobPosting,
    JobProfile,
    JobProfileSkill,
)
from model_class.knowledge_base import (
    AcceptanceSnapshot,
    CollectionRun,
    EvolutionEvent,
    ImportBatch,
    JobProfileResponsibility,
    JobProfileScenario,
)
from src.competition_rules import HIGH_CONFIDENCE_THRESHOLD
from src.job_collection.coverage import load_collection_targets
from src.job_data_service import JOB_FAMILY_NAMES


ACCEPTANCE_EVALUATION_METRICS = (
    "jd_parsing",
    "resume_extraction",
    "matching",
)


def metric_status(
    current: float | int | None, target: float | int, *, minimum: bool = True
) -> dict[str, object]:
    if current is None:
        return {
            "current": None,
            "target": target,
            "gap": None,
            "status": "not_measured",
        }
    passed = current >= target if minimum else current <= target
    gap = max(0, target - current) if minimum else max(0, current - target)
    return {
        "current": current,
        "target": target,
        "gap": round(gap, 6),
        "status": "passed" if passed else "failed",
    }


def range_metric_status(
    current: int | float | None, minimum: int | float, maximum: int | float
) -> dict[str, object]:
    if current is None:
        return {
            "current": None,
            "target": [minimum, maximum],
            "gap": None,
            "status": "not_measured",
        }
    if current < minimum:
        gap = minimum - current
    elif current > maximum:
        gap = current - maximum
    else:
        gap = 0
    return {
        "current": current,
        "target": [minimum, maximum],
        "gap": round(gap, 6),
        "status": "passed" if gap == 0 else "failed",
    }


def exact_metric_status(
    current: int | float | None, target: int | float
) -> dict[str, object]:
    if current is None:
        return {
            "current": None,
            "target": target,
            "gap": None,
            "status": "not_measured",
        }
    gap = abs(current - target)
    return {
        "current": current,
        "target": target,
        "gap": round(gap, 6),
        "status": "passed" if gap == 0 else "failed",
    }


def boolean_metric_status(current: bool | None) -> dict[str, object]:
    return {
        "current": current,
        "target": True,
        "gap": None if current is None else int(not current),
        "status": (
            "not_measured" if current is None else "passed" if current else "failed"
        ),
    }


def read_coverage_total(path: Path) -> float | None:
    if not path.exists():
        return None
    coverage_data = Coverage(data_file=str(path))
    try:
        coverage_data.load()
        return round(coverage_data.report(file=io.StringIO()) / 100, 6)
    except CoverageException:
        return None


def _overall(metrics: dict[str, dict[str, object]]) -> str:
    statuses = {
        str(item["status"])
        for item in metrics.values()
        if item["status"] != "informational"
    }
    if not statuses:
        return "not_measured"
    if "not_measured" in statuses:
        return "not_measured"
    if statuses == {"passed"}:
        return "passed"
    return "failed"


async def _latest_evaluations(db: AsyncSession) -> dict[str, EvaluationRun]:
    latest_details = await db.scalar(
        select(EvaluationRun.details_json)
        .order_by(EvaluationRun.created_at.desc(), EvaluationRun.id.desc())
        .limit(1)
    )
    latest_batch_id = None
    if latest_details is not None:
        try:
            latest_batch_id = json.loads(latest_details or "{}").get(
                "evaluation_batch_id"
            )
        except (TypeError, json.JSONDecodeError):
            latest_batch_id = None

    ranked = select(
        EvaluationRun.id.label("evaluation_id"),
        func.row_number()
        .over(
            partition_by=EvaluationRun.metric_name,
            order_by=(EvaluationRun.created_at.desc(), EvaluationRun.id.desc()),
        )
        .label("position"),
    ).where(EvaluationRun.metric_name.in_(ACCEPTANCE_EVALUATION_METRICS))
    if latest_batch_id:
        batch_id = case(
            (
                func.json_valid(EvaluationRun.details_json) == 1,
                func.json_extract(
                    EvaluationRun.details_json, "$.evaluation_batch_id"
                ),
            ),
            else_=None,
        )
        ranked = ranked.where(batch_id == str(latest_batch_id))
    ranked_rows = ranked.subquery()
    rows = (
        await db.execute(
            select(EvaluationRun)
            .join(
                ranked_rows,
                ranked_rows.c.evaluation_id == EvaluationRun.id,
            )
            .where(ranked_rows.c.position == 1)
        )
    ).scalars()
    return {row.metric_name: row for row in rows}


async def _complete_profile_counts(db: AsyncSession) -> dict[str, int]:
    rows = await db.execute(
        select(JobProfile.status, func.count(JobProfile.id))
        .where(
            JobProfile.profile_kind == "quarterly",
            JobProfile.derivation_status == "active",
            JobProfile.sample_status == "ready",
            exists(
                select(1)
                .select_from(JobProfileSkill)
                .where(JobProfileSkill.job_profile_id == JobProfile.id)
            ),
            exists(
                select(1)
                .select_from(JobProfileResponsibility)
                .where(JobProfileResponsibility.job_profile_id == JobProfile.id)
            ),
            exists(
                select(1)
                .select_from(JobProfileScenario)
                .where(JobProfileScenario.job_profile_id == JobProfile.id)
            ),
            exists(
                select(1)
                .select_from(EvidenceRecord)
                .where(EvidenceRecord.job_family_id == JobProfile.family_code)
            ),
        )
        .group_by(JobProfile.status)
    )
    counts = {"emerging": 0, "existing": 0}
    for status, count in rows:
        counts[str(status)] = int(count)
    return counts


async def _sample_family_ready(db: AsyncSession) -> bool:
    quarter_number = cast(func.substr(JobProfile.period_key, 7, 1), Integer)
    quarter_index = cast(func.substr(JobProfile.period_key, 1, 4), Integer) * 4 + (
        quarter_number
    )
    slices = (
        select(
            JobProfile.family_code.label("family_code"),
            JobProfile.level.label("level"),
            quarter_index.label("quarter_index"),
        )
        .where(
            JobProfile.profile_kind == "quarterly",
            JobProfile.derivation_status == "active",
            JobProfile.sample_status == "ready",
            JobProfile.period_key.is_not(None),
            func.substr(JobProfile.period_key, 5, 2) == "-Q",
            func.substr(JobProfile.period_key, 7, 1).in_(("1", "2", "3", "4")),
        )
        .distinct()
        .subquery()
    )
    level_families = (
        select(slices.c.family_code)
        .group_by(slices.c.family_code)
        .having(
            func.count(
                distinct(
                    case(
                        (
                            ~slices.c.level.in_(("unspecified", "all")),
                            slices.c.level,
                        )
                    )
                )
            )
            >= 3
        )
        .subquery()
    )
    periods = (
        select(slices.c.family_code, slices.c.quarter_index).distinct().subquery()
    )
    previous = periods.alias("previous_period")
    current = periods.alias("current_period")
    adjacent_families = (
        select(previous.c.family_code)
        .join(
            current,
            and_(
                current.c.family_code == previous.c.family_code,
                current.c.quarter_index == previous.c.quarter_index + 1,
            ),
        )
        .distinct()
        .subquery()
    )
    return bool(
        await db.scalar(
            select(
                exists(
                    select(1)
                    .select_from(level_families)
                    .join(
                        adjacent_families,
                        adjacent_families.c.family_code
                        == level_families.c.family_code,
                    )
                )
            )
        )
    )


def _ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 6)


def _json_list(value: str | None) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def _isoformat(value) -> str | None:
    return value.isoformat() if value else None


async def _data_quality_summary(db: AsyncSession) -> dict[str, object]:
    canonical_family_codes = tuple(JOB_FAMILY_NAMES)
    usable = and_(
        JobPosting.gate_status == "valid",
        JobPosting.duplicate_of_id.is_(None),
    )
    known_family = and_(
        usable,
        JobPosting.job_family_id.in_(canonical_family_codes),
    )
    unknown_family = and_(
        usable,
        or_(
            JobPosting.job_family_id.is_(None),
            ~JobPosting.job_family_id.in_(canonical_family_codes),
        ),
    )
    canonical = and_(usable, JobPosting.provenance_status == "approved")
    known_domain = and_(
        canonical,
        JobPosting.source_domain.is_not(None),
        func.trim(JobPosting.source_domain) != "",
    )
    trusted_publication = and_(
        canonical,
        JobPosting.published_at.is_not(None),
        JobPosting.published_at_trusted.is_(True),
    )
    first_seen_available = and_(usable, JobPosting.first_seen_at.is_not(None))
    trusted_publication_or_first_seen = and_(
        usable,
        or_(
            JobPosting.first_seen_at.is_not(None),
            and_(
                JobPosting.provenance_status == "approved",
                JobPosting.published_at.is_not(None),
                JobPosting.published_at_trusted.is_(True),
            ),
        ),
    )
    totals = (
        await db.execute(
            select(
                func.count(JobPosting.id).label("raw"),
                func.sum(case((usable, 1), else_=0)).label("usable"),
                func.sum(
                    case((JobPosting.gate_status == "valid", 1), else_=0)
                ).label("valid"),
                func.sum(
                    case((JobPosting.gate_status == "duplicate", 1), else_=0)
                ).label("duplicate"),
                func.sum(
                    case((JobPosting.gate_status == "review", 1), else_=0)
                ).label("review"),
                func.sum(
                    case((JobPosting.gate_status == "quarantined", 1), else_=0)
                ).label("quarantined"),
                func.sum(case((canonical, 1), else_=0)).label("canonical"),
                func.sum(case((known_domain, 1), else_=0)).label("known_domain"),
                func.count(
                    distinct(case((canonical, JobPosting.source_type)))
                ).label("source_types"),
                func.count(
                    distinct(case((known_domain, JobPosting.source_domain)))
                ).label("source_domains"),
                func.count(
                    distinct(case((known_family, JobPosting.job_family_id)))
                ).label("families"),
                func.sum(case((unknown_family, 1), else_=0)).label(
                    "unknown_families"
                ),
                func.sum(
                    case((trusted_publication, 1), else_=0)
                ).label("trusted_publication"),
                func.sum(
                    case((first_seen_available, 1), else_=0)
                ).label("first_seen"),
                func.sum(
                    case((trusted_publication_or_first_seen, 1), else_=0)
                ).label("trusted_or_first_seen"),
                func.sum(
                    case((trusted_publication, 1), else_=0)
                ).label("evolution_eligible"),
            )
        )
    ).one()._mapping

    family_counts = list(
        await db.execute(
            select(JobPosting.job_family_id, func.count(JobPosting.id))
            .where(known_family)
            .group_by(JobPosting.job_family_id)
        )
    )
    domain_counts = list(
        await db.scalars(
            select(func.count(JobPosting.id))
            .where(known_domain)
            .group_by(JobPosting.source_domain)
        )
    )
    eligible_core_skills = (
        select(
            JobProfile.family_code.label("family_code"),
            JobProfile.id.label("profile_id"),
            JobProfileSkill.skill_id.label("skill_id"),
            func.max(
                case(
                    (JobProfileSkill.cross_source_status == "confirmed", 1),
                    else_=0,
                )
            ).label("confirmed"),
        )
        .join(JobProfile, JobProfile.id == JobProfileSkill.job_profile_id)
        .where(
            JobProfile.profile_kind == "quarterly",
            JobProfile.derivation_status == "active",
            JobProfile.sample_status == "ready",
            JobProfile.confidence >= HIGH_CONFIDENCE_THRESHOLD,
            JobProfileSkill.requirement_type.in_(("required", "core")),
            JobProfileSkill.confidence >= HIGH_CONFIDENCE_THRESHOLD,
        )
        .group_by(
            JobProfile.family_code,
            JobProfile.id,
            JobProfileSkill.skill_id,
        )
        .subquery()
    )
    core_skills = (
        await db.execute(
            select(
                func.count().label("total"),
                func.coalesce(func.sum(eligible_core_skills.c.confirmed), 0).label(
                    "confirmed"
                ),
            )
            .select_from(eligible_core_skills)
        )
    ).one()._mapping

    latest_batch = await db.scalar(
        select(ImportBatch)
        .order_by(ImportBatch.created_at.desc(), ImportBatch.id.desc())
        .limit(1)
    )
    latest_run = await db.scalar(
        select(CollectionRun).order_by(
            CollectionRun.started_at.desc(), CollectionRun.id.desc()
        ).limit(1)
    )

    raw_count = int(totals["raw"] or 0)
    usable_count = int(totals["usable"] or 0)
    canonical_count = int(totals["canonical"] or 0)
    known_domain_count = int(totals["known_domain"] or 0)
    core_skill_count = int(core_skills["total"] or 0)
    confirmed_core_skill_count = int(core_skills["confirmed"] or 0)
    metrics = {
        "raw_job_postings": raw_count,
        "usable_unique_job_postings": usable_count,
        "valid_rate": _ratio(int(totals["valid"] or 0), raw_count),
        "duplicate_rate": _ratio(int(totals["duplicate"] or 0), raw_count),
        "review_rate": _ratio(int(totals["review"] or 0), raw_count),
        "quarantine_rate": _ratio(int(totals["quarantined"] or 0), raw_count),
        "source_type_count": int(totals["source_types"] or 0),
        "source_domain_count": int(totals["source_domains"] or 0),
        "known_source_domain_job_postings": known_domain_count,
        "missing_source_domain_job_postings": canonical_count - known_domain_count,
        "source_domain_coverage": (
            _ratio(known_domain_count, canonical_count) if canonical_count else None
        ),
        "maximum_single_domain_share": _ratio(
            max((int(count) for count in domain_counts), default=0),
            known_domain_count,
        ),
        "family_coverage": int(totals["families"] or 0),
        "unknown_family_job_postings": int(totals["unknown_families"] or 0),
        "minimum_usable_samples_per_covered_family": min(
            (int(count) for _, count in family_counts), default=0
        ),
        "trusted_publication_coverage": _ratio(
            int(totals["trusted_publication"] or 0), usable_count
        ),
        "first_seen_coverage": _ratio(
            int(totals["first_seen"] or 0), usable_count
        ),
        "trusted_publication_or_first_seen_coverage": _ratio(
            int(totals["trusted_or_first_seen"] or 0), usable_count
        ),
        "cross_source_confirmed_core_skill_coverage": (
            _ratio(confirmed_core_skill_count, core_skill_count)
            if core_skill_count
            else None
        ),
        "all_high_confidence_core_skills_confirmed": (
            confirmed_core_skill_count == core_skill_count
            if core_skill_count
            else None
        ),
        "evolution_eligible_job_postings": int(totals["evolution_eligible"] or 0),
    }
    denominators = {
        name: {"metric": "raw_job_postings", "count": raw_count}
        for name in (
            "valid_rate",
            "duplicate_rate",
            "review_rate",
            "quarantine_rate",
        )
    }
    denominators.update(
        {
            "maximum_single_domain_share": {
                "metric": (
                    "canonical_usable_unique_job_postings_with_known_domain"
                ),
                "count": known_domain_count,
            },
            "source_domain_coverage": {
                "metric": "canonical_usable_unique_job_postings",
                "count": canonical_count,
            },
            "trusted_publication_coverage": {
                "metric": "usable_unique_job_postings",
                "count": usable_count,
            },
            "first_seen_coverage": {
                "metric": "usable_unique_job_postings",
                "count": usable_count,
            },
            "trusted_publication_or_first_seen_coverage": {
                "metric": "usable_unique_job_postings",
                "count": usable_count,
            },
        }
    )
    denominators["cross_source_confirmed_core_skill_coverage"] = {
        "metric": "high_confidence_core_skill_assignments",
        "count": core_skill_count,
    }

    batch_payload = None
    if latest_batch is not None:
        batch_payload = {
            "batch_id": latest_batch.batch_id,
            "filename": latest_batch.filename,
            "status": latest_batch.status,
            "raw": latest_batch.raw_lines,
            "parsed": latest_batch.parsed_lines,
            "imported": latest_batch.imported,
            "revised": latest_batch.revised,
            "review": latest_batch.review_count,
            "quarantined": latest_batch.quarantined,
            "duplicates": latest_batch.duplicates,
            "skipped": latest_batch.skipped,
            "affected_families": _json_list(latest_batch.affected_families_json),
            "created_at": _isoformat(latest_batch.created_at),
            "completed_at": _isoformat(latest_batch.completed_at),
        }
    run_payload = None
    if latest_run is not None:
        run_payload = {
            "run_id": latest_run.run_id,
            "status": latest_run.status,
            "mode": latest_run.mode,
            "source_ids": _json_list(latest_run.source_ids_json),
            "fetched": latest_run.fetched_count,
            "parsed": latest_run.parsed_count,
            "valid": latest_run.valid_count,
            "review": latest_run.review_count,
            "quarantined": latest_run.quarantined_count,
            "duplicates": latest_run.duplicate_count,
            "imported": latest_run.imported_count,
            "started_at": _isoformat(latest_run.started_at),
            "completed_at": _isoformat(latest_run.completed_at),
        }
    return {
        "metrics": metrics,
        "denominators": denominators,
        "latest_collection": {"batch": batch_payload, "run": run_payload},
    }


async def acceptance_summary(
    db: AsyncSession,
    *,
    coverage_file: Path | None = None,
    persist: bool = False,
) -> dict[str, object]:
    latest = await _latest_evaluations(db)
    coverage = read_coverage_total(
        coverage_file or Path(__file__).resolve().parents[1] / ".coverage"
    )
    complete = await _complete_profile_counts(db)
    data_quality = await _data_quality_summary(db)
    collection_targets = load_collection_targets()
    formal_events = int(
        await db.scalar(
            select(func.count())
            .select_from(EvolutionEvent)
            .where(EvolutionEvent.event_status == "formal")
        )
        or 0
    )
    minimum_metrics = {
        "jd_benchmark_cases": metric_status(
            latest.get("jd_parsing").sample_count
            if latest.get("jd_parsing")
            else None,
            100,
        ),
        "jd_parsing_accuracy": metric_status(
            latest.get("jd_parsing").accuracy if latest.get("jd_parsing") else None,
            0.90,
        ),
        "resume_extraction_accuracy": metric_status(
            latest.get("resume_extraction").accuracy
            if latest.get("resume_extraction")
            else None,
            0.90,
        ),
        "matching_accuracy": metric_status(
            latest.get("matching").accuracy if latest.get("matching") else None,
            0.90,
        ),
        "unit_test_coverage": metric_status(coverage, 0.60),
        "emerging_profile_case": metric_status(complete.get("emerging", 0), 1),
        "existing_profile_case": metric_status(complete.get("existing", 0), 1),
        "adjacent_quarter_evolution": metric_status(formal_events, 1),
    }

    quality_metrics = data_quality["metrics"]
    internal_metrics = {
        "raw_job_postings": {
            "current": quality_metrics["raw_job_postings"],
            "target": None,
            "gap": None,
            "status": "informational",
        },
        "usable_unique_job_postings": range_metric_status(
            quality_metrics["usable_unique_job_postings"],
            collection_targets.minimum_usable_unique,
            collection_targets.maximum_usable_unique,
        ),
        "job_families": exact_metric_status(
            quality_metrics["family_coverage"], 22
        ),
        "unknown_family_job_postings": {
            "current": quality_metrics["unknown_family_job_postings"],
            "target": None,
            "gap": None,
            "status": "informational",
        },
        "minimum_usable_samples_per_covered_family": metric_status(
            quality_metrics["minimum_usable_samples_per_covered_family"],
            collection_targets.minimum_usable_per_family,
        ),
        "source_types": metric_status(
            quality_metrics["source_type_count"],
            collection_targets.minimum_source_types,
        ),
        "source_domains": metric_status(
            quality_metrics["source_domain_count"],
            collection_targets.minimum_source_domains,
        ),
        "source_domain_coverage": metric_status(
            quality_metrics["source_domain_coverage"], 1.0
        ),
        "maximum_single_domain_share": metric_status(
            quality_metrics["maximum_single_domain_share"],
            collection_targets.maximum_single_domain_share,
            minimum=False,
        ),
        "trusted_publication_or_first_seen_coverage": metric_status(
            quality_metrics["trusted_publication_or_first_seen_coverage"], 0.90
        ),
        "all_high_confidence_core_skills_confirmed": boolean_metric_status(
            quality_metrics["all_high_confidence_core_skills_confirmed"]
        ),
        "sample_family_level_period_coverage": metric_status(
            int(await _sample_family_ready(db)), 1
        ),
        "jd_parsing_accuracy": metric_status(
            latest.get("jd_parsing").accuracy if latest.get("jd_parsing") else None,
            0.92,
        ),
        "resume_extraction_accuracy": metric_status(
            latest.get("resume_extraction").accuracy
            if latest.get("resume_extraction")
            else None,
            0.92,
        ),
        "matching_accuracy": metric_status(
            latest.get("matching").accuracy if latest.get("matching") else None,
            0.92,
        ),
    }
    minimum = {"overall": _overall(minimum_metrics), "metrics": minimum_metrics}
    internal = {"overall": _overall(internal_metrics), "metrics": internal_metrics}
    result = {
        "minimum": minimum,
        "internal": internal,
        "data_quality": data_quality,
    }
    if persist:
        canonical = json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        snapshot_key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        existing = await db.scalar(
            select(AcceptanceSnapshot).where(
                AcceptanceSnapshot.snapshot_key == snapshot_key
            )
        )
        if existing is None:
            db.add(
                AcceptanceSnapshot(
                    snapshot_key=snapshot_key,
                    minimum_json=json.dumps(
                        minimum, ensure_ascii=False, sort_keys=True
                    ),
                    internal_json=json.dumps(
                        internal, ensure_ascii=False, sort_keys=True
                    ),
                    overall_status=str(minimum["overall"]),
                )
            )
            await db.flush()
    return result
