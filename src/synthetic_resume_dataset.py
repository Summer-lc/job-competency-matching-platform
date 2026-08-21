from __future__ import annotations

from copy import deepcopy
from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
from random import Random
import re
import shutil
import tempfile


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


FAMILY_SPECS = (
    FamilySpec(
        "BIG_DATA_DEVELOPER",
        "大数据开发工程师",
        ("Java", "Spark", "Flink"),
        ("Kafka", "Hadoop"),
        ("ROS", "数字孪生"),
        "实时数据处理",
        ("离线与实时任务开发", "数据链路性能优化"),
    ),
    FamilySpec(
        "CYBERSECURITY_ENGINEER",
        "网络安全工程师",
        ("网络安全", "Linux", "Python"),
        ("Docker", "Kubernetes"),
        ("Spark", "数字孪生"),
        "企业安全运营",
        ("安全事件分析", "系统风险加固"),
    ),
    FamilySpec(
        "DATA_ENGINEER",
        "数据工程师",
        ("Python", "ETL", "数据仓库"),
        ("MySQL", "数据质量"),
        ("ROS", "网络安全"),
        "企业数据平台",
        ("数据管道开发", "数仓模型建设"),
    ),
    FamilySpec(
        "DATA_GOVERNANCE_ENGINEER",
        "数据治理工程师",
        ("数据质量", "元数据管理", "数据仓库"),
        ("Python", "ETL"),
        ("ROS", "React"),
        "数据治理平台",
        ("数据标准建设", "质量规则治理"),
    ),
    FamilySpec(
        "DIGITAL_TWIN_ENGINEER",
        "数字孪生工程师",
        ("数字孪生", "物联网", "Python"),
        ("MQTT", "多模态"),
        ("Hadoop", "Spring Cloud"),
        "工业数字孪生",
        ("设备模型构建", "仿真数据联动"),
    ),
    FamilySpec(
        "EDGE_COMPUTING_ENGINEER",
        "边缘计算工程师",
        ("Linux", "嵌入式", "边缘计算"),
        ("Docker", "MQTT"),
        ("Hadoop", "RAG"),
        "边缘设备平台",
        ("边缘服务开发", "设备侧性能优化"),
    ),
    FamilySpec(
        "IOT_ENGINEER",
        "物联网工程师",
        ("物联网", "MQTT", "嵌入式"),
        ("Linux", "边缘计算"),
        ("Spark", "LangChain"),
        "工业物联网",
        ("设备接入开发", "采集链路维护"),
    ),
    FamilySpec(
        "ROBOTICS_ENGINEER",
        "机器人与智能系统工程师",
        ("ROS", "Python", "Linux"),
        ("嵌入式", "PyTorch"),
        ("Hadoop", "Spring Boot"),
        "智能机器人",
        ("机器人控制开发", "感知模块集成"),
    ),
)

ADJACENT_FAMILIES = {
    "BIG_DATA_DEVELOPER": "DATA_ENGINEER",
    "CYBERSECURITY_ENGINEER": "EDGE_COMPUTING_ENGINEER",
    "DATA_ENGINEER": "DATA_GOVERNANCE_ENGINEER",
    "DATA_GOVERNANCE_ENGINEER": "DATA_ENGINEER",
    "DIGITAL_TWIN_ENGINEER": "IOT_ENGINEER",
    "EDGE_COMPUTING_ENGINEER": "CYBERSECURITY_ENGINEER",
    "IOT_ENGINEER": "DIGITAL_TWIN_ENGINEER",
    "ROBOTICS_ENGINEER": "DIGITAL_TWIN_ENGINEER",
}


SCENARIO_SPECS = (
    ScenarioSpec("junior", "high", 2, "sectioned", "dot"),
    ScenarioSpec("mid", "high", 4, "markdown", "dash"),
    ScenarioSpec("senior", "high", 7, "compact", "chinese", overlap_months=6),
    ScenarioSpec("junior", "medium", 2, "markdown", "present", include_negation=True),
    ScenarioSpec("mid", "medium", 4, "compact", "dot"),
    ScenarioSpec("senior", "medium", 7, "sectioned", "dash", overlap_months=6),
    ScenarioSpec("junior", "low", 1, "compact", "chinese", include_negation=True),
    ScenarioSpec("mid", "low", 4, "sectioned", "present", include_negation=True),
)


