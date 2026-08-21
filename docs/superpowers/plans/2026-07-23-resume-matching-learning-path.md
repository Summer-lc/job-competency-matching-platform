# Resume Matching and Learning Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an evidence-grounded resume parser, deterministic seven-dimension matching engine, idempotent Top 5 job recommendation flow, and personalized 30/60/90-day learning path.

**Architecture:** Keep file extraction and rule parsing deterministic, then optionally enrich only evidence-grounded fields through DeepSeek. Normalize skills through a shared ontology, score every job with a versioned deterministic engine, persist recommendation runs by input signature, and generate learning paths from scored gaps and prerequisite relations. Existing parse and match endpoints remain compatible.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy async, SQLite/MySQL-compatible models, pypdf, python-docx, LangChain OpenAI-compatible DeepSeek client, pytest, vanilla HTML/CSS/JavaScript.

**Repository note:** This workspace has no `.git` directory. Each task ends with a verification checkpoint instead of a commit; no repository initialization is part of this plan.

---

## File Structure

- Create `src/skill_ontology.py`: canonical names, aliases, related skills, prerequisites and proficiency ordering.
- Create `src/resume_enrichment_service.py`: evidence-grounded DeepSeek augmentation with deterministic fallback.
- Create `src/learning_path_service.py`: gap ordering and 30/60/90-day plan generation.
- Create `src/job_recommendation_service.py`: candidate loading, stable ranking, deduplication and recommendation persistence.
- Modify `src/resume_service.py`: section-aware resume profile, merged work timeline and evidence objects.
- Modify `src/matching_service.py`: seven-dimension versioned scoring, caps, confidence and explanations.
- Modify `model_class/job_competency.py`: recommendation run and ranked result models.
- Modify `schemes/job_competency.py`: recommendation request schema.
- Modify `src/api.py`: hybrid parse, recommendation and stored match-detail endpoints.
- Modify `src/evaluation_service.py`: optional recommendation ranking metrics and richer resume checks.
- Modify `index.html`: capability profile, Top 5 recommendations, seven-dimension evidence and phased path.
- Modify `data/benchmark/README.md`, `README.md`, `USER_GUIDE.md`: updated behavior and benchmark format.
- Create focused tests for each new module and extend API, evaluation, model and UI regression tests.

---

### Task 1: Shared Skill Ontology

**Files:**
- Create: `src/skill_ontology.py`
- Test: `tests/test_skill_ontology.py`
- Modify: `src/job_data_service.py`

- [ ] **Step 1: Write failing normalization and relationship tests**

```python
def test_aliases_normalize_to_canonical_skill():
    from src.skill_ontology import normalize_skill

    assert normalize_skill("K8s")["name"] == "Kubernetes"
    assert normalize_skill("SpringBoot")["name"] == "Spring Boot"
    assert normalize_skill("Postgres")["name"] == "PostgreSQL"


def test_relationships_distinguish_related_and_prerequisite():
    from src.skill_ontology import skill_relationship

    assert skill_relationship("Docker", "Kubernetes") == "prerequisite"
    assert skill_relationship("MySQL", "PostgreSQL") == "related"
    assert skill_relationship("Java", "Java") == "exact"
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `pytest tests/test_skill_ontology.py -q --no-cov`  
Expected: FAIL with `ModuleNotFoundError: src.skill_ontology`.

- [ ] **Step 3: Implement the ontology API**

Create constants for canonical skill metadata, aliases, related pairs, prerequisites and proficiency ranks. Expose deterministic functions:

```python
ONTOLOGY_VERSION = "skill-ontology-v1"
PROFICIENCY_RANK = {"aware": 1, "working": 2, "advanced": 3, "expert": 4}


def normalize_skill(value: str) -> dict[str, str | None]:
    """Return canonical name, category and original alias."""


def skill_relationship(candidate: str, target: str) -> str:
    """Return exact, related, prerequisite or none."""


def prerequisite_chain(skill: str) -> list[str]:
    """Return stable, de-duplicated prerequisites."""
