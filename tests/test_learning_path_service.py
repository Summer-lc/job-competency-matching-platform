def _gaps():
    return [
        {
            "skill": "Kubernetes",
            "priority": "core",
            "reason": "目标岗位核心必备技能",
            "max_uplift": 12,
        },
        {
            "skill": "Prometheus",
            "priority": "preferred",
            "reason": "目标岗位监控能力",
            "max_uplift": 4,
        },
    ]


def _nodes(path):
    return [node for phase in path["phases"] for node in phase["nodes"]]


def test_learning_path_orders_prerequisites_before_target_skill():
    from src.learning_path_service import build_learning_path

    path = build_learning_path(_gaps(), [], current_score=55)
    names = [node["skill"] for node in _nodes(path)]

    assert names.index("Linux") < names.index("Docker") < names.index("Kubernetes")
    assert len(names) == len(set(names))


def test_owned_skills_are_removed_and_reported_as_reusable():
    from src.learning_path_service import build_learning_path

    path = build_learning_path(_gaps(), ["Linux", "Docker"], current_score=62)
    names = [node["skill"] for node in _nodes(path)]
    kubernetes = next(node for node in _nodes(path) if node["skill"] == "Kubernetes")

    assert "Linux" not in names
    assert "Docker" not in names
    assert kubernetes["reusable_skills"] == ["Docker", "Linux"]


def test_learning_path_has_verifiable_30_60_90_day_phases():
    from src.learning_path_service import build_learning_path

    path = build_learning_path(_gaps(), ["Java"], current_score=62)

    assert [phase["period"] for phase in path["phases"]] == ["0-30", "31-60", "61-90"]
    assert all(node["tasks"] for node in _nodes(path))
    assert all(node["completion_criteria"] for node in _nodes(path))
    assert path["project"]["deliverables"]
    assert path["resume_evidence_guidance"]
    assert path["projected_score"] <= 100


def test_projected_score_uses_each_gap_uplift_once():
    from src.learning_path_service import build_learning_path

    duplicated = _gaps() + [_gaps()[0]]
    path = build_learning_path(duplicated, [], current_score=90)

    assert path["projected_score"] == 100
    kubernetes = [node for node in _nodes(path) if node["skill"] == "Kubernetes"]
    assert len(kubernetes) == 1
    assert kubernetes[0]["estimated_uplift"] == 12


def test_external_links_only_come_from_supplied_evidence():
    from src.learning_path_service import build_learning_path

    evidence = [
        {
            "related_skill": "Kubernetes",
            "title": "Kubernetes 官方文档",
            "source_url": "https://kubernetes.io/docs/",
            "publisher": "Kubernetes",
        },
        {
            "related_skill": "Java",
            "title": "无关资料",
            "source_url": "https://example.com/java",
        },
    ]
    path = build_learning_path(_gaps(), [], current_score=55, evidence_records=evidence)
    kubernetes = next(node for node in _nodes(path) if node["skill"] == "Kubernetes")
    all_urls = {
        item["source_url"]
        for node in _nodes(path)
        for item in node["evidence"]
    }

    assert kubernetes["evidence"][0]["source_url"] == "https://kubernetes.io/docs/"
    assert all_urls == {"https://kubernetes.io/docs/"}


def test_empty_gaps_returns_advanced_project_without_fake_skill_nodes():
    from src.learning_path_service import build_learning_path

    path = build_learning_path([], ["Java", "Docker"], current_score=88)

    assert _nodes(path) == []
    assert path["project"]["focus"] == "行业场景与工程化强化"
    assert path["projected_score"] == 88


def test_integrated_project_is_part_of_the_61_90_day_phase():
    from src.learning_path_service import build_learning_path

    path = build_learning_path(_gaps(), ["Java"], current_score=60)
    final_phase = next(phase for phase in path["phases"] if phase["period"] == "61-90")

    assert final_phase["project"] == path["project"]
