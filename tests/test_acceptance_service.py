import runpy
from datetime import datetime

import pytest
import pytest_asyncio
from coverage import Coverage
from sqlalchemy import event, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from model_class.base import Base


@pytest_asyncio.fixture
async def session():
    import model_class.job_competency  # noqa: F401
    import model_class.knowledge_base  # noqa: F401

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as value:
        yield value
    await engine.dispose()


def _coverage_file(tmp_path):
    source = tmp_path / "measured_source.py"
    source.write_text("value = 1\n", encoding="utf-8")
    data_file = tmp_path / ".coverage"
    coverage = Coverage(data_file=str(data_file))
    coverage.start()
    runpy.run_path(str(source))
    coverage.stop()
    coverage.save()
    return data_file


@pytest.mark.asyncio
async def test_missing_measurements_never_report_ready(session, tmp_path):
    from src.acceptance_service import acceptance_summary

    result = await acceptance_summary(
        session, coverage_file=tmp_path / "missing.coverage"
    )

    assert result["minimum"]["overall"] == "not_measured"
    assert result["minimum"]["metrics"]["jd_parsing_accuracy"]["status"] == (
        "not_measured"
    )
    assert result["minimum"]["metrics"]["unit_test_coverage"]["status"] == (
        "not_measured"
    )
    assert result["internal"]["metrics"]["raw_job_postings"] == {
        "current": 0,
        "target": None,
        "gap": None,
        "status": "informational",
    }
    assert result["internal"]["metrics"]["usable_unique_job_postings"][
        "status"
    ] == "failed"
    assert result["data_quality"]["metrics"] == {
        "raw_job_postings": 0,
        "usable_unique_job_postings": 0,
        "valid_rate": 0.0,
        "duplicate_rate": 0.0,
        "review_rate": 0.0,
        "quarantine_rate": 0.0,
        "source_type_count": 0,
        "source_domain_count": 0,
        "known_source_domain_job_postings": 0,
        "missing_source_domain_job_postings": 0,
        "source_domain_coverage": None,
        "maximum_single_domain_share": 0.0,
        "family_coverage": 0,
        "unknown_family_job_postings": 0,
        "minimum_usable_samples_per_covered_family": 0,
        "trusted_publication_coverage": 0.0,
        "first_seen_coverage": 0.0,
        "trusted_publication_or_first_seen_coverage": 0.0,
        "cross_source_confirmed_core_skill_coverage": None,
        "all_high_confidence_core_skills_confirmed": None,
        "evolution_eligible_job_postings": 0,
    }
    assert result["internal"]["metrics"][
        "all_high_confidence_core_skills_confirmed"
    ]["status"] == "not_measured"
    assert result["data_quality"]["latest_collection"] == {
        "batch": None,
        "run": None,
    }


def test_metric_status_reports_exact_gap():
    from src.acceptance_service import metric_status, range_metric_status

    assert metric_status(0.87, 0.9) == {
        "current": 0.87,
        "target": 0.9,
        "gap": 0.03,
        "status": "failed",
    }
    assert range_metric_status(1852, 5000, 10000)["gap"] == 3148
    assert range_metric_status(6300, 5000, 10000)["status"] == "passed"


