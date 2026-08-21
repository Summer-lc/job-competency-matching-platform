# 环境配置、运行与部署

本指南面向第一次接手项目的组员，覆盖 Windows PowerShell 本地运行、Docker Compose、可选 Neo4j、测试验证和常见故障。生产数据库和真实密钥不会进入 GitHub，首次克隆后需要自行初始化。

## 一、已验证环境

2026 年 8 月 21 日的本地验证环境：

| 项目 | 版本/状态 |
| --- | --- |
| 操作系统 | Windows |
| Python | 3.11.9 |
| FastAPI | 0.139.0（由允许范围解析得到） |
| SQLAlchemy | 2.0.23 |
| Pydantic | 2.12.5 |
| 测试 | 1,021 passed，6 skipped |
| 覆盖率 | 87.21% |

`requirements.txt` 使用兼容版本范围，其他时间安装得到的小版本可能不同。提交或答辩前建议在一台全新机器完整演练。

## 二、本地快速启动

在项目根目录执行：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
python -m uvicorn src.api:app --reload --port 8000
```

打开：

- 系统页面：`http://127.0.0.1:8000`
- API 文档：`http://127.0.0.1:8000/docs`
- 健康检查：`http://127.0.0.1:8000/health`

首次启动会按 `DATABASE_URL` 创建 SQLite 文件和数据表。新仓库不包含生产主库，因此页面中的统计初始为空是正常现象。

## 三、环境变量

`.env.example` 可以提交，`.env` 不能提交。

| 变量 | 是否必需 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 模型增强时必需 | DeepSeek API 密钥；留空时规则路径仍可运行 |
| `DEEPSEEK_BASE_URL` | 否 | OpenAI 兼容接口地址，模板为 `https://api.deepseek.com/v1` |
| `DEEPSEEK_MODEL` | 否 | 传给接口的模型标识；必须以团队账号实际可用模型为准 |
| `DATABASE_URL` | 否 | 默认 `sqlite+aiosqlite:///./data/job_competency.db`；也支持异步 MySQL URL |
| `SQL_ECHO` | 否 | `true` 时打印 SQL，默认 `false` |
| `NEO4J_URI` | Neo4j 同步时必需 | Bolt 地址；本地默认 `bolt://localhost:7687` |
| `NEO4J_USERNAME` | Neo4j 同步时必需 | Neo4j 用户名 |
| `NEO4J_PASSWORD` | Neo4j 同步时必需 | Neo4j 密码；部署前必须替换模板默认值 |
| `NEO4J_DATABASE` | 否 | Neo4j 数据库名，默认 `neo4j` |
| `BOSS_AUTHORIZED_API_TOKEN` | 仅有授权 API 时 | 本地授权凭据占位；当前商业平台保持授权文件导入 |
| `JOB51_AUTHORIZED_API_TOKEN` | 仅有授权 API 时 | 同上 |
| `LIEPIN_AUTHORIZED_API_TOKEN` | 仅有授权 API 时 | 同上 |
| `LAGOU_AUTHORIZED_API_TOKEN` | 仅有授权 API 时 | 同上 |

注意：代码不会因为环境变量存在就自动获得采集授权。来源仍必须通过 `config/job_sources.json`、授权说明和预检流程。

## 四、无模型运行

不填写 `DEEPSEEK_API_KEY` 时仍可使用：

- 岗位文件导入、格式校验和数据治理；
- SHA-256/SimHash 去重；
- 规则技能与职责抽取；
- 岗位层级、季度画像和能力演化；
- SQLite 图谱和 JSON 导出；
- 词法知识检索和有证据摘要；
- 规则简历解析；
- 七维匹配、岗位推荐和学习路径；
- 人工审核、离线评测和验收统计。

以下操作需要模型密钥或会自动降级：

- `POST /api/extraction/jobs/{id}`：没有密钥时返回模型服务错误；
- `POST /api/resumes/parse?enrich=true`：没有密钥时保持规则结果；
- `POST /api/knowledge/answer`：没有密钥或模型失败时使用可追溯摘要。

## 五、初始化示例数据

### 方法 A：使用命令行构建知识库

```powershell
python -m src.build_knowledge_base .\data\samples\jobs.jsonl
```

输出：

- `data/job_competency.db`：新建或更新的 SQLite 数据库；
- `data/imports/<batch-id>-report.json`：导入报告；
- `data/exports/knowledge-graph.json`：图谱快照。

相同文件重复执行通过文件哈希和记录身份实现幂等，不应重复增加岗位。

### 方法 B：通过页面/API 上传

启动系统后，在“数据治理”上传：

- `data/samples/jobs.jsonl`；
- `data/samples/evidence.jsonl`。

随后可上传 `data/samples/resume.txt` 验证简历与匹配流程。