def _skills_for_case(
    family: FamilySpec, scenario: ScenarioSpec
) -> tuple[list[str], list[str]]:
    if scenario.expected_band == "high":
        positive = [*family.core_skills, *family.preferred_skills]
    elif scenario.expected_band == "medium":
        positive = [*family.core_skills[:2], family.preferred_skills[0]]
    else:
        positive = [family.core_skills[0], family.preferred_skills[0]]
    missing = [skill for skill in family.core_skills if skill not in positive]
    return positive, missing


def _month_index(value: str) -> int:
    year, month = map(int, value.split("-"))
    return year * 12 + month - 1


def _month_value(index: int) -> str:
    year, month = divmod(index, 12)
    return f"{year:04d}-{month + 1:02d}"


def _work_ranges(scenario: ScenarioSpec) -> list[dict[str, str]]:
    end = _month_index("2026-06")
    start = end - scenario.years * 12 + 1
    if not scenario.overlap_months:
        return [{"start": _month_value(start), "end": _month_value(end)}]

    second_start = _month_index("2023-07")
    first_end = second_start + scenario.overlap_months - 1
    return [
        {"start": _month_value(start), "end": _month_value(first_end)},
        {"start": _month_value(second_start), "end": _month_value(end)},
    ]


def _format_month(value: str, style: str) -> str:
    year, month = value.split("-")
    if style == "dot":
        return f"{year}.{month}"
    if style == "chinese":
        return f"{year}年{int(month)}月"
    return value


def _format_range(value: dict[str, str], style: str, *, final: bool) -> str:
    start = _format_month(value["start"], style)
    if style == "present" and final and value["end"] == "2026-06":
        return f"{start}-至今"
    end = _format_month(value["end"], style)
    separator = " 至 " if style == "dash" else "-"
    return f"{start}{separator}{end}"


def _render_sections(
    layout: str,
    summary: str,
    skill_line: str,
    work_lines: list[str],
    project_lines: list[str],
    education_line: str,
) -> str:
    if layout == "markdown":
        sections = [
            ("## 职业概述", [summary]),
            ("## 专业技能", [skill_line]),
            ("## 工作经历", work_lines),
            ("## 项目经历", project_lines),
            ("## 教育经历", [education_line]),
        ]
    elif layout == "compact":
        sections = [
            (f"职业概述：{summary}", []),
            (f"专业技能：{skill_line}", []),
            ("工作经历：", work_lines),
            ("项目经历：", project_lines),
            (f"教育经历：{education_line}", []),
        ]
    else:
        sections = [
            ("职业概述", [summary]),
            ("专业技能", [skill_line]),
            ("工作经历", work_lines),
            ("项目经历", project_lines),
            ("教育经历", [education_line]),
        ]
    return "\n\n".join("\n".join([heading, *lines]) for heading, lines in sections)


def _render_resume(
    record: dict,
    family: FamilySpec,
    scenario: ScenarioSpec,
    rng: Random,
) -> tuple[str, dict]:
    positive_skills, missing_skills = _skills_for_case(family, scenario)
    ranges = _work_ranges(scenario)
    company_labels = ("合成甲方科技", "合成乙方科技", "合成丙方科技")
    project_labels = ("合成能力验证平台", "合成业务处理平台", "合成技术实验平台")
    work_lines: list[str] = []
    for index, work_range in enumerate(ranges):
        work_lines.extend(
            [
                _format_range(
                    work_range,
                    scenario.date_style,
                    final=index == len(ranges) - 1,
                ),
                f"{company_labels[(record['variation'] + index) % len(company_labels)]} | 技术工程师",
                f"负责使用{'、'.join(positive_skills)}完成系统模块建设与维护。",
            ]
        )

    project_skill_line = f"使用{'、'.join(positive_skills)}完成核心模块开发与验证。"
    achievement = "通过流程优化将处理时延降低30%。"
    project_lines = [
        f"项目名称：{project_labels[record['variation'] % len(project_labels)]}",
        "2025.01-2026.06",
        "项目角色：核心开发",
        project_skill_line,
        achievement,
    ]
    if scenario.include_negation and missing_skills:
        project_lines.append(f"计划学习{missing_skills[0]}，尚未在项目中使用该技术。")

    degree = "硕士" if scenario.level == "senior" else "本科"
    summary = (
        f"{record['resume_id']}，本简历为合成测试数据，不对应真实人员。"
        f"具备{scenario.years}年技术工作经验。"
    )
    skill_line = f"熟练使用{'、'.join(positive_skills)}。"
    education_line = f"2014.09-2018.06 合成示例大学 | {degree}"
    text = _render_sections(
        scenario.layout,
        summary,
        skill_line,
        work_lines,
        project_lines,
        education_line,
    )
    expected = {
        "skills": sorted(positive_skills, key=str.casefold),
        "experience_years": float(scenario.years),
        "education": [degree],
        "work_ranges": ranges,
        "project_skills": sorted(positive_skills, key=str.casefold),
        "achievements": [achievement],
        "evidence_substrings": [project_skill_line],
        "parser_mode": "rules",
    }
    return text, expected


