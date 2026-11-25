param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $PytestArguments = @()
)

$ErrorActionPreference = 'Stop'
$backendRoot = Split-Path -Parent $PSScriptRoot
$candidates = @()
$candidates += (Join-Path $env:LOCALAPPDATA 'Python\pythoncore-3.15-64\python.exe')
$pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
if ($pythonCommand) { $candidates += $pythonCommand.Source }

$python = $null
foreach ($candidate in ($candidates | Select-Object -Unique)) {
    if (-not (Test-Path -LiteralPath $candidate)) { continue }
    try { $null = & $candidate -c 'import pytest' 2>&1 } catch { continue }
    if ($LASTEXITCODE -eq 0) {
        $python = $candidate
        break
    }
}
if (-not $python) {
    throw 'No Python interpreter with pytest was found. Install requirements-dev.txt first.'
}

Push-Location $backendRoot
try {
    $env:APP_ENV = if ($env:APP_ENV) { $env:APP_ENV } else { 'development' }
    & $python -m pytest @PytestArguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
