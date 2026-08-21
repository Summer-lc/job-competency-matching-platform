# 系统使用指南

> 本指南侧重系统操作和数据更新值班流程。项目完成情况、代码架构、算法细节、部署复现和文件上传边界分别见 [项目总览](docs/PROJECT_OVERVIEW.md)、[架构与运行流程](docs/ARCHITECTURE_AND_FLOW.md)、[输入输出与算法](docs/INPUT_OUTPUT_AND_ALGORITHMS.md)、[环境配置与部署](docs/ENVIRONMENT_AND_DEPLOYMENT.md) 和 [文件清单与发布说明](docs/FILE_INVENTORY_AND_RELEASE.md)。

## 一、系统亮点

1. **持续增量更新**：通过文件指纹、记录编号和内容签名识别新数据、修改数据与重复数据，只更新受影响的岗位族，不会反复生成相同画像。
2. **全过程可信治理**：保留原始数据，自动执行格式校验、质量检查、精确去重和近似去重；异常记录进入隔离区，低置信结果进入人工审核。
3. **结论可追溯**：岗位技能、职责和行业场景均关联原始 JD 证据，可查看来源链接，避免只有结论、没有依据。
4. **岗位能力动态演化**：按岗位族、岗位层级和自然季度生成画像，只在相邻且样本充足的季度间识别正式变化。
5. **技能点级全景图谱**：统一展示岗位族、岗位版本、技能、职责、行业场景和来源证据，并支持按技术栈、级别和节点类型筛选。
6. **证据约束知识问答**：先检索 JD 与外部标准证据，再生成带 `[K1]`、`[K2]` 引用的回答；引用异常或模型不可用时自动降级为证据摘要。
7. **可解释人岗匹配**：不仅输出匹配总分，还拆解必备技能、经验水平、技能时效、项目证据和加分技能，给出能力缺口与学习路径。
8. **自动质量评测**：上传人工标注集后自动计算 Precision、Recall、F1 和 Accuracy，并保存失败案例，便于持续改进模型。

## 二、启动与停止

在项目目录中运行：

```powershell
cd "D:\VScode\.vscode\Job competency matching\langchain_deepseek"
python -m uvicorn src.api:app --host 127.0.0.1 --port 8000
```

浏览器访问：

- 系统页面：`http://127.0.0.1:8000`
- 接口文档：`http://127.0.0.1:8000/docs`
- 服务检查：`http://127.0.0.1:8000/health`

在运行窗口中按 `Ctrl+C` 停止系统。

## 三、推荐使用顺序

### 1. 系统总览

首页展示岗位数量、数据来源、标准技能、岗位画像、重复率和待审核数量。首次进入时先确认右上角显示“服务正常”。

### 2. 数据治理

进入“数据治理”，选择岗位数据文件并点击“开始导入”。支持：

- `.jsonl`：每行一个 JSON 对象，适合持续追加数据；
- `.json`：JSON 数组或逐行 JSON；
- `.csv`：首行为字段名。

导入后重点查看：

- **质量门禁分布**：有效、待审核、重复和隔离数据数量；
- **导入批次**：文件指纹、处理数量和受影响岗位族；
- **异常隔离**：无法解析或违反字段约束的记录；
- **人工审核**：可解析但证据不足或质量偏低的记录。

导入完成后可点击“更新质量与画像”，系统会重算重复组、质量门禁、岗位层级、季度画像、演化证据和系统验收状态。运行记录会保留本次处理范围、状态和结果签名。

修复隔离数据后，将其放入新文件重新导入即可。不要直接修改数据库。

### 3. 岗位发现

进入“岗位发现”，点击“重新分析岗位”。系统根据岗位增长、来源覆盖、持续性和技能组合生成候选岗位。候选结果需要在“人工审核”中确认后再进入正式使用范围。

### 4. 能力演化

进入“能力演化”，选择岗位族、岗位层级和当前季度，再点击“生成对比”。页面会分别展示：

- 新增技能；
- 删除技能；
- 需求强度或普及率变化；
- 支撑变化结论的原始来源。