```

Move the existing skill catalog values from `job_data_service.py` into this module and import them back as `SKILL_CATALOG`, so JD extraction and resume matching use one vocabulary.

- [ ] **Step 4: Verify focused and JD extraction tests**

Run: `pytest tests/test_skill_ontology.py tests/test_job_data_service.py -q --no-cov`  
Expected: PASS.

- [ ] **Step 5: Checkpoint**

Record the passing command and confirm no skill extraction behavior was removed.

---

### Task 2: Resume Parser V2

**Files:**
- Modify: `src/resume_service.py`
- Test: `tests/test_resume_profile_v2.py`
- Modify: `tests/test_resume_matching.py`

- [ ] **Step 1: Write failing section, timeline and evidence tests**

```python
def test_resume_parser_builds_sections_and_merged_work_timeline():
    from src.resume_service import parse_resume_text

    text = """工作经历
2020.01-2022.12 甲公司 Java工程师，使用Spring Boot开发订单系统
2022.07-2024.06 乙公司 高级工程师，使用K8s部署服务
教育经历
2016.09-2020.06 本科
"""
    parsed = parse_resume_text(text, reference_date=date(2026, 7, 23))
    assert parsed["schema_version"] == "resume-profile-v2"
    assert parsed["experience_months"] == 54
    assert parsed["experience_years"] == 4.5
    assert len(parsed["work_experiences"]) == 2
    assert parsed["skills"][0]["evidence"]


def test_education_duration_is_not_work_experience():
    from src.resume_service import parse_resume_text

    parsed = parse_resume_text("2018-2022 本科四年，掌握Python")
    assert parsed["experience_years"] == 0


def test_project_skill_has_stronger_evidence_than_skill_list_only():
    from src.resume_service import parse_resume_text

    parsed = parse_resume_text(
        "专业技能：Docker\n项目经历：使用Docker完成服务容器化，部署20个服务。"
    )
    docker = next(item for item in parsed["skills"] if item["name"] == "Docker")
    assert "project" in docker["evidence_sources"]
    assert docker["proficiency"] in {"working", "advanced"}
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `pytest tests/test_resume_profile_v2.py -q --no-cov`  
Expected: FAIL because V2 fields and timeline merging are absent.

- [ ] **Step 3: Implement section-aware parsing**

Add pure helpers to `resume_service.py`:

```python
RESUME_SCHEMA_VERSION = "resume-profile-v2"
SECTION_NAMES = {
    "专业技能": "skills", "技能": "skills", "工作经历": "work",
    "工作经验": "work", "项目经历": "projects", "项目经验": "projects",
    "教育经历": "education", "教育背景": "education", "证书": "certificates",
}


def split_resume_sections(text: str) -> list[dict]:
    sections = [{"name": "summary", "lines": []}]
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        heading = SECTION_NAMES.get(line.rstrip("：:"))
        if heading:
            sections.append({"name": heading, "lines": []})
        else:
            sections[-1]["lines"].append(line)
    return sections


def merge_month_ranges(ranges: list[tuple[date, date]]) -> int:
    months = set()
    for start, end in ranges:
        cursor = start.year * 12 + start.month - 1
        final = end.year * 12 + end.month - 1
        months.update(range(cursor, final + 1))
    return len(months)


def parse_resume_text(text: str, reference_date: date | None = None) -> dict:
    reference_date = reference_date or date.today()
    sections = split_resume_sections(text)
    work = extract_work_experiences(sections, reference_date)
    projects = extract_projects(sections)
    experience_months = merge_month_ranges(
        [(item["start_date"], item["end_date"]) for item in work if item["start_date"] and item["end_date"]]
    )
    return {
        "schema_version": RESUME_SCHEMA_VERSION,
        "parser_mode": "rules",
        "sections": sections,
        "work_experiences": work,
        "project_experiences": projects,
        "experience_months": experience_months,
        "experience_years": round(experience_months / 12, 2),
        "skills": build_skill_evidence(sections, work, projects),
        "parse_warnings": [],
    }
```

Implement `parse_date_range`, `extract_work_experiences`, `extract_projects` and `build_skill_evidence` as pure helpers used by the complete `parse_resume_text` flow above. `parse_date_range` accepts `YYYY.MM`/`YYYY-MM`/Chinese year-month ranges and “至今”; experience extraction reads only `work` sections; project extraction reads only `projects` sections; skill evidence is normalized through Task 1 and carries source section and verbatim text.

Keep legacy keys `skills`, `recent_skills`, `experience_years`, `projects`, `education` and `evidence_count`. Add `schema_version`, `parser_mode`, `sections`, `experience_months`, `work_experiences`, `project_experiences`, `parse_warnings` and evidence-rich skill fields. Use an optional `reference_date` argument for deterministic tests.

- [ ] **Step 4: Verify parser compatibility**

