# Multisource Job Collection and Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不虚构岗位、不绕过站点限制的前提下，为现有系统增加可持续的多源真实岗位采集、可信时间治理、旧数据修复、跨来源画像置信度和按有效唯一岗位计算的验收能力，并完成首批真实岗位补采与导入。

**Architecture:** 保留现有 `ImportBatch -> RawJobRecord -> JobPosting -> 质量门禁 -> 季度画像/知识图谱` 主链路，在其前面增加独立的来源注册、合规检查、限速请求、缓存快照、来源适配器和暂存报告。采集器只生成统一 JSONL；显式 `--commit` 时才备份数据库并调用现有导入服务。岗位表增加来源和时间证据字段，画像与验收只消费符合各自规则的数据集合。

**Tech Stack:** Python 3.11、FastAPI、SQLAlchemy 2、SQLite、Pydantic 2、HTTPX、BeautifulSoup 4、Pytest、现有原生 HTML/JavaScript 前端。

---

## Implementation Constraints

- 自动采集只允许 `compliance_status=approved` 的来源；`manual_only` 只处理团队提供的 URL 清单或导出文件。
- 默认命令是 dry-run；只有 `--commit` 可写主数据库，且写入前必须创建 SQLite 备份。
- 遇到 401、403、429、登录页、验证码页或页面结构异常时立即停止该来源，不实现绕过逻辑。
- `published_at` 只能来自页面明确字段及其证据；不得从 JD 正文中的项目日期、毕业日期或任职日期推断。
- 合成简历继续仅作测试，不计入真实岗位数量；不得生成虚构岗位填充数据目标。
- 首批目标为新增 800-1200 条有效唯一真实岗位。若公开来源当日可用性或合规门禁限制数量，导入实际通过的数量并在报告中说明差额。
- 自动化测试只使用本地固定响应，不依赖实时网站；实时连通性检查作为独立低频命令。
- 项目不是 Git 仓库，因此计划中的验证不包含提交动作。

## Task 1: Add Provenance and Collection Storage

**Files:**
- Modify: `model_class/job_competency.py`
- Modify: `model_class/knowledge_base.py`
- Modify: `schemes/job_competency.py`
- Modify: `src/schema_migration.py`
- Test: `tests/test_job_models.py`
- Test: `tests/test_schema_migration.py`

- [ ] **Step 1: Write failing model tests**

Add assertions that `JobPosting` exposes:

```python
required = {
    "source_id",
    "source_domain",
    "source_record_id",
    "published_at_evidence",
    "published_at_confidence",
    "published_at_trusted",
    "first_seen_at",
    "last_seen_at",
    "snapshot_hash",
    "parser_name",
    "parser_version",
    "collection_method",
}
assert required <= set(JobPosting.__table__.columns.keys())
```

Assert that metadata contains `job_source`, `collection_run`, `collection_snapshot`, and `data_repair_audit` tables. Assert `JobProfileSkill` contains `source_type_count`, `source_domain_count`, `company_count`, and `cross_source_status`.

- [ ] **Step 2: Write a failing legacy migration test**

Extend `tests/test_schema_migration.py` so a legacy row survives migration and receives nullable/default provenance fields. Retain the existing `competition_hard_metrics_v1` record, add a separate `multisource_provenance_v1` migration id, and expect an idempotent second execution.

- [ ] **Step 3: Run focused tests and confirm failure**

Run: `python -m pytest tests/test_job_models.py tests/test_schema_migration.py -q`

Expected: failures for missing columns/tables and migration id.

- [ ] **Step 4: Implement additive models and migration**

Add the provenance columns above. Use `published_at_confidence FLOAT NOT NULL DEFAULT 0`, `published_at_trusted BOOLEAN NOT NULL DEFAULT 0`, and nullable evidence/observation fields so legacy rows remain readable.

Add these models in `model_class/knowledge_base.py`:

```python
class JobSource(Base):
    # source_id unique, source_name/type, base_url, allowed_paths_json,
    # collection_mode, compliance_status/note, rate/page/record limits,
    # parser name/version, enabled, created_at, updated_at

class CollectionRun(Base):
    # run_id unique, source_ids_json, mode, status, staging_dir,
    # fetched/parsed/valid/review/quarantined/duplicate/imported counts,
    # summary_json, started_at, completed_at

class CollectionSnapshot(Base):
    # run FK, source FK, source_record_id, source_url, response_status,
    # content_hash, relative_path, fetched_at, parser_version, parse_status/error

class DataRepairAudit(Base):
    # repair_run_id, posting FK, field_name, before_json, after_json,
    # reason_code, rule_version, applied, created_at
```

Add indexes on `JobPosting.source_domain`, `(source_id, source_record_id)`, and trusted publication time. Add matching optional fields to `JobPostingInput`; keep `extra="allow"` for backward compatibility.

Refactor `src/schema_migration.py` to support ordered migration definitions rather than replacing the existing migration id. A database that already has hard-metric columns must receive only `multisource_provenance_v1`.

- [ ] **Step 5: Run focused tests**

Run: `python -m pytest tests/test_job_models.py tests/test_schema_migration.py -q`

Expected: pass and legacy row count remains one.

## Task 2: Build the Source Registry and Compliance Guard

**Files:**
- Create: `config/job_sources.json`
- Create: `src/job_collection/__init__.py`
- Create: `src/job_collection/models.py`
- Create: `src/job_collection/source_registry.py`
- Create: `tests/test_source_registry.py`

- [ ] **Step 1: Write failing registry tests**

Cover these cases:

```python
registry.require_automatic("ncss_public_jobs")       # allowed
registry.require_automatic("mohrss_public_jobs")     # allowed
with pytest.raises(CollectionBlocked):
    registry.require_automatic("company_official_manifest")
with pytest.raises(CollectionBlocked):
    registry.require_automatic("unknown_source")
```

Also reject URLs whose host or path is outside the source registration, including redirects to an unregistered host.

- [ ] **Step 2: Run the test and confirm failure**

Run: `python -m pytest tests/test_source_registry.py -q`

- [ ] **Step 3: Implement typed registry records**

Create frozen dataclasses or Pydantic models for `SourceDefinition`, `CollectionRequest`, `UnifiedJobRecord`, and `CollectionResult`. Normalize hosts to lowercase and compare parsed URL hosts exactly, not with substring checks.

- [ ] **Step 4: Add the initial registry**

Configure:

- `ncss_public_jobs`: `university_recruitment`, public JSON/list and public HTML detail paths, low request rate, explicitly bounded pages and records.
- `mohrss_public_jobs`: `public_service`, public server-rendered list/detail paths, low request rate, explicitly bounded pages and records.
- `company_official_manifest`: `company_official`, `manual_url_manifest`, `manual_only`.
- `zhaopin_legacy_import`: `authorized_platform`, `file_import`, `manual_only`; this labels historical imported data but never triggers network access.

Store the manual review date and reason in `compliance_note`. Registry loading must fail closed if a required field or status is invalid.

At the start of each collection run, upsert the validated JSON definitions into `JobSource`; the JSON file remains the reviewed configuration source, while the database row records exactly which definition the run used.

- [ ] **Step 5: Run focused tests**

Run: `python -m pytest tests/test_source_registry.py -q`

Expected: all compliance and URL-scope tests pass.

## Task 3: Implement Safe HTTP Fetching, Cache, and Checkpoints

**Files:**
- Create: `src/job_collection/http_client.py`
- Create: `src/job_collection/storage.py`
- Create: `tests/test_collection_http.py`

- [ ] **Step 1: Write failing HTTP tests with `httpx.MockTransport`**

Test:

- successful response is written under `data/collections/<run-id>/raw/<source-id>/` with SHA-256 metadata;
- the same URL and response hash are served from cache on resume;
- 500 and transport errors retry at most the configured limit;
- 401, 403, and 429 raise `SourceStopped` immediately;
- HTML containing login/captcha markers raises `SourceStopped`;
- redirect targets pass through source host/path validation;
- checkpoint records the last completed page and detail URL.