只有相邻两个季度均达到画像样本要求时，系统才生成正式演化事件；样本不足会如实显示，不会用低样本结果替代正式结论。

### 5. 全景图谱

进入“全景图谱”后，可以组合使用以下筛选项：

- 岗位族和画像版本；
- 技能、职责、行业场景或证据节点；
- 草稿图谱或已发布图谱；
- 技术栈和能力级别；
- “显示证据”开关。

点击“刷新图谱”应用筛选，点击任意节点查看版本、审核状态、证据原文和来源链接。总览时建议关闭证据节点；分析单个岗位时再打开证据，可减少画面拥挤。

### 6. 知识问答

进入“知识问答”，按以下顺序操作：

1. 输入岗位能力问题，或点击推荐问题；
2. 根据需要选择岗位族和证据数量；
3. 点击“仅检索证据”查看知识库中的原始命中结果；
4. 点击“生成证据回答”获得带引用的回答；
5. 对照下方 `[K1]`、`[K2]` 证据卡片检查岗位、企业、审核状态、证据原文和来源链接。

回答状态说明：

- **证据约束回答**：DeepSeek 回答已通过引用编号校验；
- **证据摘要**：模型不可用或引用不合格，系统改为展示确定性证据摘要；
- **无法回答**：当前问题没有匹配证据，系统拒绝生成无来源内容。

推荐问题示例：

- 数据工程师需要掌握哪些实时计算技能？
- 网络安全工程师的核心职责是什么？
- 物联网工程师常见的技术栈有哪些？

### 7. 人岗匹配

进入“人岗匹配”，按顺序操作：

1. 上传 PDF、DOCX、TXT 或 Markdown 简历；
2. 默认直接点击“解析简历”使用本地规则；确需模型补充时，先勾选“启用模型增强”，此时简历文本会发送至已配置的模型服务；
3. 检查技能熟练度、工作时间线、项目经历和对应原文证据；
4. 查看按岗位族去重的 Top 5 岗位推荐，可直接从推荐卡片进入深度诊断；
5. 也可手动选择目标岗位，再点击“开始匹配诊断”；
6. 按 0-30 天、31-60 天、61-90 天执行学习任务，并用完成标准核验结果。

结果包含匹配总分、七个维度得分、正负向因素、证据明细、置信度、能力缺口和分阶段学习路径。DeepSeek 不可用或结果缺少原文依据时，系统自动使用规则解析继续完成推荐和诊断。姓名、性别、年龄、籍贯等个人属性不参与评分。匹配结果用于能力诊断，不应作为单一招聘决策依据。

### 8. 人工审核

进入“人工审核”，逐条查看候选岗位或低置信结果的原因和证据。选择“通过”或“驳回”，并填写简短说明，形成可追踪的审核记录。

### 9. 模型质量评测

进入“模型质量评测”，上传人工标注的 `.jsonl` 或 `.json` 文件，点击“运行自动评测”。系统会分别评测：

- JD 技能解析；
- 简历技能、经验、学历、工作时间线和证据提取；
- 单岗位匹配等级判断；
- Top 5 岗位推荐排序。

页面展示真实计算得到的 Accuracy、Precision、Recall 和 F1；扩展结果还包括时间线准确率、证据有效率、项目技能准确率、成果准确率、Top-1 Accuracy、Recall@5、MRR 和 NDCG@5。示例格式见 `data/benchmark/benchmark-example.jsonl`，详细字段说明见 `data/benchmark/README.md`。

每次上传是一个独立评测批次。若只上传一种任务，质量状态不会自动拼接此前其他数据集的结果。

页面中的“系统验收状态”来自数据库、独立标注评测和实际测试覆盖率。尚未测量的指标显示为 `not_measured`，不会自动显示为通过。

## 四、可持续采集与更新

维护期间先停止 Web 服务。以下任一步报错、返回非零退出码，或报告中任一来源为 `stopped`，系统保持停止，先查明原因，不继续提交、修复或重建。

### 0. 停止服务并确认端口

