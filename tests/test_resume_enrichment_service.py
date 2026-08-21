import json


def _rule_profile():
    return {
        "schema_version": "resume-profile-v2",
        "parser_mode": "rules",
        "skills": [
            {
                "name": "Java",
                "category": "language",
                "aliases": ["Java"],
                "proficiency": "working",
                "confidence": 0.93,
                "evidence_text": "使用Java开发订单服务",
                "evidence_sources": ["project"],
                "evidence": [
                    {
                        "text": "使用Java开发订单服务",
                        "source": "project",
                        "strength": 0.93,
                        "used_at": None,
                    }
                ],
                "last_used_at": None,
            }
        ],
        "project_experiences": [],
        "parse_warnings": [],
    }


def test_validation_rejects_non_verbatim_skill_and_achievement_evidence():
    from src.resume_enrichment_service import validate_resume_enrichment

    source = "负责订单服务，将接口延迟降低30%，使用Java和Redis。"
    result = validate_resume_enrichment(
        {
            "skills": [
                {"name": "Redis", "evidence": "使用Java和Redis"},
                {"name": "Kafka", "evidence": "使用Kafka"},
            ],
            "achievements": [
                {"text": "接口延迟降低30%", "evidence": "将接口延迟降低30%"},
                {"text": "吞吐提升一倍", "evidence": "吞吐提升一倍"},
            ],
        },
        source,
    )

    assert [item["name"] for item in result["skills"]] == ["Redis"]
    assert [item["text"] for item in result["achievements"]] == ["接口延迟降低30%"]
    assert {item["reason"] for item in result["rejected"]} == {"evidence_not_found"}


def test_validation_rejects_unsupported_and_low_confidence_model_claims():
    from src.resume_enrichment_service import validate_resume_enrichment

    source = "熟练使用Java开发订单服务。"
    result = validate_resume_enrichment(
        {
            "skills": [
                {"name": "Kafka", "confidence": 0.99, "evidence": source},
                {"name": "Java", "confidence": 0.1, "evidence": source},
            ],
            "achievements": [
                {"text": "吞吐提升999%", "confidence": 0.99, "evidence": source}
            ],
        },
        source,
    )

    assert result["skills"] == []
    assert result["achievements"] == []
    assert {item["reason"] for item in result["rejected"]} == {
        "skill_not_supported_by_evidence",
        "confidence_below_threshold",
        "achievement_not_verbatim",
    }


def test_enrichment_merges_aliases_without_duplicating_existing_skill():
    from src.resume_enrichment_service import enrich_resume_profile

    payload = {
        "skills": [
            {
                "name": "SpringBoot",
                "proficiency": "advanced",
                "evidence": "通过SpringBoot建设订单服务",
            },
            {"name": "Java", "proficiency": "advanced", "evidence": "使用Java开发订单服务"},
        ],
        "achievements": [],
    }
    result = enrich_resume_profile(
        "使用Java开发订单服务，通过SpringBoot建设订单服务。",
        _rule_profile(),
        invoke=lambda _: json.dumps(payload, ensure_ascii=False),
    )

    assert result["parser_mode"] == "hybrid"
    assert [item["name"] for item in result["skills"]].count("Java") == 1
    spring = next(item for item in result["skills"] if item["name"] == "Spring Boot")
    assert spring["aliases"] == ["SpringBoot"]
    assert spring["evidence"][0]["text"] == "通过SpringBoot建设订单服务"
    assert result["evidence_count"] == 3


def test_model_failure_returns_rule_profile_with_stable_warning():
    from src.resume_enrichment_service import enrich_resume_profile

    def fail(_):
        raise TimeoutError("offline")

    profile = _rule_profile()
    result = enrich_resume_profile("使用Java开发订单服务", profile, invoke=fail)

    assert result is not profile
    assert result["skills"] == profile["skills"]
    assert result["parser_mode"] == "rules"
    assert result["parse_warnings"] == ["model_unavailable"]


def test_invalid_json_falls_back_without_leaking_model_text():
    from src.resume_enrichment_service import enrich_resume_profile

    result = enrich_resume_profile(
        "使用Java开发订单服务",
        _rule_profile(),
        invoke=lambda _: "Java、Kafka，候选人非常优秀",
    )

    assert result["parser_mode"] == "rules"
    assert [item["name"] for item in result["skills"]] == ["Java"]
    assert result["parse_warnings"] == ["model_invalid_response"]


def test_prompt_prohibits_personal_attributes_and_requires_verbatim_evidence():
    from src.resume_enrichment_service import build_resume_prompt

    prompt = build_resume_prompt("张三，使用Java开发订单服务", _rule_profile())

    assert "连续原文" in prompt
    assert "姓名" in prompt
    assert "不得" in prompt
    assert "匹配分数" in prompt
