param([string]$WorkerName = 'accounting-extraction')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONPATH = $PSScriptRoot
$workerPython = if (Test-Path -LiteralPath 'C:\AI-Python312\python.exe') { 'C:\AI-Python312\python.exe' } else { Join-Path $PSScriptRoot '.venv-win\Scripts\python.exe' }
& $workerPython scripts/run_worker.py --name $WorkerName