Run: `pytest tests/test_resume_profile_v2.py tests/test_resume_matching.py -q --no-cov`  
Expected: PASS for V2 behavior and existing PDF/DOCX behavior.

- [ ] **Step 5: Checkpoint**

Confirm PDF, DOCX, TXT and Markdown still produce the legacy keys consumed by current APIs.

---

### Task 3: Evidence-Grounded DeepSeek Enrichment

**Files:**
- Create: `src/resume_enrichment_service.py`
- Test: `tests/test_resume_enrichment_service.py`
- Modify: `src/structured_extraction.py`

- [ ] **Step 1: Write failing evidence and fallback tests**

```python
def test_enrichment_accepts_only_verbatim_evidence():
    from src.resume_enrichment_service import validate_resume_enrichment

    text = "负责订单服务，将接口延迟降低30%，使用Java和Redis。"
    payload = {
        "skills": [
            {"name": "Redis", "evidence": "使用Java和Redis"},
            {"name": "Kafka", "evidence": "使用Kafka"},
        ],
        "achievements": [
            {"text": "接口延迟降低30%", "evidence": "将接口延迟降低30%"}
        ],
    }
    result = validate_resume_enrichment(payload, text)
    assert [item["name"] for item in result["skills"]] == ["Redis"]
    assert result["rejected"][0]["reason"] == "evidence_not_found"


def test_model_failure_returns_unchanged_rule_profile():
    from src.resume_enrichment_service import enrich_resume_profile

    profile = {"skills": [{"name": "Java"}], "parser_mode": "rules"}
    result = enrich_resume_profile("Java项目", profile, invoke=lambda _: (_ for _ in ()).throw(RuntimeError()))
    assert result["skills"] == profile["skills"]
    assert result["parser_mode"] == "rules"
    assert "model_unavailable" in result["parse_warnings"]
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `pytest tests/test_resume_enrichment_service.py -q --no-cov`  
Expected: FAIL with missing enrichment module.

- [ ] **Step 3: Implement strict enrichment and merge**

Create:

```python
ENRICHMENT_VERSION = "resume-enrichment-v1"


def validate_resume_enrichment(payload: dict, source_text: str) -> dict:
    source_normalized = _normalized(source_text)
    accepted = {"skills": [], "achievements": []}
    rejected = []
    for field in accepted:
        for raw in payload.get(field, []) if isinstance(payload.get(field), list) else []:
            item = raw if isinstance(raw, dict) else {}
            evidence = str(item.get("evidence", "")).strip()
            if not evidence or _normalized(evidence) not in source_normalized:
                rejected.append({**item, "reason": "evidence_not_found"})
            else:
                accepted[field].append(item)
    return {**accepted, "rejected": rejected}


def merge_resume_enrichment(profile: dict, accepted: dict) -> dict:
    merged = deepcopy(profile)
    existing = {item["name"]: item for item in merged.get("skills", [])}
    for item in accepted["skills"]:
        normalized = normalize_skill(str(item.get("name", "")))
        if normalized["name"] and normalized["name"] not in existing:
            merged.setdefault("skills", []).append({
                "name": normalized["name"], "aliases": [item["name"]],
                "proficiency": item.get("proficiency", "working"),
                "evidence": [item["evidence"]], "confidence": 0.75,
            })
    merged.setdefault("achievements", []).extend(accepted["achievements"])
    merged["parser_mode"] = "hybrid"
    merged["enrichment_version"] = ENRICHMENT_VERSION
    return merged


def enrich_resume_profile(
    source_text: str,
    profile: dict,
    *,
    model: str | None = None,
    invoke: Callable[[str], object] | None = None,
) -> dict:
    invoke = invoke or (lambda prompt: get_llm(model).invoke(prompt))
    try:
        response = invoke(build_resume_prompt(source_text, profile))
        raw = response.content if hasattr(response, "content") else str(response)
        accepted = validate_resume_enrichment(parse_llm_json(raw), source_text)
        return merge_resume_enrichment(profile, accepted)
    except Exception:
        fallback = deepcopy(profile)
        fallback["parser_mode"] = "rules"
        fallback.setdefault("parse_warnings", []).append("model_unavailable")
        return fallback
