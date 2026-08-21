from datetime import date

import pytest


def _skill(profile: dict, name: str) -> dict:
    return next(item for item in profile["skills"] if item["name"] == name)


def test_profile_v2_parses_sections_and_overlapping_work_months_once():
    from src.resume_service import parse_resume_text

    profile = parse_resume_text(
        """
个人简介：Java 后端工程师
专业技能
Java、Spring Boot、MySQL
工作经历
2020.01-2022.12 甲公司 | Java工程师
负责 Java 服务开发与 MySQL 数据库设计。
2022年07月-2024年06月 乙公司 | 高级开发工程师
使用 Spring Boot 建设交易平台。
项目经历：交易平台
2023-01-2024-06
负责核心接口开发。
教育经历
2016.09-2020.06 本科
证书：软件设计师
""",
        reference_date=date(2024, 6, 30),
    )

    assert profile["schema_version"] == "resume-profile-v2"
    assert profile["parser_mode"] == "rules"
    assert profile["experience_months"] == 54
    assert profile["experience_years"] == 4.5
    assert set(profile["sections"]) == {
        "basic",
        "skills",
        "work",
        "projects",
        "education",
        "certificates",
    }
    assert len(profile["work_experiences"]) == 2
    assert profile["work_experiences"][0]["company"] == "甲公司"
    assert profile["work_experiences"][0]["role"] == "Java工程师"
    assert profile["work_experiences"][0]["start_date"] == "2020-01"
    assert profile["work_experiences"][0]["end_date"] == "2022-12"
    assert profile["work_experiences"][0]["duration_months"] == 36
    assert profile["work_experiences"][0]["responsibilities"]
    assert profile["projects"]
    assert profile["education"] == ["本科"]


def test_education_four_years_is_not_work_experience():
    from src.resume_service import parse_resume_text

    profile = parse_resume_text("教育经历：本科四年，计算机科学与技术\n项目经历：项目周期2年")

    assert profile["experience_months"] == 0
    assert profile["experience_years"] == 0


def test_explicit_work_year_phrase_is_used_only_without_dated_timeline():
    from src.resume_service import parse_resume_text

    profile = parse_resume_text("个人简介：4年Java开发经验\n专业技能：Java、MySQL")

    assert profile["experience_months"] == 48
    assert profile["experience_years"] == 4
    assert profile["work_experiences"] == []


def test_project_evidence_is_stronger_than_skill_list_only_evidence():
    from src.resume_service import parse_resume_text

    profile = parse_resume_text(
        """
专业技能：Java、Python
项目经历：支付平台
2024.01-2024.06
使用 Java 和 MySQL 实现支付服务。
""",
        reference_date=date(2024, 6, 30),
    )

    java = _skill(profile, "Java")
    python = _skill(profile, "Python")
    assert java["confidence"] > python["confidence"]
    assert java["evidence_text"] == "使用 Java 和 MySQL 实现支付服务。"
    assert java["evidence_sources"] == ["project", "skill"]
    assert any(item["source"] == "project" for item in java["evidence"])
    assert java["aliases"] == ["Java"]
    assert java["last_used_at"] == "2024-06"


def test_negated_planned_and_learning_only_skills_are_excluded():
    from src.resume_service import parse_resume_text

    profile = parse_resume_text(
        "专业技能\n熟练使用Java\n不会Kubernetes\n计划学习Kafka\n正在学习Go"
    )

    assert {item["name"] for item in profile["skills"]} == {"Java"}


def test_multi_skill_exclusion_is_scoped_to_each_clause():
    from src.resume_service import parse_resume_text

    excluded = parse_resume_text(
        "专业技能：不会 Kubernetes 和 Kafka；计划学习 Go 和 Python"
    )
    mixed = parse_resume_text(
        "专业技能：不会 Kubernetes 和 Kafka；计划学习 Go 和 Python；"
        "后续项目熟练使用 Kafka 和 Python"
    )

    assert excluded["skills"] == []
    assert {item["name"] for item in mixed["skills"]} == {"Kafka", "Python"}


