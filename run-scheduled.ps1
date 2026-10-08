param(
    [switch]$Scheduled,
    [ValidateSet('service', 'run', 'check', 'login', 'inspect')][string]$Mode = 'service',
    [switch]$NoPause,
    [string]$PythonPath
)
$ErrorActionPreference = 'Continue'
$python = if ($PythonPath) { $PythonPath } else { Join-Path $PSScriptRoot '.venv\Scripts\python.exe' }
$script = Join-Path $PSScriptRoot 'hh_bump.py'
$state = Join-Path $PSScriptRoot '.state'
$consoleDir = Join-Path $state 'console'
New-Item -ItemType Directory -Force -Path $consoleDir | Out-Null
# Separate per-launch logs avoid mixed encodings and concurrent writers.
$log = Join-Path $consoleDir ((Get-Date -Format 'yyyyMMdd-HHmmss-fff') + "-$PID.log")
$writer = [IO.StreamWriter]::new($log, $false, [Text.UTF8Encoding]::new($false))
$writer.AutoFlush = $true
$exitCode = 1
$previousPythonEncoding = $env:PYTHONIOENCODING
$previousPythonUtf8 = $env:PYTHONUTF8
try {
    $writer.WriteLine('START ' + (Get-Date -Format o) + ' mode=' + $Mode)
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        $message = 'Local Python environment is missing. Run .\setup.ps1 first.'
        $writer.WriteLine($message)
        Write-Host $message -ForegroundColor Red
    } else {
        $env:PYTHONIOENCODING = 'utf-8'
        $env:PYTHONUTF8 = '1'
        # Native commands update the global automatic variable. A script-local
        # assignment shadows it when this file is called from an existing PS prompt.
        $global:LASTEXITCODE = $null
        & $python -u $script $Mode 2>&1 | ForEach-Object {
            $line = $_.ToString()
            $writer.WriteLine($line)
            Write-Host $line
        }
        if ($null -ne $global:LASTEXITCODE) { $exitCode = $global:LASTEXITCODE }
    }
} catch {
    $writer.WriteLine($_.Exception.Message)
    Write-Host $_.Exception.Message -ForegroundColor Red
} finally {
    $writer.WriteLine('EXIT ' + $exitCode + ' ' + (Get-Date -Format o))
    $writer.Dispose()
    $env:PYTHONIOENCODING = $previousPythonEncoding
    $env:PYTHONUTF8 = $previousPythonUtf8
}
if ($exitCode -eq 3) {
    Write-Host 'HH bot is already running. Current status:'
    $statusPath = Join-Path $state 'status.json'
    if (Test-Path -LiteralPath $statusPath) { Get-Content -LiteralPath $statusPath -Encoding UTF8 }
    if ($Scheduled) { exit 0 }
} elseif ($exitCode -ne 0) {
    Write-Host "HH bot failed (exit $exitCode). Log: $log" -ForegroundColor Red
    if (-not $Scheduled -and -not $NoPause) { Read-Host 'Press Enter to return' | Out-Null }
}
exit $exitCode