```

Use `parse_llm_json` and the same normalized continuous-evidence check as JD extraction. The prompt must prohibit inference of personal attributes and require strict JSON. Catch configuration, timeout, parse and validation errors; append a stable warning code and return the rule profile.

- [ ] **Step 4: Verify enrichment tests**

Run: `pytest tests/test_resume_enrichment_service.py tests/test_structured_extraction.py -q --no-cov`  
Expected: PASS.

- [ ] **Step 5: Checkpoint**

Confirm the test suite never calls the network and all model behavior is injected.

---

### Task 4: Seven-Dimension Matching Engine

**Files:**
- Modify: `src/matching_service.py`
- Test: `tests/test_matching_engine_v2.py`
- Modify: `tests/test_resume_matching.py`

- [ ] **Step 1: Write failing scoring, cap and compatibility tests**

```python
def test_v2_scores_seven_dimensions_and_preserves_total():
    from src.matching_service import MATCHING_WEIGHTS, match_resume_to_job

    resume = {
        "skills": [{"name": "Java", "proficiency": "advanced", "evidence_sources": ["project"]}],
        "experience_years": 6,
        "project_experiences": [{"name": "订单平台", "skills": ["Java"], "achievements": ["延迟降低30%"]}],
    }
    profile = {
        "name": "高级Java工程师", "level": "senior",
        "required_skills": ["Java"], "preferred_skills": ["Docker"],
        "responsibilities": ["平台开发"], "industry_scenarios": ["电商"],
    }
    result = match_resume_to_job(resume, profile)
    assert set(result["dimension_scores"]) == set(MATCHING_WEIGHTS)
    assert sum(item["score"] for item in result["dimensions"].values()) == result["total_score"]
    assert result["scoring_version"] == "evidence-match-v2"
    assert result["match_band"] in {"high", "medium", "low"}


def test_missing_majority_of_required_skills_caps_score_below_medium():
    from src.matching_service import match_resume_to_job

    result = match_resume_to_job(
        {"skills": [{"name": "Docker"}], "projects": []},
        {"required_skills": ["Java", "MySQL", "Kubernetes"], "preferred_skills": ["Docker"]},
    )
    assert result["total_score"] <= 59
    assert "required_coverage_below_half" in result["score_caps"]


def test_related_skill_receives_partial_credit_but_remains_a_gap():
    from src.matching_service import match_resume_to_job

    result = match_resume_to_job(
        {"skills": [{"name": "PostgreSQL", "proficiency": "advanced"}]},
        {"required_skills": ["MySQL"]},
    )
    assert 0 < result["dimensions"]["required_skill_coverage"]["score"] < 30
    assert result["missing_required_skills"] == ["MySQL"]
    assert result["transferable_skills"][0]["relationship"] == "related"
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `pytest tests/test_matching_engine_v2.py -q --no-cov`  
Expected: FAIL because the old five-dimension engine lacks V2 output.

- [ ] **Step 3: Implement versioned seven-dimension scoring**

Replace the weight map with:

```python
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
```

Build one scorer per dimension. Normalize legacy resumes before scoring. Return both the legacy flat `dimension_scores` values and rich `dimensions` objects. Apply caps only after raw score calculation. Add `match_band`, `confidence`, `confidence_reasons`, `score_caps`, `positive_factors`, `negative_factors`, `transferable_skills` and version fields.

Map profile levels to minimum experience with a stable policy: junior 0, mid 3, senior 5, expert 8, unspecified 0. Prefer explicit `required_years` when it is greater than zero.

- [ ] **Step 4: Verify matching and evaluation compatibility**

Run: `pytest tests/test_matching_engine_v2.py tests/test_resume_matching.py tests/test_evaluation_service.py -q --no-cov`  
Expected: PASS. Existing high/medium/low fixtures may be adjusted only when the new evidence rules intentionally change their expected band.

- [ ] **Step 5: Checkpoint**

Confirm the same payload produces byte-for-byte equal scores and stable factor ordering on repeated calls.

---

### Task 5: Personalized Learning Path

**Files:**
- Create: `src/learning_path_service.py`
- Test: `tests/test_learning_path_service.py`
- Modify: `src/matching_service.py`

- [ ] **Step 1: Write failing prerequisite, phase and score-uplift tests**

