# Official Evidence Gap-Fill Design

## Objective

Add a reviewed supplemental evidence corpus for the 14 job families that are not covered by the existing 24-record official corpus. The supplement contains exactly three records per family, for 42 records total, and remains independently auditable and incrementally importable.

## Scope

Covered families:

- `AI_AGENT_ENGINEER`
- `AI_SOLUTION_ENGINEER`
- `CLOUD_NATIVE_ENGINEER`
- `DEVOPS_ENGINEER`
- `FRONTEND_DEVELOPER`
- `GO_DEVELOPER`
- `JAVA_DEVELOPER`
- `LLM_APPLICATION_ENGINEER`
- `MLOPS_ENGINEER`
- `MULTIMODAL_ENGINEER`
- `PROMPT_ENGINEER`
- `PYTHON_BACKEND`
- `RAG_ENGINEER`
- `SRE_ENGINEER`

The work does not relax job-posting quality gates, generate synthetic job postings, or import the unverified 600-record raw evidence batch.

## Data Contract

The supplemental file is `data/evidence/official-standards-2026-supplement.jsonl`. Each UTF-8 JSONL record uses the existing `EvidenceInput` fields and must satisfy all of the following:

- Unique `evidence_id`, title, and source URL within the supplement.
- An HTTPS URL on an explicitly reviewed official standards, government, project, or vendor documentation domain.
- Evidence type in `occupation_standard`, `technical_standard`, `policy_document`, or `official_document`.
- A 60-200 Chinese-character original summary that explains both the source content and its competency mapping.
- Exactly three records for every covered family.
- At least two evidence types per family and at least one standards or specification record per family.
- No copied standards text or long source quotation; store only metadata and an original grounded summary.

## Validation Design

Keep the original eight-family baseline contract unchanged. Extend the evidence validator with a distinct supported-family set and an explicit `required_families` option so the original 24-record corpus and the new 42-record supplement can be validated independently.

Add reviewed official domain suffixes only for domains used by the supplement. Reject unlisted domains, HTTP URLs, duplicate identifiers, duplicate titles, duplicate URLs, unsupported families, invalid evidence types, and summaries outside the required length.

## Import And Graph Flow

1. Validate the supplemental JSONL offline.
2. Back up the SQLite database.
3. Import through the existing evidence import service, using stable evidence IDs for idempotency.
4. Import the same file a second time in verification and require all records to be skipped.
5. Rebuild knowledge chunks and export the knowledge graph.
6. Confirm the graph exposes evidence nodes and family-to-evidence relationships without duplicate records.

## Source Review

Use only source pages opened and checked on 2026-08-11. Correct known raw-data errors rather than inheriting them. In particular, do not use ISO URL `/standard/78843.html` as AI evidence because it resolves to a steel-tube nondestructive-testing standard; use the verified ISO, NIST, official project, and official product documentation pages selected for each family.

## Testing And Acceptance

- A dataset test requires 42 valid records and exactly three records for each supplemental family.
- A regression test confirms the original 24-record corpus still validates unchanged.
- A negative test rejects the known incorrect ISO 78843 AI mapping.
- Evidence import tests prove first-import persistence and repeat-import idempotency.
- The focused tests, full test suite, linter, database integrity check, backup integrity check, and stopped-service check must pass before completion is reported.

## Expected Outcome

Official evidence coverage increases from 8 to all 22 modeled job families, with 66 curated records across the baseline and supplemental corpora. This closes the external-evidence family-coverage gap, but it does not by itself close the separate 5,000-7,000 usable job-posting target.
