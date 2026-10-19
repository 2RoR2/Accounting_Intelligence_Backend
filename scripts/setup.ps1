$ErrorActionPreference = 'Stop'

$backendRoot = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $backendRoot '.venv'
$python = $null

function Test-Python($candidate) {
    try {
        $version = & $candidate -c "import sys; print(sys.version_info[0], sys.version_info[1], sys.platform)" 2>$null
        return $LASTEXITCODE -eq 0 -and ($version -match '^3 (1[1-9]|[2-9][0-9]) win32$')
    } catch { return $false }
}

try { $py312 = (Get-Command py -ErrorAction Stop).Source; if (Test-Python @($py312, '-3.12')) { $python = @($py312, '-3.12') } } catch {}
if (-not $python) {
    $candidates = @(
        'C:\Python312\python.exe',
        'C:\Users\User\AppData\Local\Programs\Python\Python312\python.exe',
        'C:\Users\User\AppData\Local\Programs\Python\Python311\python.exe',
        'python.exe'
    )
    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        $resolved = if ($command) { $command.Source } else { $candidate }
        if ($resolved -match '(?i)msys|mingw') { continue }
        if ($command -or (Test-Path $resolved -ErrorAction SilentlyContinue)) {
            if (Test-Python $resolved) { $python = $resolved; break }
        }
    }
}
if (-not $python) {
    throw 'A Windows Python 3.11+ installation was not found. Install Python from python.org with pip and venv enabled, then rerun this script.'
}

$venvPython = Join-Path $venv 'Scripts\python.exe'
$validVenv = Test-Path $venvPython
if ($validVenv) { $validVenv = Test-Python $venvPython }
if (-not $validVenv) {
    if (Test-Path $venv) {
        $backup = "$venv-broken-$(Get-Date -Format yyyyMMddHHmmss)"
        Move-Item -LiteralPath $venv -Destination $backup
        Write-Host "Moved stale environment to $backup"
    }
    if ($python -is [array]) { & $python[0] $python[1] -m venv $venv } else { & $python -m venv $venv }
}

if (-not (Test-Path $venvPython -ErrorAction SilentlyContinue)) {
    throw 'The selected interpreter did not create a Windows Scripts folder. Install official Windows Python 3.11+ from python.org; MSYS2 Python is not supported for this project.'
}

& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r (Join-Path $backendRoot 'requirements.txt')
Write-Host "Backend environment ready: $venvPython"
