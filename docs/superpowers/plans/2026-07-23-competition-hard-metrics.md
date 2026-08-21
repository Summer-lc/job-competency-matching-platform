# Competition Hard Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a traceable hard-metrics pipeline that reclassifies all existing JDs, enforces data-quality gates, creates level-aware quarterly profiles and evidence-backed evolution events, and calculates honest acceptance readiness.

**Architecture:** Add a small schema migration layer around the existing SQLAlchemy models, implement pure deterministic competition rules, and orchestrate them through an idempotent pipeline. Existing `JobProfile` and `EvolutionEvent` remain the canonical graph entities; quarterly metadata and evidence links extend them without replacing knowledge, matching, or review features.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy async, SQLite/aiosqlite, Pydantic, pytest/pytest-asyncio, vanilla HTML/CSS/JavaScript.

**Version-control note:** The workspace has no `.git` directory. Do not initialize a repository without user approval. Each task therefore ends with a verification checkpoint instead of a commit.

---

## File Map

**Create**

- `src/schema_migration.py`: safe database backup, schema inspection, and additive migration.
- `src/competition_rules.py`: pure quality-gate, seniority, quarter, and change-rule functions.
- `src/hard_metrics_pipeline.py`: full and incremental pipeline orchestration.
- `src/quarterly_profile_service.py`: quarterly profile aggregation and idempotent persistence.
- `src/evolution_service.py`: adjacent-quarter comparisons and JD evidence persistence.
- `src/acceptance_service.py`: minimum and internal readiness calculations.
- `src/rebuild_hard_metrics.py`: guarded command-line full rebuild.
- `tests/test_schema_migration.py`: old-database migration and backup tests.
- `tests/test_competition_rules.py`: deterministic gate, level, quarter, and change tests.
- `tests/test_hard_metrics_pipeline.py`: persistence, full rebuild, and idempotency tests.
- `tests/test_quarterly_profiles.py`: slice thresholds and quarterly profile tests.
- `tests/test_quarterly_evolution.py`: added, removed, modified, and evidence tests.
- `tests/test_acceptance_service.py`: measured, failed, and not-measured readiness tests.

**Modify**

- `model_class/job_competency.py`: JD gate and level fields.
- `model_class/knowledge_base.py`: pipeline, profile, evolution, evidence, and snapshot fields/tables.
- `config/DB_config.py`: run additive migration during initialization.
- `src/import_service.py`: classify and gate future imports with the same rule version.
- `src/job_analysis_service.py`: query canonical quarterly profiles and delegate evolution.
- `src/api.py`: hard-metrics, quarterly profile, evolution evidence, and acceptance endpoints.
- `schemes/job_competency.py`: pipeline and manual level-review request schemas.
- `index.html`: neutral quality dashboard, level/quarter filters, and readiness gaps.
- `tests/test_competition_api.py`: endpoint contract and prohibited-copy regression tests.
- `tests/test_ui_static.py`: filter and neutral-copy assertions.
- `README.md`: CLI/API operation and backup behavior.
- `USER_GUIDE.md`: full rebuild, review, and ongoing update workflow.

## Task 1: Additive Schema Migration and Safe Backup

**Files:**
- Create: `src/schema_migration.py`
- Create: `tests/test_schema_migration.py`
- Modify: `model_class/job_competency.py`
- Modify: `model_class/knowledge_base.py`
- Modify: `config/DB_config.py`

- [ ] **Step 1: Write the failing old-database migration test**

Create a temporary SQLite database containing the current `job_posting`, `job_profile`, and `evolution_event` schemas, then assert migration preserves rows and adds the new columns and tables:

```python
@pytest.mark.asyncio
async def test_migration_preserves_legacy_rows_and_adds_hard_metric_schema(tmp_path):
    database = tmp_path / "legacy.db"
    create_legacy_database(database)

    applied = await migrate_database(f"sqlite+aiosqlite:///{database.as_posix()}")

    assert "competition_hard_metrics_v1" in applied
    with sqlite3.connect(database) as connection:
        posting_columns = columns(connection, "job_posting")
        profile_columns = columns(connection, "job_profile")
        event_columns = columns(connection, "evolution_event")
        assert {"machine_level", "gate_status", "gate_rule_version"} <= posting_columns
        assert {"profile_kind", "period_key", "generation_key"} <= profile_columns
        assert {"previous_period", "current_period", "generation_key"} <= event_columns
        assert connection.execute("select count(*) from job_posting").fetchone()[0] == 1
        assert {"pipeline_run", "evolution_evidence", "acceptance_snapshot"} <= tables(connection)
```

- [ ] **Step 2: Run the migration test and verify RED**

Run: `pytest tests/test_schema_migration.py::test_migration_preserves_legacy_rows_and_adds_hard_metric_schema -q`