```python
def test_learning_path_orders_prerequisite_before_target_skill():
    from src.learning_path_service import build_learning_path

    path = build_learning_path(
        gaps=[{"skill": "Kubernetes", "priority": "core", "max_uplift": 12}],
        resume_skills=[],
        current_score=55,
    )
    names = [node["skill"] for phase in path["phases"] for node in phase["nodes"]]
    assert names.index("Docker") < names.index("Kubernetes")


def test_learning_path_has_verifiable_30_60_90_day_phases():
    from src.learning_path_service import build_learning_path

    gaps = [
        {"skill": "Kubernetes", "priority": "core", "max_uplift": 12},
        {"skill": "Prometheus", "priority": "preferred", "max_uplift": 4},
    ]
    path = build_learning_path(gaps, ["Java"], current_score=62)
    assert [phase["period"] for phase in path["phases"]] == ["0-30", "31-60", "61-90"]
    assert all(node["completion_criteria"] for phase in path["phases"] for node in phase["nodes"])
    assert path["project"]["deliverables"]
    assert path["projected_score"] <= 100
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `pytest tests/test_learning_path_service.py -q --no-cov`  
Expected: FAIL with missing learning path module.

- [ ] **Step 3: Implement deterministic path generation**

Expose:

```python
LEARNING_PATH_VERSION = "learning-path-v2"


def build_learning_path(
    gaps: list[dict],
    resume_skills: Iterable[str],
    current_score: float,
    *,
    evidence_records: list[dict] | None = None,
) -> dict:
    evidence_records = evidence_records or []
    owned = {normalize_skill(item)["name"] for item in resume_skills}
    ordered = order_gaps_with_prerequisites(gaps, owned)
    phases = allocate_learning_phases(ordered)
    uplift = sum(float(item.get("max_uplift", 0)) for item in ordered)
    return {
        "version": LEARNING_PATH_VERSION,
        "phases": phases,
        "project": build_integrated_project(ordered),
        "projected_score": round(min(100.0, current_score + uplift), 2),
        "evidence": evidence_for_skills(ordered, evidence_records),
    }
```

Insert missing prerequisites, remove skills already possessed, group foundational nodes into days 0-30, target capabilities into days 31-60, and an integrated project into days 61-90. Every node includes reason, reusable skills, prerequisite skills, tasks, project task, completion criteria, estimated uplift and available external evidence. Never emit a URL that is not present in `evidence_records`.

- [ ] **Step 4: Integrate the path into matching output**

Convert matching gaps into learning-path inputs and preserve legacy `learning_path` as a flattened node list while returning `learning_plan` as the phased V2 structure.

- [ ] **Step 5: Verify focused tests**

Run: `pytest tests/test_learning_path_service.py tests/test_matching_engine_v2.py tests/test_resume_matching.py -q --no-cov`  
Expected: PASS.

- [ ] **Step 6: Checkpoint**

Confirm path order and projected score are deterministic and no fabricated links appear.

---

### Task 6: Recommendation Persistence Models

**Files:**
- Modify: `model_class/job_competency.py`
- Modify: `model_class/__init__.py` if exports are used
- Test: `tests/test_recommendation_models.py`

- [ ] **Step 1: Write failing model creation and uniqueness tests**

```python
@pytest.mark.asyncio
async def test_recommendation_tables_persist_ranked_results():
    from model_class.job_competency import RecommendationResult, RecommendationRun

    run = RecommendationRun(
        run_id="rec-1", resume_id=1, scoring_version="evidence-match-v2",
        input_signature="a" * 64, filters_json="{}", status="completed",
    )
    session.add(run)
    await session.flush()
    session.add(RecommendationResult(
        recommendation_run_id=run.id, job_profile_id=1, rank=1,
        total_score=88.0, confidence="high", result_json="{}",
    ))
    await session.commit()
    assert await session.scalar(select(func.count(RecommendationResult.id))) == 1
