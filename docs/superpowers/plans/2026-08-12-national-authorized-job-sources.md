# 全国大型招聘平台授权采集 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 BOSS直聘、前程无忧、猎聘、拉勾招聘及全国公共就业平台的授权岗位数据安全接入现有采集流水线，并把正式库补充到 5000 至 10000 条可用唯一岗位。

**Architecture:** 保留现有 `SourceRegistry -> CollectionService -> staging -> guarded commit -> hard metrics pipeline` 主链路。新增本机授权清单、通用授权导出适配器、平台字段配置和覆盖配额服务；每个平台单独暂存和提交，最终统一执行跨平台去重、质量门禁、知识库、知识图谱和验收重建。没有正式 API 契约的平台使用授权导出文件，不解析浏览器私有接口，也不绕过访问控制。

**Tech Stack:** Python 3.11、Pydantic 2、SQLAlchemy 2、SQLite、httpx、pytest、标准库 `json`/`csv`/`hashlib`。

---

## File Map

- `config/job_sources.json`: 登记六个全国性授权导出来源。
- `config/job_collection_targets.json`: 保存总量、来源、岗位族和城市层级目标。
- `.gitignore`: 排除本机授权清单和待导入原始文件。
- `.env.example`: 说明授权 API 凭据只能通过环境变量提供。
- `src/job_collection/authorization.py`: 加载并校验本机授权清单，不接触凭据值。
- `src/job_collection/adapters/authorized_export.py`: 读取 JSONL、JSON、CSV 授权导出并规范化记录。
- `src/job_collection/coverage.py`: 计算总量、来源、岗位族和地域覆盖缺口。
- `src/job_collection/service.py`: 将通用授权导出适配器接入现有暂存、证据和提交校验。
- `src/collect_jobs.py`: 允许所有已登记的 `file_import` 来源使用显式输入文件与授权引用。
- `src/acceptance_service.py`: 将可用唯一岗位目标调整为 5000 至 10000，并保留 8 域名、3 类型、35% 上限。
- `src/rebuild_hard_metrics.py`: 增加采集完成后的统一全量重建入口参数。
- `tests/fixtures/authorized_exports/`: 保存完全虚构且无个人信息的平台格式样例。
- `tests/test_authorized_source_grants.py`: 授权清单失败关闭测试。
- `tests/test_authorized_export_adapter.py`: 多格式、多平台映射、脱敏和证据测试。
- `tests/test_collection_coverage.py`: 数量与覆盖缺口测试。
- `tests/test_collection_service.py`: 授权文件暂存、恢复和提交语义测试。
- `tests/test_collect_jobs_cli.py`: 通用授权来源命令行测试。
- `tests/test_acceptance_service.py`: 5000 至 10000 条及来源多样性验收测试。
- `docs/NATIONAL_AUTHORIZED_DATA_INTAKE.md`: 团队交付文件与正式运行说明。

项目当前不是 Git 仓库，因此计划不包含无法执行的 commit 步骤。每项任务以对应测试通过和计划勾选作为检查点。

### Task 1: 建立本机授权清单的失败关闭模型

**Files:**
- Create: `src/job_collection/authorization.py`
- Create: `tests/test_authorized_source_grants.py`
- Modify: `.gitignore`
- Modify: `.env.example`

- [x] **Step 1: Write failing authorization tests**

