# 评测标注集说明

`benchmark-example.jsonl` 仅用于验证文件格式和自动评测流程，不是参赛要求的最终测试集，也不能作为“100条真实JD”的证明。

`../synthetic_resumes/` 中的64份简历及三类评测文件用于系统回归测试，可通过 `python -m src.generate_synthetic_resumes` 重建。它们全部是合成数据，不得计入真实采集量，不得与独立人工标注集混合，也不能用于证明正式评测准确率。

## 最终数据要求

- 从团队实际采集的5000-10000条岗位数据中抽取独立评测集。
- `jd_parsing` 至少标注100条真实JD，建议覆盖人工智能、大数据、智能系统、物联网及传统软件岗位。
- 每条JD必须保留可访问的 `source_url`、采集时间和稳定 `case_id`，禁止虚构来源。
- 标注集不得用于调整技能词典、规则阈值或提示词，避免测试数据泄漏。
- 每条数据由两名成员独立标注，冲突由第三名成员复核，并保留冲突处理记录。
- 建议同时准备不少于30条简历解析案例和不少于50条匹配案例，覆盖 high、medium、low 三档。

## 四类记录

### JD解析

`expected.required_skills` 与 `expected.preferred_skills` 分开标注。系统以带类型的技能集合计算 Precision、Recall、F1，以整条技能集合完全一致计算 Accuracy。

### 简历提取

标注 `skills`、`experience_years` 和 `education`。需要评测结构化时间线时，增加 `work_ranges`，每项包含 `start`、`end`，格式为 `YYYY-MM`；需要评测证据时，增加 `evidence_substrings`，其内容必须能在简历原文及解析证据中找到。项目能力可增加 `project_skills`，量化成果可增加 `achievements`，解析方式可增加 `parser_mode`。系统额外计算 `timeline_accuracy`、`evidence_validity`、`project_skill_accuracy`、`achievement_accuracy` 和 `parser_mode_accuracy`。

### 人岗匹配

人工给出 `high`、`medium`、`low` 匹配档位。系统按照匹配分数 `>=80`、`>=60`、`<60` 生成预测档位并计算分类指标。

### 岗位推荐

`input.resume` 提供结构化简历，`input.candidates` 提供候选岗位画像。`expected.relevance` 使用“岗位族编码: 相关性等级”格式，例如 `{"JAVA_DEVELOPER":3,"BACKEND_ENGINEER":2}`。相关性等级必须大于0，数值越高表示越相关。系统按线上推荐逻辑计算 Top-1 Accuracy、Recall@5、MRR 和 NDCG@5，并按岗位族去重。

## 使用方式

启动系统后进入“模型质量评测”，上传JSONL或JSON文件并点击“运行自动评测”。核心质量状态仍按以下三项判断，岗位推荐指标作为独立扩展结果保存：

每个上传文件形成独立评测批次；质量状态不会混合不同批次的最新单项结果。

1. JD解析案例不少于100条。
2. JD解析、简历提取、人岗匹配三项指标均已测量。
3. 三项Accuracy均不低于90%。