def build_dataset(seed: int = DEFAULT_SEED) -> list[dict]:
    rng = Random(seed)
    records = []
    sequence = 0
    for family in FAMILY_SPECS:
        for scenario_index, scenario in enumerate(SCENARIO_SPECS, start=1):
            sequence += 1
            record = {
                "resume_id": f"SYN-CV-{sequence:04d}",
                "dataset_version": DATASET_VERSION,
                "synthetic": True,
                "data_usage": "test_only",
                "target_family": family.code,
                "target_level": scenario.level,
                "expected_band": scenario.expected_band,
                "scenario_index": scenario_index,
                "variation": rng.randrange(1_000_000),
            }
            text, expected = _render_resume(record, family, scenario, rng)
            record.update(
                text=text,
                expected=expected,
                resume_path=f"resumes/{record['resume_id']}.txt",
            )
            records.append(record)
    return records


def _family_by_code(code: str) -> FamilySpec:
    return next(family for family in FAMILY_SPECS if family.code == code)


def _resume_profile(record: dict) -> dict:
    from src.resume_service import parse_resume_text

    return parse_resume_text(record["text"], reference_date=REFERENCE_DATE)


def _job_profile(family: FamilySpec, level: str) -> dict:
    required_years = {"junior": 1, "mid": 3, "senior": 5}[level]
    return {
        "family_code": family.code,
        "name": family.name,
        "level": level,
        "required_years": required_years,
        "required_skills": list(family.core_skills),
        "preferred_skills": list(family.preferred_skills),
        "skills": [
            {
                "name": skill,
                "requirement_type": "required",
                "proficiency_level": "working",
                "prevalence": 0.9,
                "evidence_count": 20,
            }
            for skill in family.core_skills
        ]
        + [
            {
                "name": skill,
                "requirement_type": "preferred",
                "proficiency_level": "working",
                "prevalence": 0.5,
                "evidence_count": 10,
            }
            for skill in family.preferred_skills
        ],
        "responsibilities": list(family.responsibilities),
        "industry_scenarios": [family.scenario],
        "confidence": 0.95,
        "sample_count": 120,
        "sample_status": "ready",
        "profile_kind": "synthetic_test",
    }


def _recommendation_candidates(record: dict) -> list[dict]:
    candidates = []
    for index, family in enumerate(FAMILY_SPECS, start=1):
        candidate = _job_profile(family, record["target_level"])
        candidate["id"] = 10_000 + index
        candidates.append(candidate)
    offset = record["scenario_index"] % len(candidates)
    return candidates[offset:] + candidates[:offset]


