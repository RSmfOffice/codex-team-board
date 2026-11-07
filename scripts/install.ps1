param(
    [string]$Repo = "",
    [string]$ContextMeterRoot = "",
    [string]$DashboardUrl = "http://127.0.0.1:8765"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $projectRoot "src"
$pythonRoot = Join-Path $env:LOCALAPPDATA "Python"
$python = Get-ChildItem -LiteralPath $pythonRoot -Filter "python.exe" `
    -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match "pythoncore-[^\\]+\\python\.exe$" } |
    Sort-Object FullName -Descending |
    Select-Object -First 1

if (-not $python) {
    throw "pythoncore interpreter not found"
}

$teamHome = (& $python.FullName -c "from codex_team.config import Settings; print(Settings.load().home)").Trim()
if (!$teamHome -or !(Test-Path -LiteralPath (Split-Path $teamHome -Parent))) {
    throw "failed to resolve CodexTeam data directory"
}
[Environment]::SetEnvironmentVariable("CODEX_TEAM_BOARD_ROOT", $projectRoot, "User")
[Environment]::SetEnvironmentVariable("CODEX_TEAM_DASHBOARD_URL", $DashboardUrl, "User")
$env:CODEX_TEAM_BOARD_ROOT = $projectRoot
$env:CODEX_TEAM_DASHBOARD_URL = $DashboardUrl

$globalAgents = Join-Path $env:USERPROFILE ".codex\agents"
New-Item -ItemType Directory -Path $globalAgents -Force | Out-Null
foreach ($name in @("team-explorer.toml", "team-worker.toml", "team-reviewer.toml")) {
    $source = Join-Path $projectRoot "templates\agents\$name"
    $target = Join-Path $globalAgents $name
    if (-not (Test-Path -LiteralPath $target)) {
        Copy-Item -LiteralPath $source -Destination $target
        Write-Output "Installed role: $target"
    } else {
        Write-Output "Preserved role: $target"
    }
}

$promptRoles = Join-Path $env:USERPROFILE ".codex\team-roles"
New-Item -ItemType Directory -Path $promptRoles -Force | Out-Null
foreach ($name in @("team-explorer.md", "team-worker.md", "team-reviewer.md")) {
    $source = Join-Path $projectRoot "templates\roles\$name"
    $target = Join-Path $promptRoles $name
    if (-not (Test-Path -LiteralPath $target)) {
        Copy-Item -LiteralPath $source -Destination $target
        Write-Output "Installed role prompt: $target"
    } else {
        Write-Output "Preserved role prompt: $target"
    }
}

$codex = Get-Command codex.exe -ErrorAction Stop
& $codex.Source mcp remove codexTeamBoard 2>$null | Out-Null
& $codex.Source mcp add codexTeamBoard `
    --env PYTHONUTF8=1 `
    --env "CODEX_TEAM_HOME=$teamHome" `
    -- $python.FullName -u (Join-Path $projectRoot "src\mcp_server.py")
if ($LASTEXITCODE -ne 0) {
    throw "failed to register codexTeamBoard MCP"
}

& $python.FullName -m codex_team.bootstrap
if ($LASTEXITCODE -ne 0) {
    throw "failed to configure global Codex integration"
}
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "install-cluster-task.ps1")
if ($LASTEXITCODE -ne 0) {
    throw "failed to install cluster daemon scheduled task"
}
if ($Repo) {
    & $python.FullName -m codex_team.cli init --repo $Repo
} else {
    & $python.FullName -m codex_team.cli status
}

$dashboardUri = [Uri]$DashboardUrl
if ($dashboardUri.Scheme -ne "http") {
    throw "DashboardUrl must use http."
}
$dashboardReachable = $false
try {
    $health = Invoke-RestMethod -Uri ($DashboardUrl.TrimEnd("/") + "/api/health") -TimeoutSec 2
    $dashboardReachable = [bool]($health.ok -and $health.service -eq "codex-team-board")
}
catch {}
if (!$dashboardReachable) {
    $port = if ($dashboardUri.IsDefaultPort) { 8765 } else { $dashboardUri.Port }
    $dashboardScript = Join-Path $PSScriptRoot "dashboard.ps1"
    Start-Process `
        -FilePath "powershell.exe" `
        -ArgumentList @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", $dashboardScript,
            "-Port", [string]$port,
            "-NoBrowser"
        ) `
        -WindowStyle Hidden
}

if ($ContextMeterRoot) {
    $contextRoot = [IO.Path]::GetFullPath($ContextMeterRoot)
    $contextInstaller = Join-Path $contextRoot "codex-context-meter-plugin\scripts\install.ps1"
    if (!(Test-Path -LiteralPath $contextInstaller -PathType Leaf)) {
        throw "context meter installer not found: $contextInstaller"
    }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass `
        -File $contextInstaller `
        -TeamBoardRoot $projectRoot `
        -DashboardUrl $DashboardUrl
    if ($LASTEXITCODE -ne 0) {
        throw "failed to install and link codex-context-meter"
    }
}

Write-Output ""
Write-Output "Installation complete."
Write-Output "Team board root: $projectRoot"
Write-Output "Team board URL: $DashboardUrl"
if ($ContextMeterRoot) {
    Write-Output "Context meter: $([IO.Path]::GetFullPath($ContextMeterRoot))"
}
Write-Output "Dashboard: powershell -ExecutionPolicy Bypass -File `"$projectRoot\scripts\dashboard.ps1`""
