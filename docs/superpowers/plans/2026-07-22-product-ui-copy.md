# Product UI Copy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove competition-specific details from the product interface while preserving automated quality evaluation and documenting how users operate the system.

**Architecture:** Keep all API contracts, evaluation thresholds, database models, and business behavior unchanged. Restrict implementation changes to static product copy, FastAPI metadata, regression tests, and a standalone user guide so the change has no data or algorithm migration risk.

**Tech Stack:** HTML/CSS/JavaScript, FastAPI, pytest/httpx, Markdown.

**Repository note:** This workspace is not a Git repository. Use focused tests and file hashes as checkpoints instead of commits.

---

### Task 1: Product-Language Regression Test

**Files:**
- Modify: `tests/test_competition_api.py`

- [x] **Step 1: Add a failing test for product-facing language**

Extend the frontend test to require `模型质量评测`, `核心业务闭环`, `质量评测状态`, and `多源岗位智能分析`. Assert that the HTML does not contain `XH-202621`, `比赛核心闭环`, `赛题要求`, `参赛就绪`, `最终参赛就绪`, `至少100条JD`, `均不低于90%`, `目标 ≥`, or `单测覆盖门槛`.

- [x] **Step 2: Run the focused test and verify RED**

Run: `python -m pytest tests/test_competition_api.py::test_health_and_frontend_are_product_focused -q`

Expected: FAIL because the current interface still contains competition copy.

- [x] **Step 3: Record the test checkpoint**

Run: `Get-FileHash tests/test_competition_api.py`

Expected: a SHA-256 hash for the new regression test.

### Task 2: Productize Interface Copy Without Removing Evaluation

**Files:**
- Modify: `index.html`
- Modify: `src/api.py`

- [x] **Step 1: Replace visible competition copy**

Apply the approved copy mapping:

```text
XH-202621 -> 多源岗位智能分析
比赛核心闭环 -> 核心业务闭环
六个环节均可独立验证，也能串联完成现场演示 -> 六个环节相互衔接，覆盖从数据导入到匹配诊断的完整流程
指标评测 -> 模型质量评测
核心指标评测 -> 模型质量评测
参赛就绪状态 -> 质量评测状态
```

Replace visible target and threshold descriptions with neutral explanations based on labelled-set calculation. Keep calls to `/api/evaluation/summary` and `/api/evaluation/run`, but do not render internal target values, required case counts, or `competition_ready` wording.

- [x] **Step 2: Productize FastAPI metadata**

Set the FastAPI description to `多源异构数据驱动的岗位能力图谱与动态演化分析平台` while leaving the title, version, routes, and responses unchanged.

- [x] **Step 3: Run focused frontend and evaluation tests**

Run: `python -m pytest tests/test_competition_api.py -q`

Expected: all API tests PASS, including automated evaluation behavior.

### Task 3: User Guide and Final Verification

**Files:**
- Create: `USER_GUIDE.md`
- Modify: `README.md`

- [x] **Step 1: Write the operator-facing user guide**

Document startup, seven page workflows, supported import formats, incremental CLI updates, generated artifacts, common status interpretation, and shutdown. Add a dedicated highlights section covering incremental updates, trustworthy governance, traceable evidence, versioned evolution, panoramic graph views, explainable matching, and automated evaluation.

- [x] **Step 2: Link the guide from README**

Add a visible `系统使用指南` link near the README introduction without changing internal competition research documentation.

- [x] **Step 3: Run full automated verification**

Run:

```powershell
python -m compileall -q src model_class schemes config tests
python -m pytest -q
```

Expected: compilation exits 0, all tests pass, and configured coverage remains at or above 60%.

- [x] **Step 4: Run interface text scan**

Run: `rg -n "XH-202621|比赛核心闭环|赛题要求|参赛就绪|最终参赛就绪|至少100条JD|均不低于90%|目标 ≥|单测覆盖门槛" index.html`

Expected: no matches.

- [x] **Step 5: Browser smoke test**

Start the service, inspect the dashboard and model quality evaluation pages in the in-app browser, and verify that product copy appears, evaluation controls remain available, and the browser console has no errors. Stop the service after verification because the user explicitly requested the system remain stopped.
