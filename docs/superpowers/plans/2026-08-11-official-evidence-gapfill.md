# Official Evidence Gap-Fill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add, validate, import, and graph 42 reviewed official evidence records so all 22 modeled job families have curated external evidence.

**Architecture:** Preserve the original 24-record baseline contract and add a separate 42-record supplemental contract. Extend the validator with an explicit supported-family boundary and configurable required-family mix, then expose imported `EvidenceRecord` rows as evidence nodes in graph exports when evidence is requested.

**Tech Stack:** Python 3.11, Pydantic, SQLAlchemy async, FastAPI/httpx ASGI transport, pytest, SQLite, JSONL.

**Repository note:** This workspace is not a Git repository. Each task ends with focused tests and artifact hashes instead of a commit.

---

### Task 1: Supplemental Corpus Contract

**Files:**
- Modify: `tests/test_official_evidence_dataset.py`
- Modify: `src/evidence_dataset_service.py`

- [ ] **Step 1: Write failing tests for supplemental-family validation**

Add imports for `SUPPLEMENTAL_JOB_FAMILIES` and `SUPPORTED_JOB_FAMILIES`, then add tests that require all 22 families to be supported while the original baseline remains eight families. Add a validator call with `required_families=SUPPLEMENTAL_JOB_FAMILIES` and a negative case for the incorrect ISO URL:

```python
def test_supported_families_cover_baseline_and_supplement():
    assert SUPPORTED_JOB_FAMILIES == APPROVED_JOB_FAMILIES | SUPPLEMENTAL_JOB_FAMILIES
    assert len(SUPPORTED_JOB_FAMILIES) == 22


def test_validator_rejects_known_incorrect_ai_standard_mapping():
    record = valid_supplemental_record(
        source_url="https://www.iso.org/standard/78843.html"
    )
    with pytest.raises(ValueError, match="known incorrect source mapping"):
        validate_official_evidence_records(
            [record],
            expected_total=None,
            enforce_family_mix=False,
        )
```

- [ ] **Step 2: Run the new tests and verify RED**

Run: `python -m pytest tests/test_official_evidence_dataset.py -q`

Expected: collection or assertion failure because the supplemental constants and `required_families` parameter do not exist.

- [ ] **Step 3: Implement the minimal validator extension**

Keep `APPROVED_JOB_FAMILIES` as the original baseline set. Add the 14-family `SUPPLEMENTAL_JOB_FAMILIES`, define `SUPPORTED_JOB_FAMILIES` as their union, add only the official domain suffixes used by the final corpus, require HTTPS, and reject the exact known-bad ISO URL. Extend the function signature:

```python
def validate_official_evidence_records(
    records: list[dict],
    *,
    expected_total: int | None = 24,
    enforce_family_mix: bool = True,
    required_families: set[str] | frozenset[str] | None = None,
) -> dict:
```

Membership checks use `SUPPORTED_JOB_FAMILIES`; family-mix checks use `required_families or APPROVED_JOB_FAMILIES`.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `python -m pytest tests/test_official_evidence_dataset.py -q`

Expected: all existing and new validator tests pass.

### Task 2: Curated 42-Record JSONL Corpus

**Files:**
- Create: `data/evidence/official-standards-2026-supplement.jsonl`
- Create: `data/evidence/official-standards-2026-supplement-sources.md`
- Modify: `tests/test_official_evidence_dataset.py`

- [ ] **Step 1: Write the failing dataset acceptance test**

```python
SUPPLEMENT = ROOT / "data" / "evidence" / "official-standards-2026-supplement.jsonl"


def test_official_evidence_supplement_meets_contract():
    records = load_jsonl(SUPPLEMENT)
    result = validate_official_evidence_records(
        records,
        expected_total=42,
        required_families=SUPPLEMENTAL_JOB_FAMILIES,
    )
    assert result["total"] == 42
    assert result["family_counts"] == {
        family: 3 for family in sorted(SUPPLEMENTAL_JOB_FAMILIES)
    }
```

- [ ] **Step 2: Run the dataset test and verify RED**

Run: `python -m pytest tests/test_official_evidence_dataset.py::test_official_evidence_supplement_meets_contract -q`

Expected: failure because the supplemental JSONL does not exist.

- [ ] **Step 3: Add the curated records and source manifest**

Create exactly three records per supplemental family. Use stable IDs with prefixes such as `OFF-JAVA-001`, distinct titles and URLs, evidence types from the approved set, and original 60-200 Chinese-character summaries. Record the publisher, access date `2026-08-11`, source URL, source status, and competency-mapping rationale in the Markdown manifest.