Expected: FAIL because `src.schema_migration` and the new schema do not exist.

- [ ] **Step 3: Add SQLAlchemy fields and tables**

Add these `JobPosting` fields with legacy-safe defaults:

```python
machine_level = mapped_column(String(30), nullable=False, default="unspecified")
machine_level_confidence = mapped_column(Float, nullable=False, default=0.0)
machine_level_evidence_json = mapped_column(Text, nullable=False, default="{}")
manual_level = mapped_column(String(30))
manual_level_review_json = mapped_column(Text)
gate_status = mapped_column(String(30), nullable=False, default="review")
gate_issue_codes_json = mapped_column(Text, nullable=False, default="[]")
gate_rule_version = mapped_column(String(50))
gated_at = mapped_column(DateTime)
```

Extend `JobProfile` with `profile_kind`, `period_key`, `sample_count`, `sample_status`, `input_signature`, `pipeline_run_id`, `generation_key`, and `derivation_status`. Extend `EvolutionEvent` with `previous_period`, `current_period`, `before_rate`, `after_rate`, `change_delta`, `event_status`, `pipeline_run_id`, and `generation_key`.

Add `PipelineRun`, `EvolutionEvidence`, and `AcceptanceSnapshot` using the exact names and meanings from the design. Apply unique constraints to `JobProfile.generation_key`, `EvolutionEvent.generation_key`, and `(evolution_event_id, job_posting_id, period_role)`.

- [ ] **Step 4: Implement additive migration and SQLite backup**

Implement these public functions:

```python
MIGRATION_ID = "competition_hard_metrics_v1"

def sqlite_database_path(database_url: str) -> Path | None:
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        return None
    return Path(unquote(database_url.split("?", 1)[0][len(prefix):])).resolve()

def backup_sqlite_database(database_url: str, backup_dir: Path) -> Path | None:
    source_path = sqlite_database_path(database_url)
    if source_path is None:
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target_path = backup_dir / f"{source_path.stem}-{stamp}.db"
    with sqlite3.connect(source_path) as source, sqlite3.connect(target_path) as target:
        source.backup(target)
    return target_path

async def migrate_database(database_url: str = ASYNC_DATABASE_URL) -> list[str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            return await ensure_competition_schema(connection)
    finally:
        await engine.dispose()
```

Use SQLite's `Connection.backup()` API rather than copying an open database file. Implement `ensure_competition_schema(connection)` by reading `inspect(sync_connection).get_columns(table_name)` through `run_sync`, executing only missing `ALTER TABLE ADD COLUMN` statements from a fixed tuple, creating missing indexes explicitly, then inserting `MIGRATION_ID` into `schema_migration`. Return `[MIGRATION_ID]` only when at least one schema change is applied and `[]` on a second run.

- [ ] **Step 5: Wire migration into database initialization**

Update `init_db()` to create missing tables and then call `ensure_competition_schema(connection)`. Do not create a backup on every startup; the guarded full rebuild in Task 8 creates it immediately before modifying production data.

- [ ] **Step 6: Add backup and idempotency tests**

```python
def test_sqlite_backup_is_readable_and_preserves_rows(tmp_path):
    source = tmp_path / "source.db"
    create_legacy_database(source)
    backup = backup_sqlite_database(
        f"sqlite+aiosqlite:///{source.as_posix()}", tmp_path / "backups"
    )
    assert backup and backup.exists()
    with sqlite3.connect(backup) as connection:
        assert connection.execute("select count(*) from job_posting").fetchone()[0] == 1

@pytest.mark.asyncio
async def test_migration_is_idempotent(tmp_path):
    database = tmp_path / "legacy.db"
    create_legacy_database(database)
    assert await migrate_database(url(database)) == ["competition_hard_metrics_v1"]
    assert await migrate_database(url(database)) == []
```

- [ ] **Step 7: Verify Task 1**

Run: `pytest tests/test_schema_migration.py tests/test_job_models.py -q`

Expected: all tests PASS and the legacy row count remains unchanged.

## Task 2: Deterministic Quality and Seniority Rules

**Files:**
- Create: `src/competition_rules.py`
- Create: `tests/test_competition_rules.py`

- [ ] **Step 1: Write failing quality-gate tests**

Cover quarantined precedence, future dates, ten-year gaps, invalid URLs, short descriptions, missing evidence, low quality, and valid official documents:

```python
def test_gate_status_uses_strict_precedence():
    decision = assess_gate(record(missing_description=True, duplicate_of_id=9), now=NOW)
    assert decision.status == "quarantined"
    assert "missing_job_description" in decision.issue_codes

def test_future_publication_requires_review():
    decision = assess_gate(record(published_at=NOW + timedelta(days=2)), now=NOW)
    assert decision.status == "review"
    assert "future_published_at" in decision.issue_codes

def test_valid_record_passes_gate():
    assert assess_gate(record(), now=NOW) == GateDecision("valid", ())
```

