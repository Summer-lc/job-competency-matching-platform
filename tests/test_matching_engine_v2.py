import pytest


def _profile(**overrides):
    profile = {
        "name": "高级Java工程师",
        "level": "senior",
        "required_skills": ["Java", "MySQL", "Kubernetes"],
        "preferred_skills": ["Docker"],
        "skills": [
            {"name": "Java", "requirement_type": "required", "proficiency_level": "advanced", "prevalence": 0.9},
            {"name": "MySQL", "requirement_type": "required", "proficiency_level": "working", "prevalence": 0.8},
            {"name": "Kubernetes", "requirement_type": "required", "proficiency_level": "working", "prevalence": 0.7},
            {"name": "Docker", "requirement_type": "preferred", "proficiency_level": "working", "prevalence": 0.5},
        ],
        "responsibilities": ["订单平台开发", "服务部署"],
        "industry_scenarios": ["电商"],
        "confidence": 0.9,
        "sample_status": "ready",
    }
    profile.update(overrides)
    return profile


def _resume(**overrides):
    resume = {
        "skills": [
            {"name": "Java", "proficiency": "advanced", "last_used_at": "2026-06", "evidence_sources": ["project"]},
            {"name": "MySQL", "proficiency": "working", "last_used_at": "2026-06", "evidence_sources": ["project"]},
            {"name": "K8s", "proficiency": "working", "last_used_at": "2026-06", "evidence_sources": ["work"]},
            {"name": "Docker", "proficiency": "working", "last_used_at": "2026-06", "evidence_sources": ["project"]},
        ],
        "recent_skills": ["Java", "MySQL", "K8s", "Docker"],
        "experience_years": 6,
        "project_experiences": [
            {
                "name": "电商订单平台",
                "skills": ["Java", "MySQL", "Docker"],
                "responsibilities": ["订单平台开发", "服务部署"],
                "achievements": ["接口延迟降低30%"],
                "industry_scenario": "电商",
            }
        ],
        "work_experiences": [],
        "projects": ["电商订单平台"],
    }
    resume.update(overrides)
    return resume


def test_v2_scores_seven_dimensions_and_preserves_total():
    from src.matching_service import MATCHING_WEIGHTS, SCORING_VERSION, match_resume_to_job

    result = match_resume_to_job(_resume(), _profile())

    assert SCORING_VERSION == "evidence-match-v2"
    assert set(result["dimensions"]) == set(MATCHING_WEIGHTS)
    assert sum(item["score"] for item in result["dimensions"].values()) == result["total_score"]
    assert result["dimension_scores"] == {
        name: item["score"] for name, item in result["dimensions"].items()
    }
    assert result["match_band"] == "high"
    assert result["scoring_version"] == SCORING_VERSION


def test_missing_majority_of_required_skills_caps_score_below_medium():
    from src.matching_service import match_resume_to_job

    result = match_resume_to_job(
        _resume(
            skills=[{"name": "Docker", "proficiency": "advanced", "evidence_sources": ["project"]}],
            recent_skills=["Docker"],
            experience_years=10,
        ),
        _profile(),
    )

    assert result["total_score"] <= 59
    assert result["match_band"] == "low"
    assert "required_coverage_below_half" in result["score_caps"]


def test_missing_high_prevalence_core_skill_caps_score_below_high():
    from src.matching_service import match_resume_to_job

    result = match_resume_to_job(
        _resume(skills=[{"name": "Java"}, {"name": "Kubernetes"}, {"name": "Docker"}]),
        _profile(),
    )

    assert result["total_score"] <= 79
    assert "missing_core_required_skill" in result["score_caps"]


def test_related_skill_receives_partial_credit_but_remains_a_gap():
    from src.matching_service import match_resume_to_job

    result = match_resume_to_job(
        _resume(
            skills=[{"name": "PostgreSQL", "proficiency": "advanced", "evidence_sources": ["project"]}],
            recent_skills=["PostgreSQL"],
            experience_years=3,
            project_experiences=[],
            projects=[],
        ),
        _profile(
            level="mid",
            required_skills=["MySQL"],
            preferred_skills=[],
            skills=[{"name": "MySQL", "requirement_type": "required", "prevalence": 0.4}],
            responsibilities=[],
            industry_scenarios=[],
        ),
    )

    assert 0 < result["dimensions"]["required_skill_coverage"]["score"] < 30
    assert result["missing_required_skills"] == ["MySQL"]
    assert result["transferable_skills"] == [
        {"candidate_skill": "PostgreSQL", "target_skill": "MySQL", "relationship": "related"}
    ]