- [ ] **Step 2: Run the tests and confirm failure**

Run: `python -m pytest tests/test_collection_http.py -q`

- [ ] **Step 3: Implement the bounded client**

Use one `httpx.AsyncClient` with explicit timeout, user agent, configured inter-request sleep, finite retries, exponential backoff, and no credentials. Save response bytes atomically and write a JSON metadata sidecar containing URL, status, fetched time, content type, hash, source/parser versions, and cache decision.

- [ ] **Step 4: Implement checkpoint resume**

Write `checkpoint.json` only after a page/detail unit completes. On resume, verify snapshot hashes before skipping. Reject any resolved path outside the collection run directory.

- [ ] **Step 5: Run focused tests**

Run: `python -m pytest tests/test_collection_http.py -q`

## Task 4: Normalize Fields and Classify the 22 Job Families

**Files:**
- Create: `config/job_family_queries.json`
- Create: `src/job_collection/normalizer.py`
- Create: `src/job_collection/family_classifier.py`
- Create: `tests/test_job_collection_normalizer.py`
- Create: `tests/test_job_family_classifier.py`

- [ ] **Step 1: Write failing normalization tests**

Test that salary-like or requirement-like industry values become `unknown`, blank publication dates remain `None`, publication evidence is retained, source domain is derived from the URL, and observation fields are populated without impersonating publication time.

- [ ] **Step 2: Write failing family classification tests**

Create at least two positive title/JD examples and one ambiguity example for each of the 22 existing family codes. Require high-confidence title plus skill evidence for an automatic assignment; ambiguous AI/数据/运维 titles return a review result with candidate codes and evidence.

- [ ] **Step 3: Run tests and confirm failure**

Run: `python -m pytest tests/test_job_collection_normalizer.py tests/test_job_family_classifier.py -q`

- [ ] **Step 4: Implement deterministic normalization**

Reuse `normalize_text`, `content_hash`, and `simhash64`. Add field-specific validation for industry, region, education, experience, and salary. Set:

```python
published_at_trusted = bool(
    published_at and published_at_evidence and published_at_confidence >= 0.8
)
```

Keep parser output and the original source payload in staging JSONL.

- [ ] **Step 5: Implement query quotas and family classification**

Map the 22 family codes to precise Chinese query terms, title aliases, required skill indicators, and exclusion terms. The scheduler prioritizes families below 100 valid unique records and stops increasing a family after its configured quota.

- [ ] **Step 6: Run focused tests**

Run: `python -m pytest tests/test_job_collection_normalizer.py tests/test_job_family_classifier.py -q`

## Task 5: Add the National College Employment Service Adapter

**Files:**
- Create: `src/job_collection/adapters/__init__.py`
- Create: `src/job_collection/adapters/base.py`
- Create: `src/job_collection/adapters/ncss.py`
- Create: `tests/fixtures/ncss/jobs-list.json`
- Create: `tests/fixtures/ncss/job-detail.html`
- Create: `tests/test_ncss_adapter.py`

- [ ] **Step 1: Save small sanitized fixtures from the observed public responses**

Keep only the fields needed by parser tests. The list fixture must include pagination and two records; the detail fixture must include title, company, visible publication metadata, and the JD `<pre>` content.

- [ ] **Step 2: Write failing adapter tests**

Assert the list request uses `GET https://cnu.ncss.cn/student/jobs/jobslist/ajax/` with bounded `offset`/`limit`, and details resolve to `https://cnu.ncss.cn/student/jobs/{jobId}/detail.html`. Assert parsed output retains `jobId`, pay, degree, region, company, publication evidence, source URL, and full JD text.

- [ ] **Step 3: Run the test and confirm failure**

Run: `python -m pytest tests/test_ncss_adapter.py -q`

- [ ] **Step 4: Implement the adapter**

Parse list JSON through structured keys and detail HTML through BeautifulSoup selectors. Treat missing `data.list`, missing `jobId`, or missing JD container as a structure anomaly that stops/quarantines the source batch. Do not scrape script text with regular expressions.

