param(
    [double]$Interval = 5
)

$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
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
    & $python.FullName -m codex_team.clusterd --interval $Interval
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
