# China Company Public ATS Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add fail-closed collection for approved Chinese company Feishu and Beisen recruitment portals, then collect and import enough traceable records for the formal database to contain at least 5000 usable unique jobs.

**Architecture:** Extend the existing bounded collection pipeline with reviewed POST request support and two ATS adapters. Each company remains an independent registered source, list responses are retained as raw evidence, embedded job descriptions are normalized through the existing quality gate, and only valid non-duplicates are committed. Live preflight, bulk staging, database commit, and derived knowledge rebuild remain separate checkpoints.

**Tech Stack:** Python 3.11, httpx, Pydantic, SQLAlchemy, SQLite, pytest, existing `src.job_collection` pipeline.

---

## File Map

- Modify `src/job_collection/adapters/base.py`: represent bounded GET/POST requests and declare embedded-detail behavior.
- Modify `src/job_collection/http_client.py`: send reviewed JSON POST requests without accepting credentials or unsafe headers.
- Create `src/job_collection/adapters/feishu_ats.py`: parse Feishu Hire public search responses.
- Create `src/job_collection/adapters/beisen_ats.py`: parse Beisen public social-recruitment responses.
- Modify `src/job_collection/adapters/__init__.py`: export the two adapters.
- Modify `src/job_collection/models.py`: add company name and reviewed portal path to source definitions.
- Modify `src/job_collection/service.py`: dispatch adapters by parser name, pass request metadata, and reuse list snapshots as evidence for embedded job descriptions.
- Create `src/preflight_company_ats.py`: inspect a fixed candidate list and emit a read-only approval report.
- Create `config/company_ats_candidates.json`: candidate company portals; this file never grants collection approval.
- Modify `config/job_sources.json`: add only portals that pass live preflight and manual review.
- Modify `config/job_collection_targets.json`: add an aggregate company-official target without weakening the 5000 usable-unique requirement.
- Create `tests/fixtures/company_ats/feishu-list.json`: fixed Feishu parser fixture.
- Create `tests/fixtures/company_ats/beisen-list.json`: fixed Beisen parser fixture.
- Modify `tests/test_collection_http.py`: POST, cache identity, sensitive-header, redirect, and stop-condition tests.
- Create `tests/test_feishu_ats_adapter.py`: Feishu request and parser tests.
- Create `tests/test_beisen_ats_adapter.py`: Beisen request and parser tests.
- Modify `tests/test_collection_service.py`: embedded-detail evidence and recomputation tests.
- Create `tests/test_company_ats_preflight.py`: candidate validation and report tests.
- Modify `tests/test_source_registry.py`: company source schema and scope tests.
- Modify `tests/test_collect_jobs_cli.py`: company ATS dry-run acceptance test.
- Modify `README.md` and `QUICKSTART.md`: document approved public company collection and explicit exclusions.

The workspace is not a Git repository. Do not initialize Git implicitly. At each checkpoint, retain test output, collection reports, database backups, and SHA-256 values instead of attempting a commit.

### Task 1: Safe JSON POST Requests

**Files:**
- Modify: `src/job_collection/adapters/base.py`
- Modify: `src/job_collection/http_client.py`
- Modify: `tests/test_collection_http.py`

- [ ] **Step 1: Write failing request-model tests**

Add tests asserting that `RequestSpec` accepts a POST JSON body and safe public headers, while rejecting `Cookie`, `Authorization`, `Proxy-Authorization`, `X-Api-Key`, control characters, and non-JSON body types.

