# Incremental Knowledge Base and Knowledge Graph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a source-traceable, incrementally updateable job knowledge base and versioned knowledge graph from `../jd_raw.json`, while preserving malformed and low-quality records for review.

**Architecture:** SQLite/MySQL remains the system of record for import batches, raw lines, curated postings, evidence, chunks, and profile versions. A lexical-first retrieval service provides the always-available knowledge base and accepts an optional embedding provider; graph payloads are generated from the relational store and idempotently synchronized to Neo4j when available.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic 2, SQLAlchemy async, deterministic lexical retrieval with an injectable embedding provider, Neo4j 5, pytest/pytest-asyncio.

**Repository note:** The workspace is not a Git repository. Replace each commit step with the stated test and file-hash checkpoint; do not initialize Git as part of this work.

---

## File Map

- Create `model_class/knowledge_base.py`: batch, raw-line, revision, quality, responsibility, scenario, evidence, chunk, profile metadata, and evolution models.
- Modify `config/DB_config.py`: import the new model module during schema initialization.
- Create `src/import_service.py`: tolerant file parsing, file-level idempotency, raw retention, validation, revisions, quality issues, and incremental import summary.
- Modify `src/job_data_service.py`: expose reusable preparation, responsibility extraction, quality helpers, and safe posting persistence.
- Create `src/knowledge_service.py`: knowledge chunk upsert, lexical/optional vector retrieval, and evidence payloads.
- Modify `src/job_analysis_service.py`: family-scoped no-op-aware profile rebuild, responsibilities/scenarios, version signatures, evolution events, and expanded graph payload.
- Modify `src/job_graph_sync.py`: sync all supported graph node and relation types with stable IDs.
- Create `src/build_knowledge_base.py`: repeatable first-build CLI and artifact export.
- Modify `src/api.py`: batch/quarantine/search/version APIs and enhanced import/graph parameters.
- Modify `index.html`: batch governance, quarantine detail, expanded graph filters and node detail.
- Modify `README.md` and `QUICKSTART.md`: build, update, search, export, and Neo4j fallback instructions.
- Create `tests/test_import_service.py`, `tests/test_knowledge_service.py`, and `tests/test_versioned_graph.py`.
- Modify `tests/test_competition_api.py` and existing job service tests for compatibility and end-to-end coverage.

---

### Task 1: Persistent Raw Import Model and Tolerant Parser

**Files:**
- Create: `model_class/knowledge_base.py`
- Modify: `config/DB_config.py`
- Create: `src/import_service.py`
- Create: `tests/test_import_service.py`

- [x] **Step 1: Write failing parser and model tests**

```python
def test_json_suffix_with_jsonl_content_preserves_bad_line():
    raw = b'{"record_id":"A"}\n{"record_id":"broken\x01"}\n{"record_id":"B"}\n'
    lines = parse_import_lines(raw, "jd_raw.json")
    assert [line.line_number for line in lines] == [1, 2, 3]
    assert lines[0].value == {"record_id": "A"}
    assert lines[1].error_code == "invalid_json"
    assert lines[2].value == {"record_id": "B"}

@pytest.mark.asyncio
async def test_raw_lines_and_batch_are_persisted(memory_session):
    result = await import_job_file(memory_session, SAMPLE_BYTES, "jobs.json")
    assert result["raw_lines"] == 3
    assert await memory_session.scalar(select(func.count()).select_from(ImportBatch)) == 1
    assert await memory_session.scalar(select(func.count()).select_from(RawJobRecord)) == 3
```

- [x] **Step 2: Run the focused tests and verify RED**

Run: `python -m pytest tests/test_import_service.py -q`

Expected: FAIL because `model_class.knowledge_base` and `src.import_service` do not exist.

- [x] **Step 3: Add the persistence models and parser API**

Implement these public SQLAlchemy model classes in `model_class/knowledge_base.py`: `ImportBatch`, `RawJobRecord`, `JobPostingRevision`, `QualityIssue`, `Responsibility`, `IndustryScenario`, `EvidenceSnippet`, `JobProfileResponsibility`, `JobProfileScenario`, `KnowledgeChunk`, `JobProfileSnapshot`, and `EvolutionEvent`.