- 前台运行 Uvicorn：回到启动窗口按 `Ctrl+C`，等待进程退出。
- 使用本项目 Compose 后台运行：在项目目录执行 `docker compose down`。
- 只有已记录“本项目启动 PID”时才能按 PID 停止。先确认该 PID 的命令行是本项目的 `uvicorn src.api:app`，并确认它正监听 8000；不得仅凭端口号结束进程，更不能结束无法确认归属的进程。

可选的项目 PID 停止方法如下。把示例数字替换为启动本项目时记录的准确 PID：

```powershell
$ErrorActionPreference = "Stop"
$projectPid = 12345
$process = Get-CimInstance Win32_Process -Filter "ProcessId = $projectPid" -ErrorAction Stop
$listeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object { $_.LocalPort -eq 8000 })
if ($process.CommandLine -notmatch '(?i)(python(?:\.exe)?\s+-m\s+uvicorn|uvicorn(?:\.exe)?)\s+src\.api:app' -or $projectPid -notin $listeners.OwningProcess) {
    throw "PID 不是已确认的本项目 Uvicorn 监听进程"
}
Stop-Process -Id $projectPid -ErrorAction Stop
```

无论采用哪种停止方式，最后都执行：

```powershell
$remainingListeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object { $_.LocalPort -eq 8000 })
if ($remainingListeners.Count -ne 0) {
    throw "8000 端口仍有监听；确认进程归属，不要结束未知进程"
}
```

只有确认 8000 端口没有监听后才进入维护。验收用临时服务关闭后也要重复此检查；本轮维护最终保持停止，重启属于维护之外的独立操作。

### 1. 来源审批

打开 `config/job_sources.json`，逐项确认 `base_url`、`allowed_paths`、`collection_mode`、`compliance_status`、`market_scope`、请求间隔、页数/记录上限、解析器版本和人工审核说明仍然有效。

- 自动来源必须同时满足 `market_scope=china`、`compliance_status=approved` 和 `enabled=true`，采集器才会联网。
- 当前国家大学生就业服务平台和中国公共招聘网满足自动采集条件。中国公共招聘网使用公开岗位首页建立单次匿名会话，按 5 秒间隔访问已审核的列表和详情路径；Cookie 不写入日志或批次文件。国聘因公开岗位路由尚未审核完成，保持 `pending_review` 和禁用。
- 智联招聘、BOSS直聘、前程无忧、猎聘和拉勾不做自动爬取。智联授权 JSONL 使用 `zhaopin_legacy_import` 本地文件入口，并填写 `--authorization-note`；系统不会访问文件中的岗位链接。
- 人工 manifest 机制是 `manual_only` + `manual_url_manifest`，只读取团队审核的本地文件，不自动翻页、发现链接或联网；通用企业清单当前为 `excluded`、`blocked` 和禁用状态，不得提交正式库。
- 国外招聘网站或国外企业招聘系统必须标记为 `market_scope=excluded`，原始快照可以保留审计，但不得进入岗位库、知识库、知识图谱和验收统计。
- `report.json` 中的 `artifacts` 是程序自动生成的产物清单，记录路径、条数和 SHA-256；它不是人工 manifest。

来源页面、接口、授权边界或结构一旦变化，立即把该来源改为 `"enabled": false`，在 `compliance_note` 记录日期和原因，再保留失败批次。不要只改数据库中的来源快照，也不得绕过登录、验证码、401、403、429 或路径限制。

### 2. 小规模 dry-run

每个自动来源先单独运行有界试采。`--max-records`、`--max-pages`、`--max-requests` 分别限制记录、页面和请求总量：

```powershell
python -m src.collect_jobs --source ncss_public_jobs --run-id <run-id> --max-records 20 --max-pages 2 --max-requests 50 --dry-run
python -m src.collect_jobs --source mohrss_public_jobs --run-id <run-id> --max-records 20 --max-pages 2 --max-requests 50 --dry-run
```

两个命令使用不同的 `<run-id>`。程序默认只暂存，不写主库。

### 3. 检查报告、审核和隔离

逐项检查：

