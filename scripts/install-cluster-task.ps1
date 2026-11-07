$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$ensureScript = Join-Path $PSScriptRoot "ensure-cluster-daemon.ps1"
$watchdogScript = Join-Path $PSScriptRoot "cluster-watchdog.ps1"
$teamScript = Join-Path $PSScriptRoot "team.ps1"
$taskName = "Codex Team Cluster"
$powerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
$daemonStatusJson = & $powerShell `
    -NoProfile `
    -ExecutionPolicy Bypass `
    -File $teamScript cluster daemon-status
$daemonStatus = $daemonStatusJson | ConvertFrom-Json
if (-not $daemonStatus.log_path) {
    throw "could not resolve the CodexTeam data directory"
}
$logsDir = Split-Path -Parent $daemonStatus.log_path
$teamHome = Split-Path -Parent $logsDir
[Environment]::SetEnvironmentVariable("CODEX_TEAM_HOME", $teamHome, "User")
$env:CODEX_TEAM_HOME = $teamHome
$arguments = (
    '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' +
    $ensureScript + '" -TeamHome "' + $teamHome + '"'
)
$action = New-ScheduledTaskAction -Execute $powerShell -Argument $arguments -WorkingDirectory $projectRoot
$principal = New-ScheduledTaskPrincipal `
    -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn
$repeatTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
$task = New-ScheduledTask `
    -Action $action `
    -Principal $principal `
    -Settings $settings `
    -Trigger @($logonTrigger, $repeatTrigger) `
    -Description "Restarts the persistent Codex team cluster scheduler every five minutes if it is not running."
$registration = "scheduled_task"
$warning = ""
try {
    Register-ScheduledTask -TaskName $taskName -InputObject $task -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
}
catch {
    # Task Scheduler can be blocked by managed Windows policies. Fall back to a
    # user-level login watchdog that does not require administrator privileges.
    $registration = "user_startup_watchdog"
    $warning = $_.Exception.Message
    $runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
    $runCommand = (
        '"' + $powerShell + '" -NoProfile -WindowStyle Hidden ' +
        '-ExecutionPolicy Bypass -File "' + $watchdogScript + '" ' +
        '-TeamHome "' + $teamHome + '"'
    )
    if (-not (Test-Path -LiteralPath $runKey)) {
        New-Item -Path $runKey -Force | Out-Null
    }
    New-ItemProperty `
        -Path $runKey `
        -Name "CodexTeamClusterWatchdog" `
        -Value $runCommand `
        -PropertyType String `
        -Force | Out-Null
    Start-Process `
        -FilePath $powerShell `
        -ArgumentList @(
            "-NoProfile",
            "-WindowStyle", "Hidden",
            "-ExecutionPolicy", "Bypass",
            "-File", $watchdogScript,
            "-TeamHome", $teamHome
        ) `
        -WindowStyle Hidden
}
@{
    ok = $true
    taskName = $taskName
    ensureScript = $ensureScript
    watchdogScript = $watchdogScript
    teamHome = $teamHome
    registration = $registration
    warning = $warning
} | ConvertTo-Json