async def _complete_profile(session, family, status, version, period):
    from model_class.job_competency import EvidenceRecord, JobProfile, JobProfileSkill, Skill
    from model_class.knowledge_base import (
        IndustryScenario,
        JobProfileResponsibility,
        JobProfileScenario,
        Responsibility,
    )

    skill = await session.scalar(select(Skill).where(Skill.name == f"技能-{family}"))
    if skill is None:
        skill = Skill(name=f"技能-{family}", category="test", aliases_json="[]")
        session.add(skill)
        await session.flush()
    responsibility = await session.scalar(
        select(Responsibility).where(Responsibility.name == f"职责-{family}")
    )
    if responsibility is None:
        responsibility = Responsibility(name=f"职责-{family}")
        session.add(responsibility)
        await session.flush()
    scenario = await session.scalar(
        select(IndustryScenario).where(IndustryScenario.name == f"场景-{family}")
    )
    if scenario is None:
        scenario = IndustryScenario(name=f"场景-{family}")
        session.add(scenario)
        await session.flush()
    profile = JobProfile(
        family_code=family,
        name=family,
        description="完整岗位画像",
        responsibilities_json=f'["职责-{family}"]',
        industry_scenarios_json=f'["场景-{family}"]',
        status=status,
        level="mid",
        tech_stack="ai" if status == "emerging" else "big_data",
        version=version,
        confidence=0.9,
        review_status="approved",
        profile_kind="quarterly",
        period_key=period,
        sample_count=20,
        sample_status="ready",
        generation_key=f"{family}-{period}",
        derivation_status="active",
    )
    session.add(profile)
    await session.flush()
    session.add_all(
        [
            JobProfileSkill(
                job_profile_id=profile.id,
                skill_id=skill.id,
                requirement_type="required",
                proficiency_level="working",
                confidence=0.9,
                evidence_count=4,
                prevalence=0.2,
            ),
            JobProfileResponsibility(
                job_profile_id=profile.id,
                responsibility_id=responsibility.id,
                confidence=0.9,
                evidence_count=4,
                prevalence=0.2,
                review_status="approved",
            ),
            JobProfileScenario(
                job_profile_id=profile.id,
                scenario_id=scenario.id,
                confidence=0.9,
                evidence_count=4,
                prevalence=0.2,
                review_status="approved",
            ),
        ]
    )
    evidence = await session.scalar(
        select(EvidenceRecord).where(EvidenceRecord.evidence_id == f"E-{family}")
    )
    if evidence is None:
        session.add(
            EvidenceRecord(
                evidence_id=f"E-{family}",
                job_family_id=family,
                evidence_type="occupation_standard",
                title=f"{family}职业标准",
                publisher="权威机构",
                source_url="https://example.com/standard",
                evidence_summary="岗位能力标准证据",
                source_score=1.0,
            )
        )
    await session.flush()
    return profile


@pytest.mark.asyncio
async def test_measured_minimum_requirements_can_pass(session, tmp_path):
    from model_class.job_competency import EvaluationRun
    from model_class.knowledge_base import EvolutionEvent
    from src.acceptance_service import acceptance_summary

    emerging = await _complete_profile(
        session, "AI_AGENT_ENGINEER", "emerging", 1, "2026-Q1"
    )
    existing_q1 = await _complete_profile(
        session, "DATA_ENGINEER", "existing", 1, "2026-Q1"
    )
    existing_q2 = await _complete_profile(
        session, "DATA_ENGINEER", "existing", 2, "2026-Q2"
    )
    for metric_name, sample_count in (
        ("jd_parsing", 100),
        ("resume_extraction", 30),
        ("matching", 50),
    ):
        session.add(
            EvaluationRun(
                metric_name=metric_name,
                dataset_name="labeled.jsonl",
                sample_count=sample_count,
                precision=0.93,
                recall=0.93,
                f1=0.93,
                accuracy=0.93,
                details_json="{}",
            )
        )
    session.add(
        EvolutionEvent(
            family_code="DATA_ENGINEER",
            previous_profile_id=existing_q1.id,
            current_profile_id=existing_q2.id,
            entity_type="skill",
            entity_key="Flink",
            change_type="modified",
            evidence_count=4,
            previous_period="2026-Q1",
            current_period="2026-Q2",
            event_status="formal",
            generation_key="formal-event",
        )
    )
    await session.flush()

    result = await acceptance_summary(
        session, coverage_file=_coverage_file(tmp_path), persist=True
    )

    assert emerging.id
    assert result["minimum"]["overall"] == "passed"
    assert all(
        metric["status"] == "passed"
        for metric in result["minimum"]["metrics"].values()
    )


@pytest.mark.asyncio
async def test_identical_acceptance_snapshot_is_not_duplicated(session, tmp_path):
    from model_class.knowledge_base import AcceptanceSnapshot
    from src.acceptance_service import acceptance_summary

    coverage_file = _coverage_file(tmp_path)
    await acceptance_summary(session, coverage_file=coverage_file, persist=True)
    await acceptance_summary(session, coverage_file=coverage_file, persist=True)

    assert await session.scalar(
        select(func.count()).select_from(AcceptanceSnapshot)
    ) == 1