- `data/collections/<run-id>/report.json`：必须是 `status=completed`、`staging_valid=true`；同时检查各来源状态、错误、请求量、有效/审核/隔离/重复数和产物哈希。
- `data/collections/<run-id>/staged/jobs.jsonl`：准备提交的有效记录。
- `data/collections/<run-id>/review/jobs.jsonl`：需人工判断的低置信记录。
- `data/collections/<run-id>/quarantine/jobs.jsonl`：不会进入提交的异常记录。
- `data/collections/<run-id>/checkpoint.json` 和 `raw/`：恢复位置与原始响应证据。

抽查来源 URL、来源域名、快照哈希、解析器版本、观测时间和发布时间证据。合成简历仅用于测试；任何合成或生成岗位都不得计入生产总量，也不得出现在生产暂存文件中。

### 4. 恢复中断或扩采

恢复中断批次时沿用原有来源和上限，不能在恢复命令中换来源、manifest 或扩大限制：

```powershell
python -m src.collect_jobs --resume-run <run-id> --dry-run
```

已完成批次执行该命令只会校验并返回原报告。扩采必须使用新的运行编号和新的明确上限，例如：

```powershell
python -m src.collect_jobs --source ncss_public_jobs --source mohrss_public_jobs --run-id <new-run-id> --max-records 1000 --max-pages 20 --max-requests 1000 --dry-run
```

命令上限还会受注册表中每个来源上限约束。企业官方岗位只能通过已审核的人工 manifest：

```powershell
python -m src.collect_jobs --source company_official_manifest --manifest data\collection_manifests\company-official.example.jsonl --run-id <manual-run-id> --max-records 100 --max-pages 1 --max-requests 1 --dry-run
```

### 5. 备份、提交和确认

只有同一 `<run-id>` 的报告与三个 JSONL 文件检查通过后才提交：

先在当前 PowerShell 会话定义只读备份检查函数。它要求路径存在、SQLite 可读、`PRAGMA integrity_check=ok`，并返回 SHA-256 与岗位行数：

```powershell
$ErrorActionPreference = "Stop"
function Get-VerifiedSqliteBackupCheckpoint {
    param([Parameter(Mandatory = $true)][string]$ReportedPath)
    $resolved = (Resolve-Path -LiteralPath $ReportedPath -ErrorAction Stop).Path
    $sha256 = (Get-FileHash -LiteralPath $resolved -Algorithm SHA256 -ErrorAction Stop).Hash
    $rows = python -c "import sqlite3,sys; p=sys.argv[1]; c=sqlite3.connect('file:'+p.replace('\\','/')+'?mode=ro',uri=True); check=c.execute('PRAGMA integrity_check').fetchone()[0]; assert check=='ok', check; print(c.execute('SELECT COUNT(*) FROM job_posting').fetchone()[0]); c.close()" $resolved
    if ($LASTEXITCODE -ne 0) { throw "SQLite 备份不可读或完整性检查失败" }
    [PSCustomObject]@{ Path = $resolved; Sha256 = $sha256; Rows = [Int64]$rows }
}
```

执行提交并从本次 JSON 输出获取路径，不使用通配符或目录中的“最新文件”：

```powershell
$commitJson = python -m src.collect_jobs --resume-run <run-id> --commit --confirm
if ($LASTEXITCODE -ne 0) { throw "采集提交失败" }
$commitResult = $commitJson | ConvertFrom-Json -ErrorAction Stop
$commitCheckpoint = Get-VerifiedSqliteBackupCheckpoint -ReportedPath ([string]$commitResult.backup_path)
$commitBackupPath = $commitCheckpoint.Path
$commitBackupSha256 = $commitCheckpoint.Sha256
$commitBackupRows = $commitCheckpoint.Rows
```

提交会验证报告、文件哈希、受保护签名、来源定义和原始证据，随后在写主库前生成 SQLite 备份。把 `$commitBackupPath`、`$commitBackupSha256`、`$commitBackupRows` 与 `imported`、`revised`、`skipped`、`duplicates`、`idempotent` 写入本轮操作记录。提交和普通全量重建使用时间戳备份 `job_competency-<timestamp>.db`。验证失败时系统保持停止，不继续后续步骤。

### 6. 修复审计

先为本轮指定唯一、可重复使用的修复编号，只读生成变更计划：