Use unique constraints on `ImportBatch.file_hash`, `(import_batch_id, line_number)`, `(job_posting_id, revision_no)`, `KnowledgeChunk.chunk_id`, and `JobProfileSnapshot.job_profile_id`. Store counters and payloads as JSON text to preserve SQLite/MySQL compatibility.

Implement `ParsedImportLine` as a frozen dataclass with `line_number: int`, `raw_text: str`, `value: dict | None`, `error_code: str | None`, and `error_message: str | None`. Expose `parse_import_lines(raw: bytes, filename: str) -> list[ParsedImportLine]` and `import_job_file(db: AsyncSession, raw: bytes, filename: str) -> dict`.

CSV is selected by extension before JSON content detection. JSON arrays produce one logical line per array item. JSONL parsing catches errors per line instead of aborting the file.

- [x] **Step 4: Import the model module during database initialization**

Add `import model_class.knowledge_base  # noqa: F401` beside the existing model import in `init_db()` so `Base.metadata.create_all` creates all new tables.

- [x] **Step 5: Run focused tests and checkpoint**

Run: `python -m pytest tests/test_import_service.py -q`

Expected: parser and persistence tests PASS. Record `Get-FileHash model_class/knowledge_base.py,src/import_service.py` in the work log instead of committing.

---

### Task 2: Idempotent Import, Revisions, Quality, and Duplicate Governance

**Files:**
- Modify: `src/import_service.py`
- Modify: `src/job_data_service.py`
- Modify: `tests/test_import_service.py`
- Modify: `tests/test_job_data_service.py`

- [x] **Step 1: Write failing import behavior tests**

```python
@pytest.mark.asyncio
async def test_same_file_is_idempotent(memory_session):
    first = await import_job_file(memory_session, SAMPLE_BYTES, "jobs.json")
    second = await import_job_file(memory_session, SAMPLE_BYTES, "jobs.json")
    assert second["batch_id"] == first["batch_id"]
    assert second["idempotent"] is True
    assert await count_rows(memory_session, JobPosting) == first["imported"]

@pytest.mark.asyncio
async def test_changed_record_creates_revision(memory_session):
    await import_job_file(memory_session, job_bytes("R1", "熟悉Python和Flink开发"), "one.jsonl")
    result = await import_job_file(memory_session, job_bytes("R1", "熟悉Python、Flink和Kafka开发"), "two.jsonl")
    assert result["revised"] == 1
    assert await count_rows(memory_session, JobPostingRevision) == 1

@pytest.mark.asyncio
async def test_invalid_and_suspicious_records_are_classified(memory_session):
    result = await import_job_file(memory_session, MIXED_QUALITY_BYTES, "mixed.jsonl")
    assert result["quarantined"] == 1
    assert result["review"] >= 1
    assert {issue.code for issue in await quality_issues(memory_session)} >= {
        "description_too_short", "suspicious_industry"
    }
```

- [x] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_import_service.py tests/test_job_data_service.py -q`

Expected: FAIL on missing idempotency, revision, and quality behavior.

- [x] **Step 3: Implement deterministic quality assessment**

Add a frozen `QualityFinding` dataclass with `code`, `severity`, `field_name`, and `message` fields. Expose `extract_responsibilities(text: str) -> list[dict]` and `assess_job_quality(prepared: dict) -> list[QualityFinding]`.

Classify malformed/required-field failures as `quarantine`; classify suspicious industry, date conflict, no skills/responsibilities, source mismatch, or score below `0.70` as `review`. Do not perform network requests during quality assessment.

- [x] **Step 4: Implement idempotent import and revisions**

In `import_job_file()`:

```python
file_hash = hashlib.sha256(raw).hexdigest()
existing = await db.scalar(select(ImportBatch).where(ImportBatch.file_hash == file_hash))
if existing and existing.status == "completed":
    return batch_payload(existing, idempotent=True)
