# 快速开始

> 新成员建议先阅读 [项目总览与完成情况](docs/PROJECT_OVERVIEW.md)。架构、输入输出与环境配置分别见 [系统架构与代码运行流程](docs/ARCHITECTURE_AND_FLOW.md)、[输入输出、模型与算法](docs/INPUT_OUTPUT_AND_ALGORITHMS.md) 和 [环境配置、运行与部署](docs/ENVIRONMENT_AND_DEPLOYMENT.md)。

## 本地运行

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python -m uvicorn src.api:app --reload --port 8000
```

访问 `http://127.0.0.1:8000`。

## 使用现有数据构建知识库

在 `langchain_deepseek` 目录运行：

```powershell
python -m src.build_knowledge_base ..\jd_raw.json
```

命令会生成 SQLite 知识库、批次质量报告和图谱 JSON。后续采集新文件时重复执行该命令即可增量更新；同一文件重复执行会返回原批次，不产生空画像版本。

## 推荐演示顺序

1. 在“数据治理”查看首批导入批次和异常隔离，或上传新的 JSONL/JSON/CSV。
2. 导入职业标准、官方技术文档和趋势证据。
3. 增量上传会自动更新受影响岗位族；也可在“数据治理”运行质量与画像更新。
4. 按岗位层级和季度查看岗位能力演化与全景图谱。
5. 上传 PDF 或 Word 简历，默认使用本地规则解析；需要模型补充时明确勾选模型增强，并检查能力档案及原文证据。
6. 查看 Top 5 岗位推荐，选择岗位进行七维诊断和 30/60/90 天学习规划。
7. 在“人工审核”确认候选岗位或低置信结果。
8. 在“模型质量评测”上传 `data/benchmark/benchmark-example.jsonl`，验证自动评测流程。
9. 使用可追溯的独立标注集检查三项 Accuracy 和系统验收状态。

只读检查或安全全量重建：

```powershell
python -m src.rebuild_hard_metrics --dry-run
python -m src.rebuild_hard_metrics --full --confirm
```

全量重建会先在 `data/backups` 创建数据库备份。

已确认授权的旧智联批次需要显式登记来源时，先审计后应用：

```powershell
python -m src.rebuild_hard_metrics --dry-run --repair-audit --repair-run-id legacy-zhaopin-auth-audit-20260812 --authorize-legacy-zhaopin --authorization-note "团队于2026-08-12确认jd_raw.json在允许范围内采集并授权用于项目研究。"
python -m src.rebuild_hard_metrics --full --repair --confirm --repair-run-id legacy-zhaopin-auth-20260812 --authorize-legacy-zhaopin --authorization-note "团队于2026-08-12确认jd_raw.json在允许范围内采集并授权用于项目研究。"
```

该命令只修复本地历史记录的来源与观测时间证据，不会访问文件中的岗位网址。

## 企业公开岗位更新

中国企业公开招聘门户必须先做只读预检。系统包含飞书招聘适配器和北森招聘适配器，Moka 不纳入自动采集；全流程不使用登录、Cookie、验证码、签名、解密或代理，商业招聘网站保持授权文件导入。

```powershell
python -m src.preflight_company_ats --candidates config/company_ats_candidates.json --output data/company_ats_preflight-<date>.json --max-sources 47 --delay-seconds 3
python -m src.collect_jobs --dry-run --source company_beisen_dreame --run-id company-ats-smoke-<date>-001 --max-records 20 --max-pages 4 --max-requests 20
python -m src.collect_jobs --dry-run --source company_beisen_dreame --run-id company-ats-prod-<date>-001 --max-records 1000 --max-pages 80 --max-requests 100
python -m src.collect_jobs --commit --resume-run company-ats-prod-<date>-001 --confirm
```

5000 条指通过质量门禁且语义去重后的有效岗位。预检报告、待复核或隔离记录不能算入该数量。

## Neo4j

本地系统不依赖 Neo4j 也能运行。需要图数据库时：

```powershell
docker compose up -d neo4j
```

全国平台授权岗位文件的目录、字段和分批命令见 [全国授权岗位数据接入说明](docs/NATIONAL_AUTHORIZED_DATA_INTAKE.md)。导入后使用 `python -m src.rebuild_hard_metrics --full --confirm --after-collection-run <run-id>` 更新全部派生结果。

配置 `.env` 中的 `NEO4J_PASSWORD`，然后调用 `POST /api/graph/sync`。

Neo4j 不可用时，`GET /api/graph` 和 `data/exports/knowledge-graph.json` 仍可正常使用；关系型知识库是唯一事实来源。