Use only the verified pages listed in the design review, including Oracle and Spring specifications, Python/Django/FastAPI documentation, Go/gRPC/Protocol Buffers specifications, W3C/TC39/TypeScript documentation, Jenkins/GitLab/Terraform documentation, Google SRE/OpenTelemetry/Prometheus documentation, Kubernetes/Istio/Helm documentation, MCP/OpenAI/NIST sources, LlamaIndex/LangChain/Lucene documentation, MLflow/Kubeflow/ISO sources, multimodal API documentation, prompt-engineering guides, ISO/IEC 23894 at `/standard/77304.html`, NIST AI RMF, and ISO/IEC 42001.

- [ ] **Step 4: Validate the complete corpus**

Run: `python -m pytest tests/test_official_evidence_dataset.py -q`

Expected: baseline and supplemental corpus tests pass; total curated evidence is 66 records across 22 families.

### Task 3: External Evidence In The Knowledge Graph

**Files:**
- Modify: `tests/test_versioned_graph.py`
- Modify: `src/job_analysis_service.py`
- Modify: `src/build_knowledge_base.py`

- [ ] **Step 1: Write a failing graph test**

Insert one `EvidenceRecord` for `DATA_ENGINEER`, call `graph_data(..., include_evidence=True)`, and require an external evidence node plus a `supported_by` edge from the family node:

```python
assert any(
    node["id"] == "evidence:external:OFF-DATA-TEST"
    and node["evidence_kind"] == "external_standard"
    for node in graph["nodes"]
)
assert {
    "source": "family:DATA_ENGINEER",
    "target": "evidence:external:OFF-DATA-TEST",
    "type": "supported_by",
} in graph["edges"]
```

- [ ] **Step 2: Run the graph test and verify RED**

Run: `python -m pytest tests/test_versioned_graph.py::test_graph_contains_external_standard_evidence -q`

Expected: failure because `graph_data` currently exports only posting snippets.

- [ ] **Step 3: Add external evidence nodes**

Import `EvidenceRecord` in `src/job_analysis_service.py`. When `include_evidence=True`, query records for the current profile family, create stable `evidence:external:<evidence_id>` nodes containing title, publisher, evidence type, summary, related skill, and source URL, and add a family-to-evidence `supported_by` edge. Reuse `node_ids` to prevent duplicate nodes across profile versions.

Change the JSON graph export in `build_knowledge_base` to call:

```python
graph = await graph_data(session, include_evidence=True)
```

- [ ] **Step 4: Run graph regression tests**

Run: `python -m pytest tests/test_versioned_graph.py tests/test_competition_api.py -q`

Expected: all graph and API tests pass.

### Task 4: Production Import And Final Verification

**Files:**
- Update: `data/job_competency.db`
- Create: `data/backups/job_competency-<timestamp>.db`
- Update: `data/exports/knowledge-graph.json`

- [ ] **Step 1: Run focused quality checks**

Run: `python -m pytest tests/test_official_evidence_dataset.py tests/test_versioned_graph.py -q`

Run: `python -m ruff check src tests`

Expected: both commands exit zero.

- [ ] **Step 2: Back up the production database**

Call `backup_sqlite_database(ASYNC_DATABASE_URL, Path("data/backups"))` and verify `PRAGMA integrity_check` returns `ok` for the backup before any evidence write.

- [ ] **Step 3: Import the supplement twice through the application route**

Use an in-process ASGI client against `create_app()` and post the JSONL to `/api/data/evidence/import`. The first response must report `received=42`, `imported=42`, `errors=[]`; the second must report `received=42`, `imported=0`, `skipped=42`, `errors=[]`.

- [ ] **Step 4: Rebuild metrics and export the graph**

Run: `python -m src.rebuild_hard_metrics --full --confirm`

Run: `python -m src.build_knowledge_base data/collections/zhaopin-a-20260811/staged/jobs.jsonl`

Expected: hard-metrics run completes, graph export includes external evidence, and job import remains idempotent.

- [ ] **Step 5: Run full verification**

Run: `python -m pytest -c pytest-full.ini -q`

Run: `python -m ruff check src tests`

Expected: all tests pass, coverage remains above 60%, and lint reports no issues.

- [ ] **Step 6: Verify artifacts and stopped state**

Check current and backup databases with `PRAGMA integrity_check`; count `EvidenceRecord` rows and distinct covered families; parse the exported graph and count external evidence nodes; calculate SHA-256 hashes for the supplement, manifest, backup, and graph; confirm port 8000 is closed and no application server process remains.
