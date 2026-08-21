# Legacy Zhaopin Authorization Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Safely authorize and reprocess eligible legacy `jd_raw.json` Zhaopin rows without re-importing data or inventing publication evidence.

**Architecture:** Extend the existing audited historical-repair pipeline with an explicit legacy-source authorization object built from the reviewed source registry. Keep eligibility and field planning pure, pass the authorization through dry-run and apply modes, and rely on the existing transactional backup and full hard-metrics rebuild for commit safety.

**Tech Stack:** Python 3.11, SQLAlchemy async ORM, SQLite, Pydantic source registry, pytest, Ruff.

---

### Task 1: Define Strict Authorization Eligibility

**Files:**
- Modify: `src/job_data_repair.py`
- Test: `tests/test_job_data_repair.py`

- [x] **Step 1: Write failing pure-function tests**

Add tests that construct `SimpleNamespace` postings and an approved authorization object. Verify an exact legacy Zhaopin row is eligible, while rows with an existing `source_id`, approved provenance, a different source name, malformed URLs, userinfo URLs, foreign domains, and suffix-confusion domains such as `zhaopin.com.example.test` are ineligible.

- [x] **Step 2: Run the focused test and verify RED**

Run: `python -m pytest tests/test_job_data_repair.py -k "legacy_zhaopin" -q`

Expected: FAIL because `LegacySourceAuthorization` and `is_legacy_source_candidate` do not exist.

- [x] **Step 3: Implement the minimal pure authorization model and matcher**

Add a frozen `LegacySourceAuthorization` dataclass and a pure matcher that uses `urlsplit`, lowercases the hostname, rejects credentials and invalid ports, and requires the exact old source identity plus an absent source ID and unverified provenance.

- [x] **Step 4: Run the focused test and verify GREEN**

Run: `python -m pytest tests/test_job_data_repair.py -k "legacy_zhaopin" -q`

Expected: PASS.

### Task 2: Plan And Audit Provenance Repairs

**Files:**
- Modify: `src/job_data_repair.py`
- Test: `tests/test_job_data_repair.py`

- [x] **Step 1: Write failing repair-planning tests**

Verify that an eligible row receives deterministic changes for `source_id`, `source_name`, `source_type`, `source_domain`, `source_record_id`, `first_seen_at`, `last_seen_at`, `parser_name`, `parser_version`, `collection_method`, and `provenance_status`. Verify populated observation fields and immutable `raw_payload` are preserved, and authorization-disabled planning produces no provenance changes.

- [x] **Step 2: Run the focused test and verify RED**

Run: `python -m pytest tests/test_job_data_repair.py -k "authorization_changes or authorization_audit" -q`

Expected: FAIL because `plan_repairs` and `audit_job_data` do not accept authorization.

- [x] **Step 3: Implement minimal authorization-aware planning**

Add an optional `authorization` parameter to `plan_repairs`, `audit_job_data`, and `apply_job_data_repairs`. Emit one `RepairChange` per missing or incorrect approved-source field, use `collected_at` only for absent observation timestamps, preserve raw facts, and include a redacted authorization summary in the report.

- [x] **Step 4: Run repair service tests and verify GREEN**

Run: `python -m pytest tests/test_job_data_repair.py -q`

Expected: PASS.

### Task 3: Require Explicit CLI Authorization

**Files:**
- Modify: `src/rebuild_hard_metrics.py`
- Test: `tests/test_job_data_repair.py`
- Test: `tests/test_collection_docs.py`

- [x] **Step 1: Write failing CLI validation tests**

Verify `--authorize-legacy-zhaopin` is valid only with a repair or repair-audit mode, requires `--authorization-note`, rejects a note without the switch, and loads only the `zhaopin_legacy_import` registry entry when its China/manual-file-import constraints pass.

- [x] **Step 2: Run the CLI tests and verify RED**

Run: `python -m pytest tests/test_job_data_repair.py tests/test_collection_docs.py -k "authorization or rebuild" -q`

