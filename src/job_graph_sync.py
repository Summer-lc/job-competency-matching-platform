from __future__ import annotations

from collections import defaultdict
from datetime import datetime


NODE_LABELS = {
    "family": "JobFamily",
    "job": "JobProfile",
    "skill": "Skill",
    "responsibility": "Responsibility",
    "scenario": "IndustryScenario",
    "evidence": "Evidence",
}

EDGE_RELATIONS = {
    "has_version": "HAS_VERSION",
    "required": "REQUIRES_SKILL",
    "preferred": "REQUIRES_SKILL",
    "has_responsibility": "HAS_RESPONSIBILITY",
    "applies_to": "APPLIES_TO",
    "supported_by": "SUPPORTED_BY",
}


def _scalar_properties(item: dict, excluded: set[str]) -> dict:
    return {
        key: value
        for key, value in item.items()
        if key not in excluded and value is not None and isinstance(value, (str, int, float, bool))
    }


def partition_graph(graph: dict) -> dict:
    nodes = defaultdict(list)
    edges = defaultdict(list)
    for node in graph.get("nodes", []):
        node_type = node.get("type")
        if node_type not in NODE_LABELS:
            continue
        nodes[node_type].append(
            {
                "id": node["id"],
                "props": _scalar_properties(node, {"id", "type"}),
            }
        )
    for edge in graph.get("edges", []):
        relation = EDGE_RELATIONS.get(edge.get("type"))
        if relation is None:
            continue
        props = _scalar_properties(edge, {"source", "target", "type"})
        if edge.get("type") in {"required", "preferred"}:
            props["requirement_type"] = edge["type"]
        edges[relation].append(
            {"source": edge["source"], "target": edge["target"], "props": props}
        )
    return {"nodes": dict(nodes), "edges": dict(edges)}


def sync_graph_to_neo4j(graph: dict) -> dict:
    from neo4j import GraphDatabase
    from src.app_config import NEO4J_DATABASE, NEO4J_PASSWORD, NEO4J_URI, NEO4J_USERNAME

    if not NEO4J_PASSWORD:
        raise ValueError("NEO4J_PASSWORD未配置")
    groups = partition_graph(graph)
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))
    try:
        with driver.session(database=NEO4J_DATABASE) as session:
            session.run(
                "CREATE CONSTRAINT graph_entity_id IF NOT EXISTS "
                "FOR (n:GraphEntity) REQUIRE n.id IS UNIQUE"
            )
            for node_type, rows in groups["nodes"].items():
                label = NODE_LABELS[node_type]
                session.run(
                    f"""
                    UNWIND $rows AS row
                    MERGE (n:GraphEntity:{label} {{id: row.id}})
                    SET n += row.props, n.entity_type = $entity_type
                    """,
                    rows=rows,
                    entity_type=node_type,
                )
            for relation, rows in groups["edges"].items():
                session.run(
                    f"""
                    UNWIND $rows AS row
                    MATCH (source:GraphEntity {{id: row.source}})
                    MATCH (target:GraphEntity {{id: row.target}})
                    MERGE (source)-[link:{relation}]->(target)
                    SET link += row.props
                    """,
                    rows=rows,
                )
    finally:
        driver.close()
    node_counts = {name: len(rows) for name, rows in groups["nodes"].items()}
    return {
        "node_counts": node_counts,
        "nodes": sum(node_counts.values()),
        "relations": sum(len(rows) for rows in groups["edges"].values()),
        "synced_at": datetime.now().isoformat(),
    }