- [ ] **Step 2: Write failing seniority tests**

```python
@pytest.mark.parametrize(
    ("title", "experience", "description", "expected"),
    [
        ("初级数据工程师", "1年", BASE_TEXT, "junior"),
        ("数据工程师", "3-5年", BASE_TEXT, "mid"),
        ("高级数据工程师", "3年", BASE_TEXT, "senior"),
        ("技术专家", "8年", "负责技术规划、团队管理和标准制定。", "expert"),
        ("数据工程师", None, BASE_TEXT, "unspecified"),
    ],
)
def test_classify_seniority(title, experience, description, expected):
    assert classify_seniority(title, experience, description).level == expected
```

Also assert explicit-title conflicts lower confidence by `0.10`, `expert` cannot be produced by years alone, and confidence below `0.65` becomes `unspecified`.

- [ ] **Step 3: Run the rule tests and verify RED**

Run: `pytest tests/test_competition_rules.py -q`

Expected: FAIL because the rule module does not exist.

- [ ] **Step 4: Implement immutable rule results**

```python
GATE_RULE_VERSION = "competition-gate-v1"
LEVEL_RULE_VERSION = "competition-level-v1"
VALID_LEVELS = ("junior", "mid", "senior", "expert", "unspecified")

@dataclass(frozen=True)
class GateDecision:
    status: str
    issue_codes: tuple[str, ...]

@dataclass(frozen=True)
class SeniorityDecision:
    level: str
    confidence: float
    rule_version: str
    evidence: dict[str, object]

def quarter_key(value: datetime) -> str:
    return f"{value.year}-Q{((value.month - 1) // 3) + 1}"

def are_adjacent_quarters(previous: str, current: str) -> bool:
    def ordinal(key: str) -> int:
        year, quarter = key.split("-Q")
        return int(year) * 4 + int(quarter) - 1
    return ordinal(current) - ordinal(previous) == 1
```

Implement `assess_gate` as a pure rule accumulator followed by one precedence decision: missing required fields, short normalized description, or unparseable dates add quarantine codes; duplicate linkage adds `duplicate`; date, URL, evidence, and score anomalies add review codes. Choose `quarantined`, then `duplicate`, then `review`, otherwise `valid`. Implement `classify_seniority` with compiled explicit-title terms first, parsed experience bounds second, and responsibility terms third; apply the exact confidence values and conflict deduction in the design. Sort issue codes and evidence keys before returning so repeated runs serialize identical JSON.

- [ ] **Step 5: Implement change classification**

```python
@dataclass(frozen=True)
class ChangeDecision:
    change_type: str | None
    delta: float

def classify_skill_change(
    before_rate: float,
    after_rate: float,
    *,
    before_requirement: str | None,
    after_requirement: str | None,
    before_evidence: int,
    after_evidence: int,
) -> ChangeDecision:
    delta = round(after_rate - before_rate, 6)
    if before_rate < 0.05 and after_rate >= 0.15 and delta >= 0.10 and after_evidence >= 3:
        return ChangeDecision("added", delta)
    if before_rate >= 0.15 and after_rate < 0.05 and delta <= -0.10 and before_evidence >= 3:
        return ChangeDecision("removed", delta)
    requirement_changed = (
        before_requirement in {"required", "preferred"}
        and after_requirement in {"required", "preferred"}
        and before_requirement != after_requirement
        and after_evidence >= 3
    )
    if before_rate >= 0.05 and after_rate >= 0.05 and (abs(delta) >= 0.10 or requirement_changed):
        return ChangeDecision("modified", delta)
    return ChangeDecision(None, delta)
```

Implement the exact `0.05`, `0.15`, `0.10`, and three-evidence thresholds from the design.

- [ ] **Step 6: Verify Task 2**

Run: `pytest tests/test_competition_rules.py -q`

Expected: all rule tests PASS.

## Task 3: Persist Gates and Levels for Existing and Future Imports

**Files:**
- Create: `src/hard_metrics_pipeline.py`
- Create: `tests/test_hard_metrics_pipeline.py`
- Modify: `src/import_service.py`
- Modify: `schemes/job_competency.py`

- [ ] **Step 1: Write a failing persistence test**

```python
@pytest.mark.asyncio
async def test_reclassify_postings_persists_gate_and_level(session):
    valid = await add_posting(session, title="高级数据工程师", experience="5年以上")
    future = await add_posting(session, record_id="FUTURE", published_at=NOW + timedelta(days=2))

    summary = await reclassify_postings(session, now=NOW)

    await session.refresh(valid)
    await session.refresh(future)
    assert summary == {"processed": 2, "valid": 1, "review": 1, "quarantined": 0, "duplicate": 0}
    assert valid.machine_level == "senior"
    assert valid.gate_status == "valid"
    assert future.gate_status == "review"
```