def _posting(index, **overrides):
    from model_class.job_competency import JobPosting

    values = {
        "record_id": f"acceptance-{index}",
        "job_family_id": "DATA_ENGINEER",
        "job_title_raw": "Data Engineer",
        "job_title_normalized": "Data Engineer",
        "company_name": f"Company {index}",
        "source_name": "Approved source",
        "source_type": "company_official",
        "source_url": f"https://a.example/jobs/{index}",
        "source_id": "source-a",
        "source_domain": "a.example",
        "provenance_status": "approved",
        "published_at": None,
        "published_at_trusted": False,
        "collected_at": datetime(2026, 8, 1),
        "first_seen_at": None,
        "job_description_raw": "Build reliable data platforms with Python and SQL.",
        "content_hash": f"hash-{index}",
        "simhash": f"{index:016x}",
        "quality_score": 0.9,
        "gate_status": "valid",
    }
    values.update(overrides)
    return JobPosting(**values)


@pytest.mark.asyncio
async def test_mixed_data_metrics_use_usable_and_canonical_denominators(session):
    from model_class.job_competency import JobProfile, JobProfileSkill, Skill
    from model_class.knowledge_base import CollectionRun, ImportBatch
    from src.acceptance_service import acceptance_summary

    postings = [
        _posting(
            1,
            published_at=datetime(2026, 1, 10),
            published_at_trusted=True,
            first_seen_at=datetime(2026, 1, 11),
        ),
        _posting(
            2,
            published_at=datetime(2026, 2, 10),
            published_at_trusted=True,
            first_seen_at=datetime(2026, 2, 11),
        ),
        _posting(
            3,
            job_family_id="JAVA_DEVELOPER",
            source_name="Approved aggregator",
            source_type="recruitment_platform",
            source_url="https://b.example/jobs/3",
            source_id="source-b",
            source_domain="b.example",
            first_seen_at=datetime(2026, 3, 11),
        ),
        _posting(
            4,
            job_family_id="JAVA_DEVELOPER",
            source_name="Unverified source",
            source_type="forged_type",
            source_url="https://forged.example/jobs/4",
            source_id="forged-source",
            source_domain="forged.example",
            provenance_status="unverified",
            published_at=datetime(2026, 4, 10),
            published_at_trusted=True,
            first_seen_at=datetime(2026, 4, 11),
        ),
        _posting(
            5,
            job_family_id="PYTHON_BACKEND",
            source_name="Approved aggregator",
            source_type="recruitment_platform",
            source_url="https://b.example/jobs/5",
            source_id="source-b",
            source_domain=None,
        ),
        _posting(6, gate_status="duplicate"),
        _posting(7, gate_status="review"),
        _posting(8, gate_status="quarantined"),
    ]
    session.add_all(postings)
    await session.flush()
    postings[5].duplicate_of_id = postings[0].id

    profile = JobProfile(
        family_code="FAMILY_A",
        name="Family A",
        profile_kind="quarterly",
        period_key="2026-Q1",
        sample_status="ready",
        derivation_status="active",
        confidence=0.9,
        generation_key="acceptance-profile",
    )
    confirmed = Skill(name="Confirmed core skill", category="test")
    single_source = Skill(name="Single-source core skill", category="test")
    session.add_all([profile, confirmed, single_source])
    await session.flush()
    session.add_all(
        [
            JobProfileSkill(
                job_profile_id=profile.id,
                skill_id=confirmed.id,
                requirement_type="required",
                confidence=0.9,
                cross_source_status="confirmed",
            ),
            JobProfileSkill(
                job_profile_id=profile.id,
                skill_id=single_source.id,
                requirement_type="required",
                confidence=0.9,
                cross_source_status="single_source",
            ),
        ]
    )
    session.add_all(
        [
            ImportBatch(
                batch_id="batch-latest",
                filename="latest.jsonl",
                file_hash="batch-hash",
                file_size=100,
                status="completed",
                raw_lines=8,
                imported=5,
                duplicates=1,
                review_count=1,
                quarantined=1,
                created_at=datetime(2026, 8, 2, 9),
                completed_at=datetime(2026, 8, 2, 10),
            ),
            CollectionRun(
                run_id="run-latest",
                source_ids_json='["source-a", "source-b"]',
                mode="dry-run",
                status="completed",
                staging_dir="data/collections/run-latest",
                fetched_count=8,
                parsed_count=8,
                valid_count=5,
                review_count=1,
                quarantined_count=1,
                duplicate_count=1,
                started_at=datetime(2026, 8, 3, 9),
                completed_at=datetime(2026, 8, 3, 10),
            ),
        ]
    )
    await session.flush()

    result = await acceptance_summary(session)

    assert result["data_quality"]["metrics"] == {
        "raw_job_postings": 8,
        "usable_unique_job_postings": 5,
        "valid_rate": 0.625,
        "duplicate_rate": 0.125,
        "review_rate": 0.125,
        "quarantine_rate": 0.125,
        "source_type_count": 2,
        "source_domain_count": 2,
        "known_source_domain_job_postings": 3,
        "missing_source_domain_job_postings": 1,
        "source_domain_coverage": 0.75,
        "maximum_single_domain_share": 0.666667,
        "family_coverage": 3,
        "unknown_family_job_postings": 0,
        "minimum_usable_samples_per_covered_family": 1,
        "trusted_publication_coverage": 0.4,
        "first_seen_coverage": 0.8,
        "trusted_publication_or_first_seen_coverage": 0.8,
        "cross_source_confirmed_core_skill_coverage": 0.5,
        "all_high_confidence_core_skills_confirmed": False,
        "evolution_eligible_job_postings": 2,
    }
    assert result["data_quality"]["denominators"] == {
        "valid_rate": {"metric": "raw_job_postings", "count": 8},
        "duplicate_rate": {"metric": "raw_job_postings", "count": 8},
        "review_rate": {"metric": "raw_job_postings", "count": 8},
        "quarantine_rate": {"metric": "raw_job_postings", "count": 8},
        "maximum_single_domain_share": {
            "metric": "canonical_usable_unique_job_postings_with_known_domain",
            "count": 3,
        },
        "source_domain_coverage": {
            "metric": "canonical_usable_unique_job_postings",
            "count": 4,
        },
        "trusted_publication_coverage": {
            "metric": "usable_unique_job_postings",
            "count": 5,
        },
        "first_seen_coverage": {
            "metric": "usable_unique_job_postings",
            "count": 5,
        },
        "trusted_publication_or_first_seen_coverage": {
            "metric": "usable_unique_job_postings",
            "count": 5,
        },
        "cross_source_confirmed_core_skill_coverage": {
            "metric": "high_confidence_core_skill_assignments",
            "count": 2,
        },
    }
    assert result["internal"]["metrics"]["raw_job_postings"]["status"] == (
        "informational"
    )
    assert result["internal"]["metrics"]["usable_unique_job_postings"] == {
        "current": 5,
        "target": [5000, 10000],
        "gap": 4995,
        "status": "failed",
    }
    assert result["data_quality"]["latest_collection"]["batch"]["batch_id"] == (
        "batch-latest"
    )
    assert result["data_quality"]["latest_collection"]["run"] == {
        "run_id": "run-latest",
        "status": "completed",
        "mode": "dry-run",
        "source_ids": ["source-a", "source-b"],
        "fetched": 8,
        "parsed": 8,
        "valid": 5,
        "review": 1,
        "quarantined": 1,
        "duplicates": 1,
        "imported": 0,
        "started_at": "2026-08-03T09:00:00",
        "completed_at": "2026-08-03T10:00:00",
    }


