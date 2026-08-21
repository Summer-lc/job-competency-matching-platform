# Official Evidence Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 24 validated, official external evidence records across eight job families, import them idempotently, expose them to evidence-grounded Q&A, and remove numeric prefixes from the left navigation.

**Architecture:** Keep the evidence corpus as versioned JSONL plus a human-readable source manifest. Add a focused validator that reuses the existing Pydantic input schema, then use the existing evidence import endpoint and `EvidenceRecord` model for persistence. Treat navigation numbering as a presentation-only change guarded by a static UI test.

**Tech Stack:** Python 3.11, Pydantic, SQLAlchemy async, FastAPI, pytest, JSONL, HTML/CSS.

---

### Task 1: Lock the evidence dataset contract

**Files:**
- Create: `tests/test_official_evidence_dataset.py`
- Create: `src/evidence_dataset_service.py`

- [ ] **Step 1: Write failing tests for the approved corpus contract**

  Add tests that load `data/evidence/official-standards-2026.jsonl` and require exactly 24 records, exactly three records for each approved family, globally unique IDs/titles/URLs, approved evidence types, approved official domains, 60-200 Chinese-character summaries, at least two evidence types per family, and at least one standard record per family.

- [ ] **Step 2: Verify the tests fail for the missing validator and dataset**

  Run: `pytest tests/test_official_evidence_dataset.py -q`

  Expected: FAIL because `src.evidence_dataset_service` and the corpus do not exist.

- [ ] **Step 3: Implement the minimal validator**

  Implement `load_jsonl(path)` and `validate_official_evidence_records(records)` in `src/evidence_dataset_service.py`. Validate every object with `EvidenceInput.model_validate`, parse URL hosts with `urllib.parse.urlparse`, return a summary containing `total`, `family_counts`, and `type_counts`, and raise `ValueError` with actionable messages for every violated corpus rule.

- [ ] **Step 4: Run focused validator tests**

  Run: `pytest tests/test_official_evidence_dataset.py -q`

  Expected: contract-level unit tests pass except the real-corpus test, which remains red until Task 2.

### Task 2: Curate the 24 official records and audit manifest

**Files:**
- Create: `data/evidence/official-standards-2026.jsonl`
- Create: `data/evidence/official-standards-2026-sources.md`

- [ ] **Step 1: Add three non-overlapping records per family**

  Use the approved three-layer mix: role/competency evidence, architecture or technical standards, and official engineering documentation. Use only direct pages or official PDFs from MOHRSS/government, the national standards platform, ISO, NIST, ETSI, Apache, and ROS.

- [ ] **Step 2: Record source provenance**

  For every evidence ID, record title, publisher, official URL, standard number/version where present, access date `2026-07-22`, inclusion rationale, and public access mode. Summaries must be original Chinese paraphrases and must not reproduce restricted standard text.

- [ ] **Step 3: Validate the completed corpus**

  Run: `pytest tests/test_official_evidence_dataset.py -q`

  Expected: PASS with 24 records and `3` for every family.

### Task 3: Apply evidence trust weights

**Files:**
- Modify: `src/job_data_service.py`
- Modify: `tests/test_job_data_service.py`

- [ ] **Step 1: Write a failing source-score test**

  Require `technical_standard == 0.98` and `official_document == 0.92`, while preserving `occupation_standard == 1.00` and `policy_document == 0.95`.

- [ ] **Step 2: Verify the new expectations fail**

  Run: `pytest tests/test_job_data_service.py -q`

  Expected: FAIL because the two new source types are absent.

- [ ] **Step 3: Add the two scores and rerun the test**

  Update only `SOURCE_SCORES`, then rerun the focused test and expect PASS.

### Task 4: Remove left-navigation numbers

**Files:**
- Modify: `index.html`
- Create: `tests/test_ui_static.py`

- [ ] **Step 1: Write a failing static UI test**

  Require the nine existing `data-target` values to remain in the same order, require their visible labels to remain unchanged, and reject `class="nav-icon"` and visible `01` through `09` prefixes.

- [ ] **Step 2: Verify the UI test fails**

  Run: `pytest tests/test_ui_static.py -q`

  Expected: FAIL because the current navigation contains numbered spans.

- [ ] **Step 3: Remove numeric spans and their unused style**

  Keep every navigation button and target unchanged. Remove only the `.nav-icon` CSS rule and the nine numeric `<span>` elements.

- [ ] **Step 4: Rerun the UI test**

  Run: `pytest tests/test_ui_static.py -q`

  Expected: PASS.

### Task 5: Import and prove sustainable updates

**Files:**
- Modify only if required by a failing test: `src/api.py`
- Modify: `tests/test_competition_api.py`

- [ ] **Step 1: Add an idempotent import test**

  Upload the validated JSONL to `/api/data/evidence/import` twice. Require the first response to import all supplied records and the second response to import zero and skip the same evidence IDs.

- [ ] **Step 2: Verify current endpoint behavior**

  Run: `pytest tests/test_competition_api.py -q`

  Expected: PASS if existing evidence-ID deduplication already satisfies the contract; otherwise fail for the specific response mismatch.

- [ ] **Step 3: Make only the minimal endpoint correction if needed**

  Preserve the current schema and deduplication key. Do not add a second import path or database table.

- [ ] **Step 4: Import the real corpus into the configured local database**

  Start the application temporarily, upload `data/evidence/official-standards-2026.jsonl`, repeat the upload, and confirm the corpus adds exactly 24 records with no duplicates. Stop the application after verification.

### Task 6: Verify Q&A retrieval and the complete system

**Files:**
- Modify only if required by a failing test: `tests/test_evidence_qa_service.py`, `src/evidence_qa_service.py`

- [ ] **Step 1: Add or extend an external-evidence retrieval test**

  Ask a standard-related question for a selected family and require at least one returned citation with `source_kind == "external"`, the official source URL, and a stable citation ID.

- [ ] **Step 2: Run Q&A and full automated verification**

  Run: `pytest tests/test_evidence_qa_service.py -q`

  Run: `pytest -q`

  Expected: all tests pass.

- [ ] **Step 3: Perform browser smoke checks**

  Temporarily run the system and verify: navigation labels have no numbers; all nine panels still open; data governance reports 24 external evidence records; a standards question shows an external citation card and opens its official link.

- [ ] **Step 4: Leave the system stopped**

  Stop the temporary process and verify no project server remains listening.