def build_benchmark_records(dataset: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {
        "resume_extraction": [],
        "matching": [],
        "job_recommendation": [],
    }
    for record in dataset:
        numeric_id = record["resume_id"].removeprefix("SYN-CV-")
        metadata = {
            "synthetic": True,
            "data_usage": "test_only",
            "dataset_version": DATASET_VERSION,
            "resume_id": record["resume_id"],
        }
        resume = _resume_profile(record)
        family = _family_by_code(record["target_family"])
        job_profile = _job_profile(family, record["target_level"])
        groups["resume_extraction"].append(
            {
                "case_id": f"CV-SYN-{numeric_id}",
                "task": "resume_extraction",
                "metadata": metadata,
                "input": {
                    "text": record["text"],
                    "reference_date": REFERENCE_DATE,
                },
                "expected": deepcopy(record["expected"]),
            }
        )
        groups["matching"].append(
            {
                "case_id": f"MATCH-SYN-{numeric_id}",
                "task": "matching",
                "metadata": metadata,
                "input": {"resume": resume, "job_profile": job_profile},
                "expected": {"band": record["expected_band"]},
            }
        )
        groups["job_recommendation"].append(
            {
                "case_id": f"REC-SYN-{numeric_id}",
                "task": "job_recommendation",
                "metadata": metadata,
                "input": {
                    "resume": resume,
                    "candidates": _recommendation_candidates(record),
                },
                "expected": {
                    "relevance": {
                        record["target_family"]: 3,
                        ADJACENT_FAMILIES[record["target_family"]]: 1,
                    }
                },
            }
        )
    return groups


def _validate_distribution(dataset: list[dict]) -> None:
    expected_families = {family.code: 8 for family in FAMILY_SPECS}
    expected_levels = {"junior": 24, "mid": 24, "senior": 16}
    expected_bands = {"high": 24, "medium": 24, "low": 16}
    if len(dataset) != 64:
        raise ValueError(f"合成简历数量必须为64，实际为{len(dataset)}")
    if Counter(item.get("target_family") for item in dataset) != expected_families:
        raise ValueError("岗位族分布不符合每类8份的要求")
    if Counter(item.get("target_level") for item in dataset) != expected_levels:
        raise ValueError("人才层级分布不符合24/24/16的要求")
    if Counter(item.get("expected_band") for item in dataset) != expected_bands:
        raise ValueError("匹配档位分布不符合24/24/16的要求")


def _merged_month_count(ranges: list[dict]) -> int:
    months: set[int] = set()
    for item in ranges:
        start = _month_index(str(item.get("start") or ""))
        end = _month_index(str(item.get("end") or ""))
        if end < start:
            raise ValueError("结束时间早于开始时间")
        months.update(range(start, end + 1))
    return len(months)


def validate_dataset(
    dataset: list[dict], benchmarks: dict[str, list[dict]]
) -> None:
    from src.evaluation_service import parse_benchmark_records, run_benchmark
    from src.skill_ontology import SKILL_CATALOG

    _validate_distribution(dataset)
    ids = [str(item.get("resume_id") or "") for item in dataset]
    if len(ids) != len(set(ids)) or any(not value for value in ids):
        raise ValueError("resume_id必须存在且保持唯一")

    contact_pattern = re.compile(
        r"(?<!\d)1[3-9]\d{9}(?!\d)|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    )
    for record in dataset:
        resume_id = record["resume_id"]
        text = str(record.get("text") or "")
        expected = record.get("expected") or {}
        if record.get("synthetic") is not True or record.get("data_usage") != "test_only":
            raise ValueError(f"{resume_id}缺少合成测试数据标记")
        if record.get("resume_path") != f"resumes/{resume_id}.txt":
            raise ValueError(f"{resume_id}的简历相对路径无效")
        if contact_pattern.search(text):
            raise ValueError(f"{resume_id}包含疑似真实联系方式")
        skills = list(expected.get("skills") or [])
        if len(skills) != len(set(skills)):
            raise ValueError(f"{resume_id}的预期技能存在重复项")
        unknown = [skill for skill in skills if skill not in SKILL_CATALOG]
        if unknown:
            raise ValueError(f"{resume_id}包含未规范化技能: {', '.join(unknown)}")
        for evidence in expected.get("evidence_substrings") or []:
            if not evidence or evidence not in text:
                raise ValueError(f"{resume_id}的证据片段无法在正文中定位")
        try:
            months = _merged_month_count(expected.get("work_ranges") or [])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{resume_id}的工作时间范围无效: {exc}") from exc
        expected_years = float(expected.get("experience_years") or 0)
        if abs(months / 12 - expected_years) > 0.01:
            raise ValueError(f"{resume_id}的经验年限与工作时间范围不一致")

    expected_tasks = {"resume_extraction", "matching", "job_recommendation"}
    if set(benchmarks) != expected_tasks:
        raise ValueError("评测集必须包含简历解析、人岗匹配和岗位推荐三类任务")
    all_case_ids: set[str] = set()
    for task, records in benchmarks.items():
        if len(records) != 64:
            raise ValueError(f"{task}评测记录必须为64条")
        for record in records:
            case_id = str(record.get("case_id") or "")
            if case_id in all_case_ids:
                raise ValueError(f"case_id重复: {case_id}")
            all_case_ids.add(case_id)
            metadata = record.get("metadata") or {}
            if metadata.get("synthetic") is not True or metadata.get("data_usage") != "test_only":
                raise ValueError(f"{case_id}缺少合成测试数据标记")
        raw = "\n".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True)
            for record in records
        ).encode("utf-8")
        parse_benchmark_records(raw, f"{task}.jsonl")
        result = run_benchmark(records)["results"][0]
        if result["details"]["failed_case_ids"]:
            failed = ", ".join(result["details"]["failed_case_ids"][:5])
            raise ValueError(f"{task}评测存在失败案例: {failed}")


