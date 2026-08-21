# Synthetic Resume Test Data Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Build a reproducible generator for 64 clearly labelled synthetic resumes and connect its extraction, matching, recommendation, and learning-path fixtures to the existing automated evaluation flow.

**Architecture:** Add one focused dataset module that owns family definitions, scenario generation, text rendering, benchmark construction, validation, and staged output. Add a thin CLI wrapper for repeatable generation. Generated files remain outside the production database and use the existing skill ontology, resume parser, matching engine, ranking service, and benchmark parser for validation.

**Tech Stack:** Python 3.11, standard library (`argparse`, `json`, `pathlib`, `tempfile`, `shutil`), pytest, existing `src.resume_service`, `src.matching_service`, `src.evaluation_service`, and `src.skill_ontology`.

---

## File Structure

- Create `src/synthetic_resume_dataset.py`: immutable family/scenario definitions, resume rendering, manifest and benchmark builders, validation, staged output.
- Create `src/generate_synthetic_resumes.py`: command-line entry point and JSON summary.
- Create `tests/test_synthetic_resume_dataset.py`: distribution, determinism, parser-grounding, benchmark, overwrite, and CLI tests.
- Create `data/synthetic_resumes/README.md`: generated dataset purpose, files, usage, and competition-data boundary.
- Generate `data/synthetic_resumes/resumes/*.txt`: 64 synthetic resume texts.
- Generate `data/synthetic_resumes/manifest.jsonl`: 64 source-of-truth records.
- Generate `data/synthetic_resumes/benchmark-resume-extraction.jsonl`: 64 extraction cases.
- Generate `data/synthetic_resumes/benchmark-matching.jsonl`: 64 matching cases.
- Generate `data/synthetic_resumes/benchmark-recommendation.jsonl`: 64 recommendation cases.
- Modify `data/benchmark/README.md`: point to the synthetic regression set and restate that it is not the independent competition benchmark.
- Modify `README.md`: document the generation command and dataset location.

### Task 1: Dataset Contract And Distribution

**Files:**
- Create: `tests/test_synthetic_resume_dataset.py`
- Create: `src/synthetic_resume_dataset.py`

- [x] **Step 1: Write failing contract tests**

Add tests that import `build_dataset` and assert the exact matrix:

```python
from collections import Counter

from src.synthetic_resume_dataset import build_dataset


def test_build_dataset_has_expected_distribution():
    records = build_dataset()

    assert len(records) == 64
    assert len({record["resume_id"] for record in records}) == 64
    assert Counter(record["target_family"] for record in records) == {
        "BIG_DATA_DEVELOPER": 8,
        "CYBERSECURITY_ENGINEER": 8,
        "DATA_ENGINEER": 8,
        "DATA_GOVERNANCE_ENGINEER": 8,
        "DIGITAL_TWIN_ENGINEER": 8,
        "EDGE_COMPUTING_ENGINEER": 8,
        "IOT_ENGINEER": 8,
        "ROBOTICS_ENGINEER": 8,
    }
    assert Counter(record["target_level"] for record in records) == {
        "junior": 24,
        "mid": 24,
        "senior": 16,
    }
    assert Counter(record["expected_band"] for record in records) == {
        "high": 24,
        "medium": 24,
        "low": 16,
    }
    assert all(record["synthetic"] is True for record in records)
    assert all(record["data_usage"] == "test_only" for record in records)


def test_build_dataset_is_deterministic():
    assert build_dataset(seed=20260805) == build_dataset(seed=20260805)
```

- [x] **Step 2: Run tests and verify the missing-module failure**

Run: `pytest tests/test_synthetic_resume_dataset.py -q`

Expected: collection fails because `src.synthetic_resume_dataset` does not exist.

- [x] **Step 3: Implement immutable definitions and `build_dataset`**

Define these public constants and types in `src/synthetic_resume_dataset.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from random import Random


DATASET_VERSION = "synthetic-resume-v1"
DEFAULT_SEED = 20260805
REFERENCE_DATE = "2026-06-30"


@dataclass(frozen=True)
class FamilySpec:
    code: str
    name: str
    core_skills: tuple[str, ...]
    preferred_skills: tuple[str, ...]
    distractor_skills: tuple[str, ...]
    scenario: str
    responsibilities: tuple[str, ...]


@dataclass(frozen=True)
class ScenarioSpec:
    level: str
    expected_band: str
    years: int
    layout: str
    date_style: str
    include_negation: bool = False
    overlap_months: int = 0
```

Create exactly eight `FamilySpec` values using canonical skills from `SKILL_CATALOG` and eight `ScenarioSpec` values matching the approved matrix. Implement `build_dataset(seed=DEFAULT_SEED) -> list[dict]` so it assigns stable IDs `SYN-CV-0001` through `SYN-CV-0064`, applies a seeded deterministic variation order, and includes `dataset_version`, `synthetic`, `data_usage`, `target_family`, `target_level`, and `expected_band` on every record.