- [ ] **Step 2: Run and verify RED**

Run: `pytest tests/test_hard_metrics_pipeline.py::test_reclassify_postings_persists_gate_and_level -q`

Expected: FAIL because `reclassify_postings` does not exist.

- [ ] **Step 3: Implement classification persistence**

```python
async def reclassify_postings(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    posting_ids: Collection[int] | None = None,
    family_codes: Collection[str] | None = None,
) -> dict[str, int]:
    effective_now = now or datetime.now()
    query = select(JobPosting).order_by(JobPosting.id)
    if posting_ids is not None:
        query = query.where(JobPosting.id.in_(posting_ids))
    if family_codes is not None:
        query = query.where(JobPosting.job_family_id.in_(family_codes))
    postings = list((await db.execute(query)).scalars())
    counts = {"processed": 0, "valid": 0, "review": 0, "quarantined": 0, "duplicate": 0}
    for posting in postings:
        gate = assess_gate(posting_gate_payload(posting), now=effective_now)
        level = classify_seniority(
            posting.job_title_raw,
            posting.experience_requirement,
            posting.job_description_raw,
        )
        posting.gate_status = gate.status
        posting.gate_issue_codes_json = json.dumps(gate.issue_codes, ensure_ascii=False)
        posting.gate_rule_version = GATE_RULE_VERSION
        posting.gated_at = effective_now
        posting.machine_level = level.level
        posting.machine_level_confidence = level.confidence
        posting.machine_level_evidence_json = json.dumps(level.evidence, ensure_ascii=False, sort_keys=True)
        counts["processed"] += 1
        counts[gate.status] += 1
    await db.flush()
    return counts
```

Implement `posting_gate_payload(posting)` in the same module as a literal mapping of required identifiers, source fields, timestamps, description, quality score, duplicate linkage, and booleans derived from persisted skill/responsibility evidence. Preserve existing `JobPosting.status` for compatibility, but make `gate_status` authoritative for new quarterly calculations. Serialize evidence and issue codes with `sort_keys=True`.

- [ ] **Step 4: Rebalance duplicate groups before gating**

Implement `rebuild_duplicate_groups(db, *, family_codes=None)` using exact content hash first and SimHash distance at most eight within each family. Rank each group's master by source score descending, field completeness descending, publication date descending, and ID ascending. Set the winner's `duplicate_of_id` to `None` and all other members to the winner ID. Add a test where a later official-source record replaces an earlier lower-score record as the group master.

- [ ] **Step 5: Apply the same rules during import**

After each import batch and its skill evidence are persisted in `import_job_file`, rebalance duplicates for the affected families and classify the affected posting IDs. Do not duplicate rule logic inside `import_service.py`.

- [ ] **Step 6: Add manual level-review schema and persistence**

Add a request model:

```python
class JobLevelReviewRequest(BaseModel):
    level: str = Field(pattern="^(junior|mid|senior|expert|unspecified)$")
    reviewer: str = Field(min_length=1, max_length=100)
    note: str = Field(min_length=2, max_length=500)
```

The persistence helper must store manual values separately and expose `effective_level = manual_level or machine_level` without overwriting machine evidence.

- [ ] **Step 7: Add repeated-classification test**

Run classification twice and assert identical JSON, unchanged row count, and no duplicate review items.

- [ ] **Step 8: Verify Task 3**

Run: `pytest tests/test_hard_metrics_pipeline.py tests/test_import_service.py -q`

Expected: all tests PASS.

## Task 4: Build Idempotent Quarterly Profiles

**Files:**
- Create: `src/quarterly_profile_service.py`
- Create: `tests/test_quarterly_profiles.py`
- Modify: `src/job_analysis_service.py`

- [ ] **Step 1: Write failing slice and threshold tests**

Create valid, unique JDs in one family and quarter. Verify nine records produce no profile, 10 produce `low_sample`, and 20 produce `ready`:

```python
@pytest.mark.asyncio
async def test_quarterly_profile_sample_thresholds(session):
    await add_unique_postings(session, count=10, quarter="2026-Q1", level="mid")
    run = await add_pipeline_run(session)
    result = await rebuild_quarterly_profiles(
        session, pipeline_run_id=run.id, family_codes={"DATA_ENGINEER"}
    )
    profile = await active_profile(session, "DATA_ENGINEER", "mid", "2026-Q1")
    assert result["profiles_created"] == 1
    assert profile.sample_count == 10
    assert profile.sample_status == "low_sample"
```

