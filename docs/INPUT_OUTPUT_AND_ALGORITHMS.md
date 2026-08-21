# 输入输出、模型与算法说明

本文说明系统“吃什么数据、输出什么结果、哪些地方使用模型、哪些地方使用确定性算法”。示例均为脱敏或合成内容。

## 一、输入类型

| 输入 | 支持格式 | 入口 | 主要用途 |
| --- | --- | --- | --- |
| 岗位 JD | `.json`、`.jsonl`、`.csv` | `POST /api/data/import` 或 `python -m src.build_knowledge_base` | 数据治理、画像、演化和图谱 |
| 外部证据 | `.json`、`.jsonl`、`.csv` | `POST /api/data/evidence/import` | 职业标准、官方技术资料和趋势证据 |
| 简历 | `.pdf`、`.docx`、`.txt`、`.md` | `POST /api/resumes/parse` | 能力档案、匹配和推荐 |
| 匹配请求 | JSON | `POST /api/matches` | 对已保存简历与岗位画像评分 |
| 推荐请求 | JSON | `POST /api/matches/recommend` | 岗位族去重的 Top 5 推荐 |
| 评测集 | `.json`、`.jsonl` | `POST /api/evaluation/run` | JD、简历、匹配和推荐离线评测 |
| 来源配置 | JSON | `config/job_sources.json` 等 | 受控采集、访问边界和岗位族查询 |

## 二、岗位 JD 输入

最小建议字段示例：

```json
{
  "record_id": "S-JD-0001",
  "job_family_id": "AI_AGENT_ENGINEER",
  "job_title_raw": "大模型应用开发工程师",
  "company_name": "示例科技公司",
  "source_name": "企业官网",
  "source_type": "company_official",
  "source_url": "https://example.com/jobs/agent-001",
  "published_at": "2026-05-10",
  "collected_at": "2026-08-21T10:00:00+08:00",
  "job_description_raw": "负责大语言模型应用开发，使用Python和LangChain构建RAG问答服务。"
}
```

关键校验：

- `record_id`、`job_family_id`、`job_title_raw`、`company_name`、`source_name` 和 `job_description_raw` 必填；
- `job_description_raw` 的 Pydantic 最小长度为 10，硬门禁还会隔离规范化后少于 40 个字符的描述；
- URL、发布时间、采集时间和来源授权会参与门禁或复核；
- 额外字段允许保留在原始载荷中，便于追溯。

完整可运行示例见 `data/samples/jobs.jsonl`。

## 三、证据输入

```json
{
  "evidence_id": "S-EV-0001",
  "job_family_id": "AI_AGENT_ENGINEER",
  "evidence_type": "official_document",
  "title": "智能体工具调用技术文档",
  "publisher": "示例发布机构",
  "published_at": "2026-04-10",
  "source_url": "https://example.com/docs/agent-tools",
  "related_skill": "AI Agent",
  "evidence_summary": "资料说明任务规划、工具调用和状态管理是智能体应用能力。"
}
```

证据会保存岗位族、类型、发布者、来源 URL、相关技能、摘要和来源分。知识问答会把岗位知识片段与外部证据合并后编号引用。

## 四、简历输入与输出

### 输入

系统读取 PDF、Word、纯文本或 Markdown。推荐简历明确写出：

- 工作/项目的起止年月；
- 使用过的技术栈；
- 在项目中的职责；
- 可核查的成果或量化结果；
- 学历信息。

### 规则解析输出示意

```json
{
  "resume_id": 1,
  "filename": "resume.docx",
  "parser_mode": "rules",
  "experience_years": 3.5,
  "skills": [
    {
      "name": "Java",
      "proficiency": "working",
      "evidence_sources": ["project", "work"],
      "evidence": [
        {"text": "使用Java建设订单服务", "source": "work"}
      ]
    }
  ],
  "work_experiences": [],
  "project_experiences": [],
  "education": ["本科"],
  "parse_warnings": []
}
```

默认 `enrich=false`，只运行本地规则。只有请求显式设置 `enrich=true` 且存在 `DEEPSEEK_API_KEY` 时才尝试模型增强。模型调用失败、JSON 无效或新增字段无法在原文中找到证据时，结果回退为 `parser_mode=rules`；通过校验的混合结果标记为 `parser_mode=hybrid`。

