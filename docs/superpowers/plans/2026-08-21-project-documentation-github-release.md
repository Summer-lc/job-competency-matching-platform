# Project Documentation and GitHub Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a verified Chinese documentation set for new team members and publish a sanitized, reproducible project snapshot to a new private GitHub repository.

**Architecture:** Keep the existing application unchanged. Add a layered documentation set around the current code, strengthen repository exclusion rules before staging files, verify every reported metric against the local database or test output, and publish only the reviewed project subset. The GitHub repository will start with a clean history so secrets and bulk data never enter any commit.

**Tech Stack:** Markdown, Mermaid, Python 3.11, FastAPI, SQLAlchemy/SQLite, Pytest/pytest-cov, Git, GitHub.

---

## File map

- Modify `README.md`: concise project entry point, status, architecture, quick start, limitations, and document navigation.
- Create `docs/PROJECT_OVERVIEW.md`: completion status, key figures, PPT storyline, gaps, and roadmap.
- Create `docs/ARCHITECTURE_AND_FLOW.md`: component boundaries, code map, runtime sequence, and Mermaid diagrams.
- Create `docs/INPUT_OUTPUT_AND_ALGORITHMS.md`: supported inputs, outputs, APIs, models, algorithms, formulas, and evaluation interpretation.
- Create `docs/ENVIRONMENT_AND_DEPLOYMENT.md`: local setup, environment variables, Docker, optional Neo4j, verification, and troubleshooting.
- Create `docs/FILE_INVENTORY_AND_RELEASE.md`: source/config/test/model/data inventory and upload/exclusion rationale.
- Modify `.gitignore`: exclude secrets, databases, backups, raw collections, temporary test data, caches, and generated reports while preserving reviewed samples.
- Create `docs/assets/screenshots/`: selected screenshots copied from `../项目展示/系统截图/` for PPT and document reuse.
- Create `scripts/verify_release.ps1`: repeatable release checks for tests, tracked large files, prohibited extensions, and common secret patterns.

### Task 1: Establish the release boundary

**Files:**
- Modify: `.gitignore`
- Create: `docs/FILE_INVENTORY_AND_RELEASE.md`

- [ ] **Step 1: Expand `.gitignore` before staging project files**

Add explicit rules for:

```gitignore
.env
.env.*
!.env.example
.venv/
__pycache__/
*.py[cod]
.pytest_cache/
.ruff_cache/
.coverage
htmlcov/
.test-tmp-*/
tmp/
outputs/
vector_store/
vectorstore_cache/
*.db
*.db-shm
*.db-wal
data/backups/
data/collections/
data/intake/
data/incoming/
data/imports/
data/repairs/
data/verification/
data/audits/
data/expansion-reports/
config/authorized_job_sources.local.json
```

Keep these reviewed paths eligible for commit: `data/samples/`, `data/benchmark/`, `data/synthetic_resumes/`, `data/evidence/`, `data/collection_manifests/company-official.example.jsonl`, and `data/exports/knowledge-graph.json` only after size and provenance checks.

- [ ] **Step 2: Preview the ignored and untracked sets**

Run:

```powershell
git status --short --ignored
git check-ignore -v .env data/job_competency.db data/backups/job_competency-20260814-174200-658129.db
```

Expected: each secret/database path is ignored; `.env.example`, source code, tests, and Markdown documentation remain eligible.

- [ ] **Step 3: Write the file inventory and release policy**

Document the responsibilities of `src/`, `model_class/`, `schemes/`, `config/`, `tests/`, `data/samples/`, `data/benchmark/`, Docker files, `index.html`, and the documentation folders. State explicitly that there are no local LLM weight files; DeepSeek is accessed through an OpenAI-compatible API, while SQLite is the default data store and Neo4j is optional.

- [ ] **Step 4: Commit the release boundary**

```powershell
git add -- .gitignore docs/FILE_INVENTORY_AND_RELEASE.md
git commit -m "docs: define sanitized repository boundary"
```

### Task 2: Write the project overview for team members

**Files:**
- Create: `docs/PROJECT_OVERVIEW.md`

