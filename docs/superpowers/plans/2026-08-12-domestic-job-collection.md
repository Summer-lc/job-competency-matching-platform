# Domestic Job Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Ensure production job data comes only from reviewed Chinese recruitment websites, remove the 30 previously imported foreign jobs, and add traceable domestic jobs through bounded public collection or authorized local imports.

**Architecture:** Extend the source definition with a fail-closed job-market scope and enforce it both when an automatic source is selected and immediately before a staged batch is committed. Keep the existing adapters, staging, attestation, quality-gate, backup, and incremental import pipeline. Restore the exact pre-foreign-import database, then run domestic smoke collections and rebuild all derived knowledge and graph data.

**Tech Stack:** Python 3.11, Pydantic 2, SQLAlchemy async, SQLite, httpx, BeautifulSoup, JSON/JSONL, pytest, Ruff.

---

## File Map

- Modify `src/job_collection/models.py`: define and validate the source job-market scope.
- Modify `model_class/knowledge_base.py`: persist the scope on `job_source`.
- Modify `src/schema_migration.py`: add and backfill the `job_source.market_scope` column safely.
- Modify `src/job_collection/source_registry.py`: block automatic collection unless the source is approved for the China job market and persist the scope.
- Modify `src/job_collection/service.py`: reject non-China source definitions before commit.
- Modify `config/job_sources.json`: mark domestic, excluded, and pending sources explicitly; disable the foreign-capable generic company manifest.
- Modify `tests/test_job_models.py`, `tests/test_schema_migration.py`, `tests/test_source_registry.py`, `tests/test_collection_service.py`: cover the fail-closed market-scope behavior.
- Modify `tests/test_ncss_adapter.py`, `tests/test_mohrss_adapter.py`, `tests/test_collect_jobs_cli.py`, and other source fixture helpers only to provide the explicit China scope required by the new model.
- Create `data/audits/foreign-job-exclusion-20260812.json`: retain counts, domains, hashes, restoration source, and post-restore checks.
- Modify `README.md` and `docs/数据采集运行手册.md`: document domestic-only collection commands and commercial-platform file-import boundaries.

### Task 1: Add Fail-Closed Job-Market Scope

**Files:**
- Modify: `src/job_collection/models.py`
- Modify: `model_class/knowledge_base.py`
- Modify: `src/schema_migration.py`
- Test: `tests/test_job_models.py`
- Test: `tests/test_schema_migration.py`

- [x] **Step 1: Write failing source-model tests**

Add tests requiring an explicit scope and accepting only the supported values:

```python
def test_source_definition_requires_reviewed_job_market_scope():
    payload = source_payload()
    payload.pop("market_scope", None)
    with pytest.raises(ValidationError):
        SourceDefinition.model_validate(payload)


@pytest.mark.parametrize("value", ["china", "excluded", "pending_review"])
def test_source_definition_accepts_known_job_market_scopes(value):
    assert SourceDefinition.model_validate(
        source_payload(market_scope=value)
    ).market_scope == value
```

- [x] **Step 2: Run the tests and verify the expected failure**

Run: `python -m pytest tests/test_job_models.py -q --no-cov`  
Expected: FAIL because `SourceDefinition` does not require `market_scope`.

- [x] **Step 3: Add the model field and persistence column**

Add to `src/job_collection/models.py`:

```python
MarketScope = Literal["china", "excluded", "pending_review"]

class SourceDefinition(BaseModel):
    market_scope: MarketScope
```

Add to `JobSource`:

```python
market_scope: Mapped[str] = mapped_column(
    String(30), nullable=False, default="pending_review",
    server_default=text("'pending_review'")
)
```

Extend the existing schema migration so old databases receive the column with `pending_review`, then backfill the four reviewed source IDs from `config/job_sources.json` during registry synchronization.

- [x] **Step 4: Run model and migration tests**

Run: `python -m pytest tests/test_job_models.py tests/test_schema_migration.py -q --no-cov`  
Expected: PASS, including idempotent migration on a database that already has the column.

### Task 2: Enforce Domestic Scope at Source Selection and Commit