## 五、匹配与推荐输入

### 单岗位匹配

```json
{
  "resume_id": 1,
  "job_profile_id": 12
}
```

### Top 5 推荐

```json
{
  "resume_id": 1,
  "limit": 5,
  "levels": ["mid", "senior"],
  "family_codes": ["JAVA_DEVELOPER", "CLOUD_NATIVE_ENGINEER"]
}
```

`limit` 可设置 1–10。推荐先为每个候选画像计算完整匹配结果，再排序并按 `family_code` 去重。

### 匹配输出核心字段

```json
{
  "total_score": 76.5,
  "match_band": "medium",
  "confidence": "medium",
  "scoring_version": "evidence-match-v2",
  "dimension_scores": {},
  "score_caps": [],
  "positive_factors": [],
  "negative_factors": [],
  "matched_required_skills": [],
  "missing_required_skills": [],
  "transferable_skills": [],
  "learning_plan": {},
  "recommendations": []
}
```

## 六、评测集输入

每行包含 `case_id`、`task`、`input` 和 `expected`：

```json
{"case_id":"JD-DEMO-001","task":"jd_parsing","input":{"text":"要求掌握Python。加分：熟悉Docker。"},"expected":{"required_skills":["Python"],"preferred_skills":["Docker"]}}
```

支持任务：

- `jd_parsing`：技能和必备/加分属性；
- `resume_extraction`：技能、经验年限、学历、时间线和原文证据；
- `matching`：`high`、`medium`、`low` 匹配档位；
- `job_recommendation`：相关岗位族及相关性等级。

格式和流程样例见 `data/benchmark/benchmark-example.jsonl`。该文件只有 7 个用例，其中各项业务指标实际只使用 1–2 条，不能当作正式测试集。

## 七、主要输出与保存位置

| 输出 | 位置/实体 | 说明 |
| --- | --- | --- |
| 岗位原始行 | `raw_job_record` | 原始文本、行号、错误和分流状态 |
| 标准岗位 | `job_posting` | 标准字段、哈希、来源、质量、重复和门禁状态 |
| 导入批次 | `import_batch` | 文件哈希、计数、错误、受影响岗位族和幂等信息 |
| 质量问题 | `quality_issue` | 问题码、严重度、字段和处理状态 |
| 岗位画像 | `job_profile`、`job_profile_snapshot` | 岗位族、层级、季度、版本、置信度和派生状态 |
| 演化结果 | `evolution_event`、`evolution_evidence` | 新增、删除、变化及证据 |
| 知识片段 | `knowledge_chunk` | 检索文本、元数据、来源 URL 和可选向量 |
| 图谱导出 | `data/exports/knowledge-graph.json` | 无 Neo4j 时也可读取的图谱快照 |
| 简历档案 | `resume_record` | 原始文本哈希与结构化能力档案 |
| 匹配/推荐 | `match_record`、`recommendation_run/result` | 得分、缺口、解释、学习路径和结果签名 |
| 评测结果 | `evaluation_run` | 每类指标、样本数、失败案例和批次 ID |
| 验收快照 | `acceptance_snapshot` | 最低门槛、内部目标和总状态 |

## 八、确定性算法

### 1. 文本规范化与精确去重

`normalize_text` 会：

1. 转为小写；
2. 删除空白、标点和下划线；
3. 删除“和、及、与”等连接字符。

规范化文本计算 SHA-256：

```text
content_hash = SHA256(normalize_text(job_description_raw))
```

相同内容哈希视为精确重复。文件级 SHA-256 还用于导入幂等、采集报告完整性和备份校验。

### 2. SimHash 近重复

当前实现不是外部模型向量：

1. 对规范化文本切分字符二元组；
2. 每个二元组用 BLAKE2b 得到 64 位值；
3. 对每一位累加正负权重；
4. 权重大于等于 0 的位记为 1，得到 64 位 SimHash；
5. 两条记录使用异或后的置位数计算汉明距离。

当前近重复阈值为汉明距离不大于 8。

### 3. 基础质量分

`job_data_service.py` 的基础质量分公式为：

