# Job Data Quantity Expansion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Enforce the approved trusted-date counting contract and safely grow the production database from 546 qualifying JD records to at least 5,000 without unauthorized collection or duplicate inflation.

**Architecture:** Extend the existing collection target and coverage modules instead of adding a second ingestion pipeline. Authorized files continue through the existing preflight, dry-run, review, commit, rebuild, and coverage workflow; new logic changes what counts as qualifying and makes preflight yield visible before any database write.

**Tech Stack:** Python 3.11, Pydantic 2, SQLAlchemy 2, SQLite, pytest, and the existing job-collection adapters and CLI.

**Repository note:** This directory is not currently a Git repository. Do not initialize Git implicitly. Record changed-file SHA-256 hashes at each checkpoint and use the existing SQLite backup/rollback workflow. If the owner later places it under Git, use the commit messages listed below.

---

## File map

- Modify config/job_collection_targets.json: trusted-date bounds and core-family minimums.
- Modify src/job_collection/coverage.py: qualifying-count rules, per-family targets, and date reporting.
- Modify src/collect_jobs.py: coverage query fields and quality-aware authorized-file preflight.
- Modify tests/test_collection_coverage.py and tests/test_collect_jobs_cli.py: TDD coverage.
- Modify docs/NATIONAL_AUTHORIZED_DATA_INTAKE.md: quantity-A operating procedure.
- Create data/expansion-reports/quantity-a-baseline-20260814.json after verification.
- Create data/expansion-reports/quantity-a-final.json after all batches finish.
- Create data/benchmark/job-data-quantity-a-300.jsonl as the frozen audit sample.

### Task 1: Extend the collection target contract

**Files:**
- Modify: config/job_collection_targets.json:2
- Modify: src/job_collection/coverage.py:18-47
- Test: tests/test_collection_coverage.py:17-36

- [ ] **Step 1: Write failing target-contract tests**

Add json and pytest imports, then add these assertions to test_default_collection_targets_match_internal_acceptance_contract:

    assert targets.minimum_published_year == 2022
    assert targets.maximum_published_year == 2026
    assert targets.require_trusted_published_at is True
    assert targets.family_minimum_overrides == {
        "JAVA_DEVELOPER": 500,
        "AI_AGENT_ENGINEER": 500,
    }

Add this validation test:

    def test_collection_targets_reject_invalid_date_and_family_overrides(tmp_path):
        payload = json.loads(TARGETS_PATH.read_text(encoding="utf-8"))
        payload["minimum_published_year"] = 2027
        payload["maximum_published_year"] = 2026
        payload["family_minimum_overrides"] = {"UNKNOWN": 500}
        path = tmp_path / "targets.json"
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(ValueError, match="collection targets"):
            load_collection_targets(path)

- [ ] **Step 2: Run tests and verify failure**

    python -m pytest tests/test_collection_coverage.py::test_default_collection_targets_match_internal_acceptance_contract tests/test_collection_coverage.py::test_collection_targets_reject_invalid_date_and_family_overrides -q

Expected: FAIL because CollectionTargets has no date or override fields.

- [ ] **Step 3: Add the target fields**

Add to CollectionTargets:

    minimum_published_year: int = Field(ge=2000, le=2100, strict=True)
    maximum_published_year: int = Field(ge=2000, le=2100, strict=True)
    require_trusted_published_at: bool
    family_minimum_overrides: dict[str, int]

Add before validate_contract returns:

    if self.minimum_published_year > self.maximum_published_year:
        raise ValueError("minimum published year cannot exceed maximum")
    unknown_overrides = set(self.family_minimum_overrides) - set(
        self.required_job_families
    )
    if unknown_overrides:
        raise ValueError(
            f"family minimum overrides contain unknown families: {sorted(unknown_overrides)}"
        )
    if any(
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < self.minimum_usable_per_family
        for value in self.family_minimum_overrides.values()
    ):
        raise ValueError(
            "family minimum overrides must be integers at least minimum_usable_per_family"
        )