- [ ] **Step 1: Write the status table**

Use three states: `已完成`, `部分完成`, and `待完成`. Cover data governance, evidence import, job profile/evolution, knowledge graph, knowledge QA, resume parsing, seven-dimensional matching, Top 5 recommendation, learning path, review queue, evaluation, UI, Docker, and tests.

- [ ] **Step 2: Record the verified baseline**

Include the dated facts:

```text
测试：1021 passed, 6 skipped
覆盖率：87.21%
岗位记录：2568
可用唯一岗位：1318 / 目标 5000
岗位族：18 / 内部目标 22
技能：49
岗位画像：70
演化事件：492
知识片段：2077
```

Explain that the stored 1.0 evaluation scores come from only 1–2 example cases per metric and are workflow checks, not a formal accuracy claim.

- [ ] **Step 3: Add the PPT storyline and asset index**

Provide a 10-minute storyline: project problem, architecture, data governance, profile/evolution, graph and grounded QA, resume/recommendation, matching/learning path, review/evaluation, current results, gaps and roadmap. Map each step to the selected screenshot filename.

- [ ] **Step 4: Commit the overview**

```powershell
git add -- docs/PROJECT_OVERVIEW.md
git commit -m "docs: add verified project status overview"
```

### Task 3: Document architecture and runtime flows

**Files:**
- Create: `docs/ARCHITECTURE_AND_FLOW.md`

- [ ] **Step 1: Add the system architecture diagram**

Create a Mermaid flow covering input files and approved sources, import/governance, SQLite, analysis/profile/evolution, knowledge chunks/graph, FastAPI, single-page UI, optional DeepSeek, optional Neo4j, resume parsing, matching/recommendation, review, and evaluation.

- [ ] **Step 2: Add module ownership**

Map the main entry points and services, including `src/api.py`, `src/import_service.py`, `src/job_data_service.py`, `src/hard_metrics_pipeline.py`, `src/quarterly_profile_service.py`, `src/evolution_service.py`, `src/knowledge_service.py`, `src/evidence_qa_service.py`, `src/resume_service.py`, `src/matching_service.py`, `src/job_recommendation_service.py`, `src/evaluation_service.py`, and `src/job_collection/`.

- [ ] **Step 3: Add four sequence diagrams**

Document these flows with explicit inputs, calls, persistence, and outputs:

1. JD import and quality governance;
2. hard-metrics rebuild and graph export;
3. resume parse to Top 5 recommendation and learning path;
4. knowledge search/answer with model-enabled and extractive fallback branches.

- [ ] **Step 4: Commit architecture documentation**

```powershell
git add -- docs/ARCHITECTURE_AND_FLOW.md
git commit -m "docs: explain architecture and runtime flows"
```

### Task 4: Document inputs, outputs, models, and algorithms

**Files:**
- Create: `docs/INPUT_OUTPUT_AND_ALGORITHMS.md`

- [ ] **Step 1: Document the supported input contracts**

Include minimal valid examples for JD JSONL, evidence JSONL, resume files, matching/recommendation requests, and benchmark JSONL. Link examples to `data/samples/` and `data/benchmark/`.

- [ ] **Step 2: Document outputs**

Cover SQLite entities, import reports, collection reports, graph JSON, API responses, profile/evolution payloads, match explanations, learning paths, review items, evaluation runs, and acceptance snapshots.

- [ ] **Step 3: Explain deterministic algorithms**

Describe SHA-256 exact deduplication, 64-bit SimHash with Hamming distance threshold 8, source/completeness/description quality scoring, skill ontology normalization, evidence gates, seniority classification, quarterly profile aggregation, adjacent-quarter change rules, lexical plus optional cosine retrieval, and recommendation family deduplication.

- [ ] **Step 4: Explain the seven-dimensional score**

Record the implemented weights exactly:

```text
必备技能 30%
熟练度 15%
经验层级 15%
项目证据 15%
技能时效 10%
加分技能 5%
职责场景 10%
```

Explain score caps, confidence, match bands, and deterministic tie-breaking from the implementation.

