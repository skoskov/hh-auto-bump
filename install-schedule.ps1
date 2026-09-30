param([switch]$Update)
$ErrorActionPreference = 'Stop'
$taskName = 'HH-Resume-AutoBump'
$python = 'C:\Users\skoskov\Documents\_DEV\profi-bot\.venv\Scripts\python.exe'
$script = Join-Path $PSScriptRoot 'hh_bump.py'
$verified = Join-Path $PSScriptRoot '.state\live-verified.json'
if (-not (Test-Path -LiteralPath $verified)) {
    throw 'First complete login, a reviewed live implementation and a verified live bump.'
}
if (-not (Test-Path -LiteralPath $python)) { throw 'Python runtime is unavailable.' }
$action = New-ScheduledTaskAction -Execute $python -Argument ('"' + $script + '" run') -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Hours 4 -Minutes 1)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 40) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
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
    $existingExe = [IO.Path]::GetFullPath($existing.Actions[0].Execute)
    $expectedExe = [IO.Path]::GetFullPath($python)
    $expectedArguments = '"' + $script + '" run'
    if ($existingExe -ine $expectedExe -or $existing.Actions[0].Arguments -cne $expectedArguments) {
        throw "Task $taskName does not point to the expected Python executable and hh_bump.py run command; refusing update."
    }
    Set-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
} else {
    if ($Update) { throw "Task $taskName does not exist; -Update will not create it." }
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Raise available HH resumes through a local Chrome profile. Requires logged-in Windows session.' | Out-Null
}
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName,State
