# Evidence Knowledge Q&A Center Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a product-facing knowledge Q&A center that combines JD knowledge chunks and external evidence, generates citation-grounded DeepSeek answers, and remains usable through an extractive fallback.

**Architecture:** A dedicated service owns evidence retrieval, normalization, ranking, prompt construction, citation validation, and fallback behavior. FastAPI exposes one typed endpoint, while the existing search and assistant endpoints remain compatible. The static frontend adds a single-turn question workspace and renders the answer beside its exact evidence records.

**Tech Stack:** Python 3.11+, SQLAlchemy async, Pydantic 2, FastAPI, LangChain OpenAI-compatible chat client, HTML/CSS/JavaScript, pytest/pytest-asyncio.

**Repository note:** This workspace is not a Git repository. Use focused test runs, full regression tests, and file hashes instead of commits.

---

### Task 1: Evidence Retrieval and Grounded Answer Service

**Files:**
- Create: `src/evidence_qa_service.py`
- Create: `tests/test_evidence_qa_service.py`

- [x] **Step 1: Write failing service tests**

Create an in-memory async database fixture containing two `KnowledgeChunk` rows from different families and one matching `EvidenceRecord`. Add tests that require:

```python
items = await gather_answer_evidence(
    session, "Flink 实时计算能力", family_code="DATA_ENGINEER", limit=6
)
assert [item["citation_id"] for item in items] == ["K1", "K2"]
assert {item["source_kind"] for item in items} == {"jd", "external"}
assert all(item["family_code"] == "DATA_ENGINEER" for item in items)
```

Add tests for `validate_citations()` accepting `[K1]` and rejecting missing or out-of-range citations. Add async tests where an injected model invoker returns a valid cited answer, raises an exception, or returns `[K99]`; the latter two must return `mode="extractive_fallback"` and an answer containing `[K1]`.

- [x] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_evidence_qa_service.py -q --no-cov`

Expected: FAIL because `src.evidence_qa_service` does not exist.

- [x] **Step 3: Implement normalized evidence retrieval**

Expose:

```python
class NoEvidenceError(ValueError): ...

async def gather_answer_evidence(
    db: AsyncSession,
    question: str,
    *,
    family_code: str | None = None,
    limit: int = 6,
) -> list[dict]: ...
```

Use `search_knowledge()` for JD chunks and a family-filtered `EvidenceRecord` query for external evidence. Compute deterministic lexical overlap for external evidence, add a bounded source-score bonus, deduplicate by normalized text plus source URL, sort by relevance then stable source key, cap the result to 3-12 items, and assign `K1...Kn` only after sorting.

Each item must contain `citation_id`, `source_kind`, `evidence_type`, `family_code`, `title`, `organization`, `record_id`, `review_status`, `text`, `source_url`, and rounded `score`.

- [x] **Step 4: Implement prompting, citation validation, and fallback**

Expose:

```python
def build_grounded_prompt(question: str, evidence: list[dict]) -> str: ...
def validate_citations(answer: str, evidence: list[dict]) -> bool: ...
def build_extractive_answer(evidence: list[dict]) -> str: ...
async def answer_knowledge_question(
    db: AsyncSession,
    question: str,
    *,
    family_code: str | None = None,
    limit: int = 6,
    model: str | None = None,
    model_invoker=None,
) -> dict: ...
```

The default invoker calls `get_llm(model).invoke(prompt)` through `asyncio.to_thread`. Catch model errors and invalid citations, returning `extractive_fallback`; raise `NoEvidenceError` only when retrieval is empty. Return `answer`, `mode`, `family_code`, `citations_valid`, `evidence`, and `warning`.

- [x] **Step 5: Run service tests and verify GREEN**

Run: `python -m pytest tests/test_evidence_qa_service.py tests/test_knowledge_service.py -q --no-cov`

Expected: all selected tests PASS.

### Task 2: Typed Knowledge Answer API

**Files:**
- Modify: `schemes/job_competency.py`
- Modify: `src/api.py`
- Modify: `tests/test_competition_api.py`

- [x] **Step 1: Write failing API tests**

Require `/api/knowledge/answer` in OpenAPI. Add a successful endpoint test that imports a searchable JD, monkeypatches `src.api.answer_knowledge_question` with an async deterministic response, posts a question, and asserts `mode`, `citations_valid`, and evidence fields. Add a no-evidence test using the real service and assert HTTP 422.

- [x] **Step 2: Run focused API tests and verify RED**

Run: `python -m pytest tests/test_competition_api.py -q --no-cov`

Expected: FAIL because the route and request schema do not exist.

- [x] **Step 3: Add request schema and endpoint**

Add:

```python
class KnowledgeAnswerRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=500)
    family_code: Optional[str] = Field(None, max_length=80)
    limit: int = Field(6, ge=3, le=12)
    model: Optional[str] = None