- [ ] **Step 5: Explain model usage and limitations**

State that `src/llm.py` creates `ChatOpenAI` against the configured DeepSeek-compatible endpoint with temperature 0.2, timeout 30 seconds, and one retry. Identify model-assisted structured extraction, evidence-grounded QA, and optional resume enrichment. Distinguish these from local rule-first paths and state that no weight/checkpoint file is bundled.

- [ ] **Step 6: Commit the algorithm document**

```powershell
git add -- docs/INPUT_OUTPUT_AND_ALGORITHMS.md
git commit -m "docs: document data contracts models and algorithms"
```

### Task 5: Document setup, deployment, and reproducibility

**Files:**
- Create: `docs/ENVIRONMENT_AND_DEPLOYMENT.md`
- Modify: `QUICKSTART.md`
- Modify: `USER_GUIDE.md`

- [ ] **Step 1: Add the verified local environment path**

Document Python 3.11, virtual environment creation, `pip install -r requirements.txt`, `Copy-Item .env.example .env`, and `python -m uvicorn src.api:app --reload --port 8000`.

- [ ] **Step 2: Explain every public environment variable**

Describe `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL`, `DATABASE_URL`, `SQL_ECHO`, `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `NEO4J_DATABASE`, and the authorized-platform token placeholders without exposing values from the real `.env`.

- [ ] **Step 3: Add Docker, optional Neo4j, and data initialization**

Document `docker compose up --build -d`, health endpoints, SQLite-only operation, optional Neo4j sync, use of reviewed sample data, and the fact that the production database is excluded.

- [ ] **Step 4: Add verification and troubleshooting**

Use these commands and expected results:

```powershell
python -m pytest -c pytest-full.ini -q
# Expected: 1021 passed, 6 skipped, coverage >= 60%

