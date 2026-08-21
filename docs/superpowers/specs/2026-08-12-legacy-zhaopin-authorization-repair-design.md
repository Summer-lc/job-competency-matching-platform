# Legacy Zhaopin Authorization Repair Design

## Goal

Convert the previously imported `jd_raw.json` records from unverified legacy rows into fully traceable authorized-platform observations without re-importing or changing job facts, then rebuild quality gates, deduplication, profiles, evolution evidence, the knowledge base, and the knowledge graph.

## Confirmed Authorization

The team confirmed on 2026-08-12 that `jd_raw.json` was collected within the permitted scope and is authorized for this competition research. The repair command must still require an explicit authorization switch and a non-empty authorization note so provenance can never be upgraded accidentally.

## Eligibility Boundary

A row is eligible only when all of the following hold:

- `source_id` is absent.
- `provenance_status` is `unverified`.
- The original source name is exactly `智联招聘`.
- The parsed source URL hostname is `zhaopin.com` or a subdomain of `zhaopin.com`.
- The approved registry entry is `zhaopin_legacy_import`, with `market_scope=china`, `source_type=authorized_platform`, `collection_mode=file_import`, and `compliance_status=manual_only`.

Rows outside this boundary are left unchanged.

## Field Repair

For eligible rows, use the reviewed source registry entry to populate:

- `source_id`, `source_name`, `source_type`, and `source_domain`.
- `collection_method`, `parser_name`, and `parser_version`.
- `provenance_status=approved`.
- `source_record_id` from the existing team record identifier when absent.
- `first_seen_at` and `last_seen_at` from the real `collected_at` value when absent.

The repair does not manufacture publication dates. Existing unsupported and implausible publication dates continue to be cleared by the established historical repair rule. Their original values remain in the immutable raw payload and the structured repair audit. Plausible but untrusted dates remain marked `published_at_trusted=false`, so they cannot become formal evolution evidence.

## Audit And Safety

- Dry-run and apply reports include the authorization source and note.
- Every changed database field creates a `data_repair_audit` row with before value, after value, reason code, and rule version.
- The original `raw_payload`, original URL, job description, and record count remain unchanged.
- Apply mode requires `--full --repair --confirm`, holds the existing exclusive database lock, creates and verifies a SQLite backup, and rolls back the entire transaction if rebuilding fails.
- Reusing a completed run remains idempotent and must validate that the run belongs to the same database.

## Rebuild And Acceptance

After repair, the existing full hard-metrics pipeline performs deduplication, gate reassessment, seniority classification, quarterly profiles, evolution evidence, knowledge chunks, and acceptance statistics. The graph export is regenerated after the committed rebuild.

Acceptance checks are:

- No row outside the eligibility boundary changed.
- Raw job row count is unchanged.
- Foreign-market row count remains zero.
- Every eligible repaired row has approved Chinese-market provenance and real observation timestamps.
- Unsupported dates are not used as trusted evolution evidence.
- Database integrity, focused tests, the full test suite, coverage, lint, and graph audit pass.
- The final report states raw count, usable unique count, family coverage, status distribution, source distribution, backup path, repair report path, graph node/edge counts, and remaining gaps.

