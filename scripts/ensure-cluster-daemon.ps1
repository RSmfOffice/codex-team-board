param(
    [string]$TeamHome = ""
)

$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
if ($TeamHome) {
    $env:CODEX_TEAM_HOME = $TeamHome
}
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonRoot = Join-Path $env:LOCALAPPDATA "Python"
$python = Get-ChildItem -LiteralPath $pythonRoot -Filter "python.exe" `
    -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match "pythoncore-[^\\]+\\python\.exe$" } |
    Sort-Object FullName -Descending |
    Select-Object -First 1

if (-not $python) {
    throw "pythoncore interpreter not found"
}

$env:PYTHONPATH = Join-Path $projectRoot "src"
Push-Location $projectRoot
try {
    & $python.FullName -c "from codex_team.cluster import ensure_daemon; from codex_team.config import Settings; import json; print(json.dumps(ensure_daemon(Settings.load()), ensure_ascii=False))"
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