```

Persist every raw line first. Validate each parsed object independently. Preserve the prior `JobPosting.raw_payload` in `JobPostingRevision` before replacing a changed `record_id`. Mark exact and SimHash duplicates but retain their raw/source rows. Use a nested transaction per curated record so one database error cannot poison the full batch.

- [x] **Step 5: Run focused tests and regression tests**

Run: `python -m pytest tests/test_import_service.py tests/test_job_data_service.py -q`

Expected: all focused tests PASS and existing exact/near duplicate tests remain green.

---

### Task 3: Evidence-Backed Knowledge Chunks and Hybrid Retrieval

**Files:**
- Create: `src/knowledge_service.py`
- Modify: `src/import_service.py`
- Create: `tests/test_knowledge_service.py`

- [x] **Step 1: Write failing evidence and search tests**

```python
@pytest.mark.asyncio
async def test_import_creates_traceable_skill_and_responsibility_evidence(memory_session):
    await import_job_file(memory_session, PYTHON_JOB_BYTES, "python.jsonl")
    snippets = (await memory_session.execute(select(EvidenceSnippet))).scalars().all()
    assert {item.entity_type for item in snippets} >= {"skill", "responsibility"}
    assert all(item.evidence_text and item.job_posting_id for item in snippets)

@pytest.mark.asyncio
async def test_lexical_search_returns_source_metadata(memory_session):
    await import_job_file(memory_session, PYTHON_JOB_BYTES, "python.jsonl")
    await update_knowledge_chunks(memory_session, {"DATA_ENGINEER"})
    result = await search_knowledge(memory_session, "Flink 实时计算", family_code="DATA_ENGINEER")
    assert result["mode"] == "lexical"
    assert result["items"][0]["record_id"]
    assert result["items"][0]["source_url"]

def test_vector_search_uses_injected_embedder():
    embedder = DeterministicEmbedder({"实时计算": [1.0, 0.0], "数据治理": [0.0, 1.0]})
    assert cosine_similarity(embedder.embed_query("实时计算"), [1.0, 0.0]) == 1.0
```

- [x] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_knowledge_service.py -q`

Expected: FAIL because evidence/chunk/search services are missing.

- [x] **Step 3: Persist evidence during import**

For every extracted skill and responsibility, upsert an `EvidenceSnippet` containing `job_posting_id`, entity type/key, original text, optional offsets, text hash, confidence, and review status. The snippet text must be a substring of the JD; otherwise create a review issue and exclude it from published evidence.

- [x] **Step 4: Implement knowledge chunk update and retrieval**

Expose an `EmbeddingProvider` protocol with `embed_documents(texts)` and `embed_query(text)` methods. Expose `update_knowledge_chunks(db, family_codes, embedder=None) -> dict` and `search_knowledge(db, query, family_code=None, limit=10, embedder=None) -> dict`.

Use stable chunk IDs derived from source type, source entity ID, and content hash. The baseline performs deterministic lexical scoring over the persisted chunks. If an embedder is provided, store vector JSON and fuse lexical/vector scores; otherwise return `mode="lexical"`.

- [x] **Step 5: Run focused tests and checkpoint**

Run: `python -m pytest tests/test_knowledge_service.py tests/test_structured_extraction.py -q`

Expected: all tests PASS and every returned knowledge item contains evidence/source metadata.

---

### Task 4: No-Op-Aware Profile Versions, Responsibilities, Scenarios, and Evolution

**Files:**
- Modify: `src/job_analysis_service.py`
- Create: `tests/test_versioned_graph.py`
- Modify: `tests/test_job_analysis_service.py`

- [x] **Step 1: Write failing version and graph tests**

```python
@pytest.mark.asyncio
async def test_unchanged_rebuild_does_not_create_empty_version(populated_session):
    first = await rebuild_analysis(populated_session, family_codes={"DATA_ENGINEER"})
    second = await rebuild_analysis(populated_session, family_codes={"DATA_ENGINEER"})
    assert first["profiles_created"] == 1
    assert second["profiles_created"] == 0
    assert second["unchanged_families"] == ["DATA_ENGINEER"]

@pytest.mark.asyncio
async def test_changed_family_creates_evolution_events(populated_session):
    await rebuild_analysis(populated_session, family_codes={"DATA_ENGINEER"})
    await add_job(populated_session, skills="Python、Flink、Kafka")
    result = await rebuild_analysis(populated_session, family_codes={"DATA_ENGINEER"})
    assert result["profiles_created"] == 1
    events = await evolution_events(populated_session, "DATA_ENGINEER")
    assert any(item.change_type == "added" and item.entity_key == "Kafka" for item in events)

@pytest.mark.asyncio
async def test_graph_contains_version_responsibility_scenario_and_evidence(populated_session):
    graph = await graph_data(populated_session, family_code="DATA_ENGINEER", include_evidence=True)
    assert {node["type"] for node in graph["nodes"]} >= {
        "family", "job", "skill", "responsibility", "scenario", "evidence"
    }
```

