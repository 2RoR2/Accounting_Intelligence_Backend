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
if ($database.Host -ne "127.0.0.1" -or $database.Port -notin @(5432, 5433) -or
    $database.AbsolutePath.Trim("/") -ne "accounting_intelligence_dev") {
    throw "Refusing to run: local dev config does not target the isolated development database."
}

# PostgreSQL installers commonly use 5432, while the project Docker setup uses
# 5433. Prefer the configured port, but fall back to the local PostgreSQL
# service when 5433 is not listening.
if ($database.Port -eq 5433 -and
    -not (Get-NetTCPConnection -State Listen -LocalAddress 127.0.0.1 -LocalPort 5433 -ErrorAction SilentlyContinue) -and
    (Get-NetTCPConnection -State Listen -LocalAddress 0.0.0.0 -LocalPort 5432 -ErrorAction SilentlyContinue)) {
    $env:DATABASE_URL = $env:DATABASE_URL -replace ':5433/', ':5432/'
}
if ($env:APP_ENV -ne "development" -or $env:ENABLE_DEMO_ACCOUNTS -ne "true") {
    throw "Refusing to run: development mode and local demo seeding must be enabled."
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv-fixed\Scripts\python.exe"
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
    # Bind all local interfaces so the frontend can reach the API when opened
    # through localhost, 127.0.0.1, or the machine's LAN address.
    & $python -m uvicorn app:app --host 0.0.0.0 --port 8000 --reload
    if ($LASTEXITCODE -ne 0) {
        throw "The local development API stopped with an error."
    }
}
finally {
    Pop-Location
}
