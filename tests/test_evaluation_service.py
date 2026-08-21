import json

import pytest


def _exact_records():
    return [
        {
            "case_id": "JD-001",
            "task": "jd_parsing",
            "input": {
                "text": "要求掌握Python，负责后端接口开发、服务治理、日志分析和性能优化。加分：熟悉Docker。"
            },
            "expected": {
                "required_skills": ["Python"],
                "preferred_skills": ["Docker"],
            },
        },
        {
            "case_id": "CV-001",
            "task": "resume_extraction",
            "input": {"text": "3年Java开发经验，本科学历。项目：使用Java建设订单服务。"},
            "expected": {
                "skills": ["Java"],
                "experience_years": 3,
                "education": ["本科"],
            },
        },
        {
            "case_id": "MATCH-HIGH",
            "task": "matching",
            "input": {
                "resume": {
                    "skills": ["Java", "Kubernetes", "Docker"],
                    "recent_skills": ["Java", "Kubernetes"],
                    "experience_years": 4,
                    "projects": ["云原生项目"],
                },
                "job_profile": {
                    "name": "Java开发工程师",
                    "required_skills": ["Java", "Kubernetes"],
                    "preferred_skills": ["Docker"],
                    "required_years": 3,
                },
            },
            "expected": {"band": "high"},
        },
        {
            "case_id": "MATCH-MEDIUM",
            "task": "matching",
            "input": {
                "resume": {
                    "skills": ["Java", "Docker"],
                    "recent_skills": ["Java", "Docker"],
                    "experience_years": 3,
                    "projects": ["后端项目"],
                },
                "job_profile": {
                    "name": "Java开发工程师",
                    "required_skills": ["Java", "Kubernetes"],
                    "preferred_skills": ["Docker"],
                    "required_years": 3,
                },
            },
            "expected": {"band": "medium"},
        },
        {
            "case_id": "MATCH-LOW",
            "task": "matching",
            "input": {
                "resume": {
                    "skills": [],
                    "recent_skills": [],
                    "experience_years": 0,
                    "projects": [],
                },
                "job_profile": {
                    "name": "Java开发工程师",
                    "required_skills": ["Java", "Kubernetes"],
                    "preferred_skills": ["Docker"],
                    "required_years": 3,
                },
            },
            "expected": {"band": "low"},
        },
    ]


def test_parse_benchmark_records_accepts_jsonl_and_rejects_duplicate_ids():
    from src.evaluation_service import parse_benchmark_records

    records = _exact_records()[:2]
    raw = "\n".join(json.dumps(item, ensure_ascii=False) for item in records).encode()
    assert parse_benchmark_records(raw, "benchmark.jsonl") == records

    duplicated = records + [{**records[0]}]
    raw = json.dumps(duplicated, ensure_ascii=False).encode()
    with pytest.raises(ValueError, match="case_id重复"):
        parse_benchmark_records(raw, "benchmark.json")


def test_run_benchmark_computes_reproducible_metrics_for_all_tasks():
    from src.evaluation_service import run_benchmark

    report = run_benchmark(_exact_records())
    results = {item["metric_name"]: item for item in report["results"]}

    assert results["jd_parsing"]["sample_count"] == 1
    assert results["jd_parsing"]["accuracy"] == 1.0
    assert results["resume_extraction"]["f1"] == 1.0
    assert results["matching"]["sample_count"] == 3
    assert results["matching"]["precision"] == 1.0
    assert results["matching"]["accuracy"] == 1.0
    assert report["readiness"]["all_metrics_present"] is True
    assert report["readiness"]["meets_jd_case_requirement"] is False
    assert report["readiness"]["competition_ready"] is False


def test_run_benchmark_tracks_failed_case_ids():
    from src.evaluation_service import run_benchmark

    records = _exact_records()[:1]
    records.append(
        {
            "case_id": "JD-002",
            "task": "jd_parsing",
            "input": {"text": "要求掌握Python。"},
            "expected": {"required_skills": ["Go"], "preferred_skills": []},
        }
    )

    result = run_benchmark(records)["results"][0]
    assert result["sample_count"] == 2
    assert result["accuracy"] == 0.5
    assert result["details"]["failed_case_ids"] == ["JD-002"]