Add separate assertions that review, quarantined, duplicate, unspecified-date, and non-adjacent records are excluded.

- [ ] **Step 2: Run and verify RED**

Run: `pytest tests/test_quarterly_profiles.py -q`

Expected: FAIL because quarterly profile rebuilding does not exist.

- [ ] **Step 3: Implement stable grouping and signatures**

```python
@dataclass(frozen=True)
class ProfileSlice:
    family_code: str
    tech_stack: str
    level: str
    period_key: str

def profile_generation_key(slice_: ProfileSlice, rule_version: str) -> str:
    raw = "|".join((rule_version, slice_.family_code, slice_.tech_stack, slice_.level, slice_.period_key))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def input_signature(postings: Sequence[JobPosting]) -> str:
    raw = "\n".join(
        f"{posting.id}:{posting.content_hash}"
        for posting in sorted(postings, key=lambda item: item.id)
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
```

Implement `rebuild_quarterly_profiles(db, *, pipeline_run_id, family_codes=None)` by selecting only `gate_status == "valid"`, `duplicate_of_id IS NULL`, and non-null `published_at`; grouping with `ProfileSlice`; dropping groups below 10; and upserting by `generation_key`. Return counts for created, updated, superseded, insufficient, low-sample, and ready slices.

Sort posting IDs and content hashes before hashing. Assign `version = max(existing family version) + 1` only when a new generation key is inserted. Update the same profile when the generation key already exists; mark obsolete active quarterly profiles in the same slice `superseded`.

- [ ] **Step 4: Reuse canonical child tables**

Populate existing `JobProfileSkill`, `JobProfileResponsibility`, and `JobProfileScenario`. Count each skill at most once per JD. Determine required versus preferred from the majority of posting links, and persist prevalence as `evidence_count / sample_count`.

- [ ] **Step 5: Make existing queries prefer quarterly profiles**

Update `_latest_profiles`, `graph_data`, and version listing so active quarterly profiles are canonical when present; legacy profiles remain queryable through version history. Add optional `period_key` to graph filters without breaking current callers.

- [ ] **Step 6: Add idempotency and legacy compatibility tests**

Assert two identical rebuilds keep the same profile IDs, child row counts, and versions. Assert an existing legacy profile remains stored but is excluded from readiness.

- [ ] **Step 7: Verify Task 4**

Run: `pytest tests/test_quarterly_profiles.py tests/test_job_analysis_service.py tests/test_versioned_graph.py -q`

Expected: all tests PASS.

## Task 5: Generate Evidence-Backed Adjacent-Quarter Evolution

**Files:**
- Create: `src/evolution_service.py`
- Create: `tests/test_quarterly_evolution.py`
- Modify: `src/job_analysis_service.py`

- [ ] **Step 1: Write failing added, removed, and modified tests**

Build two ready adjacent quarterly profiles with controlled prevalence and evidence:

```python
@pytest.mark.asyncio
async def test_adjacent_ready_profiles_create_three_change_types(session):
    previous, current = await profile_pair(
        session,
        previous={"Spark": (0.20, "required"), "Flink": (0.20, "preferred")},
        current={"Kafka": (0.20, "required"), "Flink": (0.35, "required")},
    )
    events = await rebuild_evolution(session, previous.id, current.id, pipeline_run_id=1)
    assert {(event.entity_key, event.change_type) for event in events} == {
        ("Kafka", "added"),
        ("Spark", "removed"),
        ("Flink", "modified"),
    }
```

Add tests that non-adjacent quarters, low-sample profiles, and changes with fewer than three JD evidence links do not produce formal events.

- [ ] **Step 2: Run and verify RED**

Run: `pytest tests/test_quarterly_evolution.py -q`

Expected: FAIL because the new evolution service does not exist.

- [ ] **Step 3: Implement adjacent-quarter comparison**

Implement `rebuild_evolution(db, previous_profile_id, current_profile_id, *, pipeline_run_id)` and `rebuild_all_adjacent_evolution(db, *, pipeline_run_id, family_codes=None)`. The first validates matching family, stack, and level; calls `are_adjacent_quarters`; rejects non-ready profiles; compares the union of skill IDs; and upserts qualifying events. The second groups active ready profiles by family, stack, and level, sorts by quarter ordinal, and invokes the first only for adjacent pairs.

Use `classify_skill_change` from `competition_rules.py`. Set formal event status only when both profiles are `ready`; otherwise do not create an event.

- [ ] **Step 4: Persist traceable JD evidence**

For each event, select the independent JD records that contribute the skill link in the relevant quarter. Insert `EvolutionEvidence` rows with `period_role` equal to `before` or `after`, plus evidence text. External standards remain supplementary graph evidence and are not counted toward the three-JD threshold.