```powershell
python -m src.rebuild_hard_metrics --dry-run --repair-audit --repair-run-id <repair-run-id>
```

检查 `data/repairs/<repair-run-id>/report.json` 中的 `row_count_before`、`row_count_after`、`changes`、`fingerprint_changes`、`duplicate_changes` 和 `duplicate_summary`，并对照原始载荷抽查 before/after。审计不应改主库。

### 7. 应用修复

审计确认后，用相同修复编号应用：

```powershell
$repairJson = python -m src.rebuild_hard_metrics --full --confirm --repair --repair-run-id <repair-run-id>
if ($LASTEXITCODE -ne 0) { throw "修复应用失败" }
$repairResult = $repairJson | ConvertFrom-Json -ErrorAction Stop
$repairCheckpoint = Get-VerifiedSqliteBackupCheckpoint -ReportedPath ([string]$repairResult.backup_path)
$repairBackupPath = $repairCheckpoint.Path
$repairBackupSha256 = $repairCheckpoint.Sha256
$repairBackupRows = $repairCheckpoint.Rows
if ($repairBackupSha256 -ne [string]$repairResult.repair.backup_sha256) {
    throw "修复报告与实际备份 SHA-256 不一致"
}
```

修复应用使用修复编号备份 `data/backups/job_competency-<repair-run-id>.db`，不是时间戳名称。程序校验完整性和行数后，事务化写入修复审计并运行一次完整硬指标管线。把 `$repairBackupPath`、`$repairBackupSha256`、`$repairBackupRows` 单独写入操作记录，并检查修复报告中的 `status=completed`、实际变更数和管线结果；失败时数据库事务回滚，系统保持停止。

### 8. 全量重建与图谱导出

修复应用已包含一次完整硬指标管线。按统一班次仍执行以下显式全量重建，作为最终一致性检查；若本轮无需修复而跳过第 7 步，本步骤仍必须执行。该命令会再创建备份，重复计算的结果应保持幂等：

```powershell
$rebuildJson = python -m src.rebuild_hard_metrics --full --confirm
if ($LASTEXITCODE -ne 0) { throw "普通全量重建失败" }
$rebuildResult = $rebuildJson | ConvertFrom-Json -ErrorAction Stop
$rebuildCheckpoint = Get-VerifiedSqliteBackupCheckpoint -ReportedPath ([string]$rebuildResult.backup_path)
$rebuildBackupPath = $rebuildCheckpoint.Path
$rebuildBackupSha256 = $rebuildCheckpoint.Sha256
$rebuildBackupRows = $rebuildCheckpoint.Rows
python -m src.build_knowledge_base data\collections\<run-id>\staged\jobs.jsonl
if ($LASTEXITCODE -ne 0) { throw "图谱导出失败" }
```

完整硬指标管线会更新重复组、质量门禁、岗位层级、季度画像、演化事件、知识片段和验收快照。随后对已提交的同一暂存文件重跑 `build_knowledge_base` 时，导入是幂等的；幂等导入分支不会更新知识片段，但仍会导出当前数据库图谱到 `data/exports/knowledge-graph.json`。把 `$rebuildBackupPath`、`$rebuildBackupSha256`、`$rebuildBackupRows` 单独写入操作记录。每次操作都单独记录三个备份变量，后续步骤不得继承旧变量。

另外两种模式是只读数据库概览 `python -m src.rebuild_hard_metrics --dry-run`，以及按岗位族重算 `python -m src.rebuild_hard_metrics --incremental --family-code DATA_ENGINEER`；`--family-code` 可重复传入，但只适用于 `--incremental`。

### 9. 验收、UI、知识库和图谱检查

全部离线步骤成功后才临时启动系统：

