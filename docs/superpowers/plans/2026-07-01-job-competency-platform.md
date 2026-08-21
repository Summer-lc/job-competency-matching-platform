# Job Competency Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the educational RAG application with a competition-ready job competency graph, evolution analysis, resume parsing, and matching platform.

**Architecture:** Keep reusable LLM, retrieval, document parsing, and Neo4j adapters. Replace the business database models, API, tests, and frontend with focused job-domain modules using SQLite by default and optional Neo4j synchronization.

**Tech Stack:** Python, FastAPI, Pydantic, SQLAlchemy async, SQLite/MySQL, LangChain, DeepSeek, Neo4j, vanilla HTML/CSS/JavaScript, pytest.

---

### Task 1: Domain models and database configuration

**Files:**
- Create: `model_class/base.py`
- Create: `model_class/job_competency.py`
- Modify: `config/DB_config.py`
- Test: `tests/test_job_models.py`

- [ ] Write failing tests that create all job-domain tables in an in-memory SQLite database.
- [ ] Run `pytest tests/test_job_models.py -q` and verify failure because the models do not exist.
- [ ] Implement environment-driven database configuration and the job, skill, evidence, resume, match, review, and evaluation models.
- [ ] Run the model tests and verify they pass.

### Task 2: JD import, validation, normalization, and deduplication

**Files:**
- Create: `schemes/job_competency.py`
- Create: `src/job_data_service.py`
- Create: `tests/test_job_data_service.py`

- [ ] Write failing tests for JSONL/CSV parsing, required-field validation, title normalization, exact hash duplicate detection, SimHash generation, skill extraction, and source scoring.
- [ ] Run the focused tests and verify expected failures.
- [ ] Implement the minimum parsing and governance service required by the tests.
- [ ] Run the focused tests and verify they pass.

### Task 3: Job discovery and evolution analysis

**Files:**
- Create: `src/job_analysis_service.py`
- Create: `tests/test_job_analysis_service.py`

- [ ] Write failing tests for emerging-job scoring, time-window skill aggregation, and added/removed/changed skill output with evidence.
- [ ] Verify the tests fail because the service is missing.
- [ ] Implement deterministic analysis with configurable thresholds and evidence-backed results.
- [ ] Verify the focused tests pass.

### Task 4: Resume parsing and matching

**Files:**
- Create: `src/resume_service.py`
- Create: `src/matching_service.py`
- Create: `tests/test_resume_matching.py`

- [ ] Write failing tests for TXT/DOCX/PDF text handling, skill evidence extraction, weighted matching, gap output, and ordered learning path.
- [ ] Verify the tests fail for missing functions.
- [ ] Implement parsing and deterministic matching with explicit scoring dimensions.
- [ ] Verify the focused tests pass.

### Task 5: Competition API

**Files:**
- Replace: `src/api.py`
- Create: `tests/test_competition_api.py`

- [ ] Write failing API tests for health, import, statistics, jobs, analysis, graph, resume, match, review, and evaluation routes.
- [ ] Verify the tests fail against the educational API.
- [ ] Implement the focused FastAPI application and dependency-injected database access.
- [ ] Verify all API tests pass.

### Task 6: Competition frontend and documentation

**Files:**
- Replace: `index.html`
- Replace: `README.md`
- Replace: `QUICKSTART.md`
- Modify: `docker-compose.yml`
- Modify: `requirements.txt`

- [ ] Build a single-page interface for dashboard, data governance, discovery, evolution, graph, matching, and review.
- [ ] Update setup and deployment documentation for SQLite + optional Neo4j.
- [ ] Run a static search to verify educational labels and endpoints are absent.
- [ ] Start the application and verify the key browser flows.

### Task 7: Remove unrelated educational assets and verify

**Files:**
- Delete educational CRUD, models, schemes, agents, migrations, data, generated assets, caches, and obsolete documents listed in the design.
- Keep reusable retrieval, LLM, document parsing, vector, and Neo4j modules.

- [ ] Delete only the approved unrelated paths after verifying each resolved path is inside the project.
- [ ] Run `python -m compileall` for retained Python files.
- [ ] Run `pytest -q` and verify all tests pass.
- [ ] Start the API and validate `/health` and the main page.

