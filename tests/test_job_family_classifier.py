import json
from pathlib import Path

import pytest

import src.job_collection.family_classifier as classifier_module
from src.job_collection.family_classifier import (
    classify_job_family,
    load_family_config,
    schedule_family_deficits,
)
from src.job_data_service import JOB_FAMILY_NAMES


CONFIG_PATH = Path(__file__).parents[1] / "config" / "job_family_queries.json"


POSITIVE_CASES = {
    "JAVA_DEVELOPER": [
        ("Java开发工程师", "使用 Spring Boot、MySQL 负责后端服务开发"),
        ("高级Java工程师", "负责 JVM 调优和 Spring Cloud 微服务建设"),
    ],
    "PYTHON_BACKEND": [
        ("Python后端工程师", "使用 Django 和 PostgreSQL 开发 API 服务"),
        ("Python服务端开发", "负责 FastAPI、Celery 后端系统"),
    ],
    "GO_DEVELOPER": [
        ("Go开发工程师", "使用 Golang、Gin 开发高并发服务"),
        ("Golang后端工程师", "负责 Go 微服务和 gRPC 接口"),
    ],
    "FRONTEND_DEVELOPER": [
        ("前端开发工程师", "使用 Vue、TypeScript 开发 Web 页面"),
        ("Web前端工程师", "负责 React 和 CSS 前端组件"),
    ],
    "DEVOPS_ENGINEER": [
        ("DevOps工程师", "负责 Jenkins CI/CD 与 Terraform 自动化"),
        ("DevOps开发工程师", "建设 GitLab CI 持续交付流水线"),
    ],
    "SRE_ENGINEER": [
        ("SRE工程师", "建设 SLO、错误预算和 Prometheus 可观测性"),
        ("站点可靠性工程师", "负责服务可靠性、告警和故障演练"),
    ],
    "CLOUD_NATIVE_ENGINEER": [
        ("云原生工程师", "负责 Kubernetes、Service Mesh 平台建设"),
        ("云原生开发工程师", "开发 Operator 并维护容器编排平台"),
    ],
    "AI_AGENT_ENGINEER": [
        ("AI Agent工程师", "开发 tool calling、MCP 和多智能体工作流"),
        ("智能体应用工程师", "负责 Agent 规划、工具调用与记忆模块"),
    ],
    "LLM_APPLICATION_ENGINEER": [
        ("大模型应用工程师", "使用 LangChain 和 LLM API 开发应用"),
        ("LLM应用开发工程师", "负责大模型 API 集成与上下文管理"),
    ],
    "RAG_ENGINEER": [
        ("RAG工程师", "负责向量数据库、检索召回和重排序"),
        ("检索增强生成工程师", "建设 embedding、知识库检索链路"),
    ],
    "MLOPS_ENGINEER": [
        ("MLOps工程师", "使用 MLflow 负责模型部署和模型监控"),
        ("机器学习平台工程师", "建设特征库、训练流水线和模型注册"),
    ],
    "MULTIMODAL_ENGINEER": [
        ("多模态算法工程师", "负责视觉语言模型和图文理解"),
        ("视觉语言模型工程师", "开展 VLM、图文对齐和多模态训练"),
    ],
    "PROMPT_ENGINEER": [
        ("提示词工程师", "负责 prompt engineering 和提示词评测"),
        ("Prompt Engineer", "设计 system prompt、few-shot 与效果评估"),
    ],
    "AI_SOLUTION_ENGINEER": [
        ("AI解决方案工程师", "负责客户需求分析、AI方案设计和 PoC"),
        ("人工智能售前解决方案架构师", "完成售前交流、方案咨询与交付"),
    ],
    "BIG_DATA_DEVELOPER": [
        ("大数据开发工程师", "使用 Hadoop、Spark、Flink 开发任务"),
        ("大数据平台开发", "负责 Hive 数仓和 Spark 批处理"),
    ],
    "DATA_GOVERNANCE_ENGINEER": [
        ("数据治理工程师", "负责元数据、数据血缘和质量规则"),
        ("数据治理专家", "建设数据标准、主数据和数据目录"),
    ],
    "DATA_ENGINEER": [
        ("数据工程师", "负责 ETL、数据仓库和 Airflow 调度"),
        ("数据开发工程师", "建设数据管道、SQL 模型和数据集市"),
    ],
    "IOT_ENGINEER": [
        ("物联网工程师", "负责 MQTT、传感器和设备接入"),
        ("IoT开发工程师", "开发物联网网关和设备协议"),
    ],
    "EDGE_COMPUTING_ENGINEER": [
        ("边缘计算工程师", "负责边缘推理、边缘网关和算力调度"),
        ("边缘云工程师", "建设 edge computing 节点与云边协同"),
    ],
    "CYBERSECURITY_ENGINEER": [
        ("网络安全工程师", "负责渗透测试、漏洞扫描和安全加固"),
        ("信息安全工程师", "建设 SIEM、入侵检测和应急响应"),
    ],
    "DIGITAL_TWIN_ENGINEER": [
        ("数字孪生工程师", "负责三维仿真、实时映射与模型同步"),
        ("数字孪生开发工程师", "建设 twin model 和仿真平台"),
    ],
    "ROBOTICS_ENGINEER": [
        ("机器人工程师", "使用 ROS、SLAM 开发运动控制系统"),
        ("机器人算法工程师", "负责路径规划、感知和机械臂控制"),
    ],
}