**Files:**
- Modify: `src/job_collection/source_registry.py`
- Modify: `src/job_collection/service.py`
- Test: `tests/test_source_registry.py`
- Test: `tests/test_collection_service.py`

- [x] **Step 1: Write failing registry and commit tests**

Add tests proving an approved but non-China source cannot run or commit:

```python
def test_automatic_collection_requires_china_market_scope():
    registry = SourceRegistry([
        source_definition(
            source_id="foreign_jobs",
            market_scope="excluded",
            compliance_status="approved",
            enabled=True,
        )
    ])
    with pytest.raises(CollectionBlocked, match="market_scope=excluded"):
        registry.require_automatic("foreign_jobs")
```

```python
@pytest.mark.asyncio
async def test_commit_rejects_staged_non_china_source(tmp_path):
    # Build the normal signed staging fixture with market_scope="excluded".
    with pytest.raises(CollectionBlocked, match="not approved for China job data"):
        await commit_collection_run(
            run_id="excluded-source-run",
            collections_root=tmp_path / "collections",
            database_url=sqlite_url(tmp_path / "jobs.db"),
            backup_dir=tmp_path / "backups",
            confirm=True,
            registry=excluded_registry,
        )
```

- [x] **Step 2: Run the focused tests and verify both fail for the missing guard**

Run: `python -m pytest tests/test_source_registry.py tests/test_collection_service.py -q --no-cov`  
Expected: FAIL because approved/enabled status currently ignores job-market scope.

- [x] **Step 3: Implement the two guards**

In `require_automatic`, require:

```python
definition.market_scope == "china"
```

In `_commit_collection_run_unlocked`, after current source definitions are reconstructed and before opening the write transaction, reject every definition whose scope is not `china`:

```python
excluded = sorted(
    item.source_id for item in definitions if item.market_scope != "china"
)
if excluded:
    raise CollectionBlocked(
        "sources are not approved for China job data: " + ", ".join(excluded)
    )
```

Persist `market_scope` in `SourceRegistry.upsert_job_sources`.

- [x] **Step 4: Run the registry and commit tests**

Run: `python -m pytest tests/test_source_registry.py tests/test_collection_service.py -q --no-cov`  
Expected: PASS, including existing attestation and idempotent commit cases.

### Task 3: Register Only Reviewed Chinese Recruitment Sources

**Files:**
- Modify: `config/job_sources.json`
- Modify: source helper payloads in `tests/test_collection_http.py`, `tests/test_collect_jobs_cli.py`, `tests/test_import_service.py`, `tests/test_job_collection_normalizer.py`, `tests/test_legacy_file_adapter.py`, `tests/test_manual_manifest_adapter.py`, `tests/test_mohrss_adapter.py`, `tests/test_ncss_adapter.py`, and `tests/test_source_registry.py`
- Test: `tests/test_collection_docs.py`

- [x] **Step 1: Write a failing configuration contract test**

Require these exact states:

```python
assert sources["ncss_public_jobs"].market_scope == "china"
assert sources["mohrss_public_jobs"].market_scope == "china"
assert sources["zhaopin_legacy_import"].market_scope == "china"
assert sources["company_official_manifest"].market_scope == "excluded"
assert sources["company_official_manifest"].enabled is False
assert sources["company_official_manifest"].compliance_status == "blocked"
assert sources["iguopin_public_jobs"].market_scope == "pending_review"
assert sources["iguopin_public_jobs"].enabled is False
```

- [x] **Step 2: Run the configuration test and verify it fails**

Run: `python -m pytest tests/test_collection_docs.py -q --no-cov`  
Expected: FAIL because the source records do not yet declare market scope and 国聘 is not registered.

- [x] **Step 3: Update the registry and test fixtures**

Set domestic sources to `china`. Set the generic company manifest to `excluded`, `blocked`, and disabled. Add `iguopin_public_jobs` as `pending_review`, disabled, with `base_url=https://www.iguopin.com`, `allowed_paths=["/job/"]`, a five-second minimum request interval, and a compliance note that it cannot be enabled until a public unauthenticated route is confirmed.