```python
request = RequestSpec(
    method="POST",
    url="https://company.jobs.feishu.cn/api/v1/search/job/posts",
    headers={"Portal-Channel": "office", "website-path": "index"},
    json_body={"keyword": "Python", "limit": 20, "offset": 0},
)
assert request.method == "POST"
assert request.json_body["limit"] == 20

with pytest.raises(ValidationError):
    RequestSpec(
        method="POST",
        url="https://company.jobs.feishu.cn/api/v1/search/job/posts",
        headers={"Cookie": "session=secret"},
        json_body={},
    )
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `python -m pytest tests/test_collection_http.py -q`

Expected: new tests fail because `RequestSpec` only accepts GET and `BoundedHttpClient.fetch` cannot carry a JSON body.

- [ ] **Step 3: Extend the request contract**

Implement these fields in `RequestSpec`:

```python
method: Literal["GET", "POST"] = "GET"
url: str = Field(min_length=1)
params: dict[str, str | int | float | bool] = Field(default_factory=dict)
headers: dict[str, str] = Field(default_factory=dict)
json_body: dict[str, Any] | None = None
```

Add a model validator with these exact rules:

- GET requires `json_body is None`.
- Header names are ASCII tokens and values contain no control characters.
- Case-insensitive blocked headers are `authorization`, `proxy-authorization`, `cookie`, `set-cookie`, `x-api-key`, `x-auth-token`, and `host`.
- POST requires a JSON object and serializes it deterministically with sorted keys for snapshot identity.

- [ ] **Step 4: Extend the bounded HTTP client**

Change `BoundedHttpClient.fetch` to accept `method`, `headers`, and `json_body`. Merge only validated adapter headers with the project User-Agent. Send the request with:

```python
async with self.client.stream(
    method,
    url,
    headers=request_headers,
    json=json_body,
    timeout=self.timeout_seconds,
    follow_redirects=False,
) as streamed:
```

Use `METHOD + canonical URL + SHA-256(deterministic JSON body)` as the snapshot identity. Stop the source on any redirect from a POST request; do not replay a POST automatically to another URL. Preserve existing rate limit, request budget, response-size limit, access-gate detection, and 401/403/429 behavior.

- [ ] **Step 5: Run HTTP tests and checkpoint**

Run: `python -m pytest tests/test_collection_http.py -q`

Expected: all tests pass, including distinct cached snapshots for different POST bodies and no secret headers in saved metadata.

### Task 2: Feishu Public Recruitment Adapter

**Files:**
- Modify: `src/job_collection/models.py`
- Create: `src/job_collection/adapters/feishu_ats.py`
- Create: `tests/fixtures/company_ats/feishu-list.json`
- Create: `tests/test_feishu_ats_adapter.py`
- Modify: `tests/test_source_registry.py`

- [ ] **Step 1: Write failing source-definition tests**

Add optional `organization_name` and `portal_path` fields to `SourceDefinition`, then test that a source with `parser_name="feishu_company_ats"` is invalid unless both fields are non-empty and `portal_path` matches `^[A-Za-z0-9_/-]{1,40}$`.

```python
source = SourceDefinition.model_validate(
    {
        "source_id": "company_feishu_zhipu",
        "source_name": "智谱AI官方招聘",
        "source_type": "company_official",
        "market_scope": "china",
        "base_url": "https://zhipu-ai.jobs.feishu.cn",
        "allowed_paths": ["/api/v1/search/job/posts", "/index/position/"],
        "collection_mode": "public_json",
        "compliance_status": "approved",
        "compliance_note": "人工复核日期 2026-08-13：公开企业招聘门户。",
        "rate_limit_seconds": 3.0,
        "max_pages": 10,
        "max_records": 300,
        "parser_name": "feishu_company_ats",
        "parser_version": "v1",
        "organization_name": "智谱AI",
        "portal_path": "index",
        "enabled": True,
    }
)
assert source.organization_name == "智谱AI"
```

- [ ] **Step 2: Write failing Feishu adapter tests**

The fixture must contain two sanitized records with `id`, `title`, `description`, `requirement`, `city_list`, `job_function`, `recruit_type`, and `publish_time`. Test:

- POST URL is exactly `/api/v1/search/job/posts`.
- Body contains keyword, limit, offset, `portal_type=2`, and empty filter lists.
- Headers contain `Portal-Channel=office`, `Portal-Platform=pc`, and the reviewed `website-path`.
- Parser rejects non-object roots, non-zero API codes, missing `data`, non-array `job_post_list`, unsafe IDs, count/pagination contradictions, and records without descriptions.
- Published epoch milliseconds become timezone-aware ISO values.
- Detail URL remains on the registered host and reviewed portal path.

- [ ] **Step 3: Run Feishu tests and verify RED**

Run: `python -m pytest tests/test_feishu_ats_adapter.py tests/test_source_registry.py -q`

Expected: tests fail because the company metadata and adapter do not exist.

- [ ] **Step 4: Implement `FeishuATSAdapter`**

Expose `FeishuATSAdapter(SourceAdapter)` with `site_page_size = 50` and
`embedded_detail = True`. Its public methods are `__init__(source, registry)`,
`build_list_request(query, offset, limit) -> RequestSpec`,
`parse_list(content, content_type, expected_offset, expected_limit) -> ListPage`,
`build_detail_url(item) -> str`, and
`parse_detail(content, item, url) -> dict[str, object]`.

Combine `description` and `requirement` with one blank line. Set company name from the reviewed source definition, not from response text. Store non-sensitive department and recruitment type under `adapter_extra`. Do not infer salary, education, experience, or publication time when absent.

- [ ] **Step 5: Run Feishu tests and checkpoint**

Run: `python -m pytest tests/test_feishu_ats_adapter.py tests/test_source_registry.py -q`

Expected: all tests pass.

### Task 3: Beisen Public Recruitment Adapter

**Files:**
- Create: `src/job_collection/adapters/beisen_ats.py`
- Create: `tests/fixtures/company_ats/beisen-list.json`
- Create: `tests/test_beisen_ats_adapter.py`

- [ ] **Step 1: Write failing Beisen adapter tests**

The fixture must contain two sanitized records with `JobAdId`, `JobAdName`, `LocNames`, `Category`, `PostDate`, `Salary`, and `Duty`. Test the exact POST body:

```python
{
    "PageIndex": 0,
    "PageSize": 20,
    "LocId": [],
    "Category": ["1"],
    "KeyWords": "Python",
    "SpecialType": 0,
    "PortalId": "",
    "DisplayFields": ["Category", "Kind", "LocId", "PostDate", "Salary"],
}
```

Also test list totals, location joining, ISO publication time, `(J12345)` request-number extraction, empty-duty rejection, and same-origin detail URL validation.

- [ ] **Step 2: Run Beisen tests and verify RED**

Run: `python -m pytest tests/test_beisen_ats_adapter.py -q`

Expected: import fails because `BeisenATSAdapter` does not exist.

- [ ] **Step 3: Implement `BeisenATSAdapter`**

Expose the same `SourceAdapter` methods as Feishu with `site_page_size=50` and `embedded_detail=True`. Convert record offsets to `PageIndex = offset // limit`; parse response `Data` and `Count`; use the registered company name; preserve salary as source text; and use the public `/social/jobs` portal URL as the traceable source URL when no stable SPA deep link is available.