NEGATIVE_DESCRIPTIONS = {
    "JAVA_DEVELOPER": "负责订单业务需求梳理与评审，不提供具体技术栈信息",
    "PYTHON_BACKEND": "维护内容发布流程与操作手册，协调日常排期",
    "GO_DEVELOPER": "参与支付业务会议并跟踪项目事项和完成时间",
    "FRONTEND_DEVELOPER": "协调产品原型验收、会议记录和文档归档",
    "DEVOPS_ENGINEER": "管理团队排期、变更审批和跨部门沟通事项",
    "SRE_ENGINEER": "整理值班安排、服务台工单和月度工作记录",
    "CLOUD_NATIVE_ENGINEER": "负责供应商合同、资源台账和采购流程跟踪",
    "AI_AGENT_ENGINEER": "跟进产品需求、用户反馈和版本发布计划",
    "LLM_APPLICATION_ENGINEER": "维护项目文档、版本记录和会议纪要",
    "RAG_ENGINEER": "整理内部资料、访问权限和文档更新记录",
    "MLOPS_ENGINEER": "协调算法团队会议、项目排期和人员安排",
    "MULTIMODAL_ENGINEER": "审核业务素材并记录处理结果和交付进度",
    "PROMPT_ENGINEER": "编写产品说明、运营文案和活动复盘材料",
    "AI_SOLUTION_ENGINEER": "跟进合同流程、会议安排和项目里程碑",
    "BIG_DATA_DEVELOPER": "核对业务报表并整理需求清单和验收记录",
    "DATA_GOVERNANCE_ENGINEER": "组织制度宣贯、培训安排和材料归档",
    "DATA_ENGINEER": "跟进数据需求并维护项目台账和沟通记录",
    "IOT_ENGINEER": "维护设备资产清单、采购记录和借用登记",
    "EDGE_COMPUTING_ENGINEER": "整理机房资源清单、值班安排和巡检记录",
    "CYBERSECURITY_ENGINEER": "维护员工账号申请、审批记录和培训签到",
    "DIGITAL_TWIN_ENGINEER": "协调三维素材交付、会议排期和验收文档",
    "ROBOTICS_ENGINEER": "管理实验室设备借用、耗材登记和文档归档",
}


def test_config_covers_exactly_all_22_families_with_required_fields():
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    config = load_family_config(CONFIG_PATH)

    assert set(raw) == set(JOB_FAMILY_NAMES)
    assert set(config) == set(JOB_FAMILY_NAMES)
    assert len(config) == 22
    for code, family in config.items():
        assert family.queries
        assert family.title_aliases
        assert family.skill_indicators
        assert isinstance(family.exclusions, tuple)
        assert family.minimum_title_evidence >= 1
        assert family.minimum_skill_evidence >= 1
        assert 0.8 <= family.confidence <= 1.0
        assert family.quota.target == 300
        assert family.quota.batch_size == 100
    assert sum(family.quota.target for family in config.values()) >= 5000


