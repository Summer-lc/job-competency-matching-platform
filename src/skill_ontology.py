from __future__ import annotations


ONTOLOGY_VERSION = "skill-ontology-v1"

PROFICIENCY_RANK = {
    "aware": 1,
    "working": 2,
    "advanced": 3,
    "expert": 4,
}

SKILL_CATALOG = {
    "Java": ("language", ["java"]),
    "Python": ("language", ["python"]),
    "Go": ("language", ["golang", "go语言"]),
    "JavaScript": ("language", ["javascript", "js开发"]),
    "TypeScript": ("language", ["typescript"]),
    "Spring Boot": ("framework", ["spring boot", "springboot"]),
    "Spring Cloud": ("framework", ["spring cloud", "springcloud"]),
    "FastAPI": ("framework", ["fastapi"]),
    "Django": ("framework", ["django"]),
    "Vue": ("framework", ["vue.js", "vuejs", "vue"]),
    "React": ("framework", ["react.js", "reactjs", "react"]),
    "MySQL": ("database", ["mysql"]),
    "PostgreSQL": ("database", ["postgresql", "postgres"]),
    "Redis": ("database", ["redis"]),
    "MongoDB": ("database", ["mongodb"]),
    "Neo4j": ("database", ["neo4j"]),
    "Docker": ("cloud_native", ["docker", "容器化"]),
    "Kubernetes": ("cloud_native", ["kubernetes", "k8s"]),
    "Linux": ("platform", ["linux"]),
    "Git": ("tool", ["git版本", "git"]),
    "CI/CD": ("devops", ["ci/cd", "持续集成", "持续交付"]),
    "Jenkins": ("devops", ["jenkins"]),
    "Prometheus": ("observability", ["prometheus"]),
    "Grafana": ("observability", ["grafana"]),
    "大语言模型": ("ai", ["大语言模型", "大模型", "llm"]),
    "RAG": ("ai", ["检索增强生成", "rag"]),
    "LangChain": ("ai_framework", ["langchain"]),
    "AI Agent": ("ai", ["ai agent", "智能体", "agent开发"]),
    "MCP": ("ai", ["model context protocol", "mcp协议", "mcp"]),
    "向量数据库": ("ai_infra", ["向量数据库", "vector database"]),
    "Embedding": ("ai", ["embedding", "向量嵌入"]),
    "PyTorch": ("ml_framework", ["pytorch"]),
    "TensorFlow": ("ml_framework", ["tensorflow"]),
    "多模态": ("ai", ["多模态", "vision language"]),
    "Hadoop": ("big_data", ["hadoop"]),
    "Spark": ("big_data", ["apache spark", "spark"]),
    "Flink": ("big_data", ["apache flink", "flink"]),
    "Kafka": ("big_data", ["kafka"]),
    "ETL": ("data", ["etl"]),
    "数据仓库": ("data", ["数据仓库", "data warehouse"]),
    "元数据管理": ("data_governance", ["元数据管理", "metadata management"]),
    "数据质量": ("data_governance", ["数据质量", "data quality"]),
    "MQTT": ("iot", ["mqtt"]),
    "物联网": ("iot", ["物联网", "iot"]),
    "嵌入式": ("embedded", ["嵌入式"]),
    "边缘计算": ("edge", ["边缘计算", "edge computing"]),
    "网络安全": ("security", ["网络安全", "攻防", "渗透测试"]),
    "数字孪生": ("intelligent_system", ["数字孪生", "digital twin"]),
    "ROS": ("robotics", ["robot operating system", "ros"]),
}

RELATED_SKILL_PAIRS = {
    frozenset(("MySQL", "PostgreSQL")),
}

PREREQUISITES = {
    "Docker": ("Linux",),
    "Kubernetes": ("Docker",),
}


def _build_skill_index() -> dict[str, tuple[str, str]]:
    index: dict[str, tuple[str, str]] = {}
    for name, (category, aliases) in SKILL_CATALOG.items():
        index[name.casefold()] = (name, category)
        for alias in aliases:
            index[alias.casefold()] = (name, category)
    return index


_SKILL_INDEX = _build_skill_index()


def normalize_skill(value: str | None) -> dict[str, str | None]:
    """Return canonical name, category and the trimmed original alias."""
    alias = str(value).strip() if value is not None else ""
    if not alias:
        return {"name": None, "category": "general", "alias": alias}

    match = _SKILL_INDEX.get(alias.casefold())
    if match is None:
        return {"name": alias, "category": "general", "alias": alias}

    name, category = match
    return {"name": name, "category": category, "alias": alias}


def prerequisite_chain(skill: str | None) -> list[str]:
    """Return prerequisites in stable foundational-first order without duplicates."""
    target = normalize_skill(skill)["name"]
    if target is None:
        return []

    ordered: list[str] = []
    visited = {target}

    def visit(current: str) -> None:
        for prerequisite in PREREQUISITES.get(current, ()):
            canonical = normalize_skill(prerequisite)["name"]
            if canonical is None or canonical in visited:
                continue
            visited.add(canonical)
            visit(canonical)
            ordered.append(canonical)

    visit(target)
    return ordered


def skill_relationship(candidate: str | None, target: str | None) -> str:
    """Return exact, related, prerequisite or none for two skills."""
    candidate_name = normalize_skill(candidate)["name"]
    target_name = normalize_skill(target)["name"]
    if candidate_name is None or target_name is None:
        return "none"
    if candidate_name == target_name:
        return "exact"
    if frozenset((candidate_name, target_name)) in RELATED_SKILL_PAIRS:
        return "related"
    if candidate_name in prerequisite_chain(target_name):
        return "prerequisite"
    return "none"