```

- [ ] **Step 2: Run the test and verify RED**

Run: `pytest tests/test_recommendation_models.py -q --no-cov`  
Expected: FAIL because the models do not exist.

- [ ] **Step 3: Add new-table-only models**

Add `RecommendationRun` with run ID, resume FK, scoring version, input signature, filters JSON, status, result signature and timestamps. Add `RecommendationResult` with run FK, profile FK, rank, score, confidence and result JSON. Enforce unique `(recommendation_run_id, rank)` and `(recommendation_run_id, job_profile_id)` constraints and an index on input signature.

No existing table is altered. Startup `Base.metadata.create_all` creates both tables for current and new databases.

- [ ] **Step 4: Verify model and migration regressions**

Run: `pytest tests/test_recommendation_models.py tests/test_job_models.py tests/test_schema_migration.py -q --no-cov`  
Expected: PASS.

- [ ] **Step 5: Checkpoint**

Confirm existing resume and match tables remain unchanged.

---

### Task 7: Top 5 Recommendation Service

**Files:**
- Create: `src/job_recommendation_service.py`
- Test: `tests/test_job_recommendation_service.py`
- Modify: `src/api.py` only after service tests pass

- [ ] **Step 1: Write failing ranking, deduplication and idempotency tests**

```python
@pytest.mark.asyncio
async def test_recommendations_are_stable_and_deduplicated_by_family(session):
    from src.job_recommendation_service import recommend_jobs

    first = await recommend_jobs(session, resume_id=resume.id, limit=5)
    second = await recommend_jobs(session, resume_id=resume.id, limit=5)
    assert len({item["family_code"] for item in first["items"]}) == len(first["items"])
    assert [item["profile_id"] for item in first["items"]] == [item["profile_id"] for item in second["items"]]
    assert first["result_signature"] == second["result_signature"]
    assert await session.scalar(select(func.count(RecommendationRun.id))) == 1


@pytest.mark.asyncio
async def test_recommendation_prefers_active_quarterly_profile_and_marks_fallback(session):
    result = await recommend_jobs(session, resume_id=resume.id, limit=5)
    quarterly = next(item for item in result["items"] if item["family_code"] == "JAVA_DEVELOPER")
    assert quarterly["profile_kind"] == "quarterly"
    assert quarterly["confidence_notes"]
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `pytest tests/test_job_recommendation_service.py -q --no-cov`  
Expected: FAIL with missing recommendation service.

- [ ] **Step 3: Implement profile payload and experience policy**

Add service helpers to load profile skills, responsibilities and scenarios. Derive `required_years` from an explicit value when available, otherwise from profile level. Fix `_profile_payload` in `api.py` to reuse this helper instead of returning `required_years: 0`.

- [ ] **Step 4: Implement candidate selection and persistence**

Expose:

```python
RECOMMENDATION_VERSION = "job-recommendation-v1"


async def recommend_jobs(
    db: AsyncSession,
    *,
    resume_id: int,
    limit: int = 5,
    levels: Collection[str] | None = None,
    family_codes: Collection[str] | None = None,
) -> dict:
    resume = await db.get(ResumeRecord, resume_id)
    if resume is None:
        raise ValueError("简历不存在")
    filters = {"levels": sorted(levels or []), "family_codes": sorted(family_codes or [])}
    candidates = await load_candidate_profiles(db, levels=levels, family_codes=family_codes)
    if not candidates:
        return {"items": [], "reason": "no_eligible_profiles", "result_signature": None}
    parsed = adapt_resume_profile(json.loads(resume.parsed_json))
    scored = [score_candidate(parsed, candidate) for candidate in candidates]
    ranked = deduplicate_and_rank(scored, limit=limit)
    signature = recommendation_signature(resume.content_hash, candidates, filters)
    return await persist_or_reuse_recommendation(db, resume, ranked, filters, signature)
```

Select active quarterly profiles first and latest usable legacy profile as fallback. Exclude rejected, superseded and skill-empty profiles. Score candidates with `match_resume_to_job`, sort by score, confidence rank, evidence count and stable profile key, then deduplicate by family. Hash resume content, candidate profile signatures, filters and scoring versions. Reuse an existing completed run with the same signature.

- [ ] **Step 5: Verify recommendation tests**

Run: `pytest tests/test_job_recommendation_service.py tests/test_matching_engine_v2.py -q --no-cov`  
Expected: PASS.

- [ ] **Step 6: Checkpoint**

Confirm an empty candidate set returns `{items: [], reason: "no_eligible_profiles"}` without creating fake entries.

---

### Task 8: API Integration

**Files:**
- Modify: `schemes/job_competency.py`
- Modify: `src/api.py`
- Modify: `tests/test_competition_api.py`
- Create: `tests/test_matching_api_v2.py`

- [ ] **Step 1: Write failing API tests**