def test_collection_queries_are_short_search_terms_without_recruitment_noise():
    config = load_family_config(CONFIG_PATH)

    for family in config.values():
        assert len(family.queries) == 2
        assert len(set(family.queries)) == 2
        for query in family.queries:
            assert query == query.strip()
            assert "招聘" not in query
            assert len(query) <= 12


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        (("JAVA_DEVELOPER", "minimum_title_evidence"), 0),
        (("JAVA_DEVELOPER", "minimum_skill_evidence"), -1),
        (("JAVA_DEVELOPER", "confidence"), 1.1),
        (("JAVA_DEVELOPER", "quota", "target"), 0),
        (("JAVA_DEVELOPER", "unexpected"), "not allowed"),
        (("JAVA_DEVELOPER", "queries"), [" "]),
        (("JAVA_DEVELOPER", "title_aliases"), ["Java", "Ｊａｖａ"]),
    ],
)
def test_family_config_rejects_invalid_or_noncanonical_values(
    tmp_path, mutation, value
):
    document = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    target = document
    for key in mutation[:-1]:
        target = target[key]
    target[mutation[-1]] = value
    path = tmp_path / "invalid-family-config.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError):
        load_family_config(path)


@pytest.mark.parametrize(
    ("expected, title, description"),
    [
        (code, title, description)
        for code, examples in POSITIVE_CASES.items()
        for title, description in examples
    ],
)
def test_each_family_requires_title_and_capability_evidence(
    expected, title, description
):
    result = classify_job_family(title, description)

    assert result.status == "auto"
    assert result.family_code == expected
    assert result.candidates[0] == expected
    assert result.matched_title_terms
    assert result.matched_skill_terms
    assert result.confidence >= 0.8


@pytest.mark.parametrize(
    ("target, title, description"),
    [
        (code, POSITIVE_CASES[code][0][0], description)
        for code, description in NEGATIVE_DESCRIPTIONS.items()
    ],
)
def test_each_family_title_without_its_skill_evidence_requires_review(
    target, title, description
):
    assert set(NEGATIVE_DESCRIPTIONS) == set(JOB_FAMILY_NAMES)

    result = classify_job_family(title, description)

    assert result.status == "review"
    assert result.family_code is None
    assert target in result.candidates
    assert result.matched_title_terms or result.excluded_terms
    assert result.confidence < 0.8


@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("AI数据开发运维工程师", "负责平台开发、数据处理和系统运维"),
        ("开发工程师", "参与 AI 平台和数据系统开发"),
        ("云平台工程师", "负责平台日常建设"),
    ],
)
def test_generic_or_weak_evidence_goes_to_review(title, description):
    result = classify_job_family(title, description)

    assert result.status == "review"
    assert result.family_code is None
    assert result.confidence < 0.8
    assert result.reason


def test_three_unique_capabilities_can_classify_a_generic_title():
    result = classify_job_family(
        "软件工程师",
        "负责Hadoop、Spark和Hive数据平台的开发、优化与稳定性建设。",
    )

    assert result.status == "auto"
    assert result.family_code == "BIG_DATA_DEVELOPER"
    assert {"Hadoop", "Spark", "Hive"} <= set(result.matched_skill_terms)
    assert result.reason == "strong_capability_only_evidence"


def test_capability_only_evidence_with_multiple_families_remains_review():
    result = classify_job_family(
        "智能系统工程师",
        "负责MQTT、传感器、设备接入、ROS、SLAM和运动控制系统建设。",
    )

    assert result.status == "review"
    assert result.family_code is None
    assert result.reason == "multi_family_conflict"