Add these JSON keys after maximum_usable_unique:

    "minimum_published_year": 2022,
    "maximum_published_year": 2026,
    "require_trusted_published_at": true,
    "family_minimum_overrides": {
      "JAVA_DEVELOPER": 500,
      "AI_AGENT_ENGINEER": 500
    },

- [ ] **Step 4: Run the coverage test module**

    python -m pytest tests/test_collection_coverage.py -q

Expected: the new contract tests pass after Task 2 updates dated fixtures.

- [ ] **Step 5: Record checkpoint hashes**

    Get-FileHash config/job_collection_targets.json,src/job_collection/coverage.py,tests/test_collection_coverage.py -Algorithm SHA256

Git commit message if Git becomes available: feat: define trusted-date collection targets

### Task 2: Count only trusted 2022-2026 records

**Files:**
- Modify: src/job_collection/coverage.py:95-195
- Test: tests/test_collection_coverage.py:38-105

- [ ] **Step 1: Make the test row time-qualified**

Add to _row:

    "published_at": "2026-08-01T00:00:00",
    "published_at_trusted": True,

Add:

    def test_coverage_excludes_untrusted_and_out_of_window_dates():
        targets = load_collection_targets(TARGETS_PATH)
        rows = [
            _row(1),
            _row(2, published_at_trusted=False),
            _row(3, published_at="2021-12-31T00:00:00"),
            _row(4, published_at="2027-01-01T00:00:00"),
            _row(5, published_at=None),
        ]

        report = build_coverage_report(targets=targets, usable_rows=rows)

        assert report["usable_unique"]["current"] == 1
        assert report["date_window"] == {
            "minimum_year": 2022,
            "maximum_year": 2026,
            "trusted_required": True,
            "qualifying": 1,
            "excluded": 4,
            "year_counts": {"2026": 1},
        }

Add:

    def test_coverage_applies_core_family_minimum_overrides():
        targets = load_collection_targets(TARGETS_PATH)
        rows = [_row(index) for index in range(1, 102)]
        rows.extend(
            _row(200 + index, job_family_id="AI_AGENT_ENGINEER")
            for index in range(1, 102)
        )

        report = build_coverage_report(targets=targets, usable_rows=rows)
        below = {
            item["job_family_id"]: item
            for item in report["families_below_minimum"]
        }

        assert below["JAVA_DEVELOPER"]["target"] == 500
        assert below["JAVA_DEVELOPER"]["gap"] == 399
        assert below["AI_AGENT_ENGINEER"]["target"] == 500
        assert report["family_targets"]["PYTHON_BACKEND"] == 100

- [ ] **Step 2: Run tests and verify failure**

    python -m pytest tests/test_collection_coverage.py -q

Expected: FAIL because date trust and core overrides are ignored.

- [ ] **Step 3: Implement the pure helpers**

Add datetime imports and these helpers before _is_usable:

    from datetime import date, datetime

    def _published_year(value: object) -> int | None:
        if isinstance(value, (datetime, date)):
            return value.year
        raw = str(value or "").strip()
        if len(raw) < 4 or not raw[:4].isdigit():
            return None
        return int(raw[:4])

    def _family_target(targets: CollectionTargets, family: str) -> int:
        return targets.family_minimum_overrides.get(
            family, targets.minimum_usable_per_family
        )

Replace _is_usable:

    def _is_usable(row: Mapping[str, object], targets: CollectionTargets) -> bool:
        year = _published_year(row.get("published_at"))
        date_qualified = (
            year is not None
            and targets.minimum_published_year <= year <= targets.maximum_published_year
            and (
                not targets.require_trusted_published_at
                or bool(row.get("published_at_trusted"))
            )
        )
        return bool(
            row.get("gate_status") == "valid"
            and row.get("provenance_status") == "approved"
            and row.get("duplicate_of_id") is None
            and row.get("job_family_id") in targets.required_job_families
            and date_qualified
        )

