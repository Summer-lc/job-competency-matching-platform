[CmdletBinding()]
param(
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

function Stop-ReleaseVerification {
    param([string]$Message)
    throw "Release verification failed: $Message"
}

Push-Location $projectRoot
try {
    if (-not $SkipTests) {
        Write-Host 'Running the full test and coverage gate...'
        & python -m pytest -c pytest-full.ini -q
        if ($LASTEXITCODE -ne 0) {
            Stop-ReleaseVerification 'the full pytest suite or coverage gate failed.'
        }
    }

    $tracked = @(& git ls-files)
    if ($LASTEXITCODE -ne 0) {
        Stop-ReleaseVerification 'Git could not enumerate tracked files.'
    }

    $prohibitedPatterns = @(
        '(^|/)\.env($|\.)',
        '\.(db|db-shm|db-wal)$',
        '(^|/)(tmp|data/backups|data/collection_locks|data/collections|data/intake|data/incoming|data/imports|data/repairs|data/verification|data/audits|data/expansion-reports)/',
        '(^|/)(__pycache__|\.pytest_cache|\.ruff_cache|\.test-tmp-[^/]+)/',
        '(^|/)\.coverage$'
    )
    $prohibited = @(
        $tracked | Where-Object {
            $path = $_
            $matches = @($prohibitedPatterns | Where-Object { $path -match $_ })
            $matches.Count -gt 0 -and $path -ne '.env.example'
        }
    )
    if ($prohibited.Count -gt 0) {
        Stop-ReleaseVerification "prohibited tracked paths were found:`n$($prohibited -join "`n")"
    }

    $largeFiles = @(
        foreach ($relativePath in $tracked) {
            $fullPath = Join-Path $projectRoot $relativePath
            if (
                (Test-Path -LiteralPath $fullPath -PathType Leaf) -and
                (Get-Item -LiteralPath $fullPath).Length -gt 25MB
            ) {
                $relativePath
            }
        }
    )
    if ($largeFiles.Count -gt 0) {
        Stop-ReleaseVerification "tracked files over 25 MB were found:`n$($largeFiles -join "`n")"
    }

    $secretPatterns = @(
        '-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----',
        '\bsk-[A-Za-z0-9_-]{20,}\b',
        '\bghp_[A-Za-z0-9]{30,}\b',
        '\bgithub_pat_[A-Za-z0-9_]{40,}\b',
        '(?i)^\s*(DEEPSEEK|OPENAI|ZHIPU)_API_KEY\s*=\s*[A-Za-z0-9_-]{12,}\s*$'
    )
    $textExtensions = @(
        '.py', '.md', '.txt', '.json', '.jsonl', '.csv', '.toml', '.ini',
        '.yml', '.yaml', '.ps1', '.html', '.example'
    )
    $secretHits = @(
        foreach ($relativePath in $tracked) {
            $fullPath = Join-Path $projectRoot $relativePath
            if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
                continue
            }
            $extension = [IO.Path]::GetExtension($fullPath).ToLowerInvariant()
            if (
                $textExtensions -notcontains $extension -and
                [IO.Path]::GetFileName($fullPath) -ne '.env.example'
            ) {
                continue
            }
            foreach ($pattern in $secretPatterns) {
                $matches = Select-String -LiteralPath $fullPath -Pattern $pattern -ErrorAction SilentlyContinue
                foreach ($match in $matches) {
                    "${relativePath}:$($match.LineNumber)"
                }
            }
        }
    )
    if ($secretHits.Count -gt 0) {
        Stop-ReleaseVerification "potential secrets were found:`n$($secretHits -join "`n")"
    }

    Write-Host "Release verification passed: $($tracked.Count) tracked files checked."
}
finally {
    Pop-Location
}