- [ ] **Step 5: Run focused tests**

Run: `python -m pytest tests/test_ncss_adapter.py -q`

## Task 6: Add the China Public Recruitment Adapter

**Files:**
- Create: `src/job_collection/adapters/mohrss.py`
- Create: `tests/fixtures/mohrss/job-list.html`
- Create: `tests/fixtures/mohrss/job-detail.html`
- Create: `tests/test_mohrss_adapter.py`

- [ ] **Step 1: Save small sanitized list/detail fixtures**

Include two list records from the hidden `findjoblist` structure and one public detail page. Preserve HTML entities and Chinese text to test decoding.

- [ ] **Step 2: Write failing parser tests**

Verify pagination is bounded, stable job id `acb200` is extracted, detail URL is `/cjobs/jobinfolist/cb21/showgw?id=<acb200>`, and missing explicit publication time results in `published_at=None` while `first_seen_at` is set by the normalizer.

- [ ] **Step 3: Run the test and confirm failure**

Run: `python -m pytest tests/test_mohrss_adapter.py -q`

- [ ] **Step 4: Implement robust HTML parsing**

Use BeautifulSoup for forms/detail fields and `json.loads` for embedded structured content after HTML entity decoding. Do not parse the embedded object with ad hoc field regexes. Permit the registered HTTP base URL only and record the transport scheme in snapshot metadata.

- [ ] **Step 5: Run focused tests**

Run: `python -m pytest tests/test_mohrss_adapter.py -q`

## Task 7: Add Manual Official-Company Manifest Import

**Files:**
- Create: `data/collection_manifests/company-official.example.jsonl`
- Create: `src/job_collection/adapters/manual_manifest.py`
- Create: `tests/fixtures/manual/company-job.html`
- Create: `tests/test_manual_manifest_adapter.py`

- [ ] **Step 1: Write failing manifest tests**

Require each line to contain `source_name`, `source_url`, `company_name`, `collection_authorization_note`, and either `exported_html_path` or a pre-extracted `job_description_raw`. Reject direct network fetching when the source remains `manual_only`.

- [ ] **Step 2: Run the test and confirm failure**

Run: `python -m pytest tests/test_manual_manifest_adapter.py -q`

- [ ] **Step 3: Implement manual-only processing**

Parse provided HTML/file content locally, preserve its original URL and hash, then pass records through the same normalizer, classifier, and quality gate. The example manifest must contain documentation fields but no fabricated production record.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/test_manual_manifest_adapter.py -q`

## Task 8: Build the Collection Orchestrator and Dry-Run/Commit CLI

**Files:**
- Create: `src/job_collection/service.py`
- Create: `src/collect_jobs.py`
- Modify: `src/import_service.py`
- Modify: `src/job_data_service.py`
- Modify: `requirements.txt`
- Create: `tests/test_collection_service.py`
- Create: `tests/test_collect_jobs_cli.py`
- Modify: `tests/test_import_service.py`

- [ ] **Step 1: Write failing orchestration tests**

Test that a dry-run creates:

```text
data/collections/<run-id>/
  raw/
  staged/jobs.jsonl
  review/jobs.jsonl
  quarantine/jobs.jsonl
  checkpoint.json
  report.json