@pytest.mark.asyncio
async def test_usable_unique_ignores_mismatched_legacy_status(session):
    from src.acceptance_service import acceptance_summary

    session.add(_posting(101, status="review", gate_status="valid"))
    await session.flush()

    result = await acceptance_summary(session)

    assert result["data_quality"]["metrics"]["usable_unique_job_postings"] == 1


@pytest.mark.asyncio
async def test_core_skill_coverage_excludes_incidental_and_low_confidence_rows(session):
    from model_class.job_competency import JobProfile, JobProfileSkill, Skill
    from src.acceptance_service import acceptance_summary

    eligible = JobProfile(
        family_code="ELIGIBLE",
        name="Eligible",
        profile_kind="quarterly",
        period_key="2026-Q1",
        sample_status="ready",
        derivation_status="active",
        confidence=0.9,
        generation_key="eligible-core-profile",
    )
    low_sample = JobProfile(
        family_code="LOW_SAMPLE",
        name="Low sample",
        profile_kind="quarterly",
        period_key="2026-Q1",
        sample_status="low_sample",
        derivation_status="active",
        confidence=0.9,
        generation_key="low-sample-core-profile",
    )
    low_confidence = JobProfile(
        family_code="LOW_CONFIDENCE",
        name="Low confidence",
        profile_kind="quarterly",
        period_key="2026-Q1",
        sample_status="ready",
        derivation_status="active",
        confidence=0.69,
        generation_key="low-confidence-core-profile",
    )
    skills = [
        Skill(name=f"Acceptance skill {index}", category="test")
        for index in range(5)
    ]
    session.add_all([eligible, low_sample, low_confidence, *skills])
    await session.flush()
    session.add_all(
        [
            JobProfileSkill(
                job_profile_id=eligible.id,
                skill_id=skills[0].id,
                requirement_type="required",
                confidence=0.9,
                cross_source_status="confirmed",
            ),
            JobProfileSkill(
                job_profile_id=low_sample.id,
                skill_id=skills[1].id,
                requirement_type="required",
                confidence=0.9,
                cross_source_status="single_source",
            ),
            JobProfileSkill(
                job_profile_id=low_confidence.id,
                skill_id=skills[2].id,
                requirement_type="required",
                confidence=0.9,
                cross_source_status="single_source",
            ),
            JobProfileSkill(
                job_profile_id=eligible.id,
                skill_id=skills[3].id,
                requirement_type="preferred",
                confidence=0.9,
                cross_source_status="single_source",
            ),
            JobProfileSkill(
                job_profile_id=eligible.id,
                skill_id=skills[4].id,
                requirement_type="required",
                confidence=0.69,
                cross_source_status="single_source",
            ),
        ]
    )
    await session.flush()

    result = await acceptance_summary(session)

    metrics = result["data_quality"]["metrics"]
    denominator = result["data_quality"]["denominators"][
        "cross_source_confirmed_core_skill_coverage"
    ]
    assert metrics["cross_source_confirmed_core_skill_coverage"] == 1.0
    assert metrics["all_high_confidence_core_skills_confirmed"] is True
    assert denominator == {
        "metric": "high_confidence_core_skill_assignments",
        "count": 1,
    }