Every test source fixture must state its intended scope explicitly. Use `china` for fixtures testing normal collection, `excluded` for denial tests, and `pending_review` for review-state tests.

- [x] **Step 4: Run configuration and source tests**

Run: `python -m pytest tests/test_collection_docs.py tests/test_source_registry.py tests/test_ncss_adapter.py tests/test_mohrss_adapter.py tests/test_collect_jobs_cli.py -q --no-cov`  
Expected: PASS.

### Task 4: Validate Domestic Public Endpoints Without Bypassing Controls

**Files:**
- Modify only if the public contract changed: `src/job_collection/adapters/ncss.py`
- Modify only if the public contract changed: `src/job_collection/adapters/mohrss.py`
- Update fixture only from a sanitized public response: `tests/fixtures/ncss/*`, `tests/fixtures/mohrss/*`
- Test: `tests/test_ncss_adapter.py`
- Test: `tests/test_mohrss_adapter.py`

- [x] **Step 1: Run bounded smoke checks with a unique run ID**

Run:

```powershell
python -m src.collect_jobs --source ncss_public_jobs --run-id ncss-cn-smoke-20260812 --max-records 20 --max-pages 2 --max-requests 50 --dry-run
```

Expected: a completed or honestly stopped report under `data/collections/ncss-cn-smoke-20260812`; no commit occurs.

For 中国公共招聘网, first issue one public request to `/cjobs/jobinfolist/listJobinfolistIndex`. If it returns the expected list structure, enable the source temporarily and run the same 20-record smoke check. If it returns `401`, `403`, `429`, `5xx`, a login page, or an incompatible structure, leave `enabled=false`, record the response status in the audit, and do not modify the parser to bypass the condition.

- [x] **Step 2: If a public response proves a parser contract change, write a failing sanitized-fixture test**

The test must assert the exact new public list/detail path and field mapping while containing no phone number, email, Cookie, token, or personal data.

- [x] **Step 3: Run the adapter test and verify the expected parser failure**

Run: `python -m pytest tests/test_ncss_adapter.py tests/test_mohrss_adapter.py -q --no-cov`  
Expected when a contract changed: FAIL at the exact path or selector assertion. If neither public contract changed, this step passes and no adapter production code is edited.

- [x] **Step 4: Make the smallest proven parser update and rerun tests**

Only change reviewed paths, selectors, or field names shown by the sanitized response. Run the same focused tests and expect PASS.

- [x] **Step 5: Review 国聘 without crawling protected endpoints**

Inspect the public homepage and `robots.txt` once. If a documented or directly linked public job route works without login, captcha, request signing, or session credentials, update a new design revision before implementing an adapter. Otherwise keep `iguopin_public_jobs` pending and disabled. Do not inspect private API calls, copy authentication state, or imitate browser fingerprints.

### Task 5: Remove Foreign Jobs From the Production Database

**Files:**
- Restore from: `data/backups/job_competency-20260811-230647-944845.db`
- Replace after backup: `data/job_competency.db`
- Create: `data/audits/foreign-job-exclusion-20260812.json`

- [x] **Step 1: Stop-state and path verification**

Verify no project server process is running and port 8000 is closed. Resolve the absolute paths of the current database, restoration backup, and backup destination, and confirm all are inside the project `data` directory.

- [x] **Step 2: Record pre-restore evidence**

Compute SHA-256 for the current database and restoration backup. Query counts for `jobs.ashbyhq.com` and `boards.greenhouse.io`; expected total is 30 rows, with 7 currently usable unique rows.

- [x] **Step 3: Back up the current database and restore the exact pre-import database**

Create a timestamped copy of the current database in `data/backups`, verify its hash matches the source, then replace `data/job_competency.db` with `job_competency-20260811-230647-944845.db` and verify the restored hash matches that backup exactly.

- [x] **Step 4: Create the exclusion audit**

Write UTF-8 JSON containing the operation date, excluded domains, excluded row count, current-backup path and hash, restoration-source path and hash, retained raw snapshot paths, and the reason `国外招聘来源不进入正式岗位库`.

- [x] **Step 5: Verify post-restore source counts**