- [ ] **Step 5: Make evolution idempotent**

Generate event keys from previous profile ID, current profile ID, entity type, entity key, change type, and rule version. Re-running updates the same event and replaces its evidence links transactionally.

- [ ] **Step 6: Delegate the public evolution payload**

Update `family_evolution` to return `previous_period`, `current_period`, rates, delta, sample status, and evidence JD records while retaining existing keys used by the UI.

- [ ] **Step 7: Verify Task 5**

Run: `pytest tests/test_quarterly_evolution.py tests/test_versioned_graph.py -q`

Expected: all tests PASS and each formal event has at least three evidence links on the changed side.

## Task 6: Calculate Honest Acceptance Readiness

**Files:**
- Create: `src/acceptance_service.py`
- Create: `tests/test_acceptance_service.py`
- Modify: `src/evaluation_service.py`

- [ ] **Step 1: Write failing not-measured tests**

```python
@pytest.mark.asyncio
async def test_missing_measurements_never_report_ready(session, tmp_path):
    result = await acceptance_summary(session, coverage_file=tmp_path / ".coverage")
    assert result["minimum"]["overall"] == "not_measured"
    assert result["minimum"]["metrics"]["jd_parsing_accuracy"]["status"] == "not_measured"
    assert result["minimum"]["metrics"]["unit_test_coverage"]["status"] == "not_measured"
```

- [ ] **Step 2: Write failing passed/failed tests**

Persist measured evaluation rows, enough benchmark samples, ready profiles, and a formal evolution event. Assert each metric reports `current`, `target`, `gap`, and one of `passed`, `failed`, or `not_measured`.

- [ ] **Step 3: Run and verify RED**

Run: `pytest tests/test_acceptance_service.py -q`

Expected: FAIL because the acceptance service does not exist.

- [ ] **Step 4: Implement metric payloads and real coverage reading**

```python
def metric_status(current: float | int | None, target: float | int, *, minimum: bool = True) -> dict:
    if current is None:
        return {"current": None, "target": target, "gap": None, "status": "not_measured"}
    passed = current >= target if minimum else current <= target
    gap = max(0, target - current) if minimum else max(0, current - target)
    return {"current": current, "target": target, "gap": round(gap, 6), "status": "passed" if passed else "failed"}

def read_coverage_total(path: Path) -> float | None:
    if not path.exists():
        return None
    coverage_data = Coverage(data_file=str(path))
    try:
        coverage_data.load()
        return round(coverage_data.report(file=io.StringIO()) / 100, 6)
    except CoverageException:
        return None
```

Implement `acceptance_summary(db, *, coverage_file=None, persist=False)` by querying the latest real evaluation per task, benchmark sample count, posting/family/source distributions, active ready profiles, emerging/existing cases, and formal evolution events. Build both metric dictionaries with `metric_status`, derive the overall status exactly as described below, and insert a canonical hashed `AcceptanceSnapshot` only when `persist=True`.

Read the real `.coverage` data through the installed `coverage` package. Return `None` when absent or unreadable. Do not use the configured threshold as a measured value.

- [ ] **Step 5: Implement both readiness scopes**

Minimum scope: 100 JD benchmark cases, all three measured accuracies at least `0.90`, measured coverage at least `0.60`, one complete emerging profile, one complete existing profile, and one formal adjacent-quarter evolution. A complete profile must be active and ready, contain at least one responsibility and one skill, link to at least one industry scenario, and have at least one traceable external evidence record for its family.

Internal scope: 5000 to 10000 raw JDs, 20 to 30 families, at least three source types, three levels and two ready adjacent quarters for a sample family, and all three accuracies at least `0.92`.

Use a separate `range_metric_status(current, minimum, maximum)` for the two bounded collection targets; values below or above the range are `failed`, not silently clamped.

Overall status is `not_measured` if any required metric is not measured, otherwise `passed` only when every metric passes, else `failed`.

- [ ] **Step 6: Persist snapshots idempotently**

Persist a snapshot only when `persist=True`. Hash the canonical sorted metrics JSON and avoid inserting a second identical snapshot.

- [ ] **Step 7: Verify Task 6**

Run: `pytest tests/test_acceptance_service.py tests/test_evaluation_service.py -q`

Expected: all tests PASS.

## Task 7: Expose Pipeline, Filters, Evidence, and Neutral UI

**Files:**
- Modify: `src/api.py`
- Modify: `schemes/job_competency.py`
- Modify: `index.html`
- Modify: `tests/test_competition_api.py`
- Modify: `tests/test_ui_static.py`

- [ ] **Step 1: Write failing API contract tests**

Add these expected paths:

```python
expected = {
    "/api/hard-metrics/rebuild",
    "/api/hard-metrics/runs",
    "/api/hard-metrics/quality",
    "/api/hard-metrics/levels/{posting_id}",
    "/api/analysis/quarterly-profiles",
    "/api/acceptance/summary",
}
assert expected <= set(openapi["paths"])
```

Add an endpoint test asserting missing evaluation and coverage data return `not_measured`, not a ready state.

- [ ] **Step 2: Run API tests and verify RED**

Run: `pytest tests/test_competition_api.py -q`

Expected: FAIL because the endpoints do not exist.

- [ ] **Step 3: Add request schemas and endpoints**

```python
class HardMetricsRunRequest(BaseModel):
    mode: str = Field("incremental", pattern="^(full|incremental)$")
    family_codes: list[str] | None = None

@app.post("/api/hard-metrics/rebuild")
async def rebuild_hard_metrics(request: HardMetricsRunRequest, confirm: bool = False, db=Depends(get_db)):
    if request.mode == "full" and not confirm:
        raise HTTPException(status_code=400, detail="全量重算需要明确确认")
    backup = backup_sqlite_database(ASYNC_DATABASE_URL, BACKUP_DIR) if request.mode == "full" else None
    result = await run_hard_metrics_pipeline(
        db, mode=request.mode, family_codes=set(request.family_codes or []) or None
    )
    return {**result, "backup_path": str(backup) if backup else None}

@app.get("/api/hard-metrics/runs")
async def hard_metric_runs(db=Depends(get_db)):
    return await pipeline_run_history(db)

@app.get("/api/hard-metrics/quality")
async def hard_metric_quality(db=Depends(get_db)):
    return await quality_distribution(db)

@app.put("/api/hard-metrics/levels/{posting_id}")
async def review_posting_level(posting_id: int, request: JobLevelReviewRequest, db=Depends(get_db)):
    return await persist_manual_level_review(db, posting_id, request.model_dump())

@app.get("/api/analysis/quarterly-profiles")
async def quarterly_profiles(family_code=None, tech_stack=None, level=None, period_key=None, db=Depends(get_db)):
    return await list_quarterly_profiles(db, family_code=family_code, tech_stack=tech_stack, level=level, period_key=period_key)

@app.get("/api/acceptance/summary")
async def acceptance(db=Depends(get_db)):
    return await acceptance_summary(db)
```

The full mode must require an explicit `confirm=true` query parameter and return HTTP 400 without it. Keep the existing `/api/analysis/rebuild` endpoint for compatibility.

Implement `pipeline_run_history` and `quality_distribution` in `hard_metrics_pipeline.py`, `persist_manual_level_review` in the same service, and `list_quarterly_profiles` in `quarterly_profile_service.py`. Each helper returns JSON-ready dictionaries sorted newest-first or by stable label; missing posting IDs are converted to HTTP 404 by the API adapter.

- [ ] **Step 4: Write failing static UI tests**

Assert the UI contains level and quarter selectors, quality gate distribution, pipeline history, evolution evidence links, and a neutral “系统验收状态” panel. Extend prohibited copy assertions to continue rejecting the competition identifier, “国奖”, “参赛就绪”, and quoted rule language.

- [ ] **Step 5: Update the interface without adding navigation numbers**

Add to existing pages rather than adding another sidebar item:

- Data governance: run button, status counts, issue codes, and latest run summary.
- Evolution: `level` and `period_key` selectors, sample status, before/after rates, and source JD links.
- Evaluation: neutral system acceptance status with current value, target, gap, and measurement state.

Use visible labels such as “系统质量门槛”, “内部数据目标”, and “尚未测量”. Do not expose the competition code, award language, submission deadline, or rule prose in the UI.

- [ ] **Step 6: Verify Task 7**

Run: `pytest tests/test_competition_api.py tests/test_ui_static.py -q`

Expected: all tests PASS and prohibited competition-specific UI copy remains absent.

## Task 8: Guarded Full Rebuild, Documentation, and Final Verification

**Files:**
- Create: `src/rebuild_hard_metrics.py`
- Modify: `src/hard_metrics_pipeline.py`
- Modify: `README.md`
- Modify: `USER_GUIDE.md`

- [ ] **Step 1: Write the failing full-pipeline idempotency test**

```python
@pytest.mark.asyncio
async def test_full_pipeline_is_idempotent(session):
    await seed_mixed_quality_multiquarter_data(session)
    first = await run_hard_metrics_pipeline(session, mode="full", now=NOW)
    counts_after_first = await derived_counts(session)
    second = await run_hard_metrics_pipeline(session, mode="full", now=NOW)
    assert second["status"] == "completed"
    assert await derived_counts(session) == counts_after_first
    assert second["result_signature"] == first["result_signature"]
```

- [ ] **Step 2: Run and verify RED**

