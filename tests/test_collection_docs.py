from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOC_PATHS = (
    ROOT / "README.md",
    ROOT / "QUICKSTART.md",
    ROOT / "USER_GUIDE.md",
    ROOT / "data" / "collections" / "README.md",
    ROOT / "docs" / "NATIONAL_AUTHORIZED_DATA_INTAKE.md",
)


def test_source_registry_declares_domestic_only_job_market_boundaries() -> None:
    document = json.loads(
        (ROOT / "config" / "job_sources.json").read_text(encoding="utf-8")
    )
    sources = {item["source_id"]: item for item in document["sources"]}

    assert sources["ncss_public_jobs"]["market_scope"] == "china"
    assert sources["mohrss_public_jobs"]["market_scope"] == "china"
    assert sources["zhaopin_legacy_import"]["market_scope"] == "china"
    assert sources["company_official_manifest"]["market_scope"] == "excluded"
    assert sources["company_official_manifest"]["enabled"] is False
    assert sources["company_official_manifest"]["compliance_status"] == "blocked"
    assert sources["iguopin_public_jobs"]["market_scope"] == "pending_review"
    assert sources["iguopin_public_jobs"]["enabled"] is False


def _documents() -> dict[Path, str]:
    return {path: path.read_text(encoding="utf-8") for path in DOC_PATHS}


def test_collection_docs_cover_the_repeatable_safe_workflow() -> None:
    documents = _documents()
    combined = "\n".join(documents.values())

    required_commands = {
        "python -m src.collect_jobs --source ncss_public_jobs",
        "--max-records 20 --max-pages 2 --max-requests 50 --dry-run",
        "python -m src.collect_jobs --resume-run <run-id> --dry-run",
        "python -m src.collect_jobs --resume-run <run-id> --commit --confirm",
        "python -m src.rebuild_hard_metrics --dry-run --repair-audit "
        "--repair-run-id <repair-run-id>",
        "python -m src.rebuild_hard_metrics --full --confirm --repair "
        "--repair-run-id <repair-run-id>",
        "python -m src.rebuild_hard_metrics --full --confirm",
    }
    for command in required_commands:
        assert command in combined

    required_paths = {
        "config/job_sources.json",
        "data/collections/<run-id>/report.json",
        "data/collections/<run-id>/checkpoint.json",
        "data/collections/<run-id>/staged/jobs.jsonl",
        "data/collections/<run-id>/review/jobs.jsonl",
        "data/collections/<run-id>/quarantine/jobs.jsonl",
        "data/backups/job_competency-<timestamp>.db",
        "data/repairs/<repair-run-id>/report.json",
        "data/imports/<batch-id>-report.json",
        "data/exports/knowledge-graph.json",
    }
    for path in required_paths:
        assert path in combined

    for phrase in (
        "来源审批",
        "小规模 dry-run",
        "恢复中断",
        "扩采",
        "PRAGMA integrity_check",
        "回滚",
        '"enabled": false',
        "不得绕过",
        "自动来源",
        "人工 manifest",
        "系统保持停止",
        "验收",
        "知识库",
        "图谱",
        "保留",
        "运行编号",
        "修复应用已包含一次完整硬指标管线",
        "结果应保持幂等",
    ):
        assert phrase in combined

    for option in (
        "--database-url",
        "--backup-dir",
        "--repairs-root",
        "--locks-root",
    ):
        assert option in combined


def test_collection_docs_state_data_boundaries_and_exit_codes() -> None:
    combined = "\n".join(_documents().values())

    assert "合成简历仅用于测试" in combined
    assert "任何合成或生成岗位都不得计入生产总量" in combined
    assert "manual_only" in combined
    assert "manual_url_manifest" in combined
    assert "401、403、429" in combined
    for code in ("退出码 `2`", "退出码 `3`", "退出码 `4`", "退出码 `5`"):
        assert code in combined