@pytest.mark.asyncio
async def test_internal_readiness_requires_every_design_quality_goal(session, tmp_path):
    from model_class.job_competency import (
        EvaluationRun,
        JobPosting,
        JobProfile,
        JobProfileSkill,
        Skill,
    )
    from src.acceptance_service import acceptance_summary
    from src.job_data_service import JOB_FAMILY_NAMES

    family_codes = tuple(JOB_FAMILY_NAMES)
    assert len(family_codes) == 22
    postings = []
    for index in range(5000):
        postings.append(
            {
                "record_id": f"ready-{index}",
                "job_family_id": family_codes[index % len(family_codes)],
                "job_title_raw": "Data Engineer",
                "job_title_normalized": "Data Engineer",
                "company_name": f"Company {index}",
                "source_name": "Approved source",
                "source_type": f"type-{index % 3}",
                "source_url": f"https://domain-{index % 8}.example/jobs/{index}",
                "source_domain": f"domain-{index % 8}.example",
                "provenance_status": "approved",
                "published_at": datetime(2026, 1, 1),
                "published_at_trusted": True,
                "collected_at": datetime(2026, 8, 1),
                "first_seen_at": None,
                "job_description_raw": "Build reliable data systems with Python and SQL.",
                "content_hash": f"ready-hash-{index}",
                "simhash": f"{index:016x}",
                "gate_status": "valid",
            }
        )
    await session.execute(insert(JobPosting), postings)
    session.add_all(
        [
            EvaluationRun(
                metric_name=name,
                dataset_name="ready.jsonl",
                sample_count=100,
                accuracy=0.93,
            )
            for name in ("jd_parsing", "resume_extraction", "matching")
        ]
    )
    profiles = []
    version = 1
    for period in ("2026-Q1", "2026-Q2"):
        for level in ("junior", "mid", "senior"):
            profiles.append(
                JobProfile(
                    family_code=family_codes[0],
                    name=JOB_FAMILY_NAMES[family_codes[0]],
                    profile_kind="quarterly",
                    period_key=period,
                    level=level,
                    version=version,
                    sample_status="ready",
                    derivation_status="active",
                    confidence=0.9,
                    generation_key=f"ready-{period}-{level}",
                )
            )
            version += 1
    core = Skill(name="Ready confirmed core", category="test")
    session.add_all([*profiles, core])
    await session.flush()
    session.add(
        JobProfileSkill(
            job_profile_id=profiles[0].id,
            skill_id=core.id,
            requirement_type="required",
            confidence=0.9,
            cross_source_status="confirmed",
        )
    )
    await session.flush()

    result = await acceptance_summary(session, coverage_file=_coverage_file(tmp_path))

    internal = result["internal"]
    assert internal["overall"] == "passed"
    expected = {
        "usable_unique_job_postings": [5000, 10000],
        "job_families": 22,
        "minimum_usable_samples_per_covered_family": 100,
        "source_types": 3,
        "source_domains": 8,
        "source_domain_coverage": 1.0,
        "maximum_single_domain_share": 0.35,
        "trusted_publication_or_first_seen_coverage": 0.9,
        "all_high_confidence_core_skills_confirmed": True,
    }
    for name, target in expected.items():
        assert internal["metrics"][name]["target"] == target
        assert internal["metrics"][name]["status"] == "passed"
    assert internal["metrics"]["raw_job_postings"]["status"] == "informational"
    assert internal["metrics"]["unknown_family_job_postings"] == {
        "current": 0,
        "target": None,
        "gap": None,
        "status": "informational",
    }


