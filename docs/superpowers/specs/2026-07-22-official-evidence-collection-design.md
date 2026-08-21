# 官方外部标准证据采集设计

## 目标

为现有 8 个岗位族各补充 3 条可公开核验的官方外部证据，共 24 条。证据用于岗位画像交叉验证和知识问答引用，不替代原始 JD，也不以模型生成内容冒充标准原文。

## 岗位族范围

- `BIG_DATA_DEVELOPER`：大数据开发工程师；
- `CYBERSECURITY_ENGINEER`：网络安全工程师；
- `DATA_ENGINEER`：数据工程师；
- `DATA_GOVERNANCE_ENGINEER`：数据治理工程师；
- `DIGITAL_TWIN_ENGINEER`：数字孪生工程师；
- `EDGE_COMPUTING_ENGINEER`：边缘计算工程师；
- `IOT_ENGINEER`：物联网工程师；
- `ROBOTICS_ENGINEER`：机器人与智能系统工程师。

## 三层证据结构

每个岗位族配置以下三类证据：

1. **岗位或能力标准层**：国家职业技术技能标准、网络安全工作角色框架、能力成熟度标准或等价的官方能力文件。
2. **架构与技术标准层**：国家标准、ISO/IEC、NIST、ETSI 等机构发布的架构、治理、安全或互操作标准。
3. **官方工程实践层**：Apache、Kubernetes、ROS 等项目的官方文档，用于补足可操作的技术栈和工程职责。

当某岗位族没有独立职业标准时，使用与该岗位直接相关的国家或国际能力/架构标准替代，不使用商业培训文章、媒体转载或个人博客。

## 来源准入规则

来源域名必须属于以下范围之一：

- 中国政府部门及国家标准平台：`gov.cn`、`mohrss.gov.cn`、`miit.gov.cn`、`openstd.samr.gov.cn`；
- 国际标准与公共技术机构：`iso.org`、`iec.ch`、`nist.gov`、`etsi.org`；
- 官方开源项目：`apache.org`、`kubernetes.io`、`docs.ros.org`、`ros.org`；
- 经核验的其他标准组织官方域名，但必须在来源清单中解释采用理由。

搜索结果页、聚合页和转载页不能作为最终 `source_url`。链接必须指向标准详情页、正式 PDF、官方框架页面或官方文档页面。

## 数据格式

生成 `data/evidence/official-standards-2026.jsonl`，每行一个 UTF-8 JSON 对象：

```json
{
  "evidence_id": "OFF-DATA-GOV-001",
  "job_family_id": "DATA_GOVERNANCE_ENGINEER",
  "evidence_type": "technical_standard",
  "title": "数据管理能力成熟度评估模型",
  "publisher": "国家市场监督管理总局、国家标准化管理委员会",
  "published_at": "2025-12-31",
  "source_url": "https://openstd.samr.gov.cn/...",
  "related_skill": "数据治理",
  "evidence_summary": "标准从数据战略、治理、架构、应用、安全和质量等方面定义数据管理能力，可用于映射数据治理岗位的制度建设、质量管理与持续改进职责。"
}
```

约束如下：

- `evidence_id` 全局唯一，使用 `OFF-<岗位缩写>-001..003`；
- `job_family_id` 必须是现有 8 个岗位族之一；
- `evidence_type` 只能是 `occupation_standard`、`technical_standard`、`policy_document` 或 `official_document`；
- `published_at` 使用来源页面明确显示的发布日期；无法确认时省略，不推测日期；
- `related_skill` 使用一个最能代表该证据的技能或能力主题；
- `evidence_summary` 为 60 至 200 个汉字的事实性概括，不复制长段原文；
- `source_url` 不允许使用 `example.com`、搜索结果页或非官方镜像。

## 来源核验清单

生成 `data/evidence/official-standards-2026-sources.md`，每条证据记录：

- 证据编号与岗位族；
- 官方标题和发布机构；
- 最终来源链接；
- 页面显示的标准编号或文件版本；
- 访问日期；
- 采用理由；
- 公开状态：全文、摘要、详情页或正式 PDF。

## 可信度权重

扩展 `SOURCE_SCORES`：

- `occupation_standard = 1.00`；
- `technical_standard = 0.98`；
- `policy_document = 0.95`；
- `official_document = 0.92`。

导入接口继续根据 `evidence_type` 赋值，不增加人工填写可信度字段。

## 校验与入库

新增独立校验脚本或测试，对 JSONL 执行以下检查：

- 总数恰好为 24；
- 8 个岗位族各 3 条；
- 证据编号、标题和来源链接唯一；
- 字段类型、日期格式和摘要长度符合约束；
- 域名属于准入范围；
- 不含示例链接或空摘要；
- 每个岗位族至少覆盖两种证据类型；
- 每个岗位族至少包含一条标准类证据。

通过校验后调用现有证据导入服务写入 `EvidenceRecord`。重复执行时依据 `evidence_id` 跳过，不产生重复记录。导入后数据库中新增 24 条外部证据。

## 知识问答验收

分别对 8 个岗位族运行一个标准相关问题，验证：

- 检索结果包含 `source_kind=external`；
- 同等相关度下外部标准排在普通 JD 前；
- 回答中的引用编号能定位到外部证据卡片；
- 来源链接可访问；
- 外部证据与 JD 表述不一致时，回答明确区分来源，不强行合并。

## 非目标

- 不抓取或存储受版权限制的完整标准全文；
- 不将搜索摘要直接当作证据摘要；
- 不采集新闻、营销文章、培训课程或个人博客；
- 不为凑数量重复使用同一链接；
- 不修改原始岗位数据或岗位族定义。