def test_rollback_and_service_stop_instructions_are_fail_closed() -> None:
    guide = _documents()[ROOT / "USER_GUIDE.md"]

    required = (
        '$ErrorActionPreference = "Stop"',
        "$commitBackupPath",
        "$repairBackupPath",
        "$rebuildBackupPath",
        "$rollbackBackupPath",
        "Resolve-Path -LiteralPath $rollbackBackupPath -ErrorAction Stop",
        "Get-FileHash -LiteralPath $resolvedRollbackBackup -Algorithm SHA256",
        "PRAGMA integrity_check",
        "job_competency.db-wal",
        "job_competency.db-shm",
        "job_competency.db-journal",
        "Move-Item -LiteralPath",
        "Copy-Item -LiteralPath $resolvedRollbackBackup",
        "docker compose down",
        "Get-NetTCPConnection -State Listen -ErrorAction Stop",
        "Where-Object { $_.LocalPort -eq 8000 }",
        "Stop-Process -Id $projectPid -ErrorAction Stop",
        "不得仅凭端口号结束进程",
        "最终保持停止",
        "归档中途失败",
    )
    for phrase in required:
        assert phrase in guide

    rollback = guide[guide.index("### 10. 报告保留与 SQLite 回滚") :]
    assert rollback.index('$ErrorActionPreference = "Stop"') < rollback.index(
        "Resolve-Path -LiteralPath $rollbackBackupPath"
    )
    assert rollback.index("Resolve-Path -LiteralPath $rollbackBackupPath") < (
        rollback.index("Move-Item -LiteralPath")
    )
    assert rollback.index("Move-Item -LiteralPath") < rollback.index(
        "Copy-Item -LiteralPath $resolvedRollbackBackup"
    )
    assert "SilentlyContinue" not in rollback


def test_backup_and_knowledge_semantics_are_unambiguous() -> None:
    combined = "\n".join(_documents().values())

    for phrase in (
        "提交和普通全量重建使用时间戳备份",
        "修复应用使用修复编号备份",
        "每次操作都单独记录",
        "不得继承旧变量",
        "完整硬指标管线会更新",
        "幂等导入分支不会更新知识片段",
        "仍会导出当前数据库图谱",
    ):
        assert phrase in combined


def test_docs_do_not_contain_prohibited_competition_details() -> None:
    prohibited = (
        "competition",
        "比赛代码",
        "赛题代码",
        "奖金",
        "奖项金额",
        "比赛规则",
        "竞赛规则",
        "评分规则",
    )
    for path, text in _documents().items():
        lowered = text.lower()
        for phrase in prohibited:
            assert phrase not in lowered, f"{path.relative_to(ROOT)} contains {phrase!r}"


def test_documented_command_combinations_match_current_argparse() -> None:
    from src.collect_jobs import build_parser as build_collect_parser
    from src.collect_jobs import validate_args as validate_collect_args
    from src.rebuild_hard_metrics import build_parser as build_rebuild_parser
    from src.rebuild_hard_metrics import validate_args as validate_rebuild_args

    collect_commands = (
        [
            "--source",
            "ncss_public_jobs",
            "--run-id",
            "collection-20260811-001",
            "--max-records",
            "20",
            "--max-pages",
            "2",
            "--max-requests",
            "50",
            "--dry-run",
        ],
        ["--resume-run", "collection-20260811-001", "--dry-run"],
        [
            "--resume-run",
            "collection-20260811-001",
            "--commit",
            "--confirm",
        ],
    )
    for argv in collect_commands:
        validate_collect_args(build_collect_parser().parse_args(argv))

    rebuild_commands = (
        ["--dry-run", "--repair-audit", "--repair-run-id", "repair-20260811-001"],
        [
            "--full",
            "--confirm",
            "--repair",
            "--repair-run-id",
            "repair-20260811-001",
        ],
        ["--full", "--confirm"],
    )
    for argv in rebuild_commands:
        validate_rebuild_args(build_rebuild_parser().parse_args(argv))


def test_docs_cover_explicit_legacy_zhaopin_authorization_repair() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "README.md", ROOT / "QUICKSTART.md", ROOT / "USER_GUIDE.md")
    )

    audit_command = (
        "python -m src.rebuild_hard_metrics --dry-run --repair-audit "
        "--repair-run-id legacy-zhaopin-auth-audit-20260812 "
        "--authorize-legacy-zhaopin --authorization-note"
    )
    apply_command = (
        "python -m src.rebuild_hard_metrics --full --repair --confirm "
        "--repair-run-id legacy-zhaopin-auth-20260812 "
        "--authorize-legacy-zhaopin --authorization-note"
    )
    assert audit_command in combined
    assert apply_command in combined
    assert "仅处理来源未登记且网址属于 `zhaopin.com` 的智联历史记录" in combined
    assert "不会从岗位链接发起联网请求" in combined


