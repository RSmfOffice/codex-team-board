param(
    [int]$IntervalSeconds = 60,
    [string]$TeamHome = ""
)

$ErrorActionPreference = "Continue"
$projectRoot = Split-Path -Parent $PSScriptRoot
$ensureScript = Join-Path $PSScriptRoot "ensure-cluster-daemon.ps1"
$powerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"

if ($TeamHome) {
    $env:CODEX_TEAM_HOME = $TeamHome
    $logDir = Join-Path $TeamHome "logs"
}
else {
    $logDir = Join-Path $env:LOCALAPPDATA "CodexTeam\logs"
}
$logPath = Join-Path $logDir "cluster-watchdog.log"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$mutex = [System.Threading.Mutex]::new($false, "Local\CodexTeamClusterWatchdog")
$ownsMutex = $false
try {
    try {
        $ownsMutex = $mutex.WaitOne(0)
    }
    catch [System.Threading.AbandonedMutexException] {
        $ownsMutex = $true
    }
    if (-not $ownsMutex) {
        exit 0
    }

    while ($true) {
        try {
            $ensureArguments = @(
                "-NoProfile",
                "-WindowStyle", "Hidden",
                "-ExecutionPolicy", "Bypass",
                "-File", $ensureScript
            )
            if ($TeamHome) {
                $ensureArguments += @("-TeamHome", $TeamHome)
            }
            & $powerShell @ensureArguments *>> $logPath
        }
        catch {
            "[$(Get-Date -Format o)] watchdog error: $($_.Exception.Message)" |
                Add-Content -LiteralPath $logPath -Encoding utf8
        }
        Start-Sleep -Seconds ([Math]::Max(15, $IntervalSeconds))
    }
}
finally {
    if ($ownsMutex) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
