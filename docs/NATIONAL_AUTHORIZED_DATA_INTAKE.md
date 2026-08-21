# 全国授权岗位数据接入说明

本说明供数据采集团队内部使用。目标是将可用、唯一的真实岗位稳定补充到 5000 至 10000 条，覆盖全部岗位族、不同城市层级和至少 8 个来源域名。按域名统计时，单一来源不超过 35%。只有 `gate_status=valid`、来源已批准且不是重复项的岗位才能计入完成量。

## 数量优先扩充口径

- 当前可信基线为 546 条，缺口为 4,454 条；考虑质量分流、重复和时序淘汰，原始接入预算为 8,000 至 9,000 条。
- 正式计数要求可信发布日期位于 2022 至 2026 年。仅有日期文本但缺少可信证据的记录不计入 5,000 条目标。
- Java 开发和 AI Agent 工程师分别以至少 500 条为目标；其他岗位族原则上至少 100 条，同时补齐当前为空的岗位族。
- 城市层级和来源必须同步扩展。达到单一来源不超过 35% 的边界后，只允许为明确缺口做受控补充，不能继续堆叠同类岗位。
- 授权清单或导出文件缺失时不得提交，也不得用样例、合成岗位或无法核验的历史文件替代。

## 文件目录

每个平台单独交付，不得把多个来源混在同一文件中：

```text
data/incoming/authorized/boss_zhipin/jobs.jsonl
data/incoming/authorized/job51/jobs.csv
data/incoming/authorized/liepin/jobs.json
data/incoming/authorized/lagou/jobs.jsonl
data/incoming/authorized/newjobs/jobs.jsonl
data/incoming/authorized/jobonline/jobs.jsonl
```

文件必须为 UTF-8。JSONL 每行一个岗位对象；JSON 文件必须是岗位对象数组；CSV 第一行必须是唯一字段名。不要使用 Excel 公式单元格。

## 岗位字段

| 含义 | 建议字段名 | 要求 |
| --- | --- | --- |
| 平台岗位编号 | `岗位ID` / `job_id` | 必填，在平台内唯一 |
| 岗位名称 | `岗位名称` / `job_title` | 必填 |
| 企业名称 | `公司名称` / `company_name` | 必填 |
| 岗位描述 | `岗位描述` / `description` | 必填，保留职责、技能和任职要求 |
| 岗位链接 | `职位链接` / `source_url` | 必填，域名必须属于本平台 |
| 发布时间 | `发布时间` / `published_at` | 必填，使用 ISO 日期或带时区时间；正式计数须为 2022 至 2026 年可信发布日期 |
| 工作地区 | `工作城市` / `region` | 推荐 |
| 薪资 | `薪资范围` / `salary` | 可选 |
| 工作经验 | `工作经验` / `experience` | 可选 |
| 学历 | `学历要求` / `education` | 可选 |
| 行业 | `行业` / `industry` | 可选 |
| 岗位族 | `岗位族编码` / `job_family_id` | 可选，只能使用系统登记编码 |

系统会删除岗位描述中的电话、邮箱、微信和 QQ 等个人信息。不得采集简历、聊天记录、联系人信息。不得绕过验证码，不得使用代理轮换，不得逆向请求签名，也不得绕过登录或平台访问控制。不得使用合成岗位填充正式数据量。

## 授权清单

在本机创建 `config/authorized_job_sources.local.json`。该文件已被 Git 忽略，不得提交，也不得在文档、聊天或源代码中填写令牌、Cookie、密码等凭据。

```json
{
  "sources": {
    "boss_zhipin_authorized": {
      "authorization_reference": "正式授权编号",
      "valid_until": "2026-12-31",
      "access_methods": ["file_export"],
      "scope": "授权覆盖的数据范围和用途",
      "credential_env_vars": []
    }
  }
}
```

每个待导入平台都要有独立条目。授权必须处于有效期内，且包含 `file_export`。系统只保存授权编号、有效期、范围摘要和清单摘要，不复制授权清单。