- [ ] **Step 4: Run Beisen tests and checkpoint**

Run: `python -m pytest tests/test_beisen_ats_adapter.py -q`

Expected: all tests pass.

### Task 4: Embedded-Detail Evidence in the Existing Pipeline

**Files:**
- Modify: `src/job_collection/adapters/__init__.py`
- Modify: `src/job_collection/service.py`
- Modify: `tests/test_collection_service.py`
- Modify: `tests/test_collect_jobs_cli.py`

- [ ] **Step 1: Write failing service tests**

Create an in-memory adapter fixture with `embedded_detail=True`. Assert one list request stages two records with no detail network requests, and each staged record contains:

```python
evidence = record.adapter_extra["collection_evidence"]
assert evidence["detail_embedded"] is True
assert evidence["list_url"].startswith("https://")
assert evidence["detail_request_url"].startswith("https://")
assert record.snapshot_hash == list_fetch_result.content_hash
```

Tamper with the list snapshot and assert commit recomputation raises `CollectionReportError`. Tamper with the detail URL, source ID, company name, portal path, or list record ID and assert commit fails closed.

- [ ] **Step 2: Run service tests and verify RED**

Run: `python -m pytest tests/test_collection_service.py tests/test_collect_jobs_cli.py -q`

Expected: embedded records still trigger detail GET requests and recomputation requires a detail snapshot.

- [ ] **Step 3: Dispatch adapters by reviewed parser name**

Change `_default_adapter` to this mapping:

```python
factories = {
    "ncss": NCSSAdapter,
    "mohrss": MOHRSSAdapter,
    "feishu_company_ats": FeishuATSAdapter,
    "beisen_company_ats": BeisenATSAdapter,
}
factory = factories.get(source.parser_name)
```