- [x] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_versioned_graph.py tests/test_job_analysis_service.py -q`

Expected: FAIL because rebuild always creates a version and graph payload only includes jobs/skills.

- [x] **Step 3: Refactor family-scoped profile aggregation**

Change the public signature to `rebuild_analysis(db: AsyncSession, family_codes: set[str] | None = None) -> dict` without breaking callers that omit `family_codes`.

Build a canonical profile payload with sorted skills, responsibilities, scenarios, source count, posting count, and data cutoff. Hash its canonical JSON. Compare with the latest `JobProfileSnapshot`; create a new `JobProfile` only when the signature changes.

- [x] **Step 4: Persist profile relationships and evolution events**

Upsert normalized `Responsibility` and `IndustryScenario` entities, add profile links with prevalence/evidence/confidence, and compare latest/previous canonical payloads. Persist one `EvolutionEvent` per added, removed, or changed entity. Keep the existing `family_evolution()` prevalence response and add stored version events to it.

- [x] **Step 5: Expand graph payload with bounded evidence loading**

Extend `graph_data()` with keyword parameters `tech_stack`, `level`, `family_code`, `version`, `scope="draft"`, and `include_evidence=False`.

Return family, job-version, skill, responsibility, scenario, and optional evidence nodes. Default to latest versions and omit evidence to keep the overview bounded. Include `review_status`, version, confidence, and stable string IDs.

- [x] **Step 6: Run version and graph tests**

Run: `python -m pytest tests/test_versioned_graph.py tests/test_job_analysis_service.py tests/test_competition_api.py::test_competition_closed_loop -q`

Expected: all selected tests PASS; the existing closed-loop graph assertions remain compatible.

---

### Task 5: API, CLI, Export, and Neo4j Synchronization

**Files:**
- Modify: `src/api.py`
- Create: `src/build_knowledge_base.py`
- Modify: `src/job_graph_sync.py`
- Modify: `tests/test_competition_api.py`
- Create: `tests/test_build_knowledge_base.py`

- [x] **Step 1: Write failing API and CLI tests**

```python
@pytest.mark.asyncio
async def test_import_api_returns_batch_and_quarantine(competition_client):
    response = await competition_client.post(
        "/api/data/import", files={"file": ("jobs.json", MIXED_BYTES, "application/json")}
    )
    assert response.status_code == 200
    assert response.json()["batch_id"]
    batches = (await competition_client.get("/api/data/import-batches")).json()
    quarantine = (await competition_client.get("/api/data/quarantine")).json()
    assert batches["total"] == 1
    assert quarantine["total"] >= 1

@pytest.mark.asyncio
async def test_search_and_version_endpoints(competition_client):
    assert (await competition_client.get("/api/knowledge/search", params={"q": "Flink"})).status_code == 200
    assert (await competition_client.get("/api/graph/versions/DATA_ENGINEER")).status_code == 200

def test_cli_build_writes_report_and_graph(tmp_path):
    result = run_build(input_path, data_dir=tmp_path)
    assert result.report_path.exists()
    assert result.graph_path.exists()
```

- [x] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_competition_api.py tests/test_build_knowledge_base.py -q`

Expected: FAIL on missing batch, quarantine, search, version, and CLI behavior.

- [x] **Step 3: Add API routes and integrate incremental updates**

Change `/api/data/import` to call `import_job_file()`, then rebuild only `affected_families` and update their knowledge chunks. Add:

```text
GET /api/data/import-batches
GET /api/data/quarantine
GET /api/knowledge/search?q=&family_code=&limit=
GET /api/graph/versions/{family_code}
```

Extend `/api/graph` query parameters with `family_code`, `version`, `scope`, and `include_evidence`. Return actual retrieval and graph-sync status instead of masking fallbacks.

- [x] **Step 4: Implement repeatable build CLI and artifact export**

Expose a frozen `BuildResult` dataclass containing `report_path: Path`, `graph_path: Path`, and `summary: dict`. Expose `build_knowledge_base(input_path: Path, data_dir: Path | None = None) -> BuildResult` and a zero-argument `main()` CLI entry point.