@pytest.mark.parametrize("title", ["大数据工程师", "数据开发工程师"])
def test_big_data_ecosystem_evidence_disambiguates_public_job_titles(title):
    result = classify_job_family(
        title,
        "负责SQL数据清洗、转换和集成，参与数据平台维护，了解大数据技术生态。",
    )

    assert result.status == "auto"
    assert result.family_code == "BIG_DATA_DEVELOPER"
    assert "大数据技术生态" in result.matched_skill_terms


def test_data_developer_with_etl_evidence_remains_data_engineer():
    result = classify_job_family(
        "数据开发工程师",
        "负责ETL、数据仓库、Airflow调度和数据管道建设。",
    )

    assert result.status == "auto"
    assert result.family_code == "DATA_ENGINEER"


def test_reviewed_family_hint_accepts_two_matching_capabilities():
    result = classifier_module.classify_job_family_with_hint(
        "Backend Engineer",
        "Build production services with Spring Boot and Spring Cloud.",
        "JAVA_DEVELOPER",
    )

    assert result.status == "annotated"
    assert result.family_code == "JAVA_DEVELOPER"
    assert {"Spring Boot", "Spring Cloud"} <= set(result.matched_skill_terms)
    assert result.reason == "reviewed_family_hint_with_capability_evidence"


def test_reviewed_family_hint_does_not_override_conflicting_capabilities():
    result = classifier_module.classify_job_family_with_hint(
        "Frontend Engineer",
        "Build web interfaces with Vue, TypeScript, CSS, and Webpack.",
        "JAVA_DEVELOPER",
    )

    assert result.status == "auto"
    assert result.family_code == "FRONTEND_DEVELOPER"


def test_query_terms_alone_never_trigger_automatic_classification():
    config = load_family_config()
    query_only = config["RAG_ENGINEER"].queries[0]

    result = classify_job_family("普通研发岗位", f"招聘方向：{query_only}")

    assert result.status == "review"
    assert result.family_code is None


def test_live_style_web_developer_with_css_evidence_is_frontend():
    result = classify_job_family(
        "web开发工程师",
        "负责 Web 页面开发与维护，使用 CSS 完成响应式布局和组件样式。",
    )

    assert result.status == "auto"
    assert result.family_code == "FRONTEND_DEVELOPER"
    assert "Web开发工程师" in result.matched_title_terms
    assert "CSS" in result.matched_skill_terms


def test_english_skill_terms_require_real_word_boundaries():
    result = classify_job_family(
        "Python后端工程师",
        "维护 MyDjangoXPortal 与 FastAPIClient 的文档和排期",
    )

    assert result.status == "review"
    assert result.family_code is None
    assert "PYTHON_BACKEND" in result.candidates
    assert not result.matched_skill_terms


def test_english_skill_terms_match_when_delimited_by_punctuation():
    result = classify_job_family(
        "Python后端工程师",
        "使用 Django/FastAPI 开发接口并维护服务",
    )

    assert result.status == "auto"
    assert result.family_code == "PYTHON_BACKEND"
    assert {"Django", "FastAPI"} <= set(result.matched_skill_terms)


@pytest.mark.parametrize(
    ("expected", "title", "description"),
    [
        (
            "FRONTEND_DEVELOPER",
            "Frontend Engineer",
            "Build accessible interfaces with React, TypeScript, and CSS.",
        ),
        (
            "CYBERSECURITY_ENGINEER",
            "Security Engineer",
            "Own SIEM detection rules, incident response, and vulnerability management.",
        ),
        (
            "DATA_ENGINEER",
            "Data Engineer",
            "Build ETL data pipelines with Airflow and a cloud data warehouse.",
        ),
        (
            "CLOUD_NATIVE_ENGINEER",
            "Cloud Infrastructure Engineer",
            "Operate Kubernetes, Istio, and container orchestration platforms.",
        ),
        (
            "AI_AGENT_ENGINEER",
            "Software Engineer, AI Agents",
            "Build agentic systems with tool calling, MCP, and agent memory.",
        ),
        (
            "AI_SOLUTION_ENGINEER",
            "Solutions Engineer",
            "Translate customer requirements into AI solution design and PoC delivery.",
        ),
        (
            "MLOPS_ENGINEER",
            "Machine Learning Platform Engineer",
            "Own MLflow, model deployment, model monitoring, and training pipelines.",
        ),
        (
            "LLM_APPLICATION_ENGINEER",
            "Applied AI Engineer",
            "Build LLM applications with LangChain, function calling, and context management.",
        ),
        (
            "RAG_ENGINEER",
            "Retrieval Engineer",
            "Develop embedding retrieval, vector database search, and reranking pipelines.",
        ),
        (
            "ROBOTICS_ENGINEER",
            "Robotics Software Engineer",
            "Develop ROS, SLAM, motion planning, and robot control software.",
        ),
    ],
)
def test_reviewed_english_job_aliases_require_matching_capabilities(
    expected, title, description
):
    result = classify_job_family(title, description)

    assert result.status == "auto"
    assert result.family_code == expected
    assert result.matched_title_terms
    assert result.matched_skill_terms