- [x] **Step 4: Run the contract tests**

Run: `pytest tests/test_synthetic_resume_dataset.py -q`

Expected: the two contract tests pass.

- [x] **Step 5: Record a checkpoint**

The workspace is not a Git repository. Record Task 1 completion by checking its boxes in this plan; do not initialize Git or alter repository metadata.

### Task 2: Resume Rendering And Ground-Truth Manifest

**Files:**
- Modify: `src/synthetic_resume_dataset.py`
- Modify: `tests/test_synthetic_resume_dataset.py`

- [x] **Step 1: Write failing rendering and grounding tests**

Add tests using the existing parser:

```python
from src.resume_service import parse_resume_text
from src.skill_ontology import SKILL_CATALOG


def test_resume_text_and_expected_fields_are_grounded():
    for record in build_dataset():
        text = record["text"]
        expected = record["expected"]
        assert text.strip()
        assert record["resume_id"] in text
        assert all(skill in SKILL_CATALOG for skill in expected["skills"])
        assert all(value in text for value in expected["evidence_substrings"])
        assert len(expected["work_ranges"]) >= 1


def test_all_generated_resumes_match_parser_ground_truth():
    for record in build_dataset():
        parsed = parse_resume_text(record["text"], reference_date="2026-06-30")
        expected = record["expected"]
        assert {item["name"] for item in parsed["skills"]} == set(expected["skills"])
        assert parsed["experience_years"] == expected["experience_years"]
        assert set(expected["education"]).issubset(parsed["education"])
        assert {
            (item["start_date"], item["end_date"])
            for item in parsed["work_experiences"]
        } == {
            (item["start"], item["end"])
            for item in expected["work_ranges"]
        }
```

- [x] **Step 2: Run rendering tests and verify they fail**

Run: `pytest tests/test_synthetic_resume_dataset.py -q`

Expected: failures report missing `text`, `expected`, or rendering helpers.

- [x] **Step 3: Implement controlled text rendering**

Implement private helpers with these signatures:

```python
def _skills_for_case(family: FamilySpec, scenario: ScenarioSpec) -> tuple[list[str], list[str]]: ...
def _work_ranges(scenario: ScenarioSpec) -> list[dict[str, str]]: ...
def _render_resume(record: dict, family: FamilySpec, scenario: ScenarioSpec, rng: Random) -> tuple[str, dict]: ...
```

Use three renderers selected by `scenario.layout`: `sectioned`, `markdown`, and `compact`. Each rendered resume must contain the synthetic ID, a synthetic-data notice, professional summary, skill section, dated work section, dated project section, education, and optional certificate section.

Use only canonical skill spellings in positive evidence. For negative cases, add a clause such as `计划学习Kubernetes，尚未在项目中使用` and exclude that skill from `expected.skills`. Build `expected` from the same positive evidence model, including `skills`, `experience_years`, `education`, `work_ranges`, `project_skills`, `achievements`, `evidence_substrings`, and `parser_mode="rules"`.

- [x] **Step 4: Run rendering tests**

Run: `pytest tests/test_synthetic_resume_dataset.py -q`

Expected: distribution, determinism, grounding, timeline, and parser equality tests pass for all 64 records.

- [x] **Step 5: Record a checkpoint**

Check the completed Task 2 boxes in this plan because Git metadata is unavailable.

### Task 3: Matching, Recommendation, And Learning-Path Fixtures

**Files:**
- Modify: `src/synthetic_resume_dataset.py`
- Modify: `src/evaluation_service.py`
- Modify: `tests/test_synthetic_resume_dataset.py`
- Modify: `tests/test_evaluation_service.py`

- [x] **Step 1: Write failing fixture and evaluation tests**

Add tests for the three benchmark builders:

```python
from src.evaluation_service import parse_benchmark_records, run_benchmark
from src.matching_service import match_resume_to_job


def test_benchmark_builders_create_64_valid_cases_per_task():
    dataset = build_dataset()
    groups = build_benchmark_records(dataset)

    assert set(groups) == {"resume_extraction", "matching", "job_recommendation"}
    assert all(len(records) == 64 for records in groups.values())
    for task, records in groups.items():
        raw = "\n".join(json.dumps(item, ensure_ascii=False) for item in records).encode()
        parsed = parse_benchmark_records(raw, f"{task}.jsonl")
        assert len(parsed) == 64


def test_matching_cases_hit_the_declared_bands_and_learning_paths_are_valid():
    records = build_benchmark_records(build_dataset())["matching"]
    for record in records:
        result = match_resume_to_job(record["input"]["resume"], record["input"]["job_profile"])
        assert result["match_band"] == record["expected"]["band"]
        assert [phase["period"] for phase in result["learning_plan"]["phases"]] == [
            "0-30", "31-60", "61-90"
        ]
        assert result["learning_plan"]["projected_score"] <= 100


def test_generated_benchmarks_execute_without_invalid_cases():
    groups = build_benchmark_records(build_dataset())
    for task, records in groups.items():
        results = run_benchmark(records)["results"]
        result = next(item for item in results if item["metric_name"] == task)
        assert result["sample_count"] == 64
        assert not result["details"]["failed_case_ids"]
```