def test_project_usage_scores_higher_than_skill_list_only():
    from src.matching_service import match_resume_to_job

    evidenced = match_resume_to_job(_resume(), _profile())
    skill_list_only = match_resume_to_job(
        _resume(
            skills=[{"name": "Java"}, {"name": "MySQL"}, {"name": "Kubernetes"}, {"name": "Docker"}],
            project_experiences=[],
            work_experiences=[],
            projects=[],
        ),
        _profile(),
    )

    assert evidenced["dimensions"]["project_evidence"]["score"] > skill_list_only["dimensions"]["project_evidence"]["score"]
    assert skill_list_only["confidence"] != "high"


def test_profile_level_supplies_required_experience_when_years_are_missing():
    from src.matching_service import match_resume_to_job

    result = match_resume_to_job(_resume(experience_years=2), _profile(required_years=0))

    dimension = result["dimensions"]["experience_level"]
    assert dimension["score"] < dimension["max_score"]
    assert any("5" in gap for gap in dimension["gaps"])


def test_matching_is_deterministic_and_ignores_personal_attributes():
    from src.matching_service import match_resume_to_job

    first = match_resume_to_job(_resume(name="甲", gender="男", age=28), _profile())
    second = match_resume_to_job(_resume(name="乙", gender="女", age=45), _profile())

    assert first == second


def test_matching_returns_phased_learning_plan_and_legacy_flat_nodes():
    from src.matching_service import match_resume_to_job

    result = match_resume_to_job(
        _resume(
            skills=[{"name": "Java", "proficiency": "working"}],
            recent_skills=["Java"],
            project_experiences=[],
            projects=[],
        ),
        _profile(),
    )

    assert result["learning_plan"]["version"] == "learning-path-v2"
    assert [phase["period"] for phase in result["learning_plan"]["phases"]] == [
        "0-30",
        "31-60",
        "61-90",
    ]
    assert result["learning_path"]
    assert all("skill" in item and "suggestion" in item for item in result["learning_path"])
    assert result["learning_plan"]["projected_score"] >= result["total_score"]


def test_skill_recency_uses_analysis_date_instead_of_newest_resume_date():
    from src.matching_service import match_resume_to_job

    resume = {
        "reference_date": "2026-07-23",
        "skills": [{"name": "Java", "last_used_at": "2010-01"}],
        "recent_skills": [],
    }

    result = match_resume_to_job(resume, {"required_skills": ["Java"]})

    assert result["dimensions"]["skill_recency"]["score"] == 0


def test_underqualified_owned_skill_is_added_to_learning_plan():
    from src.matching_service import match_resume_to_job

    resume = {"skills": [{"name": "Java", "proficiency": "aware"}]}
    job = {
        "required_skills": ["Java"],
        "skills": [
            {
                "name": "Java",
                "requirement_type": "required",
                "proficiency_level": "expert",
                "prevalence": 0.9,
            }
        ],
    }

    result = match_resume_to_job(resume, job)
    nodes = [node for phase in result["learning_plan"]["phases"] for node in phase["nodes"]]

    assert any(node["skill"] == "Java" and "熟练度" in node["reason"] for node in nodes)


def test_empty_job_profile_does_not_produce_a_high_match():
    from src.matching_service import match_resume_to_job

    result = match_resume_to_job({}, {"name": "空画像"})

    assert result["total_score"] == 0
    assert result["match_band"] == "low"
    assert "insufficient_job_profile" in result["score_caps"]


def test_capped_dimensions_keep_ratio_consistent_with_score():
    from src.matching_service import match_resume_to_job

    present = ["Java", "MySQL", "Redis"]
    targets = [*present, "Kubernetes"]
    result = match_resume_to_job(
        {
            "skills": [
                {"name": name, "proficiency": "working", "evidence_sources": ["project"]}
                for name in present
            ],
            "recent_skills": present,
            "project_experiences": [{"skills": present}],
        },
        {
            "required_skills": targets,
            "skills": [
                {
                    "name": name,
                    "requirement_type": "required",
                    "proficiency_level": "working",
                    "prevalence": 0.9,
                }
                for name in targets
            ],
        },
    )

    assert result["score_caps"]
    for dimension in result["dimensions"].values():
        assert dimension["ratio"] == pytest.approx(
            dimension["score"] / dimension["max_score"], abs=0.001
        )


def test_unrelated_project_achievement_does_not_boost_required_skill_evidence():
    from src.matching_service import match_resume_to_job

    base = {
        "skills": [{"name": "Java"}],
        "project_experiences": [
            {"skills": ["Python"], "achievements": []}
        ],
    }
    with_unrelated_achievement = {
        **base,
        "project_experiences": [
            {"skills": ["Python"], "achievements": ["Python任务吞吐提升30%"]}
        ],
    }
    job = {"required_skills": ["Java"]}

    without = match_resume_to_job(base, job)["dimensions"]["project_evidence"]["score"]
    with_unrelated = match_resume_to_job(with_unrelated_achievement, job)["dimensions"][
        "project_evidence"
    ]["score"]

    assert with_unrelated == without