def test_negated_skill_mentions_do_not_count_as_positive_evidence():
    result = classify_job_family(
        "Python后端工程师",
        "不使用 Django，无需 FastAPI，仅了解 Flask，非核心 Celery。",
    )

    assert result.status == "review"
    assert result.family_code is None
    assert "PYTHON_BACKEND" in result.candidates
    assert not result.matched_skill_terms


def test_negated_exclusion_does_not_penalize_valid_data_engineering_evidence():
    result = classify_job_family(
        "数据工程师",
        "负责 ETL 与数据仓库建设，不涉及数据治理。",
    )

    assert result.status == "auto"
    assert result.family_code == "DATA_ENGINEER"
    assert not result.excluded_terms


@pytest.mark.parametrize(
    ("expected", "title", "description"),
    [
        ("RAG_ENGINEER", "企业知识检索开发", "负责语义召回、文档切片与结果精排"),
        ("SRE_ENGINEER", "稳定性保障工程师", "制定可用性目标并开展故障复盘"),
        (
            "DIGITAL_TWIN_ENGINEER",
            "虚实映射平台开发",
            "构建设备镜像、状态同步与实时仿真系统",
        ),
        (
            "DATA_GOVERNANCE_ENGINEER",
            "数据资产管理工程师",
            "负责血缘追踪、质量稽核与标准管理",
        ),
    ],
)
def test_real_world_aliases_independent_from_original_config_terms(
    expected, title, description
):
    result = classify_job_family(title, description)

    assert result.status == "auto"
    assert result.family_code == expected
    assert result.matched_title_terms
    assert result.matched_skill_terms


def test_reviewed_four_family_aliases_live_in_config_not_python_constants():
    config = load_family_config()
    expected = {
        "RAG_ENGINEER": {
            "titles": {"企业知识检索开发", "知识库工程师", "检索应用工程师"},
            "skills": {"语义召回", "文档切片", "结果精排", "向量检索", "召回排序"},
        },
        "SRE_ENGINEER": {
            "titles": {"稳定性保障工程师", "稳定性工程师", "可靠性保障工程师"},
            "skills": {"可用性目标", "故障复盘", "高可用", "故障治理"},
        },
        "DIGITAL_TWIN_ENGINEER": {
            "titles": {"虚实映射平台开发", "虚实融合工程师", "数字镜像工程师"},
            "skills": {"设备镜像", "状态同步", "实时仿真", "虚实同步"},
        },
        "DATA_GOVERNANCE_ENGINEER": {
            "titles": {"数据资产管理工程师", "数据资产工程师", "数据质量管理工程师"},
            "skills": {"血缘追踪", "质量稽核", "标准管理", "资产盘点"},
        },
    }

    assert not hasattr(classifier_module, "_INDEPENDENT_TITLE_ALIASES")
    assert not hasattr(classifier_module, "_INDEPENDENT_SKILL_ALIASES")
    for code, aliases in expected.items():
        assert aliases["titles"] <= set(config[code].title_aliases)
        assert aliases["skills"] <= set(config[code].skill_indicators)


