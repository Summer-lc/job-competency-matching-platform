# Automated Competition Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace manually entered evaluation numbers with reproducible benchmark execution for JD parsing, resume extraction, and job matching, including the competition's 100-JD and 90%-accuracy readiness gates.

**Architecture:** A pure evaluation service accepts JSON/JSONL benchmark records, validates each task contract, runs the existing deterministic extraction and matching services, and computes reproducible metrics. FastAPI persists each computed result in `evaluation_run`; the frontend uploads a benchmark file and displays measured values separately from competition targets. Real collected records and small format examples remain explicitly distinguished.

**Tech Stack:** Python 3.11, FastAPI, Pydantic 2, SQLAlchemy async, vanilla JavaScript, pytest.

**Repository note:** The workspace is not a Git repository, so commit steps are intentionally omitted.

---

### Task 1: Benchmark contracts and metric engine

**Files:**
- Create: `src/evaluation_service.py`
- Create: `tests/test_evaluation_service.py`

- [x] **Step 1: Write failing metric tests**

  Add tests that call `run_benchmark(records)` with one exact JD extraction case, one resume extraction case, and three matching-band cases. Assert per-task `sample_count`, micro precision/recall/F1, exact-case or classification accuracy, and detailed failed-case IDs.

- [x] **Step 2: Verify the tests fail**

  Run `python -m pytest tests/test_evaluation_service.py -q` and confirm collection fails because `src.evaluation_service` does not exist.

- [x] **Step 3: Implement benchmark validation and metrics**

  Implement public functions `parse_benchmark_records(raw: bytes, filename: str) -> list[dict]`, `run_benchmark(records: Iterable[dict]) -> dict`, and `readiness_from_results(results: list[dict]) -> dict`.

  Supported records:

  ```json
  {"case_id":"JD-001","task":"jd_parsing","input":{"text":"要求掌握Python，熟悉Docker者优先。"},"expected":{"required_skills":["Python"],"preferred_skills":["Docker"]}}
  {"case_id":"CV-001","task":"resume_extraction","input":{"text":"3年Java开发经验，本科学历。"},"expected":{"skills":["Java"],"experience_years":3,"education":["本科"]}}
  {"case_id":"MATCH-001","task":"matching","input":{"resume":{},"job_profile":{}},"expected":{"band":"high"}}
  ```

  JD metrics use typed skill labels such as `required:python`; resume P/R/F1 use skill labels and exact-case accuracy additionally checks years within `0.25` and expected education; matching maps scores to `high >= 80`, `medium >= 60`, otherwise `low`, then reports macro classification metrics.

- [x] **Step 4: Verify the focused tests pass**

  Run `python -m pytest tests/test_evaluation_service.py -q` and expect all tests to pass.

### Task 2: Reproducible evaluation API

**Files:**
- Modify: `src/api.py`
- Modify: `schemes/job_competency.py`
- Modify: `tests/test_competition_api.py`

- [x] **Step 1: Write failing API tests**

  Add a multipart upload test for `POST /api/evaluation/run`. Assert that the response contains computed results and readiness, that `GET /api/evaluation/summary` exposes the latest computed metrics, and that readiness remains false below 100 JD cases.

- [x] **Step 2: Verify the API test fails**

  Run the focused API test and expect `404` for `/api/evaluation/run`.

- [x] **Step 3: Implement automatic evaluation persistence**

  Add the upload route, parse and execute the benchmark, persist one `EvaluationRun` per task, and return the computed results. Remove the manual `POST /api/evaluation/runs` route and `EvaluationInput` schema so accuracy cannot be self-reported without evidence.

- [x] **Step 4: Make summary readiness explicit**

  Return `targets`, `latest`, `runs`, and:

  ```json
  {"readiness":{"jd_case_count":12,"required_jd_cases":100,"meets_jd_case_requirement":false,"all_metrics_present":true,"all_accuracy_targets_met":false,"competition_ready":false}}
  ```

- [x] **Step 5: Verify focused API tests pass**

  Run `python -m pytest tests/test_competition_api.py -q`.

### Task 3: Evaluation workspace in the frontend

**Files:**
- Modify: `index.html`

- [x] **Step 1: Add benchmark upload controls**

  Add a JSON/JSONL file input, an “运行自动评测” button, progress text, and a readiness panel to the evaluation page.

- [x] **Step 2: Render targets and measurements separately**

  Update `loadEvaluation()` so each metric card shows the latest measured value or `未评测`, while its note displays the 90% target. Show JD case progress as `current / 100` and never display a target as if it were a measured result.

- [x] **Step 3: Add upload behavior**

  Implement `runEvaluation()` using multipart `FormData`, refresh the summary after success, and show validation errors without clearing prior results.

### Task 4: Honest benchmark handoff files and documentation

**Files:**
- Create: `data/benchmark/README.md`
- Create: `data/benchmark/benchmark-example.jsonl`
- Modify: `README.md`
- Modify: `QUICKSTART.md`

- [x] **Step 1: Add a small format example**

  Provide examples for all three task types and label the file as a format demonstration, not as the required 100-case final benchmark.

- [x] **Step 2: Document annotation rules**

  Require at least 100 independently reviewed real JD cases, stable case IDs, source traceability, train/evaluation separation, two-person annotation with conflict resolution, and no invented source URLs.

- [x] **Step 3: Update run instructions**

  Document `POST /api/evaluation/run`, explain the metric definitions, and state that the interface reports `未达标` until sample and accuracy gates are measured.

### Task 5: Full verification

**Files:**
- Verify all changed files.

- [x] Run `python -m compileall -q src model_class schemes config tests`.
- [x] Run `python -m pytest -q` and confirm coverage remains at least 60%.
- [x] Start the API with a clean SQLite database and upload `data/benchmark/benchmark-example.jsonl`.
- [x] Verify the evaluation page shows measured results, `JD case count / 100`, and `competition_ready: false` for the small example.
- [x] Inspect the browser console and confirm there are no errors.
