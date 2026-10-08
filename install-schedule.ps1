param([switch]$Update)
$ErrorActionPreference = 'Stop'
$taskName = 'HH-Resume-AutoBump'
$taskPath = '\'
$legacyPython = 'C:\Users\skoskov\Documents\_DEV\profi-bot\.venv\Scripts\python.exe'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$script = Join-Path $PSScriptRoot 'hh_bump.py'
$runner = Join-Path $PSScriptRoot 'run-scheduled.ps1'
$windowsPowerShell = Join-Path $env:windir 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (Test-Path -LiteralPath $windowsPowerShell) {
    $powershell = $windowsPowerShell
} else {
    try {
        $powershell = [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
    } catch {
        throw 'Could not resolve the current PowerShell executable.'
    }
    if (-not (Test-Path -LiteralPath $powershell)) {
        throw 'The current PowerShell executable is unavailable.'
    }
}
$verified = Join-Path $PSScriptRoot '.state\live-verified.json'
if (-not (Test-Path -LiteralPath $verified)) {
    throw 'First complete an account-scoped check or run with the current release.'
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Run .\setup.ps1 first.' }
$verifiedCodeVersion = $null
try {
    $verificationRecord = Get-Content -LiteralPath $verified -Raw | ConvertFrom-Json -ErrorAction Stop
    $verifiedCodeVersion = [string]$verificationRecord.code_version
} catch {
    throw 'Live verification record is invalid; complete a check or run with the current release.'
}
if ($verificationRecord.validation -notin @('check', 'run') -or
    -not ($verificationRecord.total -is [int] -or $verificationRecord.total -is [long]) -or
    $verificationRecord.total -lt 1 -or $verificationRecord.total -gt 1000) {
    throw 'A complete account-scoped check or run is required before scheduling.'
}
$validationTime = $verificationRecord.time
if (-not ($validationTime -is [int] -or $validationTime -is [long] -or $validationTime -is [double]) -or
    [double]::IsNaN([double]$validationTime) -or [double]::IsInfinity([double]$validationTime)) {
    throw 'Verification has an invalid timestamp.'
}
$validationAge = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [double]$validationTime
if ($validationAge -lt -60 -or $validationAge -gt 86400) {
    throw 'Run a current-release check in the last 24 hours before scheduling.'
}
if ($verifiedCodeVersion -notmatch '^[0-9a-fA-F]{64}$') {
    throw 'Live verification record has no valid script digest; complete a check or run with the current release.'
}
$versionOutput = & $python $script --version
if ($LASTEXITCODE -ne 0) { throw 'Could not read current runtime version.' }
$currentCodeVersion = ($versionOutput | ConvertFrom-Json -ErrorAction Stop).code_version
if ($verifiedCodeVersion -ine $currentCodeVersion) {
    throw 'Verification belongs to a different release; check the current release before scheduling.'
}
if (-not (Test-Path -LiteralPath $python)) { throw 'Python runtime is unavailable.' }
if (-not (Test-Path -LiteralPath $runner)) { throw 'Scheduled task runner is unavailable.' }
$previousActionArguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $runner + '"'
$actionArguments = $previousActionArguments + ' -Scheduled'
$action = New-ScheduledTaskAction -Execute $powershell -Argument $actionArguments -WorkingDirectory $PSScriptRoot
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
# The Python service owns periodicity. Scheduler supervises its process only.
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$matchingTasks = @(Get-ScheduledTask -ErrorAction Stop | Where-Object { $_.TaskName -eq $taskName })
if ($matchingTasks.Count -gt 1 -or ($matchingTasks.Count -eq 1 -and $matchingTasks[0].TaskPath -ne $taskPath)) {
    throw 'Task name is ambiguous or belongs to another folder; refusing migration.'
}
$existing = if ($matchingTasks.Count) { $matchingTasks[0] } else { $null }
if ($existing) {
    if (-not $Update) { throw "Task $taskName already exists. Use -Update to update it after identity verification." }
    if ($existing.Actions.Count -ne 1) { throw "Task $taskName has an unexpected action count; refusing update." }
    try {
        $existingUserSid = ([System.Security.Principal.NTAccount]::new($existing.Principal.UserId)).Translate([System.Security.Principal.SecurityIdentifier]).Value
    } catch {
        throw "Task $taskName owner cannot be resolved; refusing update."
    }
    $currentUserSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    if ($existingUserSid -ne $currentUserSid) {
        throw "Task $taskName belongs to a different Windows account; refusing update."
    }
    if ($existing.Principal.LogonType -ne 'Interactive' -or
        $existing.Principal.RunLevel -ne 'Limited') {
        throw "Task $taskName must use an interactive logon and limited run level for the visible browser; refusing update."
    }
    $existingExe = [IO.Path]::GetFullPath($existing.Actions[0].Execute)
    $oldExpectedExecutables = @($python, $legacyPython) | ForEach-Object { [IO.Path]::GetFullPath($_) }
    $oldExpectedArguments = '"' + $script + '" run'
    $expectedPowerShellExecutables = @($powershell, $windowsPowerShell, [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName) |
        Where-Object { Test-Path -LiteralPath $_ } |
        ForEach-Object { [IO.Path]::GetFullPath($_) } |
        Select-Object -Unique
    $isExpectedOldAction = $existingExe -iin $oldExpectedExecutables -and
        $existing.Actions[0].Arguments -ceq $oldExpectedArguments
    $isExpectedRunnerAction = $existingExe -iin $expectedPowerShellExecutables -and
        ($existing.Actions[0].Arguments -ceq $previousActionArguments -or
         $existing.Actions[0].Arguments -ceq $actionArguments)
    if (-not ($isExpectedOldAction -or $isExpectedRunnerAction)) {
        throw "Task $taskName does not point to the expected Python executable and hh_bump.py run command; refusing update."
    }
    # AtLogOn does not start a new cycle now. An existing process needs a
    # controlled restart to load the verified release.
    Set-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Action $action -Settings $settings -Trigger $trigger | Out-Null
} else {
    if ($Update) { throw "Task $taskName does not exist; -Update will not create it." }
    Register-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Persistent HH resume service; periodicity in Python, logged-in desktop required.' | Out-Null
}
Get-ScheduledTask -TaskName $taskName -TaskPath $taskPath | Select-Object TaskName,State