```python
from datetime import date

import pytest

from src.job_collection.authorization import (
    AuthorizationBlocked,
    load_authorized_source_grants,
)


def test_grant_requires_reference_scope_method_and_future_expiry(tmp_path):
    path = tmp_path / "authorized_job_sources.local.json"
    path.write_text(
        '{"sources":{"boss_zhipin_authorized":{"authorization_reference":'
        '"AUTH-BOSS-2026-001","valid_until":"2026-12-31",'
        '"access_methods":["file_export"],"scope":"全国公开招聘岗位"}}}',
        encoding="utf-8",
    )
    grants = load_authorized_source_grants(path, today=date(2026, 8, 12))
    assert grants.require("boss_zhipin_authorized", "file_export").scope == "全国公开招聘岗位"


@pytest.mark.parametrize("bad_key", ["token", "password", "cookie", "secret", "api_key"])
def test_grant_manifest_rejects_embedded_credentials(tmp_path, bad_key):
    path = tmp_path / "authorized_job_sources.local.json"
    path.write_text(
        '{"sources":{"boss_zhipin_authorized":{'
        '"authorization_reference":"AUTH-BOSS-2026-001",'
        '"valid_until":"2026-12-31","access_methods":["file_export"],'
        f'"scope":"全国公开招聘岗位","{bad_key}":"sensitive"}}}}',
        encoding="utf-8",
    )
    with pytest.raises(AuthorizationBlocked):
        load_authorized_source_grants(path, today=date(2026, 8, 12))


def test_expired_or_missing_source_is_blocked(tmp_path):
    path = tmp_path / "authorized_job_sources.local.json"
    path.write_text(
        '{"sources":{"boss_zhipin_authorized":{'
        '"authorization_reference":"AUTH-BOSS-2025-001",'
        '"valid_until":"2025-12-31","access_methods":["file_export"],'
        '"scope":"全国公开招聘岗位"}}}',
        encoding="utf-8",
    )
    grants = load_authorized_source_grants(path, today=date(2026, 8, 12))
    with pytest.raises(AuthorizationBlocked):
        grants.require("boss_zhipin_authorized", "file_export")
```

- [x] **Step 2: Run the focused test and verify it fails**

Run: `python -m pytest tests/test_authorized_source_grants.py -q`

Expected: collection fails because `src.job_collection.authorization` does not exist.

- [x] **Step 3: Implement strict grant loading**

Implement these public types and functions in `authorization.py`:

```python
class AuthorizationBlocked(PermissionError): ...

class AuthorizedSourceGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    authorization_reference: str = Field(min_length=6, max_length=200)
    valid_until: date
    access_methods: tuple[Literal["file_export", "api"], ...] = Field(min_length=1)
    scope: str = Field(min_length=6, max_length=1000)
    credential_env_vars: tuple[str, ...] = ()

class AuthorizedSourceGrants:
    def require(self, source_id: str, method: str) -> AuthorizedSourceGrant: ...

def load_authorized_source_grants(path: str | Path, *, today: date | None = None) -> AuthorizedSourceGrants: ...
```

The loader must reject symlinks, files larger than 256 KiB, duplicate source ids, expired grants, unknown fields, credential-looking keys or values, and environment variable names outside `^[A-Z][A-Z0-9_]{2,80}$`. It records only variable names, never their values.

Add these lines to `.gitignore`:

```gitignore
config/authorized_job_sources.local.json
data/incoming/authorized/
```

Add only variable-name examples to `.env.example`:

```dotenv
# Authorized platform API credentials are local-only. Populate only when the grant permits API access.
BOSS_AUTHORIZED_API_TOKEN=
JOB51_AUTHORIZED_API_TOKEN=
LIEPIN_AUTHORIZED_API_TOKEN=
LAGOU_AUTHORIZED_API_TOKEN=
```

- [x] **Step 4: Run authorization and security tests**

Run: `python -m pytest tests/test_authorized_source_grants.py tests/test_collection_security.py -q`

Expected: all selected tests pass and no secret value appears in pytest output.

### Task 2: 登记全国大型平台和采集目标

**Files:**
- Modify: `config/job_sources.json`
- Create: `config/job_collection_targets.json`
- Modify: `tests/test_source_registry.py`
- Create: `tests/test_collection_coverage.py`

- [x] **Step 1: Add failing registry assertions**

```python
AUTHORIZED_EXPORT_SOURCES = {
    "boss_zhipin_authorized": "zhipin.com",
    "job51_authorized": "51job.com",
    "liepin_authorized": "liepin.com",
    "lagou_authorized": "lagou.com",
    "newjobs_authorized": "newjobs.com.cn",
    "jobonline_authorized": "jobonline.cn",
}


def test_national_authorized_sources_are_china_file_imports(default_registry):
    for source_id, domain in AUTHORIZED_EXPORT_SOURCES.items():
        source = default_registry.get(source_id)
        assert source.market_scope == "china"
        assert source.source_type in {"authorized_platform", "public_service"}
        assert source.collection_mode == "file_import"
        assert source.compliance_status == "manual_only"
        assert domain in source.base_url
        assert source.enabled is True
```

Add coverage-config tests that require:

