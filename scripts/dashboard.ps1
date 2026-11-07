param(
    [int]$Port = 8765,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$teamScript = Join-Path $PSScriptRoot "team.ps1"
$arguments = @("dashboard", "--port", $Port.ToString())
if ($NoBrowser) {
    $arguments += "--no-browser"
}
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $teamScript @arguments