```python
@pytest.mark.asyncio
async def test_parse_resume_returns_v2_profile_and_rule_fallback(client, monkeypatch):
    response = await client.post(
        "/api/resumes/parse",
        files={"file": ("resume.txt", "3年Java经验，项目使用Docker。", "text/plain")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == "resume-profile-v2"
    assert body["parser_mode"] in {"rules", "hybrid"}
    assert body["skills"][0]["evidence"]


@pytest.mark.asyncio
async def test_recommend_and_match_detail_endpoints(client, seeded_profiles):
    recommendation = await client.post(
        "/api/matches/recommend", json={"resume_id": seeded_profiles.resume_id, "limit": 5}
    )
    assert recommendation.status_code == 200
    assert len(recommendation.json()["items"]) <= 5
    profile_id = recommendation.json()["items"][0]["profile_id"]
    match = await client.post(
        "/api/matches", json={"resume_id": seeded_profiles.resume_id, "job_profile_id": profile_id}
    )
    detail = await client.get(f"/api/matches/{match.json()['match_id']}")
    assert detail.json()["scoring_version"] == "evidence-match-v2"
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `pytest tests/test_matching_api_v2.py -q --no-cov`  
Expected: FAIL with missing V2 fields and recommendation route.

- [ ] **Step 3: Add request schema and hybrid parsing**

Add:

```python
class JobRecommendationRequest(BaseModel):
    resume_id: int
    limit: int = Field(5, ge=1, le=10)
    levels: Optional[List[str]] = None
    family_codes: Optional[List[str]] = None
    model: Optional[str] = None
```

In the parse endpoint, run deterministic parsing first. If DeepSeek is configured, call enrichment through `asyncio.to_thread`; otherwise preserve rules mode. Persist the complete V2 JSON.

- [ ] **Step 4: Add recommendation and detail routes**

Add `POST /api/matches/recommend` and `GET /api/matches/{match_id}`. Save V2 score evidence, confidence, versions and phased plan in existing match JSON columns. Ensure path ordering avoids a route collision with `POST /api/matches`.

- [ ] **Step 5: Verify API regressions**

Run: `pytest tests/test_matching_api_v2.py tests/test_competition_api.py tests/test_resume_matching.py -q --no-cov`  
Expected: PASS.

- [ ] **Step 6: Checkpoint**

Confirm 400, 404 and empty-recommendation responses contain actionable, non-technical Chinese messages.

---

### Task 9: Human-Facing Matching UI

**Files:**
- Modify: `index.html`
- Modify: `tests/test_ui_static.py`
- Modify: `tests/test_competition_api.py`

- [ ] **Step 1: Write failing static UI assertions**

```python
def test_matching_ui_exposes_profile_recommendations_and_phased_path():
    html = INDEX.read_text(encoding="utf-8")
    for text in ("简历能力档案", "岗位推荐", "匹配证据", "0-30天", "31-60天", "61-90天"):
        assert text in html
    assert "/api/matches/recommend" in html
    assert "loadJobRecommendations" in html
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `pytest tests/test_ui_static.py::test_matching_ui_exposes_profile_recommendations_and_phased_path -q --no-cov`  
Expected: FAIL because the new sections are absent.

- [ ] **Step 3: Implement the three-stage UI**

Update the matching page to show:

1. Resume capability profile with experience timeline, evidence-rich skills, project achievements and parser mode.
2. Top 5 recommendation cards with rank, family, level, score, confidence, strengths, gaps and a “深度诊断” action.
3. Seven-dimension score cards, evidence detail, positive/negative factors and phased learning path.

After parsing, automatically call `loadJobRecommendations(resumeId)`. Selecting a recommendation updates the target profile and invokes the existing match endpoint. Preserve manual target selection.

- [ ] **Step 4: Preserve product-copy constraints**

Keep sidebar labels without numbers. Extend prohibited-copy assertions for the matching section so competition identifier, award language and rule thresholds do not appear.

- [ ] **Step 5: Verify UI tests**

Run: `pytest tests/test_ui_static.py tests/test_competition_api.py -q --no-cov`  
Expected: PASS.

- [ ] **Step 6: Checkpoint**

Start the app only after automated tests pass, then use browser inspection at desktop width to verify no overflow, empty-state breakage or JavaScript console errors. Stop the app after inspection.

---

### Task 10: Evaluation Metrics and Benchmark Format

**Files:**
- Modify: `src/evaluation_service.py`
- Modify: `tests/test_evaluation_service.py`
- Modify: `data/benchmark/README.md`
- Modify: `data/benchmark/benchmark-example.jsonl`

- [ ] **Step 1: Write failing recommendation metric tests**

