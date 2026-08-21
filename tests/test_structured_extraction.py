def test_grounded_extraction_rejects_skill_without_source_evidence():
    from src.structured_extraction import validate_grounded_extraction

    source = "负责Java微服务开发，要求熟悉Spring Boot和MySQL。"
    payload = {
        "responsibilities": ["负责Java微服务开发"],
        "required_skills": [
            {"name": "Java", "evidence": "Java微服务开发"},
            {"name": "Rust", "evidence": "精通Rust"},
        ],
        "preferred_skills": [],
        "industry_scenarios": ["企业软件开发"],
    }
    result = validate_grounded_extraction(payload, source)
    assert [item["name"] for item in result["required_skills"]] == ["Java"]
    assert result["rejected_skills"] == [
        {"name": "Rust", "reason": "evidence_not_found", "evidence": "精通Rust"}
    ]
    assert result["hallucination_risk"] > 0


def test_grounded_extraction_requires_structured_fields():
    from src.structured_extraction import validate_grounded_extraction

    result = validate_grounded_extraction({"required_skills": []}, "Java")
    assert result["responsibilities"] == []
    assert result["preferred_skills"] == []
    assert result["industry_scenarios"] == []