In build_coverage_report, count date_excluded before discarding rows. Use _family_target for every family gap. Count qualifying years and the three approved windows for Java and AI Agent. Add family_targets, core_family_windows, and:

    "date_window": {
        "minimum_year": targets.minimum_published_year,
        "maximum_year": targets.maximum_published_year,
        "trusted_required": targets.require_trusted_published_at,
        "qualifying": total,
        "excluded": date_excluded,
        "year_counts": dict(sorted(year_counts.items())),
    },

The core_family_windows output must have this stable shape:

    {
        "JAVA_DEVELOPER": {
            "2022-2023": 0,
            "2024-2025": 0,
            "2026": 1,
        },
        "AI_AGENT_ENGINEER": {
            "2022-2023": 0,
            "2024-2025": 0,
            "2026": 0,
        },
    }

In recommend_batches, calculate family_gap with _family_target.

- [ ] **Step 4: Run coverage tests**

    python -m pytest tests/test_collection_coverage.py -q

Expected: PASS.

- [ ] **Step 5: Record checkpoint hashes**

    Get-FileHash src/job_collection/coverage.py,tests/test_collection_coverage.py -Algorithm SHA256

Git commit message if available: feat: enforce trusted 2022-2026 coverage

### Task 3: Expose truthful authorized-export yield

**Files:**
- Modify: src/collect_jobs.py:247-281
- Modify: tests/test_collect_jobs_cli.py:468-528

- [ ] **Step 1: Expand preflight expectations**

Replace the exact result-key assertion with:

    assert set(result) == {
        "source_id",
        "authorization_reference",
        "valid_until",
        "access_method",
        "input_filename",
        "input_file_sha256",
        "row_count",
        "accepted_count",
        "rejected_count",
        "valid_candidate_count",
        "review_candidate_count",
        "quarantined_candidate_count",
        "trusted_window_candidate_count",
        "candidate_family_counts",
    }
    assert result["valid_candidate_count"] == 1
    assert result["review_candidate_count"] == 0
    assert result["quarantined_candidate_count"] == 0
    assert result["trusted_window_candidate_count"] == 1
    assert result["candidate_family_counts"] == {"JAVA_DEVELOPER": 1}

- [ ] **Step 2: Run and verify failure**

    python -m pytest tests/test_collect_jobs_cli.py::test_authorization_preflight_returns_only_non_sensitive_inventory -q

Expected: FAIL because the quality-aware fields are absent.

- [ ] **Step 3: Add a pure summary helper**

Import Counter, then add above execute:

    def _authorized_preflight_quality(
        records: Sequence[object], *, minimum_year: int, maximum_year: int
    ) -> dict[str, object]:
        statuses: Counter[str] = Counter()
        families: Counter[str] = Counter()
        trusted_window = 0
        for record in records:
            extra = dict(getattr(record, "adapter_extra", {}) or {})
            gate = dict(extra.get("quality_gate") or {})
            status = str(gate.get("status") or "quarantined")
            statuses[status] += 1
            if status == "valid":
                family = str(getattr(record, "job_family_id", "UNKNOWN"))
                families[family] += 1
                published_at = getattr(record, "published_at", None)
                trusted = bool(getattr(record, "published_at_trusted", False))
                if (
                    trusted
                    and published_at is not None
                    and minimum_year <= published_at.year <= maximum_year
                ):
                    trusted_window += 1
        return {
            "valid_candidate_count": statuses["valid"],
            "review_candidate_count": statuses["review"],
            "quarantined_candidate_count": statuses["quarantined"],
            "trusted_window_candidate_count": trusted_window,
            "candidate_family_counts": dict(sorted(families.items())),
        }

Load targets during preflight and merge:

    targets = load_collection_targets()
    quality = _authorized_preflight_quality(
        records,
        minimum_year=targets.minimum_published_year,
        maximum_year=targets.maximum_published_year,
    )