The CLI initializes schema, imports the file, updates affected profiles/chunks, exports UTF-8 JSON with `ensure_ascii=False`, and prints a concise summary. Re-running the same file returns the existing batch and does not create profile versions.

- [x] **Step 5: Generalize Neo4j synchronization**

Create constraints for stable node IDs and sync each node type with its dedicated label. Map only known edge types to Cypher relation names. Use `MERGE` for nodes and relations, update relation properties, and leave historical version nodes intact. Return counts by node type, relation count, and sync timestamp.

- [x] **Step 6: Run API/CLI tests and checkpoint**

Run: `python -m pytest tests/test_competition_api.py tests/test_build_knowledge_base.py -q`

Expected: all API/CLI tests PASS, including Neo4j-unavailable fallback behavior.

---

### Task 6: Governance and Expanded Graph Frontend

**Files:**
- Modify: `index.html`
- Modify: `tests/test_competition_api.py`

- [x] **Step 1: Add failing static frontend assertions**

```python
@pytest.mark.asyncio
async def test_frontend_exposes_batch_quarantine_and_graph_filters(competition_client):
    html = (await competition_client.get("/")).text
    assert "导入批次" in html
    assert "异常隔离" in html
    assert "节点类型" in html
    assert "画像版本" in html
    assert "/api/data/import-batches" in html
    assert "/api/data/quarantine" in html
```

- [x] **Step 2: Run the test and verify RED**

Run: `python -m pytest tests/test_competition_api.py::test_frontend_exposes_batch_quarantine_and_graph_filters -q`

Expected: FAIL because the current page only displays aggregate governance and job-skill filters.

- [x] **Step 3: Implement governance and graph UI changes**

Add compact batch and quarantine tables to the governance page. Add family, version, node-type, scope, and evidence controls to the graph page. Extend SVG rendering colors/icons for family, job, skill, responsibility, scenario, and evidence nodes. Clicking a node opens a detail panel containing version, confidence, review state, source URL, and evidence text when available.

- [x] **Step 4: Run static/API tests and browser smoke test**

Run: `python -m pytest tests/test_competition_api.py -q`

Then run the local server and verify the governance and graph views in the in-app browser. Check that the browser console contains no errors and graph controls remain usable.

Expected: tests PASS; batch/quarantine data load; graph filters and detail panel operate without console errors.

---

### Task 7: Real Data Build, Documentation, and Final Verification

**Files:**
- Modify: `README.md`
- Modify: `QUICKSTART.md`
- Generate: `data/job_competency.db`
- Generate: `data/imports/<batch-id>-report.json`
- Generate: `data/exports/knowledge-graph.json`

- [x] **Step 1: Update operator documentation**

Document the exact first-build command, repeated update command, generated artifacts, API examples, Neo4j sync, lexical fallback, quarantine review, and the distinction between unlabelled build data and the independent 90% benchmark.

- [x] **Step 2: Run all automated tests before touching real artifacts**

Run: `python -m pytest -q`

Expected: all tests PASS and configured core coverage remains at or above 60%.

- [x] **Step 3: Build the first knowledge base and graph**

Run from `langchain_deepseek`:

```powershell
python -m src.build_knowledge_base ..\jd_raw.json
```

Expected report invariants:

```text
raw_lines = 1855
parsed_lines = 1854
quarantined >= 3
affected_families = 8 on first run
graph export exists and contains family/job/skill/responsibility/scenario nodes
```

The exact valid/review/duplicate counts are data-derived and must be copied from the generated report, not predetermined.

- [x] **Step 4: Verify idempotency against the real file**

Run the same build command a second time.

Expected: the same `batch_id` is returned with `idempotent=true`; job posting, chunk, and profile-version counts do not increase.

- [x] **Step 5: Run final compilation, tests, artifact checks, and browser verification**

Run:

```powershell
python -m compileall -q src model_class schemes config tests
python -m pytest -q
python -m src.build_knowledge_base ..\jd_raw.json
```

Inspect the generated report and graph JSON with a JSON parser. Start the API, verify dashboard/governance/graph pages in the in-app browser, and confirm zero console errors.

Expected: compilation exits 0; all tests pass; core coverage is at least 60%; report and graph JSON parse successfully; the UI displays actual first-build counts and expanded graph node types.
