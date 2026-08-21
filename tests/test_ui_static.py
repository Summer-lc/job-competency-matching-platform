import re
from pathlib import Path


INDEX = Path(__file__).resolve().parents[1] / "index.html"


def test_left_navigation_keeps_labels_without_numeric_prefixes():
    html = INDEX.read_text(encoding="utf-8")
    buttons = re.findall(
        r'<button class="nav-btn(?: active)?" data-target="([^"]+)">(.*?)</button>',
        html,
    )

    assert [target for target, _ in buttons] == [
        "dashboard",
        "governance",
        "discovery",
        "evolution",
        "graph",
        "knowledge",
        "matching",
        "reviews",
        "evaluation",
    ]
    assert [re.sub(r"<[^>]+>", "", label).strip() for _, label in buttons] == [
        "系统总览",
        "数据治理",
        "岗位发现",
        "能力演化",
        "全景图谱",
        "知识问答",
        "人岗匹配",
        "人工审核",
        "模型质量评测",
    ]
    assert 'class="nav-icon"' not in html
    assert 'class="aside-foot"' not in html


def test_operational_data_metrics_are_visible_without_internal_goal_copy():
    html = INDEX.read_text(encoding="utf-8")

    required = {
        'id="hard-metrics-run"',
        'id="hard-metrics-history"',
        'id="quality-gate-stats"',
        'id="evolution-level"',
        'id="evolution-quarter"',
        'id="system-acceptance"',
        "数据健康",
        "来源结构",
        "岗位族覆盖",
        "最近采集",
        "可用岗位",
        "来源域名",
        "来源域名完整度",
        "域名完整记录",
        "缺少域名",
        "可信时间",
        "可信或可观测时间",
        "核心技能全部经多源确认",
        "可用于趋势分析",
    }
    assert not [item for item in required if item not in html]

    prohibited = (
        "XH-202621",
        "国奖",
        "奖金",
        "参赛就绪",
        "提交截止",
        "赛题要求",
        "比赛规则",
        "竞赛",
        "奖项",
        "奖励",
        "获奖",
        "内部数据目标",
        "内部质量规则",
        "系统质量门槛",
        "规则版本",
        "统一规则",
        "证据规则",
        "competition_ready",
        "5000",
        "7000",
    )
    assert not [item for item in prohibited if item in html]

    acceptance_rows = re.search(
        r"function acceptanceRows\(metrics\)\{(.*?)\n\s*function acceptancePercent",
        html,
        re.DOTALL,
    )
    load_acceptance = re.search(
        r"async function loadAcceptance\(\)\{(.*?)\n\s*async function runEvaluation",
        html,
        re.DOTALL,
    )
    assert acceptance_rows and ".target" not in acceptance_rows.group(1)
    assert load_acceptance and "r.internal" not in load_acceptance.group(1)


def test_matching_ui_exposes_profile_recommendations_evidence_and_phased_path():
    html = INDEX.read_text(encoding="utf-8")

    required = {
        "简历能力档案",
        "Top 5 岗位推荐",
        "匹配证据",
        "0-30天",
        "31-60天",
        "61-90天",
        'id="job-recommendations"',
        'id="match-evidence"',
        "/api/matches/recommend",
        "loadJobRecommendations",
        "renderResumeProfile",
        "renderLearningPlan",
        'id="resume-model-enrichment"',
        "start_date",
        "evidence_text",
        "family_code",
        "period_key",
        "sample_status",
    }
    assert not [item for item in required if item not in html]

    prohibited = ("XH-202621", "国奖", "比赛匹配标准", "获奖概率")
    assert not [item for item in prohibited if item in html]


def test_evaluation_ui_exposes_recommendation_ranking_metrics():
    html = INDEX.read_text(encoding="utf-8")

    required = {"四类任务", "Top-1", "Recall@5", "MRR", "NDCG@5"}
    assert not [item for item in required if item not in html]
