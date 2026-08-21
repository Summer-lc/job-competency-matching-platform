# Authorized Local File Intake Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a guarded, offline-only path for authorized Zhaopin JSONL exports and use it to stage and commit only valid records from the new batch.

**Architecture:** A dedicated adapter parses and normalizes the local export. `CollectionService` snapshots and recomputes it like other evidence-bearing sources, while the commit capability explicitly authorizes only the attested file-import source. Evidence files receive an audit report but are not force-imported.

**Tech Stack:** Python, Pydantic, SQLAlchemy, SQLite, pytest.

---

### Task 1: Local File Adapter

**Files:**
- Create: `src/job_collection/adapters/legacy_file.py`
- Create: `tests/test_legacy_file_adapter.py`

- [ ] Write failing tests for bounded parsing, host enforcement, date trust, classification correction, and offline operation.
- [ ] Run `python -m pytest tests/test_legacy_file_adapter.py -q` and confirm the missing adapter failure.
- [ ] Implement the smallest adapter that returns canonical `UnifiedJobRecord` values with line and file hashes.
- [ ] Re-run the focused tests.

### Task 2: Guarded Collection Integration

**Files:**
- Modify: `src/job_collection/service.py`
- Modify: `src/collect_jobs.py`
- Modify: `src/import_service.py`
- Modify: `tests/test_collection_service.py`
- Modify: `tests/test_collect_jobs_cli.py`
- Modify: `tests/test_import_service.py`

- [ ] Write failing tests requiring `--input-file` and an authorization note only for `zhaopin_legacy_import`.
- [ ] Write failing tests proving snapshot recomputation and file-import provenance approval.
- [ ] Implement dry-run snapshotting, semantic recomputation, and narrow commit authorization.
- [ ] Run the focused CLI, service, and import tests.

### Task 3: Intake And Evidence Reports

**Files:**
- Create: `data/intake/20260811-new-batch/audit-summary.json`

- [ ] Dry-run both job files through the guarded adapter.
- [ ] Record duplicate, review, quarantine, classification, date, and evidence-quality counts.
- [ ] Verify the source files are unchanged by SHA-256.

### Task 4: Production Commit And Rebuild

- [ ] Review staged artifacts and commit only valid records with `--confirm`.
- [ ] Run the incremental hard-metrics and knowledge-graph rebuild.
- [ ] Verify backup integrity, current database integrity, graph export, tests, code checks, and closed port 8000.