```text
quality_score =
    0.45 × source_score
  + 0.30 × completeness
  + 0.25 × description_score

completeness = 六个可选字段中非空字段数 / 6
description_score = min(岗位描述字符数 / 500, 1)
```

六个完整度字段是企业、行业、地区、发布时间、经验要求和学历要求。质量分低于 0.70 会进入复核；必填字段缺失、描述过短或日期无法解析等硬问题会进入隔离。

### 4. 规则技能抽取与标准化

- `src/skill_ontology.py` 定义标准技能、类别和别名；
- 文本按别名长度优先匹配；
- 命中技能时保留附近原文作为 `evidence_text`；
- 附近存在“加分、优先、preferred”等标志时分类为加分技能，否则为必备技能；
- 标准名称直接命中置信度为 0.92，别名命中为 0.86；
- 同义技能统一到标准名称，相关技能在匹配时可获得 0.4 的迁移信用。

### 5. 岗位层级

`competition_rules.py` 从岗位名称、经验年限和描述标志综合判断 `junior`、`mid`、`senior`、`expert` 或 `unspecified`。人工复核结果可覆盖机器层级，同时保留机器证据和审核记录。

### 6. 新岗位候选分数

基础候选分由增长、多来源、新颖度、持续性和样本量组成：

```text
score =
    0.35 × growth_score
  + 0.20 × source_score
  + 0.25 × novelty
  + 0.10 × persistence
  + 0.10 × volume_score

growth_score = min(max((current - previous) / max(previous, 1), 0) / 2, 1)
source_score = min(source_count / 5, 1)
volume_score = min(current_count / 30, 1)
```

候选分只是发现信号。正式画像仍要满足样本、时间证据、来源与人工审核门槛。

### 7. 季度画像与演化

- 只使用满足门禁、非重复并具备可信观测时间的岗位；
- 按岗位族、层级和季度切片；
- 技能流行度等于包含该技能的岗位数除以该切片岗位数；
- 画像保存输入签名、规则版本、样本数、样本状态和证据计数；
- 只比较相邻季度，避免跨越缺失季度制造“变化”。

基础技能窗口变化规则：

| 类型 | 条件 |
| --- | --- |
| 新增 | 历史占比 `< 0.20` 且当前占比 `>= 0.30` |
| 删除 | 历史占比 `>= 0.30` 且当前占比 `< 0.15` |
| 变化 | 两期占比均 `>= 0.15` 且绝对差值 `>= 0.20` |
| 不变 | 不满足以上条件 |

正式季度流水线还会保存规则版本、画像快照和演化证据，便于复算。

### 8. 知识检索

当前 API 默认没有注入 Embedding Provider，因此运行模式为 `lexical`：

- 英文/数字/技术符号按词切分；
- 中文连续文本生成 2–6 字子串并移除少量停用词；
- 每个命中词基础加 1 分，同一词每多出现一次加 0.1；
- 按分数降序、`chunk_id` 升序稳定排序。

`knowledge_service.py` 提供可注入的 Embedding 接口。若调用方提供向量，则：

```text
hybrid_score = lexical_score + cosine_similarity(query_vector, chunk_vector)
```

这是一项扩展接口，不代表当前仓库已附带 BGE、FAISS 或其他本地向量模型文件。

## 九、七维人岗匹配

实现版本：`evidence-match-v2`。

| 维度 | 权重 | 核心依据 |
| --- | ---: | --- |
| 必备技能覆盖 | 30 | 精确命中和相关技能迁移信用 |
| 技能熟练度 | 15 | 简历熟练度与岗位目标熟练度 |
| 经验层级 | 15 | 简历相关经验与岗位级别所需年限 |
| 项目证据 | 15 | 工作或项目中的技能原文证据 |
| 技能时效 | 10 | 最近工作/项目中的使用时间 |
| 加分技能覆盖 | 5 | 岗位 preferred skills |
| 职责与场景 | 10 | 简历职责/行业场景与岗位画像文本命中 |

每个维度先计算 0–1 比例，再乘权重。总分不是无条件相加到 100：