def test_readiness_requires_100_jd_cases_and_all_three_accuracy_targets():
    from src.evaluation_service import readiness_from_results

    results = [
        {"metric_name": "jd_parsing", "sample_count": 99, "accuracy": 0.95},
        {"metric_name": "resume_extraction", "sample_count": 20, "accuracy": 0.9},
        {"metric_name": "matching", "sample_count": 20, "accuracy": 0.92},
    ]
    readiness = readiness_from_results(results)
    assert readiness["meets_jd_case_requirement"] is False
    assert readiness["all_accuracy_targets_met"] is True
    assert readiness["competition_ready"] is False

    results[0]["sample_count"] = 100
    readiness = readiness_from_results(results)
    assert readiness["meets_jd_case_requirement"] is True
    assert readiness["competition_ready"] is True


def test_recommendation_evaluation_computes_top1_recall_mrr_and_ndcg():
    from src.evaluation_service import run_benchmark

    records = [
        {
            "case_id": "REC-001",
            "task": "job_recommendation",
            "input": {
                "resume": {
                    "skills": ["Java", "Docker"],
                    "recent_skills": ["Java", "Docker"],
                    "experience_years": 3,
                    "projects": ["Java order service"],
                },
                "candidates": [
                    {
                        "id": 11,
                        "family_code": "JAVA_DEVELOPER",
                        "name": "Java开发工程师",
                        "level": "mid",
                        "required_skills": ["Java"],
                        "preferred_skills": ["Docker"],
                        "required_years": 3,
                        "sample_count": 50,
                    },
                    {
                        "id": 12,
                        "family_code": "DATA_ANALYST",
                        "name": "数据分析师",
                        "level": "mid",
                        "required_skills": ["Python", "SQL"],
                        "preferred_skills": ["Tableau"],
                        "required_years": 3,
                        "sample_count": 50,
                    },
                ],
            },
            "expected": {"relevance": {"JAVA_DEVELOPER": 3}},
        }
    ]

    report = run_benchmark(records)
    result = report["results"][0]

    assert result["metric_name"] == "job_recommendation"
    assert result["top1_accuracy"] == 1.0
    assert result["recall_at_5"] == 1.0
    assert result["mrr"] == 1.0
    assert result["ndcg_at_5"] == 1.0
    assert result["details"]["extended_metrics"]["ndcg_at_5"] == 1.0
    assert report["readiness"]["all_metrics_present"] is False


def test_resume_evaluation_reports_timeline_and_evidence_metrics():
    from src.evaluation_service import run_benchmark

    source = "工作经历\n2022.01-2023.12 后端工程师，使用Java开发订单服务。"
    records = [
        {
            "case_id": "CV-V2-001",
            "task": "resume_extraction",
            "input": {"text": source},
            "expected": {
                "skills": ["Java"],
                "work_ranges": [{"start": "2022-01", "end": "2023-12"}],
                "evidence_substrings": ["使用Java开发订单服务"],
            },
        }
    ]

    result = run_benchmark(records)["results"][0]

    assert result["timeline_accuracy"] == 1.0
    assert result["evidence_validity"] == 1.0
    assert result["details"]["extended_metrics"]["timeline_accuracy"] == 1.0


def test_benchmark_parser_accepts_optional_recommendation_task():
    from src.evaluation_service import parse_benchmark_records

    record = {
        "case_id": "REC-002",
        "task": "job_recommendation",
        "input": {"resume": {}, "candidates": []},
        "expected": {"relevance": {}},
    }
    raw = json.dumps(record, ensure_ascii=False).encode("utf-8")

    assert parse_benchmark_records(raw, "recommendation.json") == [record]


def test_resume_evaluation_checks_project_skills_achievements_and_parser_mode():
    from src.evaluation_service import run_benchmark

    record = {
        "case_id": "CV-V2-002",
        "task": "resume_extraction",
        "input": {
            "text": (
                "项目经历\n订单平台 | 2024.01-2024.06\n"
                "技术栈：Java、Redis\nQPS提升至3000"
            )
        },
        "expected": {
            "skills": ["Java", "Redis"],
            "project_skills": ["Java", "Redis"],
            "achievements": ["QPS提升至3000"],
            "parser_mode": "rules",
        },
    }

    result = run_benchmark([record])["results"][0]

    assert result["project_skill_accuracy"] == 1.0
    assert result["achievement_accuracy"] == 1.0
    assert result["parser_mode_accuracy"] == 1.0

    record["expected"]["project_skills"] = ["Kafka"]
    assert run_benchmark([record])["results"][0]["accuracy"] == 0.0


def test_resume_evaluation_uses_optional_reference_date():
    from src.evaluation_service import run_benchmark

    record = {
        "case_id": "CV-REFERENCE-DATE",
        "task": "resume_extraction",
        "input": {
            "text": (
                "工作经历\n"
                "2025.01-至今\n"
                "示例科技 | Python工程师\n"
                "负责Python开发。"
            ),
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