```python
assert targets.minimum_usable_unique == 5000
assert targets.maximum_usable_unique == 10000
assert targets.minimum_source_domains == 8
assert targets.minimum_source_types == 3
assert targets.maximum_single_domain_share == 0.35
assert targets.minimum_usable_per_family == 100
assert set(targets.required_job_families) == set(JOB_FAMILY_NAMES)
assert sum(targets.source_targets.values()) >= 5300
```

- [x] **Step 2: Run tests and verify missing-source failures**

Run: `python -m pytest tests/test_source_registry.py tests/test_collection_coverage.py -q`

Expected: failures identify the six missing sources and missing target model.

- [x] **Step 3: Add the six source definitions**

Use these exact ids and domains in `job_sources.json`:

| source_id | base_url | source_type | usable target |
| --- | --- | --- | ---: |
| `boss_zhipin_authorized` | `https://www.zhipin.com` | `authorized_platform` | 1500 |
| `job51_authorized` | `https://we.51job.com` | `authorized_platform` | 1000 |
| `liepin_authorized` | `https://www.liepin.com` | `authorized_platform` | 800 |
| `lagou_authorized` | `https://www.lagou.com` | `authorized_platform` | 700 |
| `newjobs_authorized` | `https://www.newjobs.com.cn` | `public_service` | 400 |
| `jobonline_authorized` | `https://www.jobonline.cn` | `public_service` | 400 |

Each source uses `allowed_paths: ["/"]`, `collection_mode: "file_import"`, `compliance_status: "manual_only"`, `max_pages: 1`, `max_records: 10000`, a platform-specific parser name, and a compliance note stating that only a local grant plus an authorized export is accepted and no network request is made from file-import mode.

Create `job_collection_targets.json` with the acceptance values above, all 22 family codes from `job_family_queries.json`, and city-tier minimum shares:

```json
{
  "minimum_usable_unique": 5000,
  "maximum_usable_unique": 10000,
  "minimum_source_domains": 8,
  "minimum_source_types": 3,
  "maximum_single_domain_share": 0.35,
  "minimum_usable_per_family": 100,
  "source_targets": {
    "boss_zhipin_authorized": 1500,
    "job51_authorized": 1000,
    "liepin_authorized": 800,
    "lagou_authorized": 700,
    "ncss_public_jobs": 500,
    "mohrss_public_jobs": 500,
    "newjobs_authorized": 400,
    "jobonline_authorized": 400
  },
  "city_tier_minimum_shares": {
    "tier_1": 0.15,
    "new_tier_1": 0.25,
    "tier_2": 0.20,
    "other": 0.15
  },
  "required_job_families": [
    "JAVA_DEVELOPER",
    "PYTHON_BACKEND",
    "GO_DEVELOPER",
    "FRONTEND_DEVELOPER",
    "DEVOPS_ENGINEER",
    "SRE_ENGINEER",
    "CLOUD_NATIVE_ENGINEER",
    "AI_AGENT_ENGINEER",
    "LLM_APPLICATION_ENGINEER",
    "RAG_ENGINEER",
    "MLOPS_ENGINEER",
    "MULTIMODAL_ENGINEER",
    "PROMPT_ENGINEER",
    "AI_SOLUTION_ENGINEER",
    "BIG_DATA_DEVELOPER",
    "DATA_GOVERNANCE_ENGINEER",
    "DATA_ENGINEER",
    "IOT_ENGINEER",
    "EDGE_COMPUTING_ENGINEER",
    "CYBERSECURITY_ENGINEER",
    "DIGITAL_TWIN_ENGINEER",
    "ROBOTICS_ENGINEER"
  ]
}
```

Validate that `required_job_families` exactly matches the keys in `job_family_queries.json`; configuration drift must fail startup.

- [x] **Step 4: Re-run registry tests**

Run: `python -m pytest tests/test_source_registry.py tests/test_collection_docs.py -q`

Expected: all selected tests pass; existing blocked and disabled sources remain unchanged.

### Task 3: 实现通用授权导出适配器

**Files:**
- Create: `src/job_collection/adapters/authorized_export.py`
- Modify: `src/job_collection/adapters/__init__.py`
- Create: `tests/test_authorized_export_adapter.py`
- Create: `tests/fixtures/authorized_exports/boss_jobs.jsonl`
- Create: `tests/fixtures/authorized_exports/job51_jobs.csv`
- Create: `tests/fixtures/authorized_exports/liepin_jobs.json`
- Create: `tests/fixtures/authorized_exports/lagou_jobs.jsonl`