Add a backward-compatible evaluator test proving that a fixed analysis date can be supplied for “至今” ranges:

```python
def test_resume_evaluation_uses_optional_reference_date():
    record = {
        "case_id": "CV-REFERENCE-DATE",
        "task": "resume_extraction",
        "input": {
            "text": "工作经历\n2025.01-至今\n示例科技 | Python工程师\n负责Python开发。",
            "reference_date": "2026-06-30",
        },
        "expected": {
            "skills": ["Python"],
            "experience_years": 1.5,
            "education": [],
            "work_ranges": [{"start": "2025-01", "end": "2026-06"}],
        },
    }

    result = run_benchmark([record])["results"][0]
    assert result["accuracy"] == 1.0
```

- [x] **Step 2: Run fixture tests and verify they fail**

Run: `pytest tests/test_synthetic_resume_dataset.py -q`

Expected: failures because `build_benchmark_records` is missing and the evaluator currently ignores `input.reference_date`.

- [x] **Step 3: Implement stable structured fixtures**

Implement:

```python
def _resume_profile(record: dict) -> dict: ...
def _job_profile(family: FamilySpec, level: str) -> dict: ...
def _recommendation_candidates(record: dict) -> list[dict]: ...
def build_benchmark_records(dataset: list[dict]) -> dict[str, list[dict]]: ...
```

The extraction record uses `record["text"]`, `record["expected"]`, and `input.reference_date=REFERENCE_DATE`. Update `_evaluate_resume` to call `parse_resume_text(source_text, reference_date=record["input"].get("reference_date"))`; records without the field retain the current-date behavior. The matching record uses a structured resume derived from the parser-standard fields and a target job profile with three core required skills, two preferred skills, level, responsibilities, scenario, confidence, and ready sample status. Tune only the predefined high/medium/low scenario inputs until `match_resume_to_job` produces the declared band; never derive the expected band from the returned score.

For recommendation, include all eight family profiles as candidates, rotate candidate order deterministically, set target relevance to `3`, and assign relevance `1` only to a genuinely adjacent family defined in the static family map. Assert target family ranks first and every relevant family appears in Top 5. Add `metadata={"synthetic": true, "data_usage": "test_only", "dataset_version": DATASET_VERSION}` to every benchmark record.

- [x] **Step 4: Run fixture tests**

Run: `pytest tests/test_synthetic_resume_dataset.py -q`

Expected: all 64 extraction, matching, recommendation, and learning-path cases pass their declared contracts.

- [x] **Step 5: Record a checkpoint**

Check the completed Task 3 boxes in this plan because Git metadata is unavailable.

### Task 4: Dataset Validation And Safe Output

**Files:**
- Modify: `src/synthetic_resume_dataset.py`
- Modify: `tests/test_synthetic_resume_dataset.py`

- [x] **Step 1: Write failing validation and output tests**

Add:

```python
def test_validate_dataset_rejects_missing_grounded_evidence():
    records = build_dataset()
    records[0]["expected"]["evidence_substrings"] = ["正文中不存在的证据"]
    with pytest.raises(ValueError, match="SYN-CV-0001.*证据"):
        validate_dataset(records, build_benchmark_records(records))


def test_write_dataset_replaces_prior_generated_output(tmp_path):
    target = tmp_path / "synthetic_resumes"
    first = write_dataset(target)
    (target / "obsolete.txt").write_text("old", encoding="utf-8")
    second = write_dataset(target)

    assert first["resume_count"] == second["resume_count"] == 64
    assert not (target / "obsolete.txt").exists()
    assert len(list((target / "resumes").glob("*.txt"))) == 64
```

- [x] **Step 2: Run validation tests and verify they fail**

Run: `pytest tests/test_synthetic_resume_dataset.py -q`

Expected: failure because `validate_dataset` and `write_dataset` are missing.

- [x] **Step 3: Implement complete validation**

Implement:

```python
def validate_dataset(dataset: list[dict], benchmarks: dict[str, list[dict]]) -> None: ...
```

Validate exact counts and distributions, unique IDs, canonical and unique skills, grounded evidence, valid date ranges, expected experience after merged months, parseable benchmark records, task names, synthetic markers, relative resume paths, and absence of phone/email patterns. Raise `ValueError` containing the affected resume or case ID and field.