```powershell
python -m uvicorn src.api:app --host 127.0.0.1 --port 8000
```

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/api/acceptance/summary
Invoke-RestMethod "http://127.0.0.1:8000/api/knowledge/search?q=Python&limit=5"
Invoke-RestMethod http://127.0.0.1:8000/api/graph
```

在 UI 检查“系统总览/数据健康”“数据治理”“知识问答”“全景图谱”：数量必须来自实际有效去重岗位，知识结果要有来源，图谱节点和版本可打开。检查完成或任何检查失败时，都按第 0 步停止临时服务并确认 8000 端口没有监听。维护最终保持停止，不在本流程中重启。

### 10. 报告保留与 SQLite 回滚

本轮至少保留整个 `data/collections/<run-id>/`、提交和重建备份、`data/repairs/<repair-run-id>/report.json`、`data/imports/<batch-id>-report.json`、`data/exports/knowledge-graph.json` 以及命令输出。提交前不要删除系统在本机受保护目录中生成的签名、密钥或锁状态，否则报告无法验证。

需要回滚时，操作员必须先回答“回滚本次提交、修复或重建中的哪一步”，再从该步骤的操作记录复制准确路径、SHA-256 和备份岗位行数。`$rollbackBackupPath` 必须在当前回滚操作中显式填写，不能从 `$commitBackupPath`、`$repairBackupPath`、`$rebuildBackupPath` 或通用变量自动赋值，不得继承旧变量。

以下脚本先停止于任何错误，确认端口无监听，再验证备份的绝对路径、SHA-256、可读性和行数。只有完整归档 `data/job_competency.db`，以及存在时的 `data/job_competency.db-wal`、`data/job_competency.db-shm`、`data/job_competency.db-journal` 后，才会复制备份：

```powershell
$ErrorActionPreference = "Stop"
$remainingListeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object { $_.LocalPort -eq 8000 })
if ($remainingListeners.Count -ne 0) { throw "8000 端口仍有监听，禁止回滚" }

# 必须粘贴本次要逆转操作所报告并记录的准确值，不得使用通配符或“最新备份”。
$rollbackBackupPath = "D:\exact\path\from-this-operation\job_competency-20260811-120000-000000.db"
$rollbackExpectedSha256 = "REPLACE_WITH_RECORDED_SHA256"
$rollbackExpectedRows = 1234
$backupRoot = (Resolve-Path -LiteralPath "data\backups" -ErrorAction Stop).Path
$resolvedRollbackBackup = (Resolve-Path -LiteralPath $rollbackBackupPath -ErrorAction Stop).Path
if ((Split-Path -Parent $resolvedRollbackBackup) -ne $backupRoot) { throw "备份不在受控备份目录" }
$actualBackupSha256 = (Get-FileHash -LiteralPath $resolvedRollbackBackup -Algorithm SHA256 -ErrorAction Stop).Hash
if ($actualBackupSha256 -ne $rollbackExpectedSha256) { throw "备份 SHA-256 与操作记录不一致" }
$verifiedRows = python -c "import sqlite3,sys; p=sys.argv[1]; expected=int(sys.argv[2]); c=sqlite3.connect('file:'+p.replace('\\','/')+'?mode=ro',uri=True); check=c.execute('PRAGMA integrity_check').fetchone()[0]; assert check=='ok', check; rows=c.execute('SELECT COUNT(*) FROM job_posting').fetchone()[0]; assert rows==expected, (rows,expected); print(rows); c.close()" $resolvedRollbackBackup $rollbackExpectedRows
if ($LASTEXITCODE -ne 0) { throw "备份不可读、完整性失败或岗位行数不符" }

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$archiveRoot = Join-Path (Resolve-Path -LiteralPath "data" -ErrorAction Stop).Path "rollback-archive-$stamp"
New-Item -ItemType Directory -Path $archiveRoot -ErrorAction Stop | Out-Null
$liveDatabase = (Resolve-Path -LiteralPath "data\job_competency.db" -ErrorAction Stop).Path
$candidateFiles = @($liveDatabase, "$liveDatabase-wal", "$liveDatabase-shm", "$liveDatabase-journal")
$filesToArchive = @($candidateFiles | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf -ErrorAction Stop })
if ($liveDatabase -notin $filesToArchive) { throw "当前主库不存在，禁止继续" }
foreach ($file in $filesToArchive) {
    Move-Item -LiteralPath $file -Destination $archiveRoot -ErrorAction Stop
}
foreach ($file in $filesToArchive) {
    $archived = Join-Path $archiveRoot ([IO.Path]::GetFileName($file))
    if ((Test-Path -LiteralPath $file -ErrorAction Stop) -or -not (Test-Path -LiteralPath $archived -PathType Leaf -ErrorAction Stop)) {
        throw "当前数据库文件未完整归档"
    }
}