- [x] **Step 1: Write failing adapter tests**

The tests must prove all of these behaviors:

```python
record = adapter.load_file(
    fixture,
    run_id="authorized-export-fixture",
    authorization_reference="AUTH-BOSS-2026-001",
    authorization_scope="全国公开招聘岗位",
    max_records=20,
)[0]
assert record.source_id == "boss_zhipin_authorized"
assert record.source_domain == "www.zhipin.com"
assert record.collection_method == "file_import"
assert record.adapter_extra["authorization_reference"] == "AUTH-BOSS-2026-001"
assert len(record.adapter_extra["input_file_sha256"]) == 64
assert record.job_title_raw == "Java开发工程师"
assert record.company_name == "示例科技有限公司"
assert record.published_at_trusted is True
assert "13800138000" not in record.job_description_raw
assert "fixture@example.test" not in record.model_dump_json()
```

Also assert JSONL, JSON arrays and CSV are accepted; UTF-8 BOM is handled; lines over 1 MiB, files over 100 MiB, duplicate source ids, missing descriptions, external URLs, unknown encodings and formula-prefixed CSV cells are rejected or quarantined with deterministic reason codes.

- [x] **Step 2: Run the focused adapter test and verify it fails**

Run: `python -m pytest tests/test_authorized_export_adapter.py -q`

Expected: import failure for the missing adapter.

- [x] **Step 3: Implement one parser with platform field profiles**

Expose this interface:

```python
class AuthorizedExportAdapterError(ValueError): ...

class AuthorizedExportAdapter:
    def __init__(self, *, source: SourceDefinition, registry: SourceRegistry): ...

    def load_file(
        self,
        path: Path,
        *,
        run_id: str,
        authorization_reference: str,
        authorization_scope: str,
        max_records: int,
    ) -> list[UnifiedJobRecord]: ...
```

Use one canonical alias map for Chinese and English export headings:

```python
FIELD_ALIASES = {
    "source_record_id": ("岗位ID", "职位ID", "job_id", "position_id"),
    "job_title": ("岗位名称", "职位名称", "job_name", "position_name"),
    "company": ("公司名称", "企业名称", "company_name"),
    "region": ("工作城市", "工作地点", "城市", "city", "location"),
    "salary": ("薪资", "薪资范围", "salary", "salary_desc"),
    "experience": ("经验要求", "工作经验", "experience"),
    "education": ("学历要求", "学历", "education"),
    "description": ("岗位描述", "职位描述", "任职要求", "description", "job_description"),
    "published_at": ("发布时间", "更新日期", "发布日期", "published_at", "update_time"),
    "source_url": ("原始链接", "职位链接", "source_url", "job_url")
}
```

Reuse the MOHRSS PII behavior through a new small shared helper only if extraction does not alter existing MOHRSS outputs. Otherwise implement the same contact-line, phone, landline, email, WeChat and QQ removal rules locally and add equivalence tests before later refactoring.

Do not preserve untouched source rows in `adapter_extra`. Preserve only non-sensitive field names, row number, file hash, authorization reference/scope, parser findings and quality findings.

- [x] **Step 4: Run adapter, normalizer and PII tests**

Run: `python -m pytest tests/test_authorized_export_adapter.py tests/test_job_collection_normalizer.py tests/test_mohrss_adapter.py -q`

Expected: all selected tests pass and existing MOHRSS behavior is unchanged.

### Task 4: 将所有授权导出来源接入采集服务和 CLI

**Files:**
- Modify: `src/job_collection/service.py`
- Modify: `src/collect_jobs.py`
- Modify: `src/job_collection/storage.py`
- Modify: `tests/test_collection_service.py`
- Modify: `tests/test_collect_jobs_cli.py`

- [x] **Step 1: Write failing service and CLI tests**

Add parameterized tests over all six new source ids. Assert a dry-run requires exactly one file-import source, `--input-file`, `--authorization-manifest`, and a matching unexpired grant. Assert `zhaopin_legacy_import` remains compatible with its existing note-based path.

```python
args = parser.parse_args([
    "--dry-run",
    "--source", "boss_zhipin_authorized",
    "--input-file", str(input_file),
    "--authorization-manifest", str(grants_file),
    "--max-records", "20",
])
validate_args(args)
```