- [x] **Step 4: Implement staged directory replacement**

Implement:

```python
def write_dataset(output_dir: Path, *, seed: int = DEFAULT_SEED) -> dict[str, object]: ...
```

Resolve the output path, create staging and backup directories beside it, write UTF-8 JSONL with `ensure_ascii=False` and one object per line, validate the staged content, rename an existing target to backup, rename staging to target, and restore the backup on failure. Refuse to replace filesystem roots or paths outside the explicitly passed parent. Remove only the verified sibling backup after success.

Return a summary containing `dataset_version`, `seed`, `output_dir`, `resume_count`, per-family counts, per-level counts, per-band counts, and benchmark counts.

- [x] **Step 5: Run output tests**

Run: `pytest tests/test_synthetic_resume_dataset.py -q`

Expected: validation failures are precise, repeated generation is clean, and all dataset tests pass.

- [x] **Step 6: Record a checkpoint**

Check the completed Task 4 boxes in this plan because Git metadata is unavailable.

### Task 5: CLI, Generated Files, And Documentation

**Files:**
- Create: `src/generate_synthetic_resumes.py`
- Modify: `tests/test_synthetic_resume_dataset.py`
- Generate: `data/synthetic_resumes/**`
- Modify: `data/benchmark/README.md`
- Modify: `README.md`

- [x] **Step 1: Write failing CLI test**

Add:

```python
from src.generate_synthetic_resumes import main


def test_cli_generates_dataset_and_prints_summary(tmp_path, capsys):
    target = tmp_path / "dataset"
    assert main(["--output-dir", str(target), "--seed", "20260805"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["resume_count"] == 64
    assert summary["benchmark_counts"] == {
        "resume_extraction": 64,
        "matching": 64,
        "job_recommendation": 64,
    }
```

- [x] **Step 2: Run the CLI test and verify it fails**

Run: `pytest tests/test_synthetic_resume_dataset.py::test_cli_generates_dataset_and_prints_summary -q`

Expected: collection fails because the CLI module is missing.

- [x] **Step 3: Implement the CLI**

Create a parser with `--output-dir` defaulting to `<project>/data/synthetic_resumes` and `--seed` defaulting to `20260805`. Call `write_dataset`, print its summary with `json.dumps(..., ensure_ascii=False, indent=2)`, return `0`, and use `raise SystemExit(main())` in the module entry point.

- [x] **Step 4: Run the CLI test**

Run: `pytest tests/test_synthetic_resume_dataset.py::test_cli_generates_dataset_and_prints_summary -q`

Expected: PASS.

- [x] **Step 5: Generate the approved dataset**

Run: `python -m src.generate_synthetic_resumes --output-dir data/synthetic_resumes --seed 20260805`

Expected summary: 64 resumes; 8 per family; level counts 24/24/16; band counts 24/24/16; three benchmark files with 64 cases each.

- [x] **Step 6: Document usage and data boundaries**

Ensure `data/synthetic_resumes/README.md`, `data/benchmark/README.md`, and root `README.md` state:

```text
python -m src.generate_synthetic_resumes
```

The documentation must identify the output as synthetic regression data, prohibit counting it as real collection or independent accuracy evidence, describe all generated files, and explain that official evaluation still requires real independently annotated cases.

- [x] **Step 7: Record a checkpoint**

Check the completed Task 5 boxes in this plan because Git metadata is unavailable.

### Task 6: Full Verification

**Files:**
- Verify: `data/synthetic_resumes/**`
- Verify: entire project test suite

- [x] **Step 1: Re-run generation to prove reproducibility**

Run the generator twice and compute SHA-256 for `manifest.jsonl` and all three benchmark files after each run.

Expected: both runs produce identical hashes and no obsolete files remain.

- [x] **Step 2: Run focused tests**

Run: `pytest tests/test_synthetic_resume_dataset.py tests/test_resume_profile_v2.py tests/test_matching_engine_v2.py tests/test_evaluation_service.py -q`

Expected: all focused tests pass.

- [x] **Step 3: Run the complete test suite with coverage**

Run: `pytest -q`

Expected: all tests pass and total coverage remains at or above the configured 60% threshold.

- [x] **Step 4: Inspect final data counts and synthetic markers**

Parse all four JSONL files and count records, IDs, families, levels, bands, and `synthetic` markers.

Expected: manifest and each benchmark contain 64 valid records, IDs are unique, distribution matches the design, and every manifest record is marked `synthetic=true` and `data_usage=test_only`.

- [x] **Step 5: Record final completion**

Check all remaining boxes in this plan and report generated paths, case counts, benchmark results, test totals, coverage, and the explicit limitation that synthetic cases are regression evidence only.

