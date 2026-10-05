<#
.SYNOPSIS
  Create the jarvisd virtualenv and write requirements.lock with exact pins.

.DESCRIPTION
  Uses the Python 3.12 install on this machine (the per-user python.org location, else the py
  launcher, else the path given with -Python), installs the only allowed third party
  packages (pydantic, APScheduler, pytest, plus fastapi and uvicorn for the read-only hub and
  httpx2, which the hub tests and 'jarvis hub --check' need through fastapi's TestClient) and freezes the result. Safe to re-run: an
  existing venv is reused and the lock file is rewritten from what is installed.

  The lock file is written as UTF-8 without BOM and LF line endings so it diffs cleanly.
#>
[CmdletBinding()]
param(
    [string]$Python = (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
    [string]$VenvDir = (Join-Path $PSScriptRoot '..\.venv'),
    [string]$LockFile = (Join-Path $PSScriptRoot '..\requirements.lock')
)

$ErrorActionPreference = 'Stop'

# Native commands do not throw on a non-zero exit, so every call is checked here.
function Invoke-Checked {
    param([string]$Exe, [string[]]$Arguments)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Exe $($Arguments -join ' ')"
    }
}

if (-not (Test-Path -LiteralPath $Python)) {
    # Not the per-user python.org location: try the py launcher before giving up.
    $found = $null
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $found = (& py -3.12 -c 'import sys; print(sys.executable)' 2>$null)
        if ($LASTEXITCODE -ne 0) { $found = $null }
    }
    if ($found -and (Test-Path -LiteralPath $found)) {
        $Python = $found
    } else {
        throw "Python 3.12 not found at $Python. Install it from python.org, or pass its path: deploy\setup-venv.ps1 -Python <path-to-python.exe>"
    }
}

$venvPython = Join-Path $VenvDir 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "Creating venv at $VenvDir"
    Invoke-Checked $Python @('-m', 'venv', $VenvDir)
} else {
    Write-Host "Reusing venv at $VenvDir"
}

# Version bounds mirror pyproject.toml. APScheduler 4 is a rewrite, so it stays on 3.x (design D2).
Invoke-Checked $venvPython @('-m', 'pip', 'install', '--disable-pip-version-check',
    'pydantic>=2.7,<3', 'APScheduler>=3.10,<4', 'pytest',
    'fastapi>=0.115', 'uvicorn>=0.30', 'httpx2>=2')

$frozen = & $venvPython -m pip freeze --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { throw "pip freeze failed with exit code $LASTEXITCODE" }

$text = (($frozen | Where-Object { $_ -ne '' }) -join "`n") + "`n"
[System.IO.File]::WriteAllText($LockFile, $text, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "Wrote $LockFile"
Write-Host "Done. Run tests with: $venvPython -m pytest -q"
