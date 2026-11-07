param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
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
    & $python.FullName -m codex_team.cli @Arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