Run: `pytest tests/test_hard_metrics_pipeline.py::test_full_pipeline_is_idempotent -q`

Expected: FAIL until the orchestrator is complete.

- [ ] **Step 3: Implement the orchestrator**

```python
async def run_hard_metrics_pipeline(
    db: AsyncSession,
    *,
    mode: str,
    now: datetime | None = None,
    family_codes: set[str] | None = None,
) -> dict[str, object]:
    run = await create_pipeline_run(db, mode=mode, family_codes=family_codes)
    try:
        duplicate_summary = await rebuild_duplicate_groups(db, family_codes=family_codes)
        gate_summary = await reclassify_postings(db, now=now, family_codes=family_codes)
        profile_summary = await rebuild_quarterly_profiles(
            db, pipeline_run_id=run.id, family_codes=family_codes
        )
        evolution_summary = await rebuild_all_adjacent_evolution(
            db, pipeline_run_id=run.id, family_codes=family_codes
        )
        knowledge_summary = await update_knowledge_chunks(db)
        acceptance = await acceptance_summary(db, persist=True)
        result = canonical_pipeline_result(
            duplicate_summary,
            gate_summary,
            profile_summary,
            evolution_summary,
            knowledge_summary,
            acceptance,
        )
        await complete_pipeline_run(db, run, result)
        return result
    except Exception as exc:
        await fail_pipeline_run(db, run, exc)
        raise
```

Implement `create_pipeline_run`, `complete_pipeline_run`, and `fail_pipeline_run` in the same module. `create` inserts and commits a `running` row before work starts; `complete` stores sorted canonical result JSON, result signature, completion time, and commits; `fail` rolls back work, reloads the already committed run, updates it to `failed`, stores the exception class and message, and commits. `canonical_pipeline_result` sorts dictionary keys before hashing so repeated identical inputs produce the same signature.

Create a `PipelineRun` with `running`, execute duplicate rebalancing, classification, quarterly profiles, adjacent evolution, knowledge refresh, and acceptance snapshot in order, then mark `completed`. On failure, roll back the current family/quarter transaction, mark the run `failed`, save the exception summary, and re-raise.

- [ ] **Step 4: Implement the guarded CLI**

Support:

```powershell
python -m src.rebuild_hard_metrics --dry-run
python -m src.rebuild_hard_metrics --full --confirm
python -m src.rebuild_hard_metrics --incremental
```

`--dry-run` prints source database, row counts, pending migration, and backup destination without writes. `--full` exits with code 2 unless `--confirm` is present. A confirmed full run creates a verified SQLite backup before opening the write session and prints the backup path.

- [ ] **Step 5: Document operation and recovery**

Update `README.md` and `USER_GUIDE.md` with the three commands, gate statuses, level meanings, quarter sample thresholds, backup directory, restore procedure, and the rule that insufficient real evidence produces a visible gap instead of a fabricated evolution result.

- [ ] **Step 6: Run the complete automated suite**

Run: `pytest -q`

Expected: all tests PASS and total coverage remains at least 60%.

- [ ] **Step 7: Dry-run and rebuild a database copy**

Copy the production database through SQLite backup API to `tmp/hard-metrics-verification.db`, point `DATABASE_URL` at the copy, then run:

```powershell
$env:DATABASE_URL='sqlite+aiosqlite:///D:/VScode/.vscode/Job competency matching/langchain_deepseek/tmp/hard-metrics-verification.db'
python -m src.rebuild_hard_metrics --dry-run
python -m src.rebuild_hard_metrics --full --confirm
python -m src.rebuild_hard_metrics --full --confirm
```

Expected: both full runs complete; posting counts remain stable; the second run creates no duplicate profiles, events, evidence links, or snapshots.

- [ ] **Step 8: Run the guarded production rebuild**

Only after Step 7 passes, clear the temporary `DATABASE_URL` override and run:

```powershell
Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
python -m src.rebuild_hard_metrics --dry-run
python -m src.rebuild_hard_metrics --full --confirm
```

Expected: output includes a readable backup path, status counts, level distribution, quarter coverage, formal evolution count, and honest readiness gaps.

- [ ] **Step 9: Final data and regression audit**

Verify with read-only queries:

- Raw posting count is unchanged.
- Every posting has a gate status and machine level.
- No active generation key is duplicated.
- Every formal evolution event compares adjacent ready quarters and has the required JD evidence.
- External evidence count remains 24.
- Resume, match, evaluation, and knowledge chunk row counts do not decrease.
- UI routes and all existing API endpoints remain available.

Run `pytest -q` once more after the production rebuild. Report actual pass count, coverage, backup path, gate distribution, level distribution, ready-quarter count, formal evolution count, and every unmet acceptance metric.