Expected: zero rows for both foreign domains; domestic 智联 and NCSS records remain; the database opens successfully and all foreign raw collection files remain untouched.

### Task 6: Collect and Commit Qualified Domestic Jobs

**Files:**
- Generate: `data/collections/<domestic-run-id>/report.json`
- Generate: `data/collections/<domestic-run-id>/staged/jobs.jsonl`
- Generate: `data/collections/<domestic-run-id>/review/jobs.jsonl`
- Generate: `data/collections/<domestic-run-id>/quarantine/jobs.jsonl`

- [x] **Step 1: Inspect the 20-record smoke report**

Require `status=completed`, no access-control stop reason, nonzero parsed records, valid provenance fields, and no unexpected source domain. If these conditions fail, preserve the report and do not commit.

- [x] **Step 2: Run an expanded NCSS collection**

Run:

```powershell
python -m src.collect_jobs --source ncss_public_jobs --run-id ncss-cn-expand-20260812 --max-records 300 --max-pages 10 --max-requests 700 --dry-run
```

Expected: a bounded report. The actual valid count may be lower than 300 and must be reported honestly.

- [x] **Step 3: Commit only a qualifying signed batch**

After checking staged, review, quarantine, source distribution, family distribution, and date trust, run:

```powershell
python -m src.collect_jobs --resume-run ncss-cn-expand-20260812 --commit --confirm
```

Expected: a verified backup path, imported/revised/skipped counts, and no foreign-source record.

- [x] **Step 4: Keep commercial platforms on authorized file import**

Do not make network requests to 智联、BOSS、前程无忧、猎聘 or 拉勾. Continue importing only team-provided JSONL through `zhaopin_legacy_import` or a separately reviewed domestic platform file source with an authorization note.

### Task 7: Rebuild Knowledge Products and Verify the Project

**Files:**
- Regenerate: `data/exports/knowledge-graph.json`
- Modify: `README.md`
- Modify: `docs/数据采集运行手册.md`

- [x] **Step 1: Run focused collection tests**

Run:

```powershell
python -m pytest tests/test_job_models.py tests/test_schema_migration.py tests/test_source_registry.py tests/test_collection_service.py tests/test_collection_docs.py tests/test_ncss_adapter.py tests/test_mohrss_adapter.py tests/test_collect_jobs_cli.py -q --no-cov
```

Expected: all focused tests PASS.

- [x] **Step 2: Run lint**

Run: `python -m ruff check src tests`  
Expected: exit code 0 with no errors.

- [x] **Step 3: Rebuild all hard metrics**

Run:

```powershell
python -m src.rebuild_hard_metrics --dry-run
python -m src.rebuild_hard_metrics --full --confirm
```

Expected: a new verified database backup followed by updated duplicate groups, quality gates, levels, quarterly profiles, evolution events, knowledge chunks, and acceptance snapshot.

- [x] **Step 4: Export the current graph**

Run `python -m src.build_knowledge_base` only with the newly committed staged JSONL if the commit did not already import it through the collection pipeline; otherwise export `graph_data(include_evidence=True)` without reimporting an old raw file. Expected: `data/exports/knowledge-graph.json` contains no node or evidence URL from the two excluded foreign domains.

- [x] **Step 5: Run the complete suite with coverage**

Run: `python -m pytest -c pytest-full.ini -q`  
Expected: all tests PASS and coverage remains above the configured 60% gate.

- [x] **Step 6: Run final read-only acceptance audit**

Report exact raw and usable unique counts, source types, domestic domains, maximum-domain share, covered job families, trusted-time coverage, graph node/edge counts, latest domestic collection run, and every remaining shortfall against the 5000 to 10000 target. Explicitly confirm the system remains stopped.

- [x] **Step 7: Update user documentation**

Document the China-only source rule, public-platform smoke and commit commands, authorized commercial-platform file import, stop-on-access-control behavior, and how a team member verifies the batch report before rebuilding the graph.

## Execution Notes

This project directory is not a Git repository, so the usual per-task commit steps cannot be performed. Preserve each database backup, collection report, audit JSON, and test output as the execution checkpoints instead. Do not initialize Git as part of this task.

