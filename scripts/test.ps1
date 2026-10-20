$ErrorActionPreference = 'Stop'
$backendRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $backendRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { & (Join-Path $PSScriptRoot 'setup.ps1') }
$previousErrorAction = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $python -c "import pytest" 2>$null
$pytestCheckExit = $LASTEXITCODE
$ErrorActionPreference = $previousErrorAction
if ($pytestCheckExit -ne 0) { & (Join-Path $PSScriptRoot 'setup.ps1') }
if (-not $env:TEST_DATABASE_URL) {
    $envFile = Join-Path $backendRoot '.env'
    if (Test-Path $envFile) {
        $line = Get-Content $envFile | Where-Object { $_ -match '^\s*TEST_DATABASE_URL=' } | Select-Object -First 1
        if ($line) { $env:TEST_DATABASE_URL = ($line -replace '^\s*TEST_DATABASE_URL\s*=\s*','').Trim() }
    }
}
if ($env:TEST_DATABASE_URL) { Write-Host 'Running unit and PostgreSQL integration tests.' }
else { Write-Host 'Running unit tests; TEST_DATABASE_URL is not configured, so integration tests will skip.' }
& $python -m pytest -q -rs
exit $LASTEXITCODE