Return the existing inventory fields followed by **quality.

- [ ] **Step 4: Run CLI and adapter tests**

    python -m pytest tests/test_collect_jobs_cli.py tests/test_authorized_export_adapter.py -q

Expected: PASS.

- [ ] **Step 5: Record checkpoint hashes**

    Get-FileHash src/collect_jobs.py,tests/test_collect_jobs_cli.py -Algorithm SHA256

Git commit message if available: feat: report authorized export quality yield

### Task 4: Align the coverage query and add safe report output

**Files:**
- Modify: src/collect_jobs.py:65-92, 128-145, 282-304, 338-365
- Test: tests/test_collect_jobs_cli.py

- [ ] **Step 1: Add a query-contract test**

    def test_coverage_query_includes_publication_trust_fields():
        import inspect
        import src.collect_jobs as cli

        source = inspect.getsource(cli.execute)
        assert "JobPosting.published_at" in source
        assert "JobPosting.published_at_trusted" in source

- [ ] **Step 2: Run and verify failure**

    python -m pytest tests/test_collect_jobs_cli.py::test_coverage_query_includes_publication_trust_fields -q

Expected: FAIL.

- [ ] **Step 3: Extend the select list**

Add after JobPosting.region:

    JobPosting.published_at,
    JobPosting.published_at_trusted,

- [ ] **Step 4: Add a coverage-only output argument and atomic writer**

Add to build_parser:

    parser.add_argument(
        "--output",
        type=Path,
        help="Write coverage JSON under data/expansion-reports.",
    )

At the start of validate_args, reject output for every mode except coverage:

    if args.output and not args.coverage_report:
        raise ValueError("--output is limited to --coverage-report")

Do not include args.output among the options rejected inside the coverage-report branch. Add this helper above main:

    def _write_coverage_output(path: Path, payload: str) -> Path:
        root = (
            Path(__file__).resolve().parents[1]
            / "data"
            / "expansion-reports"
        ).resolve()
        target = path.resolve()
        if target.parent != root or target.suffix.casefold() != ".json":
            raise ValueError(
                "coverage output must be a JSON file directly under data/expansion-reports"
            )
        root.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(payload + "\n", encoding="utf-8")
        temporary.replace(target)
        return target

In main, serialize once and write only when requested:

    payload = json.dumps(
        result, ensure_ascii=False, indent=2, sort_keys=True, default=str
    )
    if args.output:
        _write_coverage_output(args.output, payload)
    print(payload)

Add tests that --output is accepted only with --coverage-report, that a nested or non-JSON target is rejected, and that a successful write can be parsed back as the exact result.

- [ ] **Step 5: Run tests and the read-only report**

    python -m pytest tests/test_collect_jobs_cli.py tests/test_collection_coverage.py -q
    python -m src.collect_jobs --coverage-report --output data/expansion-reports/quantity-a-baseline-20260814.json

Expected: tests pass; usable_unique.current equals 546 and gap_to_minimum equals 4454.

- [ ] **Step 6: Record checkpoint hashes**

    Get-FileHash src/collect_jobs.py,tests/test_collect_jobs_cli.py -Algorithm SHA256

Git commit message if available: fix: align coverage query with trusted dates

### Task 5: Add deterministic authorized-file batch offsets

**Files:**
- Modify: src/job_collection/adapters/authorized_export.py:104-175
- Modify: src/job_collection/service.py:640-835
- Modify: src/collect_jobs.py:65-240
- Modify: tests/test_authorized_export_adapter.py
- Modify: tests/test_collection_service.py
- Modify: tests/test_collect_jobs_cli.py

- [ ] **Step 1: Write failing offset tests**

In test_authorized_export_adapter.py, create three valid rows with different source_record_id values, call load_file with record_offset=1 and max_records=1, and assert only the second source record is returned. Add rejection tests for -1 and bool offsets.