Add negative assertions for a mismatched source grant, expired authorization, unsupported extension, multiple file sources in one run, missing authorization manifest, symlinked input, and resume metadata that changes the file hash or authorization reference.

- [x] **Step 2: Run tests and verify current Zhaopin-only restrictions fail**

Run: `python -m pytest tests/test_collect_jobs_cli.py tests/test_collection_service.py -q`

Expected: failures point to the hard-coded `zhaopin_legacy_import` restriction and `LegacyFileAdapter` branch.

- [x] **Step 3: Generalize file-import orchestration**

Add CLI option:

```python
parser.add_argument(
    "--authorization-manifest",
    type=Path,
    help="Local grant manifest required by authorized file-import sources.",
)
```

Add `--authorization-preflight` as a third mutually exclusive mode. It requires exactly one `--source`, one `--input-file`, and `--authorization-manifest`; it prints only source id, authorization reference, validity date, access method, input filename, SHA-256 and row count, and performs no database write.

Rules:

- A new file-import dry-run selects exactly one source.
- The selected source must be enabled, `market_scope=china`, `collection_mode=file_import`, and `compliance_status=manual_only`.
- `zhaopin_legacy_import` keeps `--authorization-note`; the six new sources require `--authorization-manifest` and reject `--authorization-note`.
- Supported authorized-export extensions are `.jsonl`, `.json`, `.csv`.
- Commit continues to accept only `--resume-run --confirm` and recomputes evidence from the immutable snapshot.

Replace the service's source-id check with parser dispatch:

```python
if source.parser_name == "zhaopin_legacy":
    adapter = LegacyFileAdapter(source=source, registry=self.registry)
else:
    grant = grants.require(source.source_id, "file_export")
    adapter = AuthorizedExportAdapter(source=source, registry=self.registry)
```

Store only the manifest SHA-256, authorization reference, validity date, scope hash and input file SHA-256 in checkpoint and report metadata. Do not copy the authorization manifest into `data/collections`.

- [x] **Step 4: Preserve semantic recomputation on commit**

Extend `_validate_semantic_evidence` so commit reloads the snapped export with the current parser and verifies every staged canonical record, source id, input hash, authorization reference and gate bucket. A changed parser version, source registry definition, grant identity or snapshot hash must block commit.

- [x] **Step 5: Run collection regression tests**

Run: `python -m pytest tests/test_collection_service.py tests/test_collect_jobs_cli.py tests/test_collection_http.py tests/test_collection_security.py -q`

Expected: all selected tests pass, including resume, attestation, lock, budget and idempotency tests.

### Task 5: 增加全国覆盖缺口和批次配额服务

**Files:**
- Create: `src/job_collection/coverage.py`
- Create: `tests/test_collection_coverage.py`
- Modify: `src/collect_jobs.py`
- Modify: `tests/test_collect_jobs_cli.py`

- [x] **Step 1: Write failing coverage tests**

```python
report = build_coverage_report(
    targets=targets,
    usable_rows=rows,
)
assert report["usable_unique"]["current"] == 1297
assert report["usable_unique"]["gap_to_minimum"] == 3703
assert set(report["missing_families"]) == {
    "AI_SOLUTION_ENGINEER",
    "MLOPS_ENGINEER",
    "MULTIMODAL_ENGINEER",
    "PROMPT_ENGINEER",
}
assert report["source_domains"]["target"] == 8
assert report["maximum_single_domain_share"]["target"] == 0.35
assert report["minimum_usable_per_family"]["target"] == 100
```

Test that batch recommendations prioritize families below 100 usable records, then the lowest valid family count, then underrepresented city tiers, and never recommend a source above its target or the 35% projected share.

- [x] **Step 2: Run tests and verify the missing module failure**

Run: `python -m pytest tests/test_collection_coverage.py -q`

Expected: import failure for `src.job_collection.coverage`.

- [x] **Step 3: Implement deterministic coverage reporting**

Expose:

```python
class CollectionTargets(BaseModel): ...
def load_collection_targets(path: str | Path) -> CollectionTargets: ...
def build_coverage_report(*, targets: CollectionTargets, usable_rows: Sequence[Mapping[str, object]]) -> dict[str, object]: ...
def recommend_batches(*, targets: CollectionTargets, coverage: Mapping[str, object], batch_size: int = 100) -> tuple[dict[str, object], ...]: ...
```