## 执行顺序

先查看当前缺口：

```powershell
python -m src.collect_jobs --coverage-report
```

再对每个平台执行只读预检。预检输出授权编号、有效期、文件名、SHA-256、行数及质量候选统计，不写数据库：

```powershell
python -m src.collect_jobs --authorization-preflight --source boss_zhipin_authorized --input-file data/incoming/authorized/boss_zhipin/jobs.jsonl --authorization-manifest config/authorized_job_sources.local.json
```

`accepted_count` 只代表文件行成功解析，不等于最终有效。必须同时查看 `valid_candidate_count` 和 `trusted_window_candidate_count`。先抽取具有代表性的来源、岗位族、地区和年份组合；若 `trusted_window_candidate_count / row_count` 的合格率低于 55%，停止该来源并核查导出范围、字段完整性与授权边界，不能直接扩大批次。

每个平台先暂存 20 条检查格式和质量分流：

```powershell
python -m src.collect_jobs --dry-run --source boss_zhipin_authorized --input-file data/incoming/authorized/boss_zhipin/jobs.jsonl --authorization-manifest config/authorized_job_sources.local.json --max-records 20 --run-id boss-smoke-20260812-001
```

核对 `data/collections/<run-id>/report.json`、`staged/jobs.jsonl`、`review/jobs.jsonl` 和 `quarantine/jobs.jsonl`。数量应与读取行数一致，来源、文件摘要和授权编号必须正确。

正式批次每批不超过 1,000 条，每个平台使用独立运行编号。运行编号使用实际执行日期和三位序号，例如 `boss-production-20260814-001`，不得复用已有运行目录。大文件通过 `--record-offset` 确定性分批；偏移量按已处理的原始行数递增，系统仍保留原始行号和行摘要：

```powershell
python -m src.collect_jobs --dry-run --source boss_zhipin_authorized --input-file data/incoming/authorized/boss_zhipin/jobs.jsonl --authorization-manifest config/authorized_job_sources.local.json --record-offset 0 --max-records 1000 --run-id boss-production-20260814-001
python -m src.collect_jobs --commit --resume-run boss-production-20260814-001 --confirm --authorization-manifest config/authorized_job_sources.local.json
python -m src.rebuild_hard_metrics --full --confirm --after-collection-run boss-production-20260814-001
python -m src.collect_jobs --coverage-report --output data/expansion-reports/quantity-a-after-boss-20260814-001.json

python -m src.collect_jobs --dry-run --source boss_zhipin_authorized --input-file data/incoming/authorized/boss_zhipin/jobs.jsonl --authorization-manifest config/authorized_job_sources.local.json --record-offset 1000 --max-records 1000 --run-id boss-production-20260814-002
```

每个提交批次后执行完整重建和覆盖率检查；只有上一批报告、备份、数据库完整性和来源占比均通过后，才开始下一批。

依次处理 `boss_zhipin_authorized`、`job51_authorized`、`liepin_authorized`、`lagou_authorized`、`newjobs_authorized` 和 `jobonline_authorized`。每次重建后重新查看缺口，优先补低于 100 条的岗位族、新一线和二线城市、当前占比较低的来源。达到来源目标或继续导入会使单一域名超过 35% 时，停止该来源。

## 交付检查

- 授权编号、有效期、平台来源和导出文件一一对应。
- 原始导出只存放在 `data/incoming/authorized/`，不进入 Git。
- 暂存报告中读取数等于有效、审核、隔离、重复与拒绝数之和。
- 提交前存在可读数据库备份，`PRAGMA integrity_check` 返回 `ok`。
- 重建结果状态为 `completed`，知识库与 `data/exports/knowledge-graph.json` 已更新。
- 最终报告列出每个岗位族、来源域名、来源类型、城市层级和质量门禁数量；任何未达项目标都必须明确保留。