@pytest.mark.parametrize(
    ("title", "description", "expected_candidates"),
    [
        (
            "DevOps与SRE工程师",
            "负责 CI/CD、SLO、Prometheus 和可靠性建设",
            {"DEVOPS_ENGINEER", "SRE_ENGINEER"},
        ),
        (
            "RAG与大模型应用工程师",
            "使用 LangChain、向量数据库、检索和 LLM API",
            {"RAG_ENGINEER", "LLM_APPLICATION_ENGINEER"},
        ),
        (
            "Java/Python/Go后端工程师",
            "使用 Spring Boot、Django、Golang 和 gRPC 开发服务",
            {"JAVA_DEVELOPER", "PYTHON_BACKEND", "GO_DEVELOPER"},
        ),
        (
            "物联网与边缘计算工程师",
            "负责 MQTT 设备接入、边缘网关和边缘推理",
            {"IOT_ENGINEER", "EDGE_COMPUTING_ENGINEER"},
        ),
    ],
)
def test_multi_family_conflicts_are_reviewed(title, description, expected_candidates):
    result = classify_job_family(title, description)

    assert result.status == "review"
    assert result.family_code is None
    assert expected_candidates <= set(result.candidates)
    assert result.matched_title_terms
    assert result.matched_skill_terms
    assert "conflict" in result.reason


def test_exclusions_are_reported_and_prevent_wrong_auto_classification():
    result = classify_job_family(
        "数据工程师",
        "主要负责数据标准、元数据、数据血缘和治理，不承担 ETL 数据管道开发",
    )

    assert result.status == "review"
    assert "DATA_ENGINEER" in result.candidates
    assert result.excluded_terms
    assert result.family_code is None


def test_deficit_scheduler_skips_quota_and_is_stable_and_batch_limited():
    valid_counts = {code: 50 for code in JOB_FAMILY_NAMES}
    valid_counts.update(
        {
            "JAVA_DEVELOPER": 300,
            "PYTHON_BACKEND": 10,
            "GO_DEVELOPER": 10,
            "FRONTEND_DEVELOPER": 299,
        }
    )
    batch_counts = {"PYTHON_BACKEND": 5, "GO_DEVELOPER": 0}

    schedule = schedule_family_deficits(valid_counts, batch_counts)

    codes = [item.family_code for item in schedule]
    assert "JAVA_DEVELOPER" not in codes
    assert codes[:2] == ["GO_DEVELOPER", "PYTHON_BACKEND"]
    requested = {item.family_code: item.requested for item in schedule}
    assert requested["PYTHON_BACKEND"] == 95
    assert requested["GO_DEVELOPER"] == 100
    frontend = next(item for item in schedule if item.family_code == "FRONTEND_DEVELOPER")
    assert frontend.requested == 1
    assert schedule == schedule_family_deficits(valid_counts, batch_counts)


def test_candidate_ties_are_sorted_by_family_code_not_mapping_order():
    config = load_family_config()
    forward = {
        code: config[code] for code in ("JAVA_DEVELOPER", "PYTHON_BACKEND")
    }
    reverse = dict(reversed(tuple(forward.items())))

    first = classify_job_family(
        "Java Python工程师", "使用 Spring Boot 和 Django 开发服务", forward
    )
    second = classify_job_family(
        "Java Python工程师", "使用 Spring Boot 和 Django 开发服务", reverse
    )

    expected = ("JAVA_DEVELOPER", "PYTHON_BACKEND")
    assert first.status == second.status == "review"
    assert first.candidates == second.candidates == expected


def test_scheduler_ties_are_sorted_by_family_code_with_reversed_config():
    config = load_family_config()
    reverse = dict(reversed(tuple(config.items())))
    valid_counts = {code: 10 for code in JOB_FAMILY_NAMES}
    batch_counts = {code: 0 for code in JOB_FAMILY_NAMES}

    schedule = schedule_family_deficits(valid_counts, batch_counts, reverse)

    assert [item.family_code for item in schedule] == sorted(JOB_FAMILY_NAMES)