def test_english_exclusions_apply_to_whole_clause_and_allow_later_use():
    from src.resume_service import parse_resume_text

    excluded = parse_resume_text(
        "Skills: Never used Java and Python; No experience with Kubernetes and Docker; "
        "Unfamiliar with Kafka and Go"
    )
    mixed = parse_resume_text(
        "Skills: Never used Java and Python; No experience with Kubernetes and Docker; "
        "Unfamiliar with Kafka and Go; Later used Java and Kubernetes in production"
    )

    assert excluded["skills"] == []
    assert {item["name"] for item in mixed["skills"]} == {"Java", "Kubernetes"}


def test_quantified_project_result_is_kept_as_an_achievement():
    from src.resume_service import parse_resume_text

    profile = parse_resume_text(
        """
项目经历
订单平台 | 后端开发 | 2023.01-2023.12
负责 Java 核心接口开发。
通过 Redis 缓存将接口耗时降低40%，吞吐量提升2倍。
行业场景：电商
"""
    )

    project = profile["project_experiences"][0]
    assert project["name"] == "订单平台"
    assert project["role"] == "后端开发"
    assert project["start_date"] == "2023-01"
    assert project["end_date"] == "2023-12"
    assert project["achievements"] == ["通过 Redis 缓存将接口耗时降低40%，吞吐量提升2倍。"]
    assert project["industry_scenario"] == "电商"
    assert set(project["skills"]) == {"Java", "Redis"}


def test_unitless_qps_result_is_an_achievement_but_dates_and_phone_are_not():
    from src.resume_service import parse_resume_text

    profile = parse_resume_text(
        """
项目经历
网关优化 | 后端开发 | 2024.01-2024.06
QPS提升至3000
项目日期：2024.01-2024.06
联系电话：13800138000
"""
    )

    assert len(profile["project_experiences"]) == 1
    assert [item["name"] for item in profile["project_experiences"]] == ["网关优化"]
    assert profile["project_experiences"][0]["achievements"] == ["QPS提升至3000"]


def test_numeric_activity_is_responsibility_without_result_semantics():
    from src.resume_service import parse_resume_text

    profile = parse_resume_text(
        """
项目经历
缓存改造 | 2024.01-2024.06
使用 Redis 优化 3 个接口
QPS提升至3000
延迟降低30%
吞吐提升2倍
"""
    )

    project = profile["project_experiences"][0]
    assert project["responsibilities"] == ["使用 Redis 优化 3 个接口"]
    assert project["achievements"] == ["QPS提升至3000", "延迟降低30%", "吞吐提升2倍"]


def test_labelled_project_dates_update_one_titled_project():
    from src.resume_service import parse_resume_text

    profile = parse_resume_text(
        """
项目经历
支付平台
项目周期：2024.01-2024.03
负责 Java 支付服务开发
项目日期：2024.01-2024.06
技术栈：Java、MySQL
"""
    )

    assert len(profile["project_experiences"]) == 1
    project = profile["project_experiences"][0]
    assert project["name"] == "支付平台"
    assert project["start_date"] == "2024-01"
    assert project["end_date"] == "2024-06"
    assert project["responsibilities"] == ["负责 Java 支付服务开发", "技术栈：Java、MySQL"]


def test_recent_skills_use_dated_work_and_project_evidence():
    from src.resume_service import parse_resume_text

    profile = parse_resume_text(
        """
工作经历
2019.01-2020.12 甲公司 | Python工程师
使用 Python 开发后台服务。
项目经历
近期平台 | 2023.07-2024.06
使用 Java 和 Spring Boot 开发平台。
专业技能：Docker
""",
        reference_date=date(2024, 6, 30),
    )

    assert profile["recent_skills"] == ["Java", "Spring Boot"]


def test_undated_resume_keeps_compatible_recent_skill_fallback():
    from src.resume_service import parse_resume_text

    profile = parse_resume_text("专业技能：Java、MySQL、Docker")

    assert profile["recent_skills"] == ["Docker", "Java", "MySQL"]
    assert all(item["proficiency"] in {"aware", "working", "advanced", "expert"} for item in profile["skills"])
    assert isinstance(profile["parse_warnings"], list)


def test_missing_project_identity_is_not_invented_from_responsibility_text():
    from src.resume_service import parse_resume_text

    profile = parse_resume_text("项目经历：负责使用 Java 开发后台服务。")

    project = profile["project_experiences"][0]
    assert project["name"] is None
    assert project["role"] is None
    assert project["start_date"] is None
    assert project["end_date"] is None
    assert project["responsibilities"] == ["负责使用 Java 开发后台服务。"]