```python
def test_recommendation_evaluation_computes_top1_recall_mrr_and_ndcg():
    from src.evaluation_service import run_benchmark

    record = {
        "case_id": "REC-001",
        "task": "job_recommendation",
        "input": {
            "resume": {"skills": ["Java", "MySQL"], "experience_years": 4, "projects": ["订单系统"]},
            "candidates": [
                {"family_code": "JAVA_DEVELOPER", "name": "Java开发工程师", "required_skills": ["Java", "MySQL"]},
                {"family_code": "DATA_ENGINEER", "name": "数据工程师", "required_skills": ["Python", "Spark"]},
            ],
        },
        "expected": {"relevance": {"JAVA_DEVELOPER": 3}},
    }
    report = run_benchmark([record])
    result = next(item for item in report["results"] if item["metric_name"] == "job_recommendation")
    assert result["top1_accuracy"] == 1.0
    assert result["recall_at_5"] == 1.0
    assert result["mrr"] == 1.0
    assert result["ndcg_at_5"] == 1.0


def test_resume_evaluation_reports_timeline_and_evidence_failures():
    from src.evaluation_service import run_benchmark

    record = {
        "case_id": "CV-V2-001",
        "task": "resume_extraction",
        "input": {"text": "工作经历：2021.01-2023.12 使用Java开发订单系统。"},
        "expected": {
            "skills": ["Java"], "experience_years": 3, "education": [],
            "work_ranges": [{"start": "2021-01", "end": "2023-12"}],
            "project_skills": ["Java"], "evidence_substrings": ["使用Java开发订单系统"],
        },
    }
    report = run_benchmark([record])
    result = report["results"][0]
    assert "timeline_accuracy" in result
    assert "evidence_validity" in result
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `pytest tests/test_evaluation_service.py -q --no-cov`  
Expected: FAIL because ranking and V2 resume metrics are absent.

- [ ] **Step 3: Implement optional V2 evaluation tasks**

Accept a `job_recommendation` task whose input contains one resume and candidate profile payloads and whose expected value contains graded relevant family codes. Calculate deterministic Top-1 Accuracy, Recall@5, MRR and NDCG@5. Extend resume extraction records with optional work ranges, project skills and expected evidence substrings.

Keep the existing three-metric readiness contract unchanged: missing optional recommendation cases must not mark existing metrics as passed or failed. Return ranking metrics as an additional result when cases are present.

- [ ] **Step 4: Update benchmark documentation and example**

Document V2 fields and add one synthetic format example clearly labelled as format-only. Require real evaluation data to remain isolated from ontology, weight and threshold development.

- [ ] **Step 5: Verify evaluation regressions**

Run: `pytest tests/test_evaluation_service.py tests/test_acceptance_service.py -q --no-cov`  
Expected: PASS.

- [ ] **Step 6: Checkpoint**

Confirm no metric is reported as passed when its benchmark task is absent.

---

### Task 11: Documentation, Full Verification and Read-Only Data Audit

**Files:**
- Modify: `README.md`
- Modify: `USER_GUIDE.md`
- Modify: `QUICKSTART.md`
- Test: all tests

- [ ] **Step 1: Update product documentation**

Document the hybrid parser and rules fallback, Top 5 flow, seven dimensions, confidence interpretation, learning phases and benchmark requirements. State clearly that matching is a capability diagnostic and not an automated hiring decision.

- [ ] **Step 2: Run focused feature tests**

Run:

```powershell
pytest tests/test_skill_ontology.py tests/test_resume_profile_v2.py tests/test_resume_enrichment_service.py tests/test_matching_engine_v2.py tests/test_learning_path_service.py tests/test_recommendation_models.py tests/test_job_recommendation_service.py tests/test_matching_api_v2.py tests/test_evaluation_service.py tests/test_ui_static.py -q --no-cov
```

Expected: all focused tests PASS.

- [ ] **Step 3: Run the full suite with coverage**

Run: `pytest -q`  
Expected: all tests PASS and total coverage remains at or above 60%.

- [ ] **Step 4: Audit the production database read-only**

Verify current profile, skill, resume, match and recommendation counts without writes. Confirm the new recommendation tables exist after normal application initialization. Do not fabricate resume or evaluation data in the production database.

- [ ] **Step 5: Run a browser smoke test with temporary data**

Use an isolated temporary SQLite database. Parse a representative resume in rules mode, load Top 5 recommendations, open one diagnosis and inspect the phased path. Verify no console errors and stop the temporary server.

- [ ] **Step 6: Final checkpoint**

Report exact test counts, coverage, browser result, parser fallback behavior and any remaining real-data gaps. Do not claim matching accuracy until an independent labeled benchmark has been run.