@pytest.mark.parametrize(
    ("current", "status", "gap"),
    [
        (4999, "failed", 1),
        (5000, "passed", 0),
        (10000, "passed", 0),
        (10001, "failed", 1),
    ],
)
def test_configured_usable_range_boundaries(current, status, gap):
    from src.acceptance_service import range_metric_status
    from src.job_collection.coverage import load_collection_targets

    targets = load_collection_targets()
    result = range_metric_status(
        current,
        targets.minimum_usable_unique,
        targets.maximum_usable_unique,
    )

    assert result["target"] == [5000, 10000]
    assert result["status"] == status
    assert result["gap"] == gap


@pytest.mark.asyncio
async def test_invented_family_codes_cannot_satisfy_family_readiness(session):
    from model_class.job_competency import JobPosting
    from src.acceptance_service import acceptance_summary

    postings = []
    for index in range(5000):
        postings.append(
            {
                "record_id": f"invented-{index}",
                "job_family_id": f"INVENTED_{index % 22:02d}",
                "job_title_raw": "Invented Engineer",
                "job_title_normalized": "Invented Engineer",
                "company_name": f"Company {index}",
                "source_name": "Approved source",
                "source_type": f"type-{index % 3}",
                "source_url": f"https://domain-{index % 8}.example/jobs/{index}",
                "source_domain": f"domain-{index % 8}.example",
                "provenance_status": "approved",
                "published_at": datetime(2026, 1, 1),
                "published_at_trusted": True,
                "collected_at": datetime(2026, 8, 1),
                "job_description_raw": "Invented family must not count.",
                "content_hash": f"invented-hash-{index}",
                "simhash": f"{index:016x}",
                "gate_status": "valid",
            }
        )
    await session.execute(insert(JobPosting), postings)
    await session.flush()

    result = await acceptance_summary(session)
    quality = result["data_quality"]["metrics"]
    internal = result["internal"]["metrics"]

    assert quality["family_coverage"] == 0
    assert quality["unknown_family_job_postings"] == 5000
    assert quality["minimum_usable_samples_per_covered_family"] == 0
    assert internal["job_families"]["status"] == "failed"
    assert internal["minimum_usable_samples_per_covered_family"]["status"] == (
        "failed"
    )