```

Import `answer_knowledge_question` and `NoEvidenceError` in `src/api.py`. Add `POST /api/knowledge/answer`, map `NoEvidenceError` to HTTP 422, and leave other exceptions visible as standard server errors rather than fabricating an answer.

- [x] **Step 4: Run API and service tests**

Run: `python -m pytest tests/test_competition_api.py tests/test_evidence_qa_service.py -q --no-cov`

Expected: all selected tests PASS and existing endpoints remain present.

### Task 3: Knowledge Q&A Frontend

**Files:**
- Modify: `index.html`
- Modify: `tests/test_competition_api.py`

- [x] **Step 1: Add failing static frontend assertions**

Require the HTML to contain `知识问答`, `knowledge-question`, `knowledge-family`, `knowledge-evidence`, `生成证据回答`, `/api/knowledge/answer`, and `[K1]`. Keep the existing product-copy prohibitions.

- [x] **Step 2: Run the frontend test and verify RED**

Run: `python -m pytest tests/test_competition_api.py::test_frontend_exposes_knowledge_qa_center -q --no-cov`

Expected: FAIL because the page is absent.

- [x] **Step 3: Add navigation, page markup, and styles**

Insert “知识问答” after “全景图谱” and renumber later navigation items. Add the page controls, recommended-question buttons, answer card, mode badge, warning area, and evidence card grid. Evidence cards must show citation ID, source kind, title, organization, score, text, review status, and source link when present.

- [x] **Step 4: Add frontend behavior**

Extend `titles`, `switchPage()`, and `loadProfiles()` for the knowledge page. Implement `askRecommendedQuestion(text)`, `searchKnowledgeEvidence()`, `answerKnowledgeQuestion()`, and `renderKnowledgeEvidence(items)`. Disable the answer button while running, use `textContent` for question/status text, render escaped evidence fields, and make empty/error states explicit.

- [x] **Step 5: Run frontend and API tests**

Run: `python -m pytest tests/test_competition_api.py -q --no-cov`

Expected: all frontend/API tests PASS.

### Task 4: Documentation and Final Verification

**Files:**
- Modify: `USER_GUIDE.md`
- Modify: `README.md`

- [x] **Step 1: Document the knowledge Q&A workflow**

Add the question workflow, evidence interpretation, answer modes, refusal behavior, and recommended example questions to `USER_GUIDE.md`. Add `POST /api/knowledge/answer` to the README interface table and include the Q&A center in the main workflow.

- [x] **Step 2: Run compilation and full tests**

Run:

```powershell
python -m compileall -q src model_class schemes config tests
python -m pytest -q
```

Expected: compilation exits 0, all tests pass, and configured coverage remains at or above 60%.

- [x] **Step 3: Smoke-test real knowledge data without calling the model**

Call `gather_answer_evidence()` against `data/job_competency.db` for `DATA_ENGINEER` and `Flink 实时计算`, verify at least one JD evidence item, stable `K1` numbering, source URLs, and family filtering.

- [x] **Step 4: Browser verification**

Start the local service, open the knowledge Q&A page, run a raw evidence search and one grounded answer, verify citations and source cards, inspect browser console errors, then stop the service so the system remains stopped after development.