def test_undated_work_item_keeps_discoverable_identity_without_duration():
    from src.resume_service import parse_resume_text

    profile = parse_resume_text(
        "工作经历\n甲公司 | Java工程师\n负责 Java 后台服务开发。"
    )

    work = profile["work_experiences"][0]
    assert work["company"] == "甲公司"
    assert work["role"] == "Java工程师"
    assert work["start_date"] is None
    assert work["end_date"] is None
    assert work["duration_months"] is None
    assert work["responsibilities"] == ["负责 Java 后台服务开发。"]
    assert work["skills"] == ["Java"]


def test_work_date_header_and_detail_lines_stay_in_one_record():
    from src.resume_service import parse_resume_text

    profile = parse_resume_text(
        """
工作经历
2024.01-2024.06
甲公司 | Java工程师
负责公司支付平台开发
技术栈 | Java | MySQL
""",
        reference_date=date(2024, 6, 30),
    )

    assert len(profile["work_experiences"]) == 1
    work = profile["work_experiences"][0]
    assert work["company"] == "甲公司"
    assert work["role"] == "Java工程师"
    assert work["start_date"] == "2024-01"
    assert work["end_date"] == "2024-06"
    assert work["responsibilities"] == ["负责公司支付平台开发", "技术栈 | Java | MySQL"]
    assert work["skills"] == ["Java", "MySQL"]
    assert profile["recent_skills"] == ["Java", "MySQL"]


@pytest.mark.parametrize("filename", ["resume.txt", "resume.md"])
def test_text_byte_parsing_forwards_reference_date_for_present_work(filename):
    from src.resume_service import parse_resume_bytes

    raw = (
        "工作经历\n"
        "2024.01-至今 甲公司 | Java工程师\n"
        "负责 Java 后台服务开发。"
    ).encode("utf-8")

    profile = parse_resume_bytes(
        raw,
        filename,
        reference_date=date(2025, 3, 20),
    )

    assert profile["schema_version"] == "resume-profile-v2"
    assert profile["parser_mode"] == "rules"
    assert profile["experience_months"] == 15
    assert profile["work_experiences"][0]["end_date"] == "2025-03"
    assert profile["filename"] == filename


def test_markdown_byte_parsing_recognizes_heading_markers():
    from src.resume_service import parse_resume_bytes

    raw_text = """# 候选人简历
## 工作经历
2024.01-至今
甲公司 | Java工程师
负责 Java 服务开发
## 项目经历
支付平台 | 2024.03-2024.12
使用 Spring Boot 实现支付服务
"""

    profile = parse_resume_bytes(
        raw_text.encode("utf-8"),
        "resume.md",
        reference_date=date(2025, 3, 20),
    )

    assert len(profile["work_experiences"]) == 1
    assert profile["work_experiences"][0]["company"] == "甲公司"
    assert profile["work_experiences"][0]["end_date"] == "2025-03"
    assert len(profile["project_experiences"]) == 1
    assert profile["project_experiences"][0]["name"] == "支付平台"


def test_skill_evidence_preserves_verbatim_bullets_and_numbering():
    from src.resume_service import parse_resume_text

    source = """专业技能
- 熟练使用 Java
工作经历
2024.01-2024.06
甲公司 | 后端工程师
1. 使用 Spring Boot 开发服务
项目经历
支付平台 | 2024.03-2024.05
• 使用 Redis 缓存数据
"""

    profile = parse_resume_text(source, reference_date=date(2024, 6, 30))

    assert _skill(profile, "Java")["evidence_text"] == "- 熟练使用 Java"
    assert _skill(profile, "Spring Boot")["evidence_text"] == "1. 使用 Spring Boot 开发服务"
    assert _skill(profile, "Redis")["evidence_text"] == "• 使用 Redis 缓存数据"
    for skill in profile["skills"]:
        assert skill["evidence_text"] in source
        assert all(item["text"] in source for item in skill["evidence"])
    assert profile["work_experiences"][0]["evidence_text"] in source
    assert profile["project_experiences"][0]["evidence_text"] in source