python -m uvicorn src.api:app --host 127.0.0.1 --port 8000
# Expected: GET /health returns a successful status
```

Cover missing API key, occupied port, empty database, Neo4j unavailable, and Windows PowerShell activation policy.

- [ ] **Step 5: Add navigation links to existing guides**

Update `QUICKSTART.md` and `USER_GUIDE.md` with links to the new architecture, algorithms, status, deployment, and release inventory documents. Preserve their existing operational instructions.

- [ ] **Step 6: Commit environment documentation**

```powershell
git add -- docs/ENVIRONMENT_AND_DEPLOYMENT.md QUICKSTART.md USER_GUIDE.md
git commit -m "docs: add reproducible setup and deployment guide"
```

### Task 6: Rebuild the README and add presentation assets

**Files:**
- Modify: `README.md`
- Create: `docs/assets/screenshots/01-system-overview.png`
- Create: `docs/assets/screenshots/02-data-governance.png`
- Create: `docs/assets/screenshots/04-capability-evolution.png`
- Create: `docs/assets/screenshots/05-knowledge-graph.png`
- Create: `docs/assets/screenshots/07-job-recommendation.png`
- Create: `docs/assets/screenshots/08-match-diagnosis.png`
- Create: `docs/assets/screenshots/09-learning-path.png`
- Create: `docs/assets/screenshots/11-evaluation.png`

- [ ] **Step 1: Copy and rename the selected screenshots**

Copy the corresponding images from `../项目展示/系统截图/` without modifying the originals. Verify each PNG opens and is below 2 MB.

```powershell
New-Item -ItemType Directory -Force -Path '.\docs\assets\screenshots' | Out-Null
Copy-Item -LiteralPath '..\项目展示\系统截图\01-系统总览.png' -Destination '.\docs\assets\screenshots\01-system-overview.png'
Copy-Item -LiteralPath '..\项目展示\系统截图\02-数据治理.png' -Destination '.\docs\assets\screenshots\02-data-governance.png'
Copy-Item -LiteralPath '..\项目展示\系统截图\04-能力演化.png' -Destination '.\docs\assets\screenshots\04-capability-evolution.png'
Copy-Item -LiteralPath '..\项目展示\系统截图\05-全景图谱.png' -Destination '.\docs\assets\screenshots\05-knowledge-graph.png'
Copy-Item -LiteralPath '..\项目展示\系统截图\07b-岗位推荐.png' -Destination '.\docs\assets\screenshots\07-job-recommendation.png'
Copy-Item -LiteralPath '..\项目展示\系统截图\08-匹配诊断.png' -Destination '.\docs\assets\screenshots\08-match-diagnosis.png'
Copy-Item -LiteralPath '..\项目展示\系统截图\09-学习路径.png' -Destination '.\docs\assets\screenshots\09-learning-path.png'
Copy-Item -LiteralPath '..\项目展示\系统截图\11-模型质量评测.png' -Destination '.\docs\assets\screenshots\11-evaluation.png'
Get-ChildItem '.\docs\assets\screenshots\*.png' | Where-Object Length -gt 2MB
```

Expected: the last command prints no files.

- [ ] **Step 2: Rewrite the README as the repository landing page**

Use this order: project statement, status badges/text, core capabilities, architecture diagram, verified results, quick start, demonstration flow, repository map, document navigation, safety/data notice, known gaps, and license/status note.

- [ ] **Step 3: Validate all local Markdown links**

Use a PowerShell link scan that resolves relative file links from each Markdown file. Expected: no missing local target.

- [ ] **Step 4: Commit README and assets**

```powershell
git add -- README.md docs/assets/screenshots
git commit -m "docs: add project landing page and presentation assets"
```

### Task 7: Add repeatable release verification

**Files:**
- Create: `scripts/verify_release.ps1`

- [ ] **Step 1: Implement the verification script**

The script must:

1. run `python -m pytest -c pytest-full.ini -q` and fail on a non-zero exit code;
2. inspect `git ls-files` and fail if a tracked file is `.env`, a database, a database backup, a cache, or inside excluded raw-data directories;
3. fail if a tracked file exceeds 25 MB;
4. scan tracked text files for common private-key headers and assignment forms such as `DEEPSEEK_API_KEY=<non-empty-value>`;
5. print a concise pass/fail summary.

Use this complete implementation:

```powershell
[CmdletBinding()]
param(
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $projectRoot
try {
    if (-not $SkipTests) {
        & python -m pytest -c pytest-full.ini -q
        if ($LASTEXITCODE -ne 0) {
            throw 'Full pytest suite or coverage gate failed.'
        }
    }

    $tracked = @(& git ls-files)
    if ($LASTEXITCODE -ne 0) {
        throw 'Unable to enumerate tracked files.'
    }

    $prohibitedPatterns = @(
        '(^|/)\.env($|\.)',
        '\.(db|db-shm|db-wal)$',
        '(^|/)(tmp|data/backups|data/collections|data/intake|data/incoming|data/imports|data/repairs|data/verification|data/audits|data/expansion-reports)/',
        '(^|/)(__pycache__|\.pytest_cache|\.ruff_cache)/',
        '(^|/)\.coverage$'
    )
    $prohibited = @($tracked | Where-Object {
        $path = $_
        ($prohibitedPatterns | Where-Object { $path -match $_ }).Count -gt 0 -and $path -ne '.env.example'
    })
    if ($prohibited.Count -gt 0) {
        throw "Prohibited tracked paths:`n$($prohibited -join "`n")"
    }

    $largeFiles = foreach ($relativePath in $tracked) {
        $fullPath = Join-Path $projectRoot $relativePath
        if ((Test-Path -LiteralPath $fullPath -PathType Leaf) -and (Get-Item -LiteralPath $fullPath).Length -gt 25MB) {
            $relativePath
        }
    }
    if (@($largeFiles).Count -gt 0) {
        throw "Tracked files over 25 MB:`n$($largeFiles -join "`n")"
    }

    $secretPatterns = @(
        '-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----',
        '(?i)DEEPSEEK_API_KEY\s*=\s*[^\s#]+',
        '(?i)(OPENAI|ZHIPU)_API_KEY\s*=\s*[^\s#]+'
    )
    $textExtensions = @('.py', '.md', '.txt', '.json', '.jsonl', '.csv', '.toml', '.ini', '.yml', '.yaml', '.ps1', '.html', '.example')
    $secretHits = foreach ($relativePath in $tracked) {
        $fullPath = Join-Path $projectRoot $relativePath
        if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) { continue }
        $extension = [IO.Path]::GetExtension($fullPath).ToLowerInvariant()
        if ($textExtensions -notcontains $extension -and [IO.Path]::GetFileName($fullPath) -ne '.env.example') { continue }
        foreach ($pattern in $secretPatterns) {
            $matches = Select-String -LiteralPath $fullPath -Pattern $pattern -ErrorAction SilentlyContinue
            foreach ($match in $matches) {
                if ($relativePath -eq '.env.example' -and $match.Line -match '=\s*$') { continue }
                "${relativePath}:$($match.LineNumber)"
            }
        }
    }
    if (@($secretHits).Count -gt 0) {
        throw "Potential secrets detected:`n$($secretHits -join "`n")"
    }

    Write-Host "Release verification passed: $($tracked.Count) tracked files checked."
}
finally {
    Pop-Location
}
```

- [ ] **Step 2: Run the script before staging the application**

Expected: the test phase passes; the tracked-file checks cover only the design commits and report no prohibited files.

- [ ] **Step 3: Stage the reviewed application subset**

```powershell
git add -- . ':!.env'
git status --short
git diff --cached --stat
```

Expected: source, tests, configs, examples, documentation, Docker files, and the front-end are staged; secrets, databases, backups, raw collections, and temporary directories are absent.

- [ ] **Step 4: Run the script against the staged/tracked snapshot**

Expected: tests pass, coverage remains at least 60%, there are no prohibited tracked files, and no tracked file is over 25 MB.

- [ ] **Step 5: Commit the project snapshot**

```powershell
git commit -m "chore: publish sanitized project snapshot"
```

### Task 8: Reader-test and correct the documentation

**Files:**
- Modify as needed: `README.md`, `docs/PROJECT_OVERVIEW.md`, `docs/ARCHITECTURE_AND_FLOW.md`, `docs/INPUT_OUTPUT_AND_ALGORITHMS.md`, `docs/ENVIRONMENT_AND_DEPLOYMENT.md`, `docs/FILE_INVENTORY_AND_RELEASE.md`

- [ ] **Step 1: Test realistic reader questions**

Verify that a fresh reader can answer: project purpose, current completion, entry point, runtime data flow, inputs, outputs, model usage, deterministic algorithms, database role, how to run, how to test, measured results, limitations, and where PPT assets are stored.

- [ ] **Step 2: Check ambiguity and contradictions**

Search for claims that confuse goals with measured results, imply Neo4j is mandatory, imply a local model is bundled, or treat the 1–2-case example benchmark as formal accuracy evidence. Correct every issue found.

- [ ] **Step 3: Commit reader-test corrections**

```powershell
git add -- README.md docs
git commit -m "docs: close reader comprehension gaps"
```

### Task 9: Create and publish the private GitHub repository

**Files:**
- Git metadata only: `.git/config`

- [ ] **Step 1: Create the remote repository**

Create `Summer-lc/job-competency-matching-platform` as a private repository with no generated README, `.gitignore`, or license so the remote starts empty.

- [ ] **Step 2: Add the remote and verify its exact target**

```powershell
git remote add origin https://github.com/Summer-lc/job-competency-matching-platform.git
git remote -v
```

Expected: both fetch and push URLs point only to the new repository.

- [ ] **Step 3: Push the main branch**

```powershell
git push -u origin main
```

Expected: `main` is created remotely and local `main` tracks `origin/main`.

- [ ] **Step 4: Verify the remote result**

Confirm that the GitHub page is private, the latest commit matches local `HEAD`, README renders, Mermaid diagrams render, document links work, and no excluded data or `.env` appears in the repository.

- [ ] **Step 5: Report the handoff**

Provide the repository URL, latest commit, test/coverage evidence, key document links, excluded-data summary, and the remaining project gaps.
