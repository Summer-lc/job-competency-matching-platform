from __future__ import annotations

from pathlib import Path

import pytest

from src.job_collection.coverage import (
    CollectionTargets,
    build_coverage_report,
    load_collection_targets,
    recommend_batches,
)
from src.job_data_service import JOB_FAMILY_NAMES


ROOT = Path(__file__).resolve().parents[1]
TARGETS_PATH = ROOT / "config" / "job_collection_targets.json"


def test_default_collection_targets_match_internal_acceptance_contract():
    targets = load_collection_targets(TARGETS_PATH)

    assert targets.minimum_usable_unique == 5000
    assert targets.maximum_usable_unique == 10000
    assert targets.minimum_source_domains == 8
    assert targets.minimum_source_types == 3
    assert targets.maximum_single_domain_share == 0.35
    assert targets.minimum_usable_per_family == 100
    assert targets.minimum_published_year == 2022
    assert targets.maximum_published_year == 2026
    assert targets.require_trusted_published_at is True
    assert targets.family_minimum_overrides == {
        "JAVA_DEVELOPER": 500,
        "AI_AGENT_ENGINEER": 500,
    }
    assert set(targets.required_job_families) == set(JOB_FAMILY_NAMES)
    assert sum(targets.source_targets.values()) >= 5300
    assert targets.city_tier_minimum_shares == {
        "tier_1": 0.15,
        "new_tier_1": 0.25,
        "tier_2": 0.20,
        "other": 0.15,
    }


def test_collection_targets_reject_invalid_date_window_and_family_overrides():
    targets = load_collection_targets(TARGETS_PATH)
    payload = targets.model_dump(mode="json")

    with pytest.raises(ValueError, match="published year"):
        CollectionTargets.model_validate(
            {**payload, "minimum_published_year": 2027, "maximum_published_year": 2026}
        )

    with pytest.raises(ValueError, match="family minimum override"):
        CollectionTargets.model_validate(
            {**payload, "family_minimum_overrides": {"UNKNOWN_FAMILY": 500}}
        )

    with pytest.raises(ValueError, match="family minimum override"):
        CollectionTargets.model_validate(
            {**payload, "family_minimum_overrides": {"JAVA_DEVELOPER": 0}}
        )


def _row(index: int, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "record_id": f"JD-{index:05d}",
        "content_hash": f"{index:064x}",
        "job_family_id": "JAVA_DEVELOPER",
        "source_id": "boss_zhipin_authorized",
        "source_domain": "www.zhipin.com",
        "source_type": "authorized_platform",
        "region": "Beijing",
        "published_at": "2026-03-15T00:00:00+00:00",
        "published_at_trusted": True,
        "gate_status": "valid",
        "provenance_status": "approved",
        "duplicate_of_id": None,
    }
    row.update(overrides)
    return row


def test_coverage_report_counts_only_valid_unique_approved_rows():
    targets = load_collection_targets(TARGETS_PATH)
    rows = [
        _row(1),
        _row(2, job_family_id="PYTHON_BACKEND", source_id="job51_authorized", source_domain="we.51job.com"),
        _row(3, gate_status="review"),
        _row(4, duplicate_of_id=1),
        _row(5, provenance_status="unverified"),
    ]

    report = build_coverage_report(targets=targets, usable_rows=rows)

    assert report["usable_unique"] == {
        "current": 2,
        "target": [5000, 10000],
        "gap_to_minimum": 4998,
    }
    assert report["family_counts"]["JAVA_DEVELOPER"] == 1
    assert report["family_counts"]["PYTHON_BACKEND"] == 1
    assert "MLOPS_ENGINEER" in report["missing_families"]
    assert report["source_domains"]["current"] == 2
    assert report["source_domains"]["target"] == 8
    assert report["maximum_single_domain_share"]["current"] == 0.5
    assert report["minimum_usable_per_family"]["target"] == 100
    assert report["date_window"] == {
        "minimum_year": 2022,
        "maximum_year": 2026,
        "trusted_required": True,
        "year_counts": {"2022": 0, "2023": 0, "2024": 0, "2025": 0, "2026": 2},
    }
    assert report["excluded_rows"] == 3


def test_coverage_report_excludes_untrusted_or_out_of_window_dates():
    targets = load_collection_targets(TARGETS_PATH)
    rows = [
        _row(1, published_at="2022-01-01"),
        _row(2, published_at="2024-06-30", job_family_id="AI_AGENT_ENGINEER"),
        _row(3, published_at="2026-12-31", job_family_id="AI_AGENT_ENGINEER"),
        _row(4, published_at="2021-12-31"),
        _row(5, published_at="2027-01-01"),
        _row(6, published_at_trusted=False),
        _row(7, published_at=None),
    ]

    report = build_coverage_report(targets=targets, usable_rows=rows)

    assert report["usable_unique"]["current"] == 3
    assert report["date_window"]["year_counts"] == {
        "2022": 1,
        "2023": 0,
        "2024": 1,
        "2025": 0,
        "2026": 1,
    }
    assert report["family_targets"]["JAVA_DEVELOPER"] == 500
    assert report["family_targets"]["AI_AGENT_ENGINEER"] == 500
    assert report["family_targets"]["PYTHON_BACKEND"] == 100
    assert report["core_family_windows"] == {
        "JAVA_DEVELOPER": {"2022-2023": 1, "2024-2025": 0, "2026": 0},
        "AI_AGENT_ENGINEER": {"2022-2023": 0, "2024-2025": 1, "2026": 1},
    }
    assert report["excluded_rows"] == 4


def test_batch_recommendations_prioritize_lowest_families_and_respect_source_caps():
    targets = load_collection_targets(TARGETS_PATH)
    rows = [
        _row(
            index,
            source_id="boss_zhipin_authorized" if index < 350 else "job51_authorized",
            source_domain="www.zhipin.com" if index < 350 else "we.51job.com",
            job_family_id="JAVA_DEVELOPER" if index < 500 else "PYTHON_BACKEND",
        )
        for index in range(1, 601)
    ]
    coverage = build_coverage_report(targets=targets, usable_rows=rows)

    recommendations = recommend_batches(
        targets=targets,
        coverage=coverage,
        batch_size=100,
    )

    assert recommendations
    assert recommendations[0]["job_family_id"] not in {
        "JAVA_DEVELOPER",
        "PYTHON_BACKEND",
    }
    assert all(item["requested_records"] <= 100 for item in recommendations)
    assert all(
        item["projected_source_count"] <= targets.source_targets[item["source_id"]]
        for item in recommendations
    )
    assert all(
        item["projected_source_share"] <= targets.maximum_single_domain_share
        for item in recommendations
    )


def test_batch_recommendations_apply_family_specific_minimums():
    targets = load_collection_targets(TARGETS_PATH)
    coverage = build_coverage_report(targets=targets, usable_rows=[])

    recommendations = recommend_batches(
        targets=targets,
        coverage=coverage,
        batch_size=600,
    )

    ai_agent_batch = next(
        item for item in recommendations if item["job_family_id"] == "AI_AGENT_ENGINEER"
    )
    assert ai_agent_batch["requested_records"] == 500
