# China Public Recruitment Collector Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore low-frequency collection of public, unauthenticated China Public Recruitment jobs and import only records that pass the existing quality gates.

**Architecture:** Keep the existing `MOHRSSAdapter`, bounded HTTP client, staging buckets, and commit pipeline. Add a scoped bootstrap request that establishes the public anonymous session, correct the job-title query parameters, support the site's current pagination semantics, and enable the source only after focused tests pass.

**Tech Stack:** Python 3.12, httpx, BeautifulSoup, Pydantic, pytest, SQLAlchemy, SQLite

**Repository note:** This workspace is not a Git worktree, so commit steps cannot be performed. All edits remain directly visible in the shared workspace.

---

### Task 1: Capture the Current Public Contract in Failing Tests

**Files:**
- Modify: `tests/test_mohrss_adapter.py`
- Modify: `tests/test_collection_service.py`

- [ ] **Step 1: Add a request-contract test**

Add assertions that `build_bootstrap_request()` targets `/cjobs/jobinfolist/listJobinfolistIndex`, and that a plain query uses `textfield`, `searchtype=gw`, and `orderType=score` without placing text in `ACB241`.

- [ ] **Step 2: Add a current-pagination test**

Create a sanitized response with `pageNo=1`, `pagecount=4`, `totalpages=4`, `totalcount=61`, and 20 list records. Assert `ListPage(total=61, offset=0, limit=20, has_more=True)`.

- [ ] **Step 3: Add a service bootstrap test**

Use a fake MOHRSS adapter and fetcher to assert the bootstrap URL is fetched before the first list URL, the bootstrap consumes request budget, and the public page response is snapshotted without persisting Cookie headers.

- [ ] **Step 4: Run the focused tests and verify RED**

Run:

```powershell
python -m pytest tests/test_mohrss_adapter.py tests/test_collection_service.py -q
```

Expected: the new tests fail because the bootstrap request does not exist, plain queries still use `ACB241`, and current pagination is interpreted as a page size.

### Task 2: Implement the Minimum Adapter and Service Repair

**Files:**
- Modify: `src/job_collection/adapters/mohrss.py`
- Modify: `src/job_collection/service.py`

- [ ] **Step 1: Add the bootstrap request**

Add `initial_list_path = "/cjobs/jobinfolist/listJobinfolistIndex"` and a `build_bootstrap_request()` method returning a scoped GET `RequestSpec` with no parameters.

- [ ] **Step 2: Correct plain-query mapping**

Map string queries to:

```python
{"textfield": query, "searchtype": "gw", "orderType": "score"}
```

Keep mapping inputs available for reviewed filters, and continue rejecting externally supplied pagination fields.

- [ ] **Step 3: Parse current pagination safely**

Use the fixed reviewed page size of 20 when `pagecount == totalpages`; require `totalpages == ceil(totalcount / 20)` for non-empty results. Preserve the prior fixture interpretation when `pagecount != totalpages` so archived snapshots remain replayable.

- [ ] **Step 4: Bootstrap the same bounded fetcher session**

Before MOHRSS list collection, fetch the bootstrap URL with `resume=False`, persist the public response evidence, count it against the request budget, and then issue list/detail requests through the same fetcher instance. A resumed run repeats the bootstrap because cached HTML cannot recreate the in-memory anonymous session.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```powershell
python -m pytest tests/test_mohrss_adapter.py tests/test_collection_service.py -q
```

Expected: all focused tests pass.

### Task 3: Enable the Revalidated Public Source

**Files:**
- Modify: `config/job_sources.json`
- Modify: `README.md`
- Modify: `USER_GUIDE.md`
- Modify: `tests/test_source_registry.py`
- Modify: `tests/test_collection_docs.py`

- [ ] **Step 1: Write failing registry expectations**

Assert `mohrss_public_jobs.enabled is True`, `compliance_status == "approved"`, the note records the 2026-08-12 anonymous-session revalidation, and the five-second rate limit remains unchanged.

- [ ] **Step 2: Run registry tests and verify RED**

Run:

```powershell
python -m pytest tests/test_source_registry.py tests/test_collection_docs.py -q
```

Expected: failure because the source is still disabled.

- [ ] **Step 3: Update registry and user documentation**

Enable only `mohrss_public_jobs`. Document the reviewed index/list/detail paths, anonymous-session bootstrap, five-second rate limit, stop conditions, and the fact that commercial platforms remain file-import only.

- [ ] **Step 4: Run registry tests and verify GREEN**

Run the same command and expect all tests to pass.

### Task 4: Live Smoke Collection and Controlled Import

**Files:**
- Create through collection service: `data/collections/mohrss-smoke-20260812-*`
- Modify through commit pipeline: configured SQLite database and derived knowledge/graph data

- [ ] **Step 1: Run a bounded dry run**

```powershell
python -m src.collect_jobs --source mohrss_public_jobs --run-id mohrss-smoke-20260812-001 --max-records 20 --max-pages 20 --max-requests 100 --dry-run
```

Expected: the source completes without a login, captcha, HTTP 500, or pagination-structure stop.

- [ ] **Step 2: Inspect the immutable report**

Verify fetched, parsed, valid, review, quarantine, duplicate, family, domain, and date-trust counts. Do not commit if staging validation fails or the source stops on a structural/access condition.

- [ ] **Step 3: Commit only valid staged records**

```powershell
python -m src.collect_jobs --commit --resume-run mohrss-smoke-20260812-001 --confirm
```

Expected: database backup is created and only new valid unique records are imported.

- [ ] **Step 4: Rebuild competition artifacts**

```powershell
python -m src.rebuild_hard_metrics --full --confirm --after-collection-run mohrss-smoke-20260812-001
```

Expected: knowledge base, graph, profiles, evolution outputs, and acceptance statistics rebuild successfully.

### Task 5: Final Verification

**Files:**
- No additional production files expected

- [ ] **Step 1: Run full tests**

```powershell
python -m pytest -c pytest-full.ini
```

- [ ] **Step 2: Run code quality checks**

```powershell
python -m ruff check src tests
```

- [ ] **Step 3: Generate current coverage report**

```powershell
python -m src.collect_jobs --coverage-report
```

Report the imported increment, current usable unique count, source-domain distribution, missing families, and remaining gap to 5000 without counting review or synthetic records.