示例来源使用 `example.com`，只用于格式和流程演示。不要把它计入正式多来源数据。

## 六、Docker Compose

复制环境模板并修改至少 Neo4j 密码；需要模型增强时再填写 DeepSeek 密钥：

```powershell
Copy-Item .env.example .env
docker compose up --build -d
docker compose ps
```

服务：

- API：`http://127.0.0.1:8000`
- Neo4j Browser：`http://127.0.0.1:7474`
- Neo4j Bolt：`bolt://127.0.0.1:7687`

Compose 将本地 `./data` 挂载到容器 `/app/data`，数据库在容器重建后仍保留。`api` 会等待 Neo4j 健康检查；如果只想使用 SQLite，本地直接启动 Uvicorn 更轻量。

停止：

```powershell
docker compose down
```

此命令不删除命名卷。只有明确需要清空 Neo4j 数据时才考虑删除卷，且应先备份。

## 七、Neo4j 是可选组件

系统默认从 SQLite 的岗位画像、技能和证据生成图谱。以下功能不要求 Neo4j：

- `GET /api/graph`；
- `GET /api/graph/versions/{family}`；
- `data/exports/knowledge-graph.json`；
- 单页图谱展示。

只有调用 `POST /api/graph/sync` 时才连接 Neo4j。连接失败返回 503，不会改变 SQLite 事实数据。

仅启动 Neo4j：

```powershell
docker compose up -d neo4j
```

## 八、测试与覆盖率

完整门禁：

```powershell
python -m pytest -c pytest-full.ini -q
```

当前已验证结果：

```text
1021 passed, 6 skipped
TOTAL coverage: 87.21%
Required coverage of 60% reached
```

`pytest.ini` 也会统计覆盖率，但只有 `pytest-full.ini` 明确设置 `--cov-fail-under=60`，发布前应使用后者。

单独运行某类测试示例：

```powershell
python -m pytest tests/test_resume_matching.py -q
python -m pytest tests/test_evaluation_service.py -q
python -m pytest tests/test_collection_security.py -q
```

## 九、健康检查与最小验收

启动服务后在另一个 PowerShell 窗口执行：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

预期结构：

```text
status  service                   version
------  -------                   -------
ok      job-competency-platform   1.0.0
```

建议最小演练：

1. 打开首页和 `/docs`；
2. 导入示例 JD 与证据；
3. 查看数据统计和岗位画像；
4. 查看图谱与知识检索；
5. 上传示例简历；
6. 生成推荐、匹配和学习路径；
7. 上传 `benchmark-example.jsonl` 验证评测流程；
8. 明确记录样例规模，避免把演示结果当成正式指标。

## 十、数据更新与备份

只读检查：

```powershell
python -m src.rebuild_hard_metrics --dry-run
```

确认全量重建：

```powershell
python -m src.rebuild_hard_metrics --full --confirm
```

全量重建会先在 `data/backups/` 创建时间戳 SQLite 备份并记录哈希。受控采集的详细顺序、停止条件和回滚要求见根目录 `USER_GUIDE.md`。

## 十一、常见问题

### PowerShell 禁止激活脚本

可只对当前进程放宽策略：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.venv\Scripts\Activate.ps1
```

也可不激活环境，直接使用：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m uvicorn src.api:app --port 8000
```

### 端口 8000 被占用

换端口：

```powershell
python -m uvicorn src.api:app --port 8010
```

或先确认占用进程，不要盲目结束不属于本项目的服务。

### 页面为空或统计为 0

新 GitHub 仓库不包含主数据库。先导入 `data/samples/jobs.jsonl`，或通过团队内部受控渠道取得数据库副本。

### DeepSeek 调用失败

检查密钥、模型标识、网络和配额。规则流程不受影响；知识问答和简历增强会按各自逻辑降级。不要把真实密钥复制到日志、截图、Issue 或提交记录。

### Neo4j 连接失败

确认容器状态、密码和 Bolt 地址。若当前只需演示主流程，可跳过同步并直接使用 SQLite 图谱。

### SQLite 被占用

停止正在使用同一数据库的 API 或重建进程再操作。项目的采集提交和全量重建已有锁与备份机制，不应手工同时运行多个写入流程。

## 十二、进一步阅读

- [项目总览与完成情况](PROJECT_OVERVIEW.md)
- [系统架构与代码运行流程](ARCHITECTURE_AND_FLOW.md)
- [输入输出、模型与算法](INPUT_OUTPUT_AND_ALGORITHMS.md)
- [项目文件清单与发布说明](FILE_INVENTORY_AND_RELEASE.md)
- 根目录 `QUICKSTART.md`：最短演示路径
- 根目录 `USER_GUIDE.md`：界面、采集、审核、验收和回滚的完整操作手册