Use only `valid` and nonduplicate canonical postings for completion counts. `review`, `duplicate`, `quarantined` and unknown-domain rows remain visible but cannot close a quota.

- [x] **Step 4: Add read-only CLI mode**

Add `--coverage-report` as a mutually exclusive mode in `collect_jobs.py`. It reads the current database and target config, prints JSON, and performs no network or file import. It rejects collection-only arguments.

- [x] **Step 5: Run coverage and CLI tests**

Run: `python -m pytest tests/test_collection_coverage.py tests/test_collect_jobs_cli.py -q`

Expected: all selected tests pass with deterministic ordering.

### Task 6: 对齐验收范围并连接全量派生流水线

**Files:**
- Modify: `src/acceptance_service.py`
- Modify: `tests/test_acceptance_service.py`
- Modify: `src/rebuild_hard_metrics.py`
- Modify: `tests/test_rebuild_hard_metrics.py`

- [x] **Step 1: Write failing acceptance tests**

```python
assert result["internal"]["metrics"]["usable_unique_job_postings"]["target"] == [5000, 10000]
assert result["internal"]["metrics"]["source_domains"]["target"] == 8
assert result["internal"]["metrics"]["source_types"]["target"] == 3
assert result["internal"]["metrics"]["maximum_single_domain_share"]["target"] == 0.35
```

Add boundary cases for 4999, 5000, 10000 and 10001 usable unique postings. Keep raw posting count informational.

- [x] **Step 2: Run acceptance tests and verify the current 7000 upper-bound failure**

Run: `python -m pytest tests/test_acceptance_service.py -q`

Expected: the target assertion fails with `[5000, 7000]`.

- [x] **Step 3: Read targets from one configuration source**

Replace the hard-coded usable range and source-diversity numbers in `acceptance_service.py` with `load_collection_targets()`. Do not expose competition wording in the UI; this remains an internal metric calculation.

- [x] **Step 4: Add a post-import rebuild switch**

Add `--after-collection-run RUN_ID` to `rebuild_hard_metrics.py`. It validates that the collection run exists, is committed, and has a verified backup and report. Then it executes one full hard-metrics pipeline covering duplicate rebalance, quality gates, levels, quarterly profiles, adjacent-quarter evolution, knowledge chunks and acceptance snapshot. Existing repair modes remain mutually exclusive with this switch.

- [x] **Step 5: Run acceptance and pipeline tests**

Run: `python -m pytest tests/test_acceptance_service.py tests/test_rebuild_hard_metrics.py tests/test_hard_metrics_pipeline.py tests/test_knowledge_service.py -q`

Expected: all selected tests pass and repeated rebuilds are idempotent.

### Task 7: 编写团队交付和运行说明

**Files:**
- Create: `docs/NATIONAL_AUTHORIZED_DATA_INTAKE.md`
- Modify: `README.md`
- Modify: `QUICKSTART.md`
- Modify: `tests/test_collection_docs.py`

- [x] **Step 1: Add failing documentation assertions**

Assert the documentation contains:

```text
config/authorized_job_sources.local.json
data/incoming/authorized/boss_zhipin/jobs.jsonl
python -m src.collect_jobs --coverage-report
--authorization-manifest
--after-collection-run
5000 至 10000
单一来源不超过 35%
```

Also assert the document explicitly forbids resume/chat/contact collection, CAPTCHA bypass, proxy rotation, signature reversal and synthetic-job padding.

- [x] **Step 2: Run doc tests and verify missing instructions**

Run: `python -m pytest tests/test_collection_docs.py -q`

Expected: failures list the missing nationwide intake documentation.

- [x] **Step 3: Write exact team handoff instructions**

Document one directory per platform:

```text
data/incoming/authorized/boss_zhipin/jobs.jsonl
data/incoming/authorized/job51/jobs.csv
data/incoming/authorized/liepin/jobs.json
data/incoming/authorized/lagou/jobs.jsonl
data/incoming/authorized/newjobs/jobs.jsonl
data/incoming/authorized/jobonline/jobs.jsonl
```

Specify required fields, UTF-8 encoding, one-posting-per-row semantics, authorization manifest schema, dry-run command, report review, commit command, rebuild command and coverage-report command. State that API credentials are not accepted in chat, source files or Git-tracked config.