@pytest.mark.asyncio
async def test_source_domain_completeness_is_strict_and_separate_from_concentration(
    session,
):
    from model_class.job_competency import JobPosting
    from src.acceptance_service import acceptance_summary

    postings = []
    for index in range(5000):
        has_domain = index < 4992
        domain = f"domain-{index % 8}.example" if has_domain else None
        postings.append(
            {
                "record_id": f"domain-coverage-{index}",
                "job_family_id": "DATA_ENGINEER",
                "job_title_raw": "Data Engineer",
                "job_title_normalized": "Data Engineer",
                "company_name": f"Company {index}",
                "source_name": "Approved source",
                "source_type": "company_official",
                "source_url": f"https://example.com/jobs/{index}",
                "source_domain": domain,
                "provenance_status": "approved",
                "collected_at": datetime(2026, 8, 1),
                "job_description_raw": "Build reliable data systems.",
                "content_hash": f"domain-coverage-hash-{index}",
                "simhash": f"{index:016x}",
                "gate_status": "valid",
            }
        )
    await session.execute(insert(JobPosting), postings)
    await session.flush()

    result = await acceptance_summary(session)
    quality = result["data_quality"]["metrics"]
    internal = result["internal"]["metrics"]

    assert quality["known_source_domain_job_postings"] == 4992
    assert quality["missing_source_domain_job_postings"] == 8
    assert quality["source_domain_coverage"] == 0.9984
    assert quality["maximum_single_domain_share"] == 0.125
    assert internal["source_domain_coverage"] == {
        "current": 0.9984,
        "target": 1.0,
        "gap": 0.0016,
        "status": "failed",
    }


@pytest.mark.asyncio
async def test_latest_collection_uses_id_to_break_timestamp_ties(session):
    from model_class.knowledge_base import CollectionRun, ImportBatch
    from src.acceptance_service import acceptance_summary

    timestamp = datetime(2026, 8, 4, 9)
    session.add_all(
        [
            ImportBatch(
                batch_id="batch-old-tie",
                filename="old.jsonl",
                file_hash="old-tie-hash",
                file_size=1,
                created_at=timestamp,
            ),
            ImportBatch(
                batch_id="batch-new-tie",
                filename="new.jsonl",
                file_hash="new-tie-hash",
                file_size=1,
                created_at=timestamp,
            ),
            CollectionRun(
                run_id="run-old-tie",
                mode="dry-run",
                staging_dir="old",
                started_at=timestamp,
            ),
            CollectionRun(
                run_id="run-new-tie",
                mode="dry-run",
                staging_dir="new",
                started_at=timestamp,
            ),
        ]
    )
    await session.flush()

    result = await acceptance_summary(session)

    latest = result["data_quality"]["latest_collection"]
    assert latest["batch"]["batch_id"] == "batch-new-tie"
    assert latest["run"]["run_id"] == "run-new-tie"


@pytest.mark.asyncio
async def test_acceptance_loads_bounded_history_and_no_profile_entities(session):
    from model_class.job_competency import EvaluationRun, JobProfile
    from src.acceptance_service import acceptance_summary

    session.add_all(
        [
            EvaluationRun(
                metric_name=("jd_parsing", "resume_extraction", "matching")[
                    index % 3
                ],
                dataset_name=f"history-{index}.jsonl",
                sample_count=100,
                accuracy=0.93,
                created_at=datetime(2026, 1, 1),
            )
            for index in range(180)
        ]
        + [
            JobProfile(
                family_code=f"ROW_BOUND_{index}",
                name=f"Row bound {index}",
                profile_kind="quarterly",
                period_key="2026-Q1",
                sample_status="ready",
                derivation_status="active",
                generation_key=f"row-bound-{index}",
            )
            for index in range(180)
        ]
    )
    await session.commit()
    session.expunge_all()
    loaded = []

    def track_load(_session, instance):
        loaded.append(instance)

    event.listen(session.sync_session, "loaded_as_persistent", track_load)
    try:
        await acceptance_summary(session)
    finally:
        event.remove(session.sync_session, "loaded_as_persistent", track_load)

    assert sum(isinstance(item, EvaluationRun) for item in loaded) <= 3
    assert not any(isinstance(item, JobProfile) for item in loaded)


@pytest.mark.asyncio
async def test_acceptance_query_count_is_bounded_by_dataset_size(session):
    from model_class.job_competency import JobProfile
    from src.acceptance_service import acceptance_summary

    session.add_all(
        [
            JobProfile(
                family_code=f"SCALE_{index}",
                name=f"Scale {index}",
                profile_kind="quarterly",
                period_key="2026-Q1",
                sample_status="ready",
                derivation_status="active",
                generation_key=f"scale-profile-{index}",
            )
            for index in range(50)
        ]
    )
    await session.flush()
    statements = []

    def count_statement(*_args):
        statements.append(1)

    engine = session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        await acceptance_summary(session)
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)

    assert len(statements) <= 12
