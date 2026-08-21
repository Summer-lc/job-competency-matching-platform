import json
from collections import Counter

import pytest

from src.evaluation_service import parse_benchmark_records, run_benchmark
from src.generate_synthetic_resumes import main
from src.matching_service import match_resume_to_job
from src.resume_service import parse_resume_text
from src.skill_ontology import SKILL_CATALOG
from src.synthetic_resume_dataset import (
    build_benchmark_records,
    build_dataset,
    validate_dataset,
    write_dataset,
)


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


def test_benchmark_builders_create_64_valid_cases_per_task():
    groups = build_benchmark_records(build_dataset())

    assert set(groups) == {"resume_extraction", "matching", "job_recommendation"}
    assert all(len(records) == 64 for records in groups.values())
    for task, records in groups.items():
        raw = "\n".join(
            json.dumps(item, ensure_ascii=False) for item in records
        ).encode()
        parsed = parse_benchmark_records(raw, f"{task}.jsonl")
        assert len(parsed) == 64
        assert all(item["metadata"]["synthetic"] is True for item in parsed)


def test_matching_cases_hit_declared_bands_and_learning_paths_are_valid():
    records = build_benchmark_records(build_dataset())["matching"]
    for record in records:
        result = match_resume_to_job(
            record["input"]["resume"], record["input"]["job_profile"]
        )
        assert result["match_band"] == record["expected"]["band"]
        assert [
            phase["period"] for phase in result["learning_plan"]["phases"]
        ] == ["0-30", "31-60", "61-90"]
        assert result["learning_plan"]["projected_score"] <= 100


def test_generated_benchmarks_execute_without_invalid_cases():
    groups = build_benchmark_records(build_dataset())
    for task, records in groups.items():
        results = run_benchmark(records)["results"]
        result = next(item for item in results if item["metric_name"] == task)
        assert result["sample_count"] == 64
        assert not result["details"]["failed_case_ids"]


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