In test_collect_jobs_cli.py, assert --record-offset is accepted only for a new authorized-export dry-run, is rejected for coverage, preflight, commit, resume, public sources, and legacy files, and is passed to CollectionService.run_dry_run.

In test_collection_service.py, use a three-row authorized fixture and assert two runs with offsets 0 and 2 create non-overlapping staged source_record_id sets.

- [ ] **Step 2: Run and verify failure**

    python -m pytest tests/test_authorized_export_adapter.py tests/test_collect_jobs_cli.py tests/test_collection_service.py -q

Expected: FAIL because record_offset is unsupported.

- [ ] **Step 3: Implement adapter offsets**

Add record_offset: int = 0 to AuthorizedExportAdapter.load_file. Validate it with:

    if (
        isinstance(record_offset, bool)
        or not isinstance(record_offset, int)
        or record_offset < 0
    ):
        raise ValueError("record_offset must be a non-negative integer")

Change row iteration to:

    for row_index, (row_number, row) in enumerate(rows):
        if row_index < record_offset:
            continue
        if len(records) + len(self._errors) >= max_records:
            break

Keep original row numbers and hashes so provenance remains stable across different offsets.

- [ ] **Step 4: Thread the offset through service and CLI**

Add --record-offset with type=int and default=0. Permit non-zero values only when all are true: dry-run mode, a new run, exactly one authorized export source, and an input file plus authorization manifest are present. Reject offsets above MAX_RECORDS.

Add record_offset: int = 0 to CollectionService._run_dry_run_unlocked, validate 0 through MAX_RUN_RECORDS, store it in run_document, and pass it only to AuthorizedExportAdapter.load_file. Pass args.record_offset from execute to CollectionService.run_dry_run.

- [ ] **Step 5: Run offset and collection tests**

    python -m pytest tests/test_authorized_export_adapter.py tests/test_collect_jobs_cli.py tests/test_collection_service.py -q

Expected: PASS; offset batches are non-overlapping and preserve original provenance.

- [ ] **Step 6: Record checkpoint hashes**

    Get-FileHash src/job_collection/adapters/authorized_export.py,src/job_collection/service.py,src/collect_jobs.py,tests/test_authorized_export_adapter.py,tests/test_collection_service.py,tests/test_collect_jobs_cli.py -Algorithm SHA256

Git commit message if available: feat: support bounded authorized export batches

### Task 6: Update the authorized-intake runbook

**Files:**
- Modify: docs/NATIONAL_AUTHORIZED_DATA_INTAKE.md
- Modify: tests/test_collection_docs.py

- [ ] **Step 1: Add the failing documentation test**

    def test_authorized_intake_docs_state_quantity_a_gates():
        root = Path(__file__).resolve().parents[1]
        text = (root / "docs" / "NATIONAL_AUTHORIZED_DATA_INTAKE.md").read_text(
            encoding="utf-8"
        )

        for required in (
            "8,000 至 9,000",
            "可信发布日期",
            "2022 至 2026",
            "valid_candidate_count",
            "trusted_window_candidate_count",
            "合格率低于 55%",
            "单一来源不超过 35%",
        ):
            assert required in text

- [ ] **Step 2: Run and verify failure**

    python -m pytest tests/test_collection_docs.py::test_authorized_intake_docs_state_quantity_a_gates -q

Expected: FAIL.

- [ ] **Step 3: Document the exact gates**

Add:

    - 当前可信基线为 546 条，缺口为 4,454 条，原始接入预算为 8,000 至 9,000 条。
    - 预检通过不等于最终有效；必须查看 valid_candidate_count 和 trusted_window_candidate_count。
    - 代表性批次可信时序合格率低于 55% 时停止该来源。
    - 正式批次每批不超过 1,000 条，每批检查后才提交。
    - 授权清单或导出文件缺失时不得提交。
    - 每个提交批次后执行完整重建和覆盖率检查。
    - 单一来源占比达到 35% 时停止该来源的非缺口岗位。

