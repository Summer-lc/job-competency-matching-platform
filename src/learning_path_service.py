from __future__ import annotations

from collections.abc import Iterable

from src.skill_ontology import normalize_skill, prerequisite_chain


LEARNING_PATH_VERSION = "learning-path-v2"
PRIORITY_ORDER = {"core": 0, "required": 0, "preferred": 1, "prerequisite": 2}


def _canonical(value: object) -> str:
    return str(normalize_skill(str(value or "")).get("name") or "")


def _deduplicated_gaps(gaps: list[dict]) -> list[dict]:
    by_skill: dict[str, dict] = {}
    for raw in gaps:
        skill = _canonical(raw.get("skill"))
        if not skill:
            continue
        item = {
            "skill": skill,
            "priority": str(raw.get("priority") or "required"),
            "reason": str(raw.get("reason") or "目标岗位能力缺口"),
            "max_uplift": max(float(raw.get("max_uplift") or 0), 0.0),
            "allow_owned": bool(raw.get("allow_owned")),
        }
        existing = by_skill.get(skill)
        if existing is None:
            by_skill[skill] = item
            continue
        existing["max_uplift"] = max(existing["max_uplift"], item["max_uplift"])
        existing["allow_owned"] = existing["allow_owned"] or item["allow_owned"]
        if PRIORITY_ORDER.get(item["priority"], 9) < PRIORITY_ORDER.get(existing["priority"], 9):
            existing["priority"] = item["priority"]
            existing["reason"] = item["reason"]
    return sorted(
        by_skill.values(),
        key=lambda item: (
            PRIORITY_ORDER.get(item["priority"], 9),
            -item["max_uplift"],
            item["skill"].casefold(),
        ),
    )


def _evidence_for_skill(skill: str, evidence_records: list[dict]) -> list[dict]:
    accepted = []
    for raw in evidence_records:
        if _canonical(raw.get("related_skill")) != skill:
            continue
        url = str(raw.get("source_url") or "").strip()
        title = str(raw.get("title") or "").strip()
        if not url or not title:
            continue
        accepted.append(
            {
                "title": title,
                "source_url": url,
                "publisher": str(raw.get("publisher") or "").strip(),
            }
        )
    return sorted(accepted, key=lambda item: (item["title"], item["source_url"]))


def _node(
    item: dict,
    *,
    owned: set[str],
    evidence_records: list[dict],
    order: int,
) -> dict:
    skill = item["skill"]
    prerequisites = prerequisite_chain(skill)
    reusable = sorted((owned & set(prerequisites)), key=str.casefold)
    if item["priority"] == "prerequisite":
        tasks = [
            f"梳理{skill}的核心概念与常用操作",
            f"完成一个可重复运行的{skill}基础练习",
        ]
        project_task = f"在本地环境中使用{skill}完成基础部署或数据处理任务"
    else:
        tasks = [
            f"学习{skill}在目标岗位中的常见职责与实践",
            f"将{skill}应用到一个带日志、测试和说明文档的练习中",
        ]
        project_task = f"在综合项目中使用{skill}解决一个真实岗位场景问题"
    return {
        "order": order,
        "skill": skill,
        "priority": item["priority"],
        "reason": item["reason"],
        "reusable_skills": reusable,
        "prerequisite_skills": prerequisites,
        "tasks": tasks,
        "project_task": project_task,
        "completion_criteria": [
            f"能够独立说明{skill}的适用场景和关键限制",
            f"提交可运行成果、测试记录和问题复盘，证明已实际使用{skill}",
        ],
        "estimated_uplift": round(float(item.get("max_uplift") or 0), 2),
        "evidence": _evidence_for_skill(skill, evidence_records),
    }


def _ordered_items(gaps: list[dict], owned: set[str]) -> list[dict]:
    ordered: list[dict] = []
    added: set[str] = set()
    for gap in gaps:
        for prerequisite in prerequisite_chain(gap["skill"]):
            if prerequisite in owned or prerequisite in added:
                continue
            ordered.append(
                {
                    "skill": prerequisite,
                    "priority": "prerequisite",
                    "reason": f"{gap['skill']}的前置技能",
                    "max_uplift": 0.0,
                }
            )
            added.add(prerequisite)
        if (
            (gap["skill"] not in owned or gap.get("allow_owned"))
            and gap["skill"] not in added
        ):
            ordered.append(gap)
            added.add(gap["skill"])
    return ordered


def _integrated_project(gaps: list[dict], owned: set[str]) -> dict:
    target_skills = [item["skill"] for item in gaps]
    if not target_skills:
        return {
            "focus": "行业场景与工程化强化",
            "summary": "选择一个真实业务场景，强化已有技能的工程质量和可观测性。",
            "skills": sorted(owned, key=str.casefold),
            "deliverables": ["可运行项目", "自动化测试", "部署说明", "量化结果与复盘"],
            "acceptance": ["项目可由第三方按说明复现", "成果数据可追溯且不虚构"],
        }
    return {
        "focus": "目标岗位综合能力验证",
        "summary": f"围绕{'、'.join(target_skills)}完成一个贴近岗位职责的综合项目。",
        "skills": target_skills,
        "deliverables": ["需求说明", "可运行代码或配置", "自动化测试", "部署文档", "量化成果记录"],
        "acceptance": [
            "每项目标技能至少有一处可定位的实际使用证据",
            "关键流程可重复运行并保留测试结果",
            "量化成果来自真实测试，不使用估算或虚构数据",
        ],
    }


def build_learning_path(
    gaps: list[dict],
    resume_skills: Iterable[str],
    current_score: float,
    *,
    evidence_records: list[dict] | None = None,
) -> dict:
    evidence_records = evidence_records or []
    owned = {_canonical(item) for item in resume_skills}
    owned.discard("")
    canonical_gaps = _deduplicated_gaps(gaps)
    ordered_items = _ordered_items(canonical_gaps, owned)
    nodes = [
        _node(
            item,
            owned=owned,
            evidence_records=evidence_records,
            order=index,
        )
        for index, item in enumerate(ordered_items, start=1)
    ]
    integrated_project = _integrated_project(canonical_gaps, owned)
    phases = [
        {
            "period": "0-30",
            "title": "基础补齐",
            "nodes": [node for node in nodes if node["priority"] == "prerequisite"],
        },
        {
            "period": "31-60",
            "title": "岗位能力",
            "nodes": [node for node in nodes if node["priority"] in {"core", "required"}],
        },
        {
            "period": "61-90",
            "title": "项目验证与能力强化",
            "nodes": [node for node in nodes if node["priority"] == "preferred"],
            "project": integrated_project,
        },
    ]
    uplift = sum(
        item["max_uplift"]
        for item in canonical_gaps
        if item["skill"] not in owned or item.get("allow_owned")
    )
    return {
        "version": LEARNING_PATH_VERSION,
        "phases": phases,
        "project": integrated_project,
        "projected_score": round(min(100.0, max(float(current_score), 0.0) + uplift), 2),
        "resume_evidence_guidance": [
            "只记录已经完成且能够复现的任务，不把学习计划写成项目经历。",
            "使用职责、技术栈、规模和量化结果描述成果，并保留代码、测试或文档证据。",
        ],
    }
