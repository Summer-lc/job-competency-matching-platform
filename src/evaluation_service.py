from __future__ import annotations

import json
import math
from collections import defaultdict
from typing import Iterable

from src.job_data_service import extract_skills
from src.job_recommendation_service import rank_job_payloads
from src.matching_service import match_resume_to_job
from src.resume_service import parse_resume_text


CORE_TASKS = ("jd_parsing", "resume_extraction", "matching")
TASKS = (*CORE_TASKS, "job_recommendation")
ACCURACY_TARGET = 0.9
MINIMUM_JD_CASES = 100


def parse_benchmark_records(raw: bytes, filename: str) -> list[dict]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("评测文件必须使用UTF-8编码") from exc

    lower_name = filename.lower()
    if lower_name.endswith(".jsonl"):
        records = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSONL第{line_number}行格式错误: {exc.msg}") from exc
            records.append(item)
    elif lower_name.endswith(".json"):
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON格式错误: {exc.msg}") from exc
        records = value if isinstance(value, list) else [value]
    else:
        raise ValueError("评测文件仅支持.jsonl和.json格式")

    if not records:
        raise ValueError("评测文件不包含有效记录")

    seen_ids: set[str] = set()
    for index, item in enumerate(records, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第{index}条评测记录必须是JSON对象")
        case_id = str(item.get("case_id", "")).strip()
        task = item.get("task")
        if not case_id:
            raise ValueError(f"第{index}条评测记录缺少case_id")
        if case_id in seen_ids:
            raise ValueError(f"case_id重复: {case_id}")
        if task not in TASKS:
            raise ValueError(f"第{index}条评测记录task必须是{', '.join(TASKS)}之一")
        if not isinstance(item.get("input"), dict) or not isinstance(item.get("expected"), dict):
            raise ValueError(f"第{index}条评测记录的input和expected必须是JSON对象")
        seen_ids.add(case_id)
    return records


def _normalized_skills(values: Iterable[object]) -> set[str]:
    return {str(value).strip().lower() for value in values or [] if str(value).strip()}


def _set_metrics(true_positive: int, false_positive: int, false_negative: int) -> tuple[float, float, float]:
    if true_positive == false_positive == false_negative == 0:
        return 1.0, 1.0, 1.0
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return round(precision, 4), round(recall, 4), round(f1, 4)


def _evaluate_jd(records: list[dict]) -> dict:
    tp = fp = fn = passed = 0
    failed = []
    for record in records:
        text = str(record["input"].get("text", ""))
        predicted_items = extract_skills(text)
        predicted = {
            f"{item['requirement_type']}:{item['name'].strip().lower()}"
            for item in predicted_items
        }
        expected = {
            f"required:{name}" for name in _normalized_skills(record["expected"].get("required_skills", []))
        } | {
            f"preferred:{name}" for name in _normalized_skills(record["expected"].get("preferred_skills", []))
        }
        tp += len(predicted & expected)
        fp += len(predicted - expected)
        fn += len(expected - predicted)
        if predicted == expected:
            passed += 1
        else:
            failed.append(
                {
                    "case_id": record["case_id"],
                    "expected": sorted(expected),
                    "predicted": sorted(predicted),
                }
            )
    precision, recall, f1 = _set_metrics(tp, fp, fn)
    return _result("jd_parsing", records, passed, precision, recall, f1, failed)


def _evaluate_resume(records: list[dict]) -> dict:
    tp = fp = fn = passed = 0
    failed = []
    timeline_cases = timeline_passed = 0
    evidence_cases = evidence_passed = 0
    project_skill_cases = project_skill_passed = 0
    achievement_cases = achievement_passed = 0
    parser_mode_cases = parser_mode_passed = 0
    for record in records:
        source_text = str(record["input"].get("text", ""))
        predicted_data = parse_resume_text(
            source_text,
            reference_date=record["input"].get("reference_date"),
        )
        predicted_skills = _normalized_skills(item["name"] for item in predicted_data["skills"])
        expected_skills = _normalized_skills(record["expected"].get("skills", []))
        tp += len(predicted_skills & expected_skills)
        fp += len(predicted_skills - expected_skills)
        fn += len(expected_skills - predicted_skills)

        expected_years = record["expected"].get("experience_years")
        years_match = expected_years is None or abs(
            predicted_data["experience_years"] - float(expected_years)
        ) <= 0.25
        expected_education = set(record["expected"].get("education", []))
        education_match = expected_education.issubset(set(predicted_data["education"]))

        timeline_match = True
        if "work_ranges" in record["expected"]:
            timeline_cases += 1
            expected_ranges = {
                (str(item.get("start") or ""), str(item.get("end") or ""))
                for item in record["expected"].get("work_ranges", [])
            }
            predicted_ranges = {
                (str(item.get("start_date") or ""), str(item.get("end_date") or ""))
                for item in predicted_data.get("work_experiences", [])
            }
            timeline_match = predicted_ranges == expected_ranges
            timeline_passed += int(timeline_match)

        evidence_match = True
        if "evidence_substrings" in record["expected"]:
            evidence_cases += 1
            evidence_texts = [
                str(evidence.get("text") or "")
                for skill in predicted_data.get("skills", [])
                for evidence in skill.get("evidence", [])
            ]
            expected_evidence = [
                str(value)
                for value in record["expected"].get("evidence_substrings", [])
                if str(value)
            ]
            expected_grounded = all(
                any(value in evidence for evidence in evidence_texts)
                for value in expected_evidence
            )
            predicted_grounded = all(
                evidence and evidence in source_text for evidence in evidence_texts
            )
            evidence_match = expected_grounded and predicted_grounded
            evidence_passed += int(evidence_match)

        project_skill_match = True
        if "project_skills" in record["expected"]:
            project_skill_cases += 1
            expected_project_skills = _normalized_skills(
                record["expected"].get("project_skills", [])
            )
            predicted_project_skills = _normalized_skills(
                skill
                for project in predicted_data.get("project_experiences", [])
                for skill in project.get("skills", [])
            )
            project_skill_match = predicted_project_skills == expected_project_skills
            project_skill_passed += int(project_skill_match)

        achievement_match = True
        if "achievements" in record["expected"]:
            achievement_cases += 1
            expected_achievements = {
                str(value).strip()
                for value in record["expected"].get("achievements", [])
                if str(value).strip()
            }
            predicted_achievements = {
                str(value).strip()
                for project in predicted_data.get("project_experiences", [])
                for value in project.get("achievements", [])
                if str(value).strip()
            }
            predicted_achievements.update(
                str(item.get("text") or "").strip()
                for item in predicted_data.get("achievements", [])
                if isinstance(item, dict) and str(item.get("text") or "").strip()
            )
            achievement_match = predicted_achievements == expected_achievements
            achievement_passed += int(achievement_match)

        parser_mode_match = True
        if "parser_mode" in record["expected"]:
            parser_mode_cases += 1
            parser_mode_match = predicted_data.get("parser_mode") == record["expected"].get(
                "parser_mode"
            )
            parser_mode_passed += int(parser_mode_match)

        exact = (
            predicted_skills == expected_skills
            and years_match
            and education_match
            and timeline_match
            and evidence_match
            and project_skill_match
            and achievement_match
            and parser_mode_match
        )
        if exact:
            passed += 1
        else:
            failed.append(
                {
                    "case_id": record["case_id"],
                    "expected_skills": sorted(expected_skills),
                    "predicted_skills": sorted(predicted_skills),
                    "predicted_experience_years": predicted_data["experience_years"],
                    "predicted_education": predicted_data["education"],
                }
            )
    precision, recall, f1 = _set_metrics(tp, fp, fn)
    result = _result("resume_extraction", records, passed, precision, recall, f1, failed)
    result["timeline_accuracy"] = (
        round(timeline_passed / timeline_cases, 4) if timeline_cases else None
    )
    result["evidence_validity"] = (
        round(evidence_passed / evidence_cases, 4) if evidence_cases else None
    )
    result["project_skill_accuracy"] = (
        round(project_skill_passed / project_skill_cases, 4)
        if project_skill_cases
        else None
    )
    result["achievement_accuracy"] = (
        round(achievement_passed / achievement_cases, 4)
        if achievement_cases
        else None
    )
    result["parser_mode_accuracy"] = (
        round(parser_mode_passed / parser_mode_cases, 4)
        if parser_mode_cases
        else None
    )
    result["details"].update(
        {
            "timeline_case_count": timeline_cases,
            "evidence_case_count": evidence_cases,
            "extended_metrics": {
                "timeline_accuracy": result["timeline_accuracy"],
                "evidence_validity": result["evidence_validity"],
                "project_skill_accuracy": result["project_skill_accuracy"],
                "achievement_accuracy": result["achievement_accuracy"],
                "parser_mode_accuracy": result["parser_mode_accuracy"],
            },
        }
    )
    return result


def _score_band(score: float) -> str:
    if score >= 80:
        return "high"
    if score >= 60:
        return "medium"
    return "low"


def _evaluate_matching(records: list[dict]) -> dict:
    predicted_labels = []
    expected_labels = []
    failed = []
    for record in records:
        score = match_resume_to_job(
            record["input"].get("resume", {}), record["input"].get("job_profile", {})
        )["total_score"]
        predicted = _score_band(score)
        expected = str(record["expected"].get("band", "")).lower()
        if expected not in {"high", "medium", "low"}:
            raise ValueError(f"{record['case_id']}的匹配期望band必须是high、medium或low")
        predicted_labels.append(predicted)
        expected_labels.append(expected)
        if predicted != expected:
            failed.append(
                {
                    "case_id": record["case_id"],
                    "expected": expected,
                    "predicted": predicted,
                    "score": score,
                }
            )

    labels = sorted(set(predicted_labels) | set(expected_labels))
    precision_values = []
    recall_values = []
    f1_values = []
    for label in labels:
        tp = sum(p == label and e == label for p, e in zip(predicted_labels, expected_labels))
        fp = sum(p == label and e != label for p, e in zip(predicted_labels, expected_labels))
        fn = sum(p != label and e == label for p, e in zip(predicted_labels, expected_labels))
        precision, recall, f1 = _set_metrics(tp, fp, fn)
        precision_values.append(precision)
        recall_values.append(recall)
        f1_values.append(f1)
    passed = len(records) - len(failed)
    return _result(
        "matching",
        records,
        passed,
        sum(precision_values) / len(precision_values),
        sum(recall_values) / len(recall_values),
        sum(f1_values) / len(f1_values),
        failed,
    )


def _dcg(relevance: list[float]) -> float:
    return sum(
        (2**value - 1) / math.log2(rank + 1)
        for rank, value in enumerate(relevance, start=1)
    )


def _evaluate_recommendation(records: list[dict]) -> dict:
    top1_total = recall_total = reciprocal_rank_total = ndcg_total = 0.0
    passed = 0
    failed = []
    for record in records:
        relevance = {
            str(family): float(value)
            for family, value in record["expected"].get("relevance", {}).items()
            if float(value) > 0
        }
        ranked = rank_job_payloads(
            record["input"].get("resume", {}),
            record["input"].get("candidates", []),
            limit=5,
        )
        ranked_families = [item["family_code"] for item in ranked]
        relevant_families = set(relevance)
        top1_hit = bool(ranked_families and ranked_families[0] in relevant_families)
        retrieved = relevant_families.intersection(ranked_families)
        recall_at_5 = (
            len(retrieved) / len(relevant_families) if relevant_families else 0.0
        )
        first_relevant_rank = next(
            (
                rank
                for rank, family in enumerate(ranked_families, start=1)
                if family in relevant_families
            ),
            None,
        )
        reciprocal_rank = 1 / first_relevant_rank if first_relevant_rank else 0.0
        actual_relevance = [relevance.get(family, 0.0) for family in ranked_families[:5]]
        ideal_relevance = sorted(relevance.values(), reverse=True)[:5]
        ideal_dcg = _dcg(ideal_relevance)
        ndcg_at_5 = _dcg(actual_relevance) / ideal_dcg if ideal_dcg else 0.0

        top1_total += float(top1_hit)
        recall_total += recall_at_5
        reciprocal_rank_total += reciprocal_rank
        ndcg_total += ndcg_at_5
        case_passed = top1_hit and recall_at_5 == 1.0
        passed += int(case_passed)
        if not case_passed:
            failed.append(
                {
                    "case_id": record["case_id"],
                    "relevant_families": sorted(relevant_families),
                    "ranked_families": ranked_families,
                }
            )

    count = len(records)
    top1_accuracy = top1_total / count
    recall_at_5 = recall_total / count
    mrr = reciprocal_rank_total / count
    ndcg_at_5 = ndcg_total / count
    result = _result(
        "job_recommendation",
        records,
        passed,
        top1_accuracy,
        recall_at_5,
        ndcg_at_5,
        failed,
    )
    result.update(
        {
            "top1_accuracy": round(top1_accuracy, 4),
            "recall_at_5": round(recall_at_5, 4),
            "mrr": round(mrr, 4),
            "ndcg_at_5": round(ndcg_at_5, 4),
        }
    )
    result["details"]["extended_metrics"] = {
        key: result[key]
        for key in ("top1_accuracy", "recall_at_5", "mrr", "ndcg_at_5")
    }
    return result


def _result(
    metric_name: str,
    records: list[dict],
    passed: int,
    precision: float,
    recall: float,
    f1: float,
    failed: list[dict],
) -> dict:
    return {
        "metric_name": metric_name,
        "sample_count": len(records),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "accuracy": round(passed / len(records), 4),
        "details": {
            "passed_count": passed,
            "failed_count": len(failed),
            "failed_case_ids": [item["case_id"] for item in failed],
            "failures": failed,
        },
    }


def readiness_from_results(results: list[dict]) -> dict:
    by_metric = {item["metric_name"]: item for item in results}
    all_metrics_present = all(task in by_metric for task in CORE_TASKS)
    all_accuracy_targets_met = all_metrics_present and all(
        float(by_metric[task].get("accuracy") or 0) >= ACCURACY_TARGET
        for task in CORE_TASKS
    )
    jd_case_count = int(by_metric.get("jd_parsing", {}).get("sample_count") or 0)
    meets_jd_case_requirement = jd_case_count >= MINIMUM_JD_CASES
    return {
        "jd_case_count": jd_case_count,
        "required_jd_cases": MINIMUM_JD_CASES,
        "meets_jd_case_requirement": meets_jd_case_requirement,
        "all_metrics_present": all_metrics_present,
        "all_accuracy_targets_met": all_accuracy_targets_met,
        "competition_ready": meets_jd_case_requirement and all_accuracy_targets_met,
    }


def run_benchmark(records: Iterable[dict]) -> dict:
    grouped: dict[str, list[dict]] = defaultdict(list)
    records = list(records)
    if not records:
        raise ValueError("评测记录不能为空")
    for record in records:
        task = record.get("task")
        if task not in TASKS:
            raise ValueError(f"不支持的评测任务: {task}")
        grouped[task].append(record)

    evaluators = {
        "jd_parsing": _evaluate_jd,
        "resume_extraction": _evaluate_resume,
        "matching": _evaluate_matching,
        "job_recommendation": _evaluate_recommendation,
    }
    results = [evaluators[task](grouped[task]) for task in TASKS if grouped[task]]
    return {
        "case_count": len(records),
        "results": results,
        "readiness": readiness_from_results(results),
    }
