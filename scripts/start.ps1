$ErrorActionPreference = 'Stop'
$backendRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $backendRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { & (Join-Path $PSScriptRoot 'setup.ps1') }
Set-Location $backendRoot
& $python -m uvicorn app:app --host 127.0.0.1 --port 8000
