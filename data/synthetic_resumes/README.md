# 合成简历测试数据

本目录由 `python -m src.generate_synthetic_resumes` 生成，用于简历解析、人岗匹配、岗位推荐和学习路径的自动回归测试。

所有记录均为虚构数据，并标记为 `synthetic=true` 和 `data_usage=test_only`。这些数据不得计入真实岗位或简历采集量，不得作为比赛独立测试集或准确率证明。

## 文件

- `resumes/`：64份UTF-8文本简历。
- `manifest.jsonl`：简历元数据和结构化标准答案。
- `benchmark-resume-extraction.jsonl`：简历解析评测集。
- `benchmark-matching.jsonl`：人岗匹配评测集。
- `benchmark-recommendation.jsonl`：岗位推荐评测集。

固定种子为 `20260805`，重复运行应生成完全一致的内容。正式参赛评测仍应使用与规则、词典和阈值调优隔离的真实人工标注数据。
