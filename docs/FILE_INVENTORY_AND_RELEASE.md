# 项目文件清单与 GitHub 发布说明

本文说明项目中各类文件的用途，以及哪些内容会进入 GitHub。目标是让组员能够找到代码和资料，同时避免把密钥、生产数据库、原始批量数据或临时文件写入 Git 历史。

## 一、仓库内容总览

| 路径 | 内容与职责 | 是否上传 |
| --- | --- | --- |
| `src/` | FastAPI 接口、数据导入治理、岗位画像与演化、知识检索、简历解析、匹配推荐、评测及采集工具 | 是 |
| `model_class/` | SQLAlchemy 数据表模型，包括岗位、技能、画像、证据、简历、匹配、审核、采集批次和验收快照 | 是 |
| `schemes/` | API 输入数据的 Pydantic 模型 | 是 |
| `config/` | 岗位族规则、来源注册表、采集目标及数据库配置代码 | 是，但本地授权配置除外 |
| `tests/` | 单元测试、接口测试、采集安全测试和小型固定样例 | 是 |
| `index.html` | 无前端构建步骤的单页演示界面 | 是 |
| `Dockerfile`、`docker-compose.yml` | API 与可选 Neo4j 的容器部署配置 | 是 |
| `requirements.txt` | Python 依赖及版本范围 | 是 |
| `.env.example` | 无秘密值的环境变量模板 | 是 |
| `.env` | 本机 API 密钥和连接信息 | 否 |
| `README.md`、`QUICKSTART.md`、`USER_GUIDE.md` | 项目入口、快速开始和完整操作说明 | 是 |
| `docs/` | 项目状态、架构、算法、环境、发布说明及历史设计/实施记录 | 是 |

## 二、主要源码文件

### 接口与启动

| 文件 | 职责 |
| --- | --- |
| `src/api.py` | 创建 FastAPI 应用，注册页面、数据、分析、图谱、简历、匹配、审核和评测接口 |
| `src/main.py` | 本地开发启动入口，默认监听 `127.0.0.1:8000` |
| `src/app_config.py` | 读取 DeepSeek 与 Neo4j 环境变量 |
| `config/DB_config.py` | 创建 SQLAlchemy 异步引擎和数据库会话 |

### 数据导入与治理

| 文件 | 职责 |
| --- | --- |
| `src/import_service.py` | JSON/JSONL/CSV 批量导入、边界校验、来源规范化、修订和批次报告 |
| `src/job_data_service.py` | 文本标准化、SHA-256、SimHash、技能/职责规则抽取和质量评分 |
| `src/hard_metrics_pipeline.py` | 重复组、质量门禁、岗位级别、画像、演化、知识片段和验收快照的统一重建 |
| `src/rebuild_hard_metrics.py` | 只读检查、带备份的全量重建和数据修复命令行入口 |
| `src/build_knowledge_base.py` | 从本地岗位文件构建或增量更新知识库与图谱导出 |
| `src/job_data_repair.py` | 历史数据修复计划、审计报告和确认应用 |

### 岗位采集

| 路径 | 职责 |
| --- | --- |
| `src/collect_jobs.py` | 受控采集命令行入口，支持 dry-run、恢复和确认提交 |
| `src/job_collection/service.py` | 采集批次编排、分流、报告签名、提交和备份 |
| `src/job_collection/security.py` | 来源边界、响应限制、文件完整性和运行互斥保护 |
| `src/job_collection/adapters/` | 国家平台、公开企业 ATS、人工清单及授权文件适配器 |
| `src/job_collection/source_registry.py` | 读取并校验来源注册表 |

### 图谱、问答与动态演化

| 文件 | 职责 |
| --- | --- |
| `src/job_analysis_service.py` | 岗位发现分数、时间窗口技能统计和图谱数据聚合 |
| `src/quarterly_profile_service.py` | 按岗位族、层级和季度生成可复现岗位画像 |
| `src/evolution_service.py` | 比较相邻季度画像并形成有证据的演化事件 |
| `src/knowledge_service.py` | 知识片段构建、词法检索和可选向量检索 |
| `src/evidence_qa_service.py` | 证据聚合、引用校验、模型回答及无模型摘要降级 |
| `src/job_graph_sync.py` | 把关系型数据库导出的图谱同步到可选 Neo4j |

### 简历、匹配与评测

| 文件 | 职责 |
| --- | --- |
| `src/resume_service.py` | 读取 PDF、DOCX、TXT、Markdown，提取技能、时间线和原文证据 |
| `src/resume_enrichment_service.py` | 在用户明确启用时调用模型补充简历结构化结果 |
| `src/matching_service.py` | 七维人岗匹配、分数上限、差距与证据解释 |
| `src/job_recommendation_service.py` | 候选岗位加载、岗位族去重、稳定排序和 Top 5 推荐 |
| `src/learning_path_service.py` | 根据能力缺口生成 30/60/90 天分阶段学习路径 |
| `src/evaluation_service.py` | JD、简历、匹配和推荐的离线指标计算 |
| `src/acceptance_service.py` | 汇总赛事最低门槛与内部目标，生成验收状态 |

## 三、数据文件边界

### 上传的示例与可复现资料

- `data/samples/`：最小岗位、证据、简历和预期输出示例。
- `data/benchmark/`：评测格式说明和流程验证样例；不是正式测试集。
- `data/synthetic_resumes/`：64 份标记为 `synthetic=true`、`test_only` 的回归数据。
- `data/evidence/`：项目整理的公开证据示例与来源说明。
- `data/collection_manifests/company-official.example.jsonl`：人工清单格式示例。
- `data/exports/knowledge-graph.json`：当前图谱的静态导出，上传前检查体积和来源字段。

### 不上传的运行数据

- `data/job_competency.db` 及所有 `*.db`：包含完整主库或演示库，当前主库约 79 MB。
- `data/backups/`、`data/verification/`、`data/intake/`：数据库备份与批量接入中间结果。
- `data/collections/`、`data/collection_locks/`：采集响应、原始载荷、断点状态和互斥文件。
- `data/imports/`、`data/repairs/`、`data/audits/`、`data/expansion-reports/`：本地运行和审计报告。
- 根目录的批量原始岗位文件：位于工程目录之外，不纳入新仓库。

如果组员需要完整数据库，应通过团队内部受控渠道单独传递，并同时说明数据授权、来源和使用边界，不应依赖 GitHub 代码仓库分发。

## 四、模型与环境文件

项目没有本地大模型权重、Embedding 权重或模型检查点文件。`src/llm.py` 通过 LangChain 的 `ChatOpenAI` 封装访问 DeepSeek 的 OpenAI 兼容接口；模型名称、地址和密钥由环境变量提供。

默认主数据存储是 SQLite。Neo4j 是可选的图谱展示/查询副本，不是系统运行的必需组件。未配置模型密钥时，本地规则解析、确定性匹配、词法检索和证据摘要降级仍可运行。

## 五、配置安全

- 只提交 `.env.example`，其中秘密字段保持空值。
- 不提交 `.env`、本地授权来源清单或平台访问令牌。
- `config/job_sources.json` 中只允许出现来源元数据和公开访问边界，不写入登录态、Cookie 或签名材料。
- 上传前检查 Git 暂存区和全部跟踪文件，扫描私钥头、非空 API Key、数据库和超过 25 MB 的文件。

## 六、仓库用途与限制

新仓库用于组内协作、复现、PPT/技术文档整理和后续开发。它不是完整数据归档：生产主库、原始采集载荷和授权数据均被有意排除。仓库暂未声明开源许可证；在项目负责人确认代码与数据授权前，不应把私有仓库直接改为公开。