Use execution-date run IDs with a three-digit sequence, such as boss-production-20260814-001, and never reuse an existing run directory.

- [ ] **Step 4: Run documentation tests**

    python -m pytest tests/test_collection_docs.py -q

Expected: PASS.

- [ ] **Step 5: Record checkpoint hashes**

    Get-FileHash docs/NATIONAL_AUTHORIZED_DATA_INTAKE.md,tests/test_collection_docs.py -Algorithm SHA256

Git commit message if available: docs: add quantity-first intake gates

### Task 7: Verify code before production data

**Files:**
- Read: pytest.ini
- Read: pytest-full.ini
- Read: data/job_competency.db
- Create: data/expansion-reports/quantity-a-baseline-20260814.json

- [ ] **Step 1: Run focused tests**

    python -m pytest tests/test_collection_coverage.py tests/test_collect_jobs_cli.py tests/test_authorized_export_adapter.py tests/test_collection_docs.py -q

Expected: PASS.

- [ ] **Step 2: Run the complete suite**

    python -m pytest -c pytest-full.ini -q

Expected: PASS with coverage at or above 60%.

- [ ] **Step 3: Verify the production database read-only**

    python -c "import sqlite3; c=sqlite3.connect('file:data/job_competency.db?mode=ro',uri=True); print(c.execute('PRAGMA integrity_check').fetchone()[0]); print(c.execute('SELECT COUNT(*) FROM job_posting').fetchone()[0]); c.close()"

Expected before new commits: ok, then 2568.

- [ ] **Step 4: Save and verify the baseline report**

Run the exact command below, then parse the saved JSON and verify the listed values:

    python -m src.collect_jobs --coverage-report --output data/expansion-reports/quantity-a-baseline-20260814.json

    usable_unique.current = 546
    usable_unique.gap_to_minimum = 4454
    date_window.minimum_year = 2022
    date_window.maximum_year = 2026
    date_window.trusted_required = true

- [ ] **Step 5: Hash the baseline artifact**

    Get-FileHash data/expansion-reports/quantity-a-baseline-20260814.json -Algorithm SHA256

### Task 8: Preflight and stage real authorized exports

**Files:**
- Read only: config/authorized_job_sources.local.json
- Read only: data/incoming/authorized
- Create through existing CLI: data/collections

- [ ] **Step 1: Enforce the external-input gate**

    $grantPath = Resolve-Path 'config\authorized_job_sources.local.json' -ErrorAction Stop
    $exports = Get-ChildItem 'data\incoming\authorized' -Recurse -File -ErrorAction Stop | Where-Object { $_.Extension -in '.jsonl','.json','.csv' }
    if ($exports.Count -eq 0) { throw 'No authorized job exports are present; production expansion must stop.' }
    $grantPath.Path
    $exports | Select-Object FullName,Length,LastWriteTime

Expected: one local grant manifest and at least one non-fixture export. Otherwise stop without modifying the database.

- [ ] **Step 2: Preflight each export**

For a BOSS export, run:

    python -m src.collect_jobs --authorization-preflight --source boss_zhipin_authorized --input-file data/incoming/authorized/boss_zhipin/jobs.jsonl --authorization-manifest config/authorized_job_sources.local.json

Use the equivalent registered source and actual existing filename for other exports. Expected: no secrets, matching file SHA-256, and representative trusted-window yield at least 55%.

- [ ] **Step 3: Run a 20-record smoke dry-run**

    python -m src.collect_jobs --dry-run --source boss_zhipin_authorized --input-file data/incoming/authorized/boss_zhipin/jobs.jsonl --authorization-manifest config/authorized_job_sources.local.json --record-offset 0 --max-records 20 --run-id boss-smoke-20260814-001

Expected: complete report; all 20 rows accounted for across staged, review, quarantine, and rejected outcomes.