- [x] **Step 4: Run documentation tests**

Run: `python -m pytest tests/test_collection_docs.py -q`

Expected: all selected tests pass.

### Task 8: 完成生产预检、分批导入和最终验收

**Files:**
- Read: `config/authorized_job_sources.local.json`
- Read: `data/incoming/authorized/*/jobs.*`
- Generate: `data/collections/<run-id>/report.json`
- Generate: `data/backups/job_competency-<timestamp>.db`
- Generate: `data/exports/knowledge-graph.json`

- [x] **Step 1: Run static and focused verification before production data**

Run:

```powershell
python -m ruff check src tests config
python -m pytest tests/test_authorized_source_grants.py tests/test_authorized_export_adapter.py tests/test_collection_coverage.py tests/test_collection_service.py tests/test_collect_jobs_cli.py tests/test_acceptance_service.py -q
```

Expected: both commands succeed. If `ruff` is not installed in the active environment, use the workspace's established Ruff executable; do not skip linting silently.

- [x] **Step 2: Validate real grants and input inventory without displaying secrets or row contents**

Run `python -m src.collect_jobs --coverage-report`, then run the explicit preflight once per supplied platform file. For example:

```powershell
python -m src.collect_jobs --authorization-preflight --source boss_zhipin_authorized --input-file data/incoming/authorized/boss_zhipin/jobs.jsonl --authorization-manifest config/authorized_job_sources.local.json
```

The command must fail if the file is missing, a grant is expired, a source does not match, or a production authorization reference contains fixture/example text.

- [ ] **Step 3: Stage each platform independently**

For each available platform, run a 20-record dry-run first. Example:

```powershell
$smokeRunId = "boss-smoke-" + (Get-Date -Format "yyyyMMdd-HHmmss")
python -m src.collect_jobs --dry-run --source boss_zhipin_authorized --input-file data/incoming/authorized/boss_zhipin/jobs.jsonl --authorization-manifest config/authorized_job_sources.local.json --max-records 20 --run-id $smokeRunId
```

Review `report.json`. Proceed only when the source is completed, no provenance error exists, and valid plus review plus quarantine plus duplicate counts reconcile with fetched records.

- [ ] **Step 4: Stage and commit bounded production batches**

Use batches no larger than 1000 records and unique run ids. Commit only completed staging runs:

```powershell
$productionRunId = "boss-production-" + (Get-Date -Format "yyyyMMdd-HHmmss")
python -m src.collect_jobs --dry-run --source boss_zhipin_authorized --input-file data/incoming/authorized/boss_zhipin/jobs.jsonl --authorization-manifest config/authorized_job_sources.local.json --max-records 1000 --run-id $productionRunId
python -m src.collect_jobs --commit --resume-run $productionRunId --confirm
```

Reuse the same `$productionRunId` only for that verified dry-run and its commit. Stop importing a source once its usable target or projected 35% share is reached.

- [ ] **Step 5: Rebuild derived data after each accepted source batch**

Run:

```powershell
python -m src.rebuild_hard_metrics --full --confirm --after-collection-run $productionRunId
```

Verify the database backup with `PRAGMA integrity_check`, then confirm the pipeline status is `completed` and the graph export contains no foreign-domain source URLs.

- [ ] **Step 6: Continue deficit-driven batches until every hard condition passes**

After each rebuild, run the coverage report and use its recommended batches. Completion requires all of these simultaneously:

```text
5000 <= usable_unique_job_postings <= 10000
family_coverage == 22
minimum_usable_samples_per_covered_family >= 100
source_domain_count >= 8
source_type_count >= 3
maximum_single_domain_share <= 0.35
missing_families == []
foreign_domain_rows == 0
```

Do not import duplicate, quarantined or synthetic rows to close a gap.

- [x] **Step 7: Run full regression verification**

Run:

```powershell
python -m ruff check src tests config
python -m pytest -c pytest-full.ini -q
```

Expected: all tests pass, coverage remains above the configured threshold, and no collection session remains running.

- [x] **Step 8: Record the final evidence summary**

Report final raw count, usable unique count, gate distribution, all 22 family counts, source-domain counts and shares, city-tier distribution, trusted-time coverage, pipeline run id, graph node/edge counts, database backup path and SHA-256, graph export path and SHA-256, plus every unmet metric if completion was blocked by absent authorized input.