Copy-Item -LiteralPath $resolvedRollbackBackup -Destination "data\job_competency.db" -ErrorAction Stop
$restoredRows = python -c "import sqlite3,sys; expected=int(sys.argv[1]); c=sqlite3.connect('file:data/job_competency.db?mode=ro',uri=True); check=c.execute('PRAGMA integrity_check').fetchone()[0]; assert check=='ok', check; rows=c.execute('SELECT COUNT(*) FROM job_posting').fetchone()[0]; assert rows==expected, (rows,expected); print(rows); c.close()" $rollbackExpectedRows
if ($LASTEXITCODE -ne 0) { throw "恢复后的数据库校验失败" }
```

若归档中途失败，`$ErrorActionPreference` 和终止错误会阻止 `Copy-Item`。此时禁止复制备份并保持服务停止：对照 `$filesToArchive` 检查原位置和 `$archiveRoot`，确认无同名覆盖后，要么用 `Move-Item -LiteralPath ... -ErrorAction Stop` 把已移动文件逐一移回原位置，要么把剩余文件完成归档；不能让同一数据库的文件分散在两处后继续。无法确认完整清单时保留两处文件并交由数据库维护人员处理。

恢复后重新核对完整性、岗位行数和对应批次报告，但本维护流程最终保持停止。不要删除归档数据库，直至复盘完成。

### 11. 新读者自检

- **回滚哪一个精确备份？** 先回答“回滚本次提交、修复或重建中的哪一步”，只填写该步单独记录的路径、SHA-256 和行数。
- **如何确认服务已经停止？** 完成前台 `Ctrl+C`、`docker compose down` 或经身份核验的项目 PID 停止后，确认 8000 端口没有监听。
- **归档中途失败后怎么办？** 保持停止并禁止复制备份，先恢复原文件集或完成整个归档。
- **幂等重跑 build_knowledge_base 会更新知识片段吗？** 不会。幂等导入分支不会更新知识片段，但仍会导出当前数据库图谱。

### 12. 常见错误与退出码

- 退出码 `2`：参数组合、来源审批、权限、报告或证据校验失败。修正输入或来源状态，不要跳过 `--confirm`。
- 退出码 `3`：采集目录、文件或运行锁错误。检查是否有另一任务正在使用同一运行编号或数据库。
- 退出码 `4`：未在批次内转为停止报告的网络/来源停止错误。检查网络和来源页面，不得绕过限制。
- 退出码 `5`：SQLite、迁移或数据库操作错误。保持服务停止，检查本轮备份和数据库完整性。

采集命令即使退出码为 `0`，仍要检查 `report.json` 中每个 `sources.<source-id>.status`；来源内部遇到结构异常时会保留已完成证据并标记 `stopped`。`rebuild_hard_metrics` 的无效参数通常由参数解析器以退出码 `2` 结束，其他异常同样视为失败。

批次目录各文件的用途和全部 CLI 选项见 [采集批次说明](data/collections/README.md)。团队收到外部 JSON、JSONL 或 CSV 文件但不走采集器时，仍可运行 `python -m src.build_knowledge_base ..\新数据文件.jsonl`；该路径不替代来源审批和生产总量边界。

## 五、常见状态说明

| 状态 | 含义 | 处理方式 |
| --- | --- | --- |
| valid | 数据完整且通过质量检查 | 可直接参与画像构建 |
| review | 数据可用，但存在低置信或质量问题 | 进入人工审核 |
| duplicate | 与已有记录完全或高度相似 | 保留来源，不重复计入分析 |
| quarantine | 格式错误或关键字段不符合约束 | 修复原始记录后重新导入 |

## 六、建议的展示流程

依次展示“系统总览 → 数据治理 → 能力演化 → 全景图谱 → 知识问答 → 人岗匹配 → 模型质量评测”。这条路径能完整体现从数据进入、可信治理、知识形成到实际应用和质量验证的闭环。