- 岗位画像没有任何有效要求或信号时，总分上限为 0；
- 缺少流行度不低于 0.65 的核心必备技能时，总分上限为 79；
- 精确必备技能覆盖低于 50% 时，总分上限为 59；
- 应用上限时，各维分数按比例缩放，保证维度和等于最终总分。

匹配档位：

- `high`：总分不低于 80；
- `medium`：总分不低于 60 且低于 80；
- `low`：总分低于 60。

置信度与分数分开。缺少项目/工作结构化证据时置信度为低；画像样本不稳定或画像置信度低于 0.6 时为中；只有画像稳定且多数核心技能有项目/工作证据时才可能为高。

## 十、Top 5 推荐排序

候选画像采用以下稳定排序键：

1. 总分降序；
2. 置信度降序；
3. 技能证据数降序；
4. 画像样本数降序；
5. 岗位名称、岗位族和画像 ID 升序。

排序后每个岗位族只保留第一条，避免同一岗位族的不同季度/层级画像占满 Top 5。最终数量限制为 1–10。

## 十一、模型使用

### 模型客户端

`src/llm.py` 使用 `langchain_openai.ChatOpenAI` 连接 DeepSeek 的 OpenAI 兼容 API：

```text
temperature = 0.2
timeout = 30 秒
max_retries = 1
```

代码注册了 `deepseek-v4-pro` 和 `deepseek-v4-flash` 两个配置标识，实际可用模型以团队账号和 API 服务端为准。仓库不包含模型权重。

### 三个明确的模型增强点

| 场景 | 入口 | 约束与降级 |
| --- | --- | --- |
| JD 结构化抽取 | `POST /api/extraction/jobs/{id}` | 要求 JSON 结构；调用失败返回 503；保存技能原文证据 |
| 带证据知识问答 | `POST /api/knowledge/answer` | 只使用编号证据；校验引用；无证据拒绝，引用无效或模型失败则摘要降级 |
| 简历增强 | `POST /api/resumes/parse?enrich=true` | 默认关闭；新增技能/成果必须能在原文中找到且通过置信度阈值，否则回退规则解析 |

模型不会决定数据来源是否合法，也不会绕过质量门禁。数据去重、门禁、画像、匹配和评测都有确定性实现。

## 十二、评测指标

### 实体抽取

```text
Precision = TP / (TP + FP)
Recall = TP / (TP + FN)
F1 = 2 × Precision × Recall / (Precision + Recall)
Accuracy = 完全正确用例数 / 用例总数
```

简历评测还可记录时间线准确率、证据有效率、项目技能准确率、成果准确率和解析模式准确率。

### 匹配

把总分映射为 high/medium/low，与人工期望档位对比，输出宏平均 Precision、Recall、F1 和完全正确率。

### 推荐排序

- Top-1 Accuracy：第一名是否属于相关岗位族；
- Recall@5：前五名覆盖了多少相关岗位族；
- MRR：第一个相关岗位名次的倒数；
- NDCG@5：考虑相关性等级和排名位置的归一化折损累计增益。

所有指标必须和 `dataset_name`、`sample_count`、评测批次及失败案例一起解读。当前示例评测的 1.0 分数不能替代正式标注集结论。

## 十三、主要 API 输出入口

| 接口 | 输出重点 |
| --- | --- |
| `GET /api/data/stats` | 岗位、有效/重复、来源、技能、证据、画像和审核统计 |
| `GET /api/analysis/quarterly-profiles` | 分岗位族/层级/季度画像 |
| `GET /api/analysis/evolution/{family}` | 前后季度、技能变化和证据 |
| `GET /api/graph` | 节点、关系、版本和可选证据 |
| `GET /api/knowledge/search` | 实际检索模式、分数、知识片段和来源 URL |
| `POST /api/knowledge/answer` | 回答、引用、证据和降级状态 |
| `POST /api/resumes/parse` | 结构化简历、解析模式和警告 |
| `POST /api/matches/recommend` | Top N、完整匹配解释、缺口和学习计划 |
| `GET /api/evaluation/summary` | 各指标最近批次、样本量、失败项和就绪状态 |
| `GET /api/acceptance/summary` | 最低门槛、内部目标、当前值和差距 |

可在系统启动后打开 `http://127.0.0.1:8000/docs` 查看 FastAPI 自动生成的交互式接口文档。