Unknown parser names remain blocked. Do not dispatch from user-controlled ATS labels or URL suffixes.

- [ ] **Step 4: Pass bounded request metadata**

For list requests, pass the reviewed request fields into the fetcher:

```python
list_result = await fetcher.fetch(
    list_url,
    method=request.method,
    headers=request.headers,
    json_body=request.json_body,
    resume=True,
)
```

Existing GET adapters continue to produce the same URLs and snapshots.

- [ ] **Step 5: Implement embedded-detail staging and recomputation**

When `adapter.embedded_detail is True`, call `parse_detail` directly from the parsed list item and use the persisted list response as the snapshot hash and commit-time evidence. Record `detail_embedded=True`; do not call the network fetcher for the detail URL. During commit, reload and verify the list snapshot, reparse the item, rebuild the detail URL, rerun normalization and quality classification, and compare the recomputed semantic record with staged data.

- [ ] **Step 6: Run service and CLI tests**

Run: `python -m pytest tests/test_collection_service.py tests/test_collect_jobs_cli.py tests/test_feishu_ats_adapter.py tests/test_beisen_ats_adapter.py -q`

Expected: all tests pass and existing NCSS/MOHRSS behavior remains unchanged.

### Task 5: Candidate Preflight and Approved Source Registry

**Files:**
- Create: `config/company_ats_candidates.json`
- Create: `src/preflight_company_ats.py`
- Create: `tests/test_company_ats_preflight.py`
- Modify: `config/job_sources.json`
- Modify: `config/job_collection_targets.json`

- [ ] **Step 1: Add the fixed candidate inventory**

Record these candidates with `status="candidate"`; candidate status must never grant collection permission:

```text
Feishu: zhipu-ai.jobs.feishu.cn, agirobot.jobs.feishu.cn,
li.jobs.feishu.cn, x2-robot.jobs.feishu.cn, zitd5je6f7j.jobs.feishu.cn,
owm6ymi5v9b.jobs.feishu.cn, k0fqxcszc9.jobs.feishu.cn,
nwd4iy9rd2s.jobs.feishu.cn, flexivrobotics.jobs.feishu.cn,
cq6qe6bvfr6.jobs.feishu.cn, vrfi1sk8a0.jobs.feishu.cn,
moonshot.jobs.feishu.cn, modelbest.jobs.feishu.cn, 01ai.jobs.feishu.cn,
infinigence.jobs.feishu.cn, shengshu.jobs.feishu.cn,
aisphere.jobs.feishu.cn, aibee.jobs.feishu.cn,
juzihudong.jobs.feishu.cn, ponyai.jobs.feishu.cn,
mthreads.jobs.feishu.cn, kwh0jtf778.jobs.feishu.cn,
anker-in.jobs.feishu.cn, arashivision.jobs.feishu.cn,
thundersoft.jobs.feishu.cn, blacklake.jobs.feishu.cn,
zegocloud.jobs.feishu.cn, kurogame.jobs.feishu.cn,
xd-legacy.jobs.feishu.cn, kengic.jobs.feishu.cn.

Beisen: dreame.zhiye.com, chery.zhiye.com, leapmotor.zhiye.com,
boe.zhiye.com, sany.zhiye.com.
```

Each entry includes a stable source ID, Chinese company name, ATS type, portal host, and reviewed public homepage URL.

- [ ] **Step 2: Write failing preflight tests**

Use `httpx.MockTransport` and assert that preflight:

- rejects hosts outside `*.jobs.feishu.cn` and `*.zhiye.com`;
- discovers Feishu `website_info.path` only from the public homepage script;
- performs one bounded 1-record ATS request;
- records status, field presence, portal path, response hash, check time, and stop reason;
- never writes `job_sources.json` or the database;
- marks login, captcha, 401, 403, 429, encrypted, malformed, and empty responses as rejected.

- [ ] **Step 3: Run preflight tests and verify RED**

Run: `python -m pytest tests/test_company_ats_preflight.py -q`

Expected: module import fails.

- [ ] **Step 4: Implement the read-only preflight command**

Command:

```powershell
python -m src.preflight_company_ats --candidates config/company_ats_candidates.json --output data/company_ats_preflight-20260813.json --max-sources 35 --delay-seconds 3
```

The output is UTF-8 JSON and contains no response bodies, cookies, tokens, request headers, contact data, or credentials. The command exits nonzero only for invalid input or an unwritable report; individual source failures are recorded in the report.

- [ ] **Step 5: Run live preflight and manually review the report**

Expected acceptance for a portal:

- public homepage and ATS query return 2xx;
- no login, captcha, cookie, signature, encrypted response, or rate-limit requirement;
- at least one record has title, ID, description, company source, region or explicit empty region, and a traceable URL;
- portal path and domain are stable;
- no personal applicant data is returned.

- [ ] **Step 6: Register only accepted sources**

For each accepted source, append one `company_official`, `market_scope="china"`, `collection_mode="public_json"`, `compliance_status="approved"`, enabled registry entry. Use the exact host and portal path from the preflight report, `rate_limit_seconds=3.0`, `max_pages=20`, `max_records=500`, and parser version `v1`.

Do not add failed candidates as approved. Retain failed candidates only in the preflight report with their stop reason. Add `company_official_public_ats: 4000` to target reporting without changing `minimum_usable_unique=5000`, `minimum_usable_per_family=100`, or `maximum_single_domain_share=0.35`.

- [ ] **Step 7: Validate the registry and checkpoint**

Run:

```powershell
python -m pytest tests/test_source_registry.py tests/test_company_ats_preflight.py -q
python -c "from src.job_collection.source_registry import SourceRegistry; r=SourceRegistry.load('config/job_sources.json'); xs=[s for s in r.definitions if s.parser_name in {'feishu_company_ats','beisen_company_ats'}]; print(len(xs), len({s.base_url for s in xs}))"
```

Expected: tests pass; approved source count equals distinct registered host count and is at least 30. If fewer than 30 candidates pass, expand the candidate inventory with additional reviewed Chinese company official portals before bulk collection.

### Task 6: Documentation and Focused Regression

**Files:**
- Modify: `README.md`
- Modify: `QUICKSTART.md`
- Modify: `tests/test_collection_docs.py`

- [ ] **Step 1: Write failing documentation assertions**

Require documentation to state: public Chinese company portals only, Feishu and Beisen support, dry-run before commit, no login/Cookie/captcha/signature/decryption/proxy, Moka excluded, commercial recruitment sites remain file-import only, and 5000 counts valid non-duplicates rather than raw rows.

- [ ] **Step 2: Run documentation tests and verify RED**

Run: `python -m pytest tests/test_collection_docs.py -q`

Expected: missing company ATS instructions fail.

- [ ] **Step 3: Add exact operator commands**

Document one 20-record smoke run, one bounded production run, report locations, commit command, rebuild command, and coverage command using a registered source such as `company_feishu_zhipu`.

- [ ] **Step 4: Run focused regression**

Run:

```powershell
python -m pytest tests/test_collection_http.py tests/test_feishu_ats_adapter.py tests/test_beisen_ats_adapter.py tests/test_collection_service.py tests/test_source_registry.py tests/test_collect_jobs_cli.py tests/test_company_ats_preflight.py tests/test_collection_docs.py -q
```

Expected: all focused tests pass.

### Task 7: Smoke Collection, Bulk Staging, and Commit

**Files:**
- Generate: `data/collections/company-ats-smoke-20260813-001/`
- Generate: `data/collections/company-ats-prod-20260813-001/` and subsequent numbered runs
- Generate: `data/backups/job_competency-*.db`

- [ ] **Step 1: Record the pre-collection baseline**

Run:

```powershell
python -m src.collect_jobs --coverage-report > data/company-ats-coverage-before-20260813.json
```

Expected: `usable_unique_job_postings` is 1310 unless another user-approved import changed the database after design approval. Record the actual value and use it as the auditable baseline.

- [ ] **Step 2: Run 20-record smoke batches**

Run approved sources in groups of at most four, respecting the CLI limit:

```powershell
python -m src.collect_jobs --dry-run --source company_feishu_zhipu --source company_feishu_agibot --source company_feishu_li --source company_beisen_dreame --run-id company-ats-smoke-20260813-001 --max-records 20 --max-pages 4 --max-requests 20
```

Expected: no source boundary errors, no detail GET requests for embedded descriptions, and report counts reconcile across staged, review, quarantine, and duplicate files.

- [ ] **Step 3: Inspect every smoke report before scaling**

Parse `report.json` and all JSONL buckets. Disable any source with access gates, response-structure errors, invalid dates, repeated empty descriptions, unexpected personal data, or less than 70% title/company/description/source-link completeness. Do not commit smoke runs whose evidence is uncertain.

- [ ] **Step 4: Run bounded production batches**

Use numbered run IDs and groups of up to four approved sources. Set each run to at most 1000 records and 400 requests:

```powershell
python -m src.collect_jobs --dry-run --source company_feishu_zhipu --source company_feishu_agibot --source company_feishu_li --source company_beisen_dreame --run-id company-ats-prod-20260813-001 --max-records 1000 --max-pages 80 --max-requests 400
```

Repeat with non-overlapping approved source groups. After each run, inspect `report.json`; only commit runs whose artifacts and counts reconcile.

- [ ] **Step 5: Commit accepted runs one at a time**

Run:

```powershell
python -m src.collect_jobs --commit --resume-run company-ats-prod-20260813-001 --confirm
```

Expected: a verified SQLite backup is created before import; imported and skipped counts are reported; review and quarantine records are not promoted.

- [ ] **Step 6: Recalculate coverage after each commit**

Run:

```powershell
python -m src.collect_jobs --coverage-report > data/company-ats-coverage-current-20260813.json
```

Use `recommended_batches` and family deficits to select subsequent company/query groups. Stop collecting a source when its domain would exceed 35% of usable unique records. Continue until usable unique jobs are at least 5000 or all approved public sources are exhausted. If exhausted below 5000, report the exact shortfall and add more reviewed official company portals; do not lower the gate or fabricate data.

### Task 8: Rebuild Knowledge Artifacts and Final Verification

**Files:**
- Generate: derived database rows and current graph/report artifacts under `data/`
- Generate: final coverage and verification reports under `data/`

- [ ] **Step 1: Run full test verification**

Run:

```powershell
python -m compileall -q src model_class schemes config tests
python -m pytest -q
python -m pytest -c pytest-full.ini -q
```

Expected: compilation succeeds, focused-default suite passes, and full suite passes its configured coverage threshold.

- [ ] **Step 2: Perform a read-only hard-metrics inspection**

Run: `python -m src.rebuild_hard_metrics --dry-run`

Expected: database is readable and no backup or mutation occurs.

- [ ] **Step 3: Rebuild all derived metrics and knowledge artifacts**

Run: `python -m src.rebuild_hard_metrics --full --confirm`

Expected: a new verified database backup is created, then duplicate groups, quality gates, job levels, quarterly profiles, evolution events, knowledge chunks, and acceptance snapshots are rebuilt.

- [ ] **Step 4: Produce final acceptance evidence**

Run:

```powershell
python -m src.collect_jobs --coverage-report > data/company-ats-coverage-final-20260813.json
python -c "import json; p='data/company-ats-coverage-final-20260813.json'; d=json.load(open(p,encoding='utf-8')); print(json.dumps(d,ensure_ascii=False,indent=2))"
```

Verify:

- usable unique jobs are at least 5000 and no more than 10000;
- every counted row has `gate_status=valid` and no duplicate parent;
- all 22 job families are covered and the report exposes any family below 100;
- at least 30 company portals and both ATS types contributed traceable records;
- maximum single-domain share is at most 0.35;
- publication or first-seen time coverage meets the existing target;
- all new source URLs belong to approved Chinese company registry entries;
- knowledge chunks, graph entities, quarterly profiles, and evolution events were rebuilt after the final import.

- [ ] **Step 5: Preserve an auditable completion bundle**

Retain the final coverage JSON, all accepted run reports, preflight report, source registry, database backup paths and SHA-256 values, full-test output, rebuild summary, and graph export. Do not include credentials, cookies, request headers, applicant data, or rejected response bodies.
