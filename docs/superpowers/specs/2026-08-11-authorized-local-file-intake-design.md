# Authorized Local File Intake Design

## Purpose

Safely ingest team-provided historical Zhaopin JSONL exports without contacting URLs from the files or treating unverified dates and labels as trusted evidence.

## Data Flow

1. Accept only an explicit local `.jsonl` file for the registered `zhaopin_legacy_import` source.
2. Require an explicit authorization note and a dry-run before commit.
3. Snapshot the exact input bytes into the collection run and record SHA-256 hashes for the file and every parsed line.
4. Recompute each record from the snapshot during commit.
5. Canonicalize Zhaopin URLs to HTTPS, preserve the original URL in audit metadata, and reject any non-Zhaopin host.
6. Reclassify from title and capability evidence. Strong automatic classifications replace search-keyword labels; uncertain classifications remain in review.
7. Trust a publication date only when it is parseable, no later than collection time plus one day, and no more than 365 days before collection. Other values remain review evidence and cannot drive evolution analysis.
8. Commit only staged valid unique records after a verified SQLite backup. Review and quarantine artifacts remain outside the production database.

## Evidence Files

Evidence JSONL files are audited separately. Duplicate URLs or titles, unsupported evidence types, non-approved domains, and summaries outside 60-200 Chinese characters remain in a review report. The system must not fabricate longer summaries or claim that an unvisited URL supports a conclusion.

## Safety And Verification

- No network request is made from a local input URL.
- File size, line size, JSON depth, duplicate keys, symlinks, and reparse points are rejected.
- Commit validates the current source registry, snapshot hash, canonical recomputation, attestation, and database backup.
- Focused adapter, service, CLI, and provenance tests run before the full suite.