- [ ] **Step 4: Stage a production batch**

    python -m src.collect_jobs --dry-run --source boss_zhipin_authorized --input-file data/incoming/authorized/boss_zhipin/jobs.jsonl --authorization-manifest config/authorized_job_sources.local.json --record-offset 0 --max-records 1000 --run-id boss-production-20260814-001

Expected: no database write; complete report and verified artifact hashes.

For the second shard of the same file, use --record-offset 1000 and run ID boss-production-20260814-002. Continue with offsets 2000, 3000, and so on; stop when preflight row_count is reached. Never overlap offsets for the same input SHA-256.

- [ ] **Step 5: Inspect before commit**

Verify authorization reference, input SHA-256, family distribution, trusted-date yield, duplicate count, and all staged/review/quarantine artifacts. Do not commit a stopped, altered, unauthorized, or below-55%-yield batch.

### Task 9: Commit, rebuild, and stop at target

**Files:**
- Modify through existing transaction: data/job_competency.db
- Create through existing commands: data/backups, data/imports, data/exports

- [ ] **Step 1: Commit one inspected batch**

    python -m src.collect_jobs --commit --resume-run boss-production-20260814-001 --confirm --authorization-manifest config/authorized_job_sources.local.json

Expected: verified SQLite backup before transaction commit and explicit imported/revised/skipped/duplicate counts.

- [ ] **Step 2: Rebuild hard metrics**

    python -m src.rebuild_hard_metrics --full --confirm --after-collection-run boss-production-20260814-001

Expected: completed status and refreshed duplicates, gates, profiles, evolution evidence, knowledge chunks, graph, and acceptance snapshot.

- [ ] **Step 3: Re-run coverage**

    python -m src.collect_jobs --coverage-report

Expected: qualifying count increases without violating constraints. Choose the next batch from recommended_batches.

- [ ] **Step 4: Repeat one batch at a time**

Stop ingestion only when all are true:

    usable_unique.current >= 5000
    missing_families is empty
    families_below_minimum is empty
    source_domains.current >= 8
    source_types.current >= 3
    maximum_single_domain_share.current <= 0.35
    every city_tiers share >= its minimum_share
    every JAVA_DEVELOPER core_family_windows count > 0
    every AI_AGENT_ENGINEER core_family_windows count > 0

- [ ] **Step 5: Verify database integrity**

    python -c "import sqlite3; c=sqlite3.connect('file:data/job_competency.db?mode=ro',uri=True); assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'; print(c.execute('SELECT COUNT(*) FROM job_posting').fetchone()[0]); c.close()"

Expected: success and a row count above 2568.

### Task 10: Freeze final acceptance artifacts

**Files:**
- Create: data/expansion-reports/quantity-a-final.json
- Create: data/benchmark/job-data-quantity-a-300.jsonl
- Modify: docs/NATIONAL_AUTHORIZED_DATA_INTAKE.md

- [ ] **Step 1: Save final coverage**

Run the exact command below and assert every stop condition in Task 9 against the saved JSON:

    python -m src.collect_jobs --coverage-report --output data/expansion-reports/quantity-a-final.json

- [ ] **Step 2: Freeze a deterministic 300-record audit sample**

Select only approved, valid, unique, trusted 2022-2026 records. Stratify across 22 families, source types, and the three time windows. Store selection inputs and hash so the sample cannot be tuned against later.

- [ ] **Step 3: Run the full suite**

    python -m pytest -c pytest-full.ini -q

Expected: PASS with coverage at least 60%.

- [ ] **Step 4: Hash final artifacts**

    Get-FileHash data/expansion-reports/quantity-a-final.json,data/benchmark/job-data-quantity-a-300.jsonl,data/job_competency.db,data/exports/knowledge-graph.json -Algorithm SHA256

- [ ] **Step 5: Record actual outcomes**

Document final qualifying count, raw rows processed, valid/review/quarantine/duplicate totals, source shares, family minimums, date windows, final backup path, and artifact hashes. Do not claim 5,000 if any stop condition is false.

Git commit message if available: data: complete quantity-first job expansion
