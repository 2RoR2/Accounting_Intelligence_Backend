param(
    [ValidateSet("api", "seed")]
    [string]$Action = "api"
)

$ErrorActionPreference = "Stop"
$configPath = Join-Path $env:LOCALAPPDATA "AccountingIntelligence\backend-dev.env"
if (-not (Test-Path -LiteralPath $configPath)) {
    throw "Local dev config not found at $configPath."
}

foreach ($line in Get-Content -LiteralPath $configPath) {
    if ($line -match '^([A-Z_]+)=(.*)$') {
        Set-Item -Path "Env:$($matches[1])" -Value $matches[2]
    }
}

$database = [Uri]$env:DATABASE_URL
if ($database.Host -ne "127.0.0.1" -or $database.Port -ne 5433 -or
    $database.AbsolutePath.Trim("/") -ne "accounting_intelligence_dev") {
    throw "Refusing to run: local dev config does not target the isolated development database."
}
if ($env:APP_ENV -ne "development" -or $env:ENABLE_DEMO_ACCOUNTS -ne "true") {
    throw "Refusing to run: development mode and local demo seeding must be enabled."
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Project Python environment not found at $python."
}

Push-Location $PSScriptRoot
try {
    if ($Action -eq "seed") {
        & $python "manage.py" "seed-demo-accounts"
        if ($LASTEXITCODE -ne 0) {
            throw "Demo account seeding failed."
        }
        return
    }

    if (Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue) {
        throw "Port 8000 is already in use. Stop the other backend before starting local dev."
    }
    & $python -m uvicorn app:app --host 127.0.0.1 --port 8000 --reload
    if ($LASTEXITCODE -ne 0) {
        throw "The local development API stopped with an error."
    }
}
finally {
    Pop-Location
}
