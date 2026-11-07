param(
    [string]$Version
)

$ErrorActionPreference = "Stop"
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
if (!$Version) {
    $Version = (
        Select-String -LiteralPath (Join-Path $root "pyproject.toml") `
            -Pattern '^version = "([^"]+)"$'
    ).Matches.Groups[1].Value
}
if (!$Version) {
    throw "version not found"
}

$releaseRoot = Join-Path $root "dist"
$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$temp = [IO.Path]::GetFullPath(
    (Join-Path $tempRoot ("codex-team-board-build-" + [Guid]::NewGuid().ToString("N")))
)
if (!$temp.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "unsafe temporary path"
}
$bundle = Join-Path $temp "codex-team-board-v$Version"
$zipPath = Join-Path $releaseRoot "codex-team-board-v$Version.zip"
$checksumPath = Join-Path $releaseRoot "codex-team-board-v$Version.sha256"

function Copy-Tree {
    param(
        [string]$Source,
        [string]$Destination
    )
    $sourceRoot = [IO.Path]::GetFullPath($Source)
    $sourceUri = [Uri](
        $sourceRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) +
        [IO.Path]::DirectorySeparatorChar
    )
    foreach ($file in Get-ChildItem -LiteralPath $sourceRoot -Recurse -File -Force) {
        $fileUri = [Uri]$file.FullName
        $relative = [Uri]::UnescapeDataString(
            $sourceUri.MakeRelativeUri($fileUri).ToString()
        ).Replace("/", [IO.Path]::DirectorySeparatorChar)
        if ($relative -match "(^|\\)(\.git|__pycache__|\.pytest_cache|artifacts|dist)(\\|$)") {
            continue
        }
        if ($file.Extension -in @(".pyc", ".pyo", ".sqlite", ".log")) {
            continue
        }
        $target = Join-Path $Destination $relative
        New-Item -ItemType Directory -Force -Path ([IO.Path]::GetDirectoryName($target)) |
            Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $target -Force
    }
}

New-Item -ItemType Directory -Force -Path $releaseRoot | Out-Null
New-Item -ItemType Directory -Force -Path $bundle | Out-Null
try {
    foreach ($name in @("src", "scripts", "templates", "tests")) {
        Copy-Tree `
            -Source (Join-Path $root $name) `
            -Destination (Join-Path $bundle $name)
    }
    foreach ($name in @(
        "README.md",
        "AUDIT-2026-11-07.md",
        "CHANGELOG.md",
        "LICENSE",
        "pyproject.toml",
        "start-team-board.cmd"
    )) {
        Copy-Item -LiteralPath (Join-Path $root $name) -Destination (Join-Path $bundle $name) -Force
    }
    Compress-Archive -LiteralPath $bundle -DestinationPath $zipPath -CompressionLevel Optimal -Force
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath).Hash.ToLowerInvariant()
    [IO.File]::WriteAllText(
        $checksumPath,
        "$hash  $([IO.Path]::GetFileName($zipPath))`n",
        [Text.UTF8Encoding]::new($false)
    )
    @{
        ok = $true
        version = $Version
        archive = $zipPath
        sha256 = $hash
        checksumFile = $checksumPath
    } | ConvertTo-Json
}
finally {
    if (Test-Path -LiteralPath $temp) {
        Remove-Item -LiteralPath $temp -Recurse -Force
    }
}