Expected: FAIL because the arguments and registry validation do not exist.

- [x] **Step 3: Implement CLI wiring**

Add `--authorize-legacy-zhaopin`, `--authorization-note`, and `--source-registry`. Build the authorization from `config/job_sources.json`, pass it to dry-run and apply services, and reject disabled, non-China, non-manual, non-authorized-platform, or wrong-parser entries.

- [x] **Step 4: Run CLI and repair tests and verify GREEN**

Run: `python -m pytest tests/test_job_data_repair.py tests/test_collection_docs.py -q`

Expected: PASS.

### Task 4: Document The Operator Command

**Files:**
- Modify: `README.md`
- Modify: `QUICKSTART.md`
- Test: `tests/test_collection_docs.py`

- [x] **Step 1: Write the failing documentation assertion**

Require the dry-run and confirmed apply examples to include the explicit authorization switch and note, and state that this command only approves eligible historical Zhaopin file rows and never performs network collection.

- [x] **Step 2: Run the documentation test and verify RED**

Run: `python -m pytest tests/test_collection_docs.py -q`

Expected: FAIL until the documented command is present.

- [x] **Step 3: Update the operator documentation**

Document these commands with a unique run ID:

```powershell
python -m src.rebuild_hard_metrics --dry-run --repair-audit --repair-run-id legacy-zhaopin-auth-audit-20260812 --authorize-legacy-zhaopin --authorization-note "团队于2026-08-12确认jd_raw.json在允许范围内采集并授权用于本次比赛研究。"
python -m src.rebuild_hard_metrics --full --repair --confirm --repair-run-id legacy-zhaopin-auth-20260812 --authorize-legacy-zhaopin --authorization-note "团队于2026-08-12确认jd_raw.json在允许范围内采集并授权用于本次比赛研究。"
```

- [x] **Step 4: Run the documentation test and verify GREEN**

Run: `python -m pytest tests/test_collection_docs.py -q`

Expected: PASS.

### Task 5: Audit, Apply, Rebuild, And Verify Production Data

**Files:**
- Create: `data/repairs/legacy-zhaopin-auth-audit-20260812/report.json` via the repair CLI
- Create: `data/repairs/legacy-zhaopin-auth-20260812/report.json` via the repair CLI
- Create: `data/backups/job_competency-legacy-zhaopin-auth-20260812.db` via the repair CLI
- Update: `data/job_competency.db` via the confirmed repair CLI
- Update: `data/exports/knowledge-graph.json` via the graph export command

- [x] **Step 1: Run the authorization-aware dry-run**

Run the documented audit command. Expected: no database writes, exactly 2,546 job rows before and after, and authorization changes only for strict legacy Zhaopin candidates.

- [x] **Step 2: Inspect the dry-run boundary**

Check candidate counts, field names, URL domains, source identities, date changes, and absence of PII in the report. Abort if any non-Zhaopin or already-approved row is targeted.

- [x] **Step 3: Run the confirmed transactional repair**

Run the documented apply command. Expected: verified backup created, row count unchanged, repair audit rows committed, and the full hard-metrics pipeline completed.

- [x] **Step 4: Regenerate the graph export**

Run the established knowledge graph export command from the project documentation and verify the output is valid JSON with nonzero nodes and edges.

- [x] **Step 5: Run focused and full verification**

Run:

```powershell
python -m pytest tests/test_job_data_repair.py tests/test_hard_metrics_pipeline.py tests/test_collection_docs.py -q
python -m pytest -q
python -m coverage report --fail-under=80
python -m ruff check src tests
```

Expected: all tests pass, coverage remains above the project threshold, and Ruff reports no errors.

- [x] **Step 6: Record final acceptance statistics**

Report raw rows, usable unique rows, status counts, usable counts by family, source/domain counts, trusted-publication-or-first-seen coverage, foreign-market count, graph nodes/edges, pipeline run ID, database SHA-256, backup path, and remaining missing families.
