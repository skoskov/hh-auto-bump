param([string]$Python)
$ErrorActionPreference = 'Stop'
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$setupTemp = Join-Path $PSScriptRoot '.tmp\setup'
New-Item -ItemType Directory -Force -Path $setupTemp | Out-Null
$previousTemp = $env:TEMP
$previousTmp = $env:TMP
try {
$env:TEMP = $setupTemp
$env:TMP = $setupTemp
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    if (-not $Python) {
        $candidate = Get-Command python -ErrorAction SilentlyContinue
        $candidates = @()
        if ($candidate) { $candidates += $candidate.Source }
        $candidates += @(Get-ChildItem -Path (Join-Path $env:APPDATA 'uv\python\cpython-*\python.exe') -File -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
        $candidates += @(Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python*\python.exe') -File -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
        foreach ($candidatePath in $candidates) {
            & $candidatePath -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>$null
            if ($LASTEXITCODE -eq 0) { $Python = $candidatePath; break }
        }
    }
    if (-not $Python) { throw 'Specify Python 3.11 or newer: .\setup.ps1 -Python <python.exe>' }
    & $Python -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"'
    if ($LASTEXITCODE -ne 0) { throw 'A working Python 3.11+ is required.' }
    & $Python -m venv (Join-Path $PSScriptRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the local Python environment.' }
}
& $venvPython -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"'
if ($LASTEXITCODE -ne 0) { throw 'The existing environment requires Python 3.11+; recreate .venv with a supported Python.' }
& $venvPython -m pip --version 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    & $venvPython -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) { throw 'Could not bootstrap pip in the local environment.' }
}
& $venvPython -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Could not install dependencies.' }
& $venvPython -c 'from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(channel="chrome", headless=True); b.close(); p.stop(); print("HH runtime ready; installed Chrome launch verified.")'
if ($LASTEXITCODE -ne 0) { throw 'Installed Google Chrome could not start. Check Chrome installation and browser launch permissions.' }
} finally {
    $env:TEMP = $previousTemp
    $env:TMP = $previousTmp
}