DATASET_README = """# 合成简历测试数据

本目录由 `python -m src.generate_synthetic_resumes` 生成，用于简历解析、人岗匹配、岗位推荐和学习路径的自动回归测试。

所有记录均为虚构数据，并标记为 `synthetic=true` 和 `data_usage=test_only`。这些数据不得计入真实岗位或简历采集量，不得作为比赛独立测试集或准确率证明。

## 文件

- `resumes/`：64份UTF-8文本简历。
- `manifest.jsonl`：简历元数据和结构化标准答案。
- `benchmark-resume-extraction.jsonl`：简历解析评测集。
- `benchmark-matching.jsonl`：人岗匹配评测集。
- `benchmark-recommendation.jsonl`：岗位推荐评测集。

固定种子为 `20260805`，重复运行应生成完全一致的内容。正式参赛评测仍应使用与规则、词典和阈值调优隔离的真实人工标注数据。
"""


def _jsonl_text(records: list[dict]) -> str:
    return "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
        for record in records
    )


def _remove_verified_sibling(path: Path, parent: Path) -> None:
    if path.parent != parent or not path.name.startswith("."):
        raise ValueError(f"拒绝删除未经验证的目录: {path}")
    if path.exists():
        shutil.rmtree(path)


def write_dataset(
    output_dir: Path | str, *, seed: int = DEFAULT_SEED
) -> dict[str, object]:
    target = Path(output_dir).resolve()
    parent = target.parent
    if target == Path(target.anchor) or not target.name:
        raise ValueError("输出目录不能是文件系统根目录")
    if target.exists() and not target.is_dir():
        raise ValueError("输出路径已存在且不是目录")
    parent.mkdir(parents=True, exist_ok=True)

    dataset = build_dataset(seed=seed)
    benchmarks = build_benchmark_records(dataset)
    validate_dataset(dataset, benchmarks)

    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}-staging-", dir=parent))
    backup = parent / f".{target.name}-backup"
    try:
        resumes_dir = staging / "resumes"
        resumes_dir.mkdir()
        for record in dataset:
            (resumes_dir / f"{record['resume_id']}.txt").write_text(
                record["text"] + "\n", encoding="utf-8"
            )
        manifest = [
            {key: value for key, value in record.items() if key != "text"}
            for record in dataset
        ]
        (staging / "manifest.jsonl").write_text(
            _jsonl_text(manifest), encoding="utf-8"
        )
        for task, filename in (
            ("resume_extraction", "benchmark-resume-extraction.jsonl"),
            ("matching", "benchmark-matching.jsonl"),
            ("job_recommendation", "benchmark-recommendation.jsonl"),
        ):
            (staging / filename).write_text(
                _jsonl_text(benchmarks[task]), encoding="utf-8"
            )
        (staging / "README.md").write_text(DATASET_README, encoding="utf-8")

        if len(list(resumes_dir.glob("*.txt"))) != 64:
            raise ValueError("暂存目录中的简历文件数量不是64")
        for filename in (
            "manifest.jsonl",
            "benchmark-resume-extraction.jsonl",
            "benchmark-matching.jsonl",
            "benchmark-recommendation.jsonl",
        ):
            lines = [
                line
                for line in (staging / filename).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if len(lines) != 64:
                raise ValueError(f"{filename}暂存记录数量不是64")
            for line in lines:
                json.loads(line)

        _remove_verified_sibling(backup, parent)
        if target.exists():
            target.rename(backup)
        try:
            staging.rename(target)
        except Exception:
            if backup.exists() and not target.exists():
                backup.rename(target)
            raise
        _remove_verified_sibling(backup, parent)
    except Exception:
        if staging.exists():
            _remove_verified_sibling(staging, parent)
        raise

    return {
        "dataset_version": DATASET_VERSION,
        "seed": seed,
        "output_dir": str(target),
        "resume_count": len(dataset),
        "family_counts": dict(sorted(Counter(item["target_family"] for item in dataset).items())),
        "level_counts": dict(sorted(Counter(item["target_level"] for item in dataset).items())),
        "band_counts": dict(sorted(Counter(item["expected_band"] for item in dataset).items())),
        "benchmark_counts": {task: len(records) for task, records in benchmarks.items()},
    }