def test_docs_cover_public_company_ats_collection_boundaries() -> None:
    combined = "\n".join(_documents().values())

    for phrase in (
        "中国企业公开招聘门户",
        "飞书招聘适配器",
        "北森招聘适配器",
        "Moka 不纳入自动采集",
        "不使用登录、Cookie、验证码、签名、解密或代理",
        "商业招聘网站保持授权文件导入",
        "5000 条指通过质量门禁且语义去重后的有效岗位",
        "company_beisen_dreame",
        "company-ats-smoke-<date>-001",
        "company-ats-prod-<date>-001",
        "data/company_ats_preflight-<date>.json",
    ):
        assert phrase in combined


def test_docs_cover_national_authorized_export_workflow() -> None:
    combined = "\n".join(_documents().values())

    for phrase in (
        "config/authorized_job_sources.local.json",
        "data/incoming/authorized/boss_zhipin/jobs.jsonl",
        "data/incoming/authorized/job51/jobs.csv",
        "data/incoming/authorized/liepin/jobs.json",
        "data/incoming/authorized/lagou/jobs.jsonl",
        "data/incoming/authorized/newjobs/jobs.jsonl",
        "data/incoming/authorized/jobonline/jobs.jsonl",
        "python -m src.collect_jobs --coverage-report",
        "--authorization-preflight",
        "--authorization-manifest",
        "--after-collection-run",
        "5000 至 10000",
        "单一来源不超过 35%",
        "不得采集简历、聊天记录、联系人信息",
        "不得绕过验证码",
        "不得使用代理轮换",
        "不得逆向请求签名",
        "不得使用合成岗位填充",
    ):
        assert phrase in combined


def test_authorized_intake_docs_state_quantity_a_gates() -> None:
    text = (ROOT / "docs" / "NATIONAL_AUTHORIZED_DATA_INTAKE.md").read_text(
        encoding="utf-8"
    )

    for required in (
        "8,000 至 9,000",
        "可信发布日期",
        "2022 至 2026",
        "当前可信基线为 546 条，缺口为 4,454 条",
        "valid_candidate_count",
        "trusted_window_candidate_count",
        "合格率低于 55%",
        "每批不超过 1,000 条",
        "授权清单或导出文件缺失时不得提交",
        "每个提交批次后执行完整重建和覆盖率检查",
        "单一来源不超过 35%",
        "--record-offset",
    ):
        assert required in text


def test_fresh_reader_questions_have_direct_answers() -> None:
    combined = "\n".join(_documents().values())
    reader_questions = {
        "回滚哪一个精确备份？": "回滚本次提交、修复或重建中的哪一步",
        "如何确认服务已经停止？": "确认 8000 端口没有监听",
        "归档中途失败后怎么办？": "禁止复制备份",
        "幂等重跑 build_knowledge_base 会更新知识片段吗？": (
            "幂等导入分支不会更新知识片段"
        ),
    }
    for question, expected_answer in reader_questions.items():
        assert question in combined
        assert expected_answer in combined


def test_documented_local_links_and_python_modules_exist() -> None:
    missing_links: list[str] = []
    missing_modules: list[str] = []
    external_modules = {"pytest", "uvicorn", "venv"}

    for doc_path, text in _documents().items():
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
            target = target.strip("<>").split("#", 1)[0]
            if not target or re.match(r"^[a-z]+://", target):
                continue
            resolved = (doc_path.parent / target).resolve()
            if not resolved.exists():
                missing_links.append(f"{doc_path.relative_to(ROOT)} -> {target}")

        for module in re.findall(r"\bpython\s+-m\s+([A-Za-z_][\w.]*)", text):
            if module in external_modules:
                continue
            module_path = ROOT.joinpath(*module.split(".")).with_suffix(".py")
            package_path = ROOT.joinpath(*module.split("."), "__main__.py")
            if not module_path.is_file() and not package_path.is_file():
                missing_modules.append(f"{doc_path.relative_to(ROOT)} -> {module}")

    assert not missing_links, f"missing local documentation links: {missing_links}"
    assert not missing_modules, f"missing documented Python modules: {missing_modules}"