```

Assert dry-run does not add a `JobPosting`. Assert `--commit` requires a completed staging report, creates a database backup, imports the staged JSONL through `import_job_file`, updates `CollectionRun`, and remains idempotent on resume.

- [ ] **Step 2: Write failing provenance import tests**

Import one unified record twice with a changed payload. Assert stable `record_id`, preserved `first_seen_at`, updated `last_seen_at`, stored source/parser fields, and a `JobPostingRevision` for the changed content.

- [ ] **Step 3: Run tests and confirm failure**

Run: `python -m pytest tests/test_collection_service.py tests/test_collect_jobs_cli.py tests/test_import_service.py -q`

- [ ] **Step 4: Implement orchestration**

The service must:

1. load and validate enabled sources;
2. calculate family deficits from valid unique records;
3. call adapters within source/page/record quotas;
4. normalize, classify, and stage records;
5. produce counts by source, domain, family, gate status, and date trust;
6. stop a source without discarding already completed snapshots;
7. write a deterministic JSON report.

- [ ] **Step 5: Implement CLI safety**

Expose:

```powershell
python -m src.collect_jobs --source ncss_public_jobs --max-records 20 --dry-run
python -m src.collect_jobs --source mohrss_public_jobs --max-records 20 --dry-run
python -m src.collect_jobs --resume-run <run-id> --dry-run
python -m src.collect_jobs --resume-run <run-id> --commit --confirm
```

Disallow `--commit` without `--confirm`. Add `beautifulsoup4>=4.12,<5.0` to requirements.

- [ ] **Step 6: Run focused tests**

Run: `python -m pytest tests/test_collection_service.py tests/test_collect_jobs_cli.py tests/test_import_service.py -q`

## Task 9: Audit and Repair Historical Job Data

**Files:**
- Create: `src/job_data_repair.py`
- Create: `tests/test_job_data_repair.py`
- Modify: `src/rebuild_hard_metrics.py`
- Modify: `tests/test_rebuild_hard_metrics_cli.py`

- [ ] **Step 1: Write failing audit tests**

Create legacy records with an unsupported old publication date, salary in `industry`, requirement text in `industry`, a valid industry, and duplicate descriptions. Assert dry-run reports exact before/after values without modifying rows.

- [ ] **Step 2: Write failing apply tests**

Assert `--repair --full --confirm` creates a backup, sets unsupported suspicious dates to `None` and `published_at_trusted=False`, changes contaminated industry to `unknown`, stores every field change in `DataRepairAudit`, recomputes content/simhash duplicate groups, and preserves the original payload and row count.

- [ ] **Step 3: Run tests and confirm failure**

Run: `python -m pytest tests/test_job_data_repair.py tests/test_rebuild_hard_metrics_cli.py -q`

- [ ] **Step 4: Implement audit-first repair**

Add a pure `plan_repairs(posting)` function and a separate transactional apply phase. Supported publication evidence must come from a structured original field or a new source snapshot; JD prose alone is never sufficient. Save reports under `data/repairs/<repair-run-id>/report.json`.

- [ ] **Step 5: Integrate with full rebuild**

After repair application, invoke the existing duplicate rebuild, quality gate, level classifier, quarterly profile, evolution, knowledge chunk, and acceptance pipeline. Mark old derived quarterly profiles superseded when their input signature changes.

- [ ] **Step 6: Run focused tests**

Run: `python -m pytest tests/test_job_data_repair.py tests/test_rebuild_hard_metrics_cli.py -q`

## Task 10: Enforce Trusted Time and Cross-Source Skill Confidence

**Files:**
- Modify: `src/competition_rules.py`
- Modify: `src/hard_metrics_pipeline.py`
- Modify: `src/job_analysis_service.py`
- Modify: `src/quarterly_profile_service.py`
- Modify: `model_class/job_competency.py`
- Modify: `src/schema_migration.py`
- Modify: `tests/test_competition_rules.py`
- Modify: `tests/test_job_analysis_service.py`
- Modify: `tests/test_quarterly_profiles.py`

- [ ] **Step 1: Write failing gate and profile tests**

Cover:

- a valid undated record participates in overall profile construction;
- a record with an untrusted publication date does not enter a quarterly profile;
- `gate_status=review` does not enter overall or quarterly formal profiles;
- a skill from one source type/domain has `cross_source_status="single_source"` and cannot become high confidence;
- a skill supported by two source types or three domains can become `cross_source_status="confirmed"`;
- source diversity counts use source types/domains, not source display names.

- [ ] **Step 2: Run tests and confirm failure**

Run: `python -m pytest tests/test_competition_rules.py tests/test_job_analysis_service.py tests/test_quarterly_profiles.py -q`

- [ ] **Step 3: Update data selection rules**

Change overall profile queries to `gate_status == "valid"` and `duplicate_of_id IS NULL`. Change quarterly queries to additionally require `published_at IS NOT NULL` and `published_at_trusted IS TRUE`.

- [ ] **Step 4: Calculate cross-source skill evidence**

For each family/skill aggregate posting count, source type count, source domain count, company count, required/preferred ratio, and time range. Persist these counts on `JobProfileSkill`. Cap single-source confidence below the approved/high-confidence threshold and include the status/counts in profile snapshots so graph versions are auditable.

- [ ] **Step 5: Keep first-seen time separate**

Use `first_seen_at` only for observation coverage and future repeated-crawl comparisons. Do not mix it into publication-quarter profiles. Include the temporal basis in snapshot metadata.

- [ ] **Step 6: Run focused tests**

Run: `python -m pytest tests/test_competition_rules.py tests/test_job_analysis_service.py tests/test_quarterly_profiles.py -q`

## Task 11: Replace Volume-Only Acceptance with Usable-Data Metrics

**Files:**
- Modify: `src/acceptance_service.py`
- Modify: `src/api.py`
- Modify: `index.html`
- Modify: `tests/test_acceptance_service.py`
- Modify: `tests/test_competition_api.py`
- Modify: `tests/test_ui_static.py`

- [ ] **Step 1: Write failing acceptance tests**

Build a mixed dataset and assert these metrics exactly:

- `raw_job_postings`;
- `usable_unique_job_postings` using valid + nonduplicate records;
- valid, duplicate, review, and quarantine rates;
- source type count, source domain count, maximum single-domain share;
- family coverage and minimum valid samples per covered family;
- trusted publication/first-seen coverage;
- cross-source confirmed core-skill coverage;
- evolution-eligible posting count.

Assert the 5000-7000 internal goal is evaluated against `usable_unique_job_postings`, while raw count remains informational.

- [ ] **Step 2: Write failing UI tests**

Assert human-readable labels exist for all new metrics and the interface contains no competition code, prize wording, or rule text. Preserve the existing left navigation without numeric prefixes.

- [ ] **Step 3: Run tests and confirm failure**

Run: `python -m pytest tests/test_acceptance_service.py tests/test_competition_api.py tests/test_ui_static.py -q`

- [ ] **Step 4: Implement one aggregate metrics service**

Use SQL aggregates and a small number of grouped queries; avoid loading all postings into Python. Return rates as 0-1 decimals and domain share as a ratio. Handle an empty database without division by zero.

- [ ] **Step 5: Update the business-facing UI**

Show data health, source structure, job-family coverage, date trust, and latest collection batch. Use concise operational labels such as “可用岗位”“来源域名”“可信时间”“可用于趋势分析”; do not expose internal competition wording.

- [ ] **Step 6: Run focused tests**

Run: `python -m pytest tests/test_acceptance_service.py tests/test_competition_api.py tests/test_ui_static.py -q`

## Task 12: Document the Sustainable Update Workflow

**Files:**
- Modify: `README.md`
- Modify: `USER_GUIDE.md`
- Create: `data/collections/README.md`
- Create: `tests/test_collection_docs.py`

- [ ] **Step 1: Write a failing documentation test**

Assert documentation contains the dry-run, resume, commit, repair audit, backup, full rebuild, source approval, and report paths. Assert it explicitly states that synthetic resumes are test-only and no synthetic jobs count toward production totals.

- [ ] **Step 2: Run the test and confirm failure**

Run: `python -m pytest tests/test_collection_docs.py -q`

- [ ] **Step 3: Update documentation**

Document the repeatable operating sequence:

```text
审核来源 -> 小规模 dry-run -> 查看 report/review/quarantine
-> 恢复或扩采 -> 备份并 commit -> 运行硬指标管线
-> 检查验收页面、知识库和图谱版本 -> 保存批次报告
```

Include rollback from the generated SQLite backup and how to disable a changed source immediately.

- [ ] **Step 4: Run the documentation test**

Run: `python -m pytest tests/test_collection_docs.py -q`

## Task 13: Run Live Smoke Collection, Scale the First Batch, and Verify End to End

**Files:**
- Generate: `data/collections/<run-id>/...`
- Generate: `data/backups/job_competency-<timestamp>.db`
- Generate: `data/imports/<batch-id>-report.json`
- Generate: `data/repairs/<repair-run-id>/report.json`
- Update: `data/job_competency.db`
- Update: `data/exports/knowledge-graph.json`

- [ ] **Step 1: Run the complete offline suite before network access**

Run: `python -m pytest -q`

Expected: all tests pass and total coverage remains at least 60%.

- [ ] **Step 2: Run bounded live smoke checks**

Run no more than 20 records per approved automatic source:

```powershell
python -m src.collect_jobs --source ncss_public_jobs --max-records 20 --dry-run
python -m src.collect_jobs --source mohrss_public_jobs --max-records 20 --dry-run
```

Inspect source status, parsed fields, publication evidence, family assignment, duplicates, and stop conditions. If either source returns a block or structure anomaly, disable it in the registry and retain the failure report; do not work around it.

- [ ] **Step 3: Audit historical data without modifying the database**

Run: `python -m src.rebuild_hard_metrics --dry-run --repair-audit`

Verify the reported suspicious dates and contaminated industries against a sample of raw payloads.

- [ ] **Step 4: Scale approved sources toward the first-batch target**

Resume successful runs with family-deficit scheduling and a combined cap of 1200 staged valid unique records. Keep per-source limits from the registry. Add enterprise official records only through reviewed manual manifests.

- [ ] **Step 5: Review staging quality before commit**

Acceptance for commit:

- every record has a source URL, source id/domain, snapshot hash, parser version, and observation time;
- every non-null publication date has explicit evidence and confidence;
- valid unique rate is reported;
- no source exceeds its cap;
- review/quarantine files are non-destructive and inspectable;
- no generated or synthetic job appears in staging.

- [ ] **Step 6: Back up, repair, and commit**

Apply the audited legacy repair and commit the reviewed staging run using explicit confirmation. Confirm backup readability before importing. Record actual imported, reviewed, quarantined, duplicate, and revised counts; do not claim the 800-1200 target if the accepted count is lower.

- [ ] **Step 7: Rebuild derived artifacts**

Run:

```powershell
python -m src.rebuild_hard_metrics --full --confirm
python -m src.build_knowledge_base data\collections\<run-id>\staged\jobs.jsonl
```

The second command uses the already committed staging JSONL. Its file hash must hit the existing idempotent import batch while still regenerating the graph export from the current database; it must not add a second copy of the jobs.

- [ ] **Step 8: Verify database and reports**

Run a read-only audit showing raw count, usable unique count, source types/domains, maximum domain share, family distribution, trusted date coverage, evolution-eligible count, and latest collection/import/repair run ids. Confirm no source record lacks its provenance fields.

- [ ] **Step 9: Run final regression tests**

Run: `python -m pytest -q`

Expected: all tests pass; coverage remains at least 60%; collection, import, repair, acceptance, knowledge, graph, evolution, matching, and UI tests all remain green.

- [ ] **Step 10: Perform a short local UI smoke test and stop the system**

Start the application only for the smoke test, verify the data-health metrics and one job family/profile/evolution path, then stop it. Confirm the UI does not expose competition-specific details and the left navigation has no numbers.

## Completion Evidence

Implementation is complete only when all of the following are available:

- passing offline test suite and coverage output;
- source registry with explicit compliance states and limits;
- reproducible collection run containing raw snapshots, checkpoint, staged/review/quarantine JSONL, and report;
- readable pre-change database backup;
- historical repair audit with before/after evidence;
- imported real-job batch report with actual counts;
- acceptance summary based on valid unique jobs and source diversity;
- rebuilt knowledge base/graph and trustworthy temporal profiles;
- no fabricated production job data and no blocked-source bypass.
