param(
    [string]$Repo = "",
    [string]$ContextMeterRoot = "",
    [string]$DashboardUrl = "http://127.0.0.1:8765",
    [string]$ResultPath = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$installStartedAt = [DateTime]::UtcNow
$testRootLiteral = [Environment]::GetEnvironmentVariable(
    "CODEX_TEAM_INSTALL_TEST_ROOT",
    "Process"
)
$isTestMode = -not [string]::IsNullOrWhiteSpace($testRootLiteral)

if ($isTestMode) {
    $testRoot = [IO.Path]::GetFullPath($testRootLiteral)
    $effectiveUserProfile = Join-Path $testRoot "user"
    $effectiveLocalAppData = Join-Path $testRoot "local"
    $codexHome = Join-Path $testRoot "codex-home"
    $testStateRoot = Join-Path $testRoot "state"
}
else {
    $effectiveUserProfile = $env:USERPROFILE
    $effectiveLocalAppData = $env:LOCALAPPDATA
    $codexHome = if ($env:CODEX_HOME) {
        [IO.Path]::GetFullPath($env:CODEX_HOME)
    }
    else {
        Join-Path $effectiveUserProfile ".codex"
    }
    $testStateRoot = ""
}

[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$script:StageRecords = New-Object System.Collections.ArrayList
$script:CurrentStage = $null
$script:FailureStage = $null
$script:FileSnapshots = New-Object System.Collections.ArrayList
$script:DirectorySnapshots = New-Object System.Collections.ArrayList
$script:CreatedDirectories = New-Object System.Collections.ArrayList
$script:TrackedProcesses = New-Object System.Collections.ArrayList
$script:SnapshottedFiles = @{}
$script:SnapshottedDirectories = @{}
$script:TransactionStarted = $false
$script:RollbackAttempted = $false
$script:RollbackSucceeded = $false
$script:RollbackErrors = New-Object System.Collections.ArrayList
$script:ScheduledTaskSnapshot = @()
$script:RunValueSnapshot = @()
$script:OriginalDaemonPid = $null
$script:StartedDaemonPid = $null
$script:EnvironmentSnapshot = @{}
$script:McpSwitchCount = 0
$script:StageRoot = ""
$script:KeepStageForRecovery = $false
$script:Preflight = $null
$script:DashboardStartedByInstaller = $false
$script:InstalledRoles = New-Object System.Collections.ArrayList
$script:Result = [ordered]@{
    schema_version = 1
    ok = $false
    status = "running"
    started_at = $installStartedAt.ToString("o")
    finished_at = $null
    failure_stage = $null
    error = $null
    project_root = $projectRoot
    installed = $null
    preflight = $null
    stages = $script:StageRecords
    rollback = $null
    artifacts = [ordered]@{
        stage_root = $null
        result_path = if ($ResultPath) { [IO.Path]::GetFullPath($ResultPath) } else { $null }
    }
}

function Get-UtcTimestamp {
    return [DateTime]::UtcNow.ToString("o")
}

function ConvertTo-CompactJson {
    param($Value)

    return ($Value | ConvertTo-Json -Depth 20 -Compress)
}

function Get-ObjectProperty {
    param(
        $Value,
        [string]$Name,
        $Default = $null
    )

    if ($null -eq $Value) {
        return $Default
    }
    $property = $Value.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $Default
    }
    return $property.Value
}

function ConvertFrom-MixedCommandJson {
    param([string]$Text)

    try {
        return $Text | ConvertFrom-Json
    }
    catch {
        $start = $Text.IndexOf("{")
        $end = $Text.LastIndexOf("}")
        if ($start -lt 0 -or $end -le $start) {
            throw
        }
        return $Text.Substring($start, $end - $start + 1) | ConvertFrom-Json
    }
}

function Normalize-FullPath {
    param([string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        return ""
    }
    return [IO.Path]::GetFullPath($Path)
}

function Test-SamePath {
    param(
        [string]$Left,
        [string]$Right
    )

    if ([string]::IsNullOrWhiteSpace($Left) -or [string]::IsNullOrWhiteSpace($Right)) {
        return $false
    }
    $normalizedLeft = (Normalize-FullPath $Left).TrimEnd("\", "/")
    $normalizedRight = (Normalize-FullPath $Right).TrimEnd("\", "/")
    return [string]::Equals(
        $normalizedLeft,
        $normalizedRight,
        [StringComparison]::OrdinalIgnoreCase
    )
}

function Write-Human {
    param([string]$Message)

    Write-Host $Message
}

function Start-InstallStage {
    param([string]$Name)

    $record = [pscustomobject][ordered]@{
        name = $Name
        status = "running"
        started_at = Get-UtcTimestamp
        finished_at = $null
        duration_ms = $null
        note = $null
        error = $null
    }
    [void]$script:StageRecords.Add($record)
    $script:CurrentStage = $record
    Write-Human "==> $Name"
    $payload = [ordered]@{
        event = "stage"
        stage = $Name
        status = "started"
        timestamp = $record.started_at
    }
    Write-Output ("CODEX_TEAM_INSTALL_STAGE=" + (ConvertTo-CompactJson $payload))
}

function Complete-InstallStage {
    param([string]$Note = "")

    if ($null -eq $script:CurrentStage) {
        return
    }
    $record = $script:CurrentStage
    $record.status = "succeeded"
    $record.finished_at = Get-UtcTimestamp
    $record.note = if ($Note) { $Note } else { $null }
    $record.duration_ms = [Math]::Round(
        ([DateTime]::Parse($record.finished_at) - [DateTime]::Parse($record.started_at)).TotalMilliseconds,
        1
    )
    $payload = [ordered]@{
        event = "stage"
        stage = $record.name
        status = "succeeded"
        timestamp = $record.finished_at
        duration_ms = $record.duration_ms
    }
    Write-Output ("CODEX_TEAM_INSTALL_STAGE=" + (ConvertTo-CompactJson $payload))
    $script:CurrentStage = $null
}

function Fail-InstallStage {
    param([System.Exception]$Exception)

    if ($null -eq $script:CurrentStage) {
        return
    }
    $record = $script:CurrentStage
    $record.status = "failed"
    $record.finished_at = Get-UtcTimestamp
    $record.error = $Exception.Message
    $record.duration_ms = [Math]::Round(
        ([DateTime]::Parse($record.finished_at) - [DateTime]::Parse($record.started_at)).TotalMilliseconds,
        1
    )
    $script:FailureStage = $record.name
    $payload = [ordered]@{
        event = "stage"
        stage = $record.name
        status = "failed"
        timestamp = $record.finished_at
        duration_ms = $record.duration_ms
        error = $record.error
    }
    Write-Output ("CODEX_TEAM_INSTALL_STAGE=" + (ConvertTo-CompactJson $payload))
    $script:CurrentStage = $null
}

function Invoke-InstallStage {
    param(
        [string]$Name,
        [scriptblock]$Action,
        [string]$Note = ""
    )

    Start-InstallStage $Name
    try {
        & $Action
        Complete-InstallStage $Note
    }
    catch {
        Fail-InstallStage $_.Exception
        throw
    }
}

function Invoke-TestFault {
    param([string]$Name)

    if (-not $isTestMode) {
        return
    }
    $fault = [Environment]::GetEnvironmentVariable(
        "CODEX_TEAM_INSTALL_TEST_FAULT",
        "Process"
    )
    if ($fault -eq $Name) {
        throw "injected test failure at $Name"
    }
}

function Resolve-Executable {
    param(
        [string]$Override,
        [string]$CommandName,
        [string]$Description
    )

    if ($Override) {
        $candidate = Normalize-FullPath $Override
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            throw "$Description not found: $candidate"
        }
        return $candidate
    }
    $command = Get-Command $CommandName -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        throw "$Description not found on PATH: $CommandName"
    }
    return $command.Source
}

function Resolve-Python {
    $override = [Environment]::GetEnvironmentVariable(
        "CODEX_TEAM_INSTALL_PYTHON",
        "Process"
    )
    if ($override) {
        return Resolve-Executable $override "python.exe" "Python interpreter"
    }

    $pythonRoot = Join-Path $env:LOCALAPPDATA "Python"
    if (Test-Path -LiteralPath $pythonRoot -PathType Container) {
        $python = Get-ChildItem -LiteralPath $pythonRoot -Filter "python.exe" `
            -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match "pythoncore-[^\\]+\\python\.exe$" } |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($null -ne $python) {
            return $python.FullName
        }
    }
    return Resolve-Executable "" "python.exe" "Python interpreter"
}

function Resolve-PowerShell {
    $candidate = Join-Path $PSHOME "powershell.exe"
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        return $candidate
    }
    return Resolve-Executable "" "powershell.exe" "Windows PowerShell"
}

function Read-TestEnvironmentState {
    $path = Join-Path $testStateRoot "user-environment.json"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        return [ordered]@{}
    }
    $value = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
    $state = [ordered]@{}
    foreach ($property in $value.PSObject.Properties) {
        $state[$property.Name] = [string]$property.Value
    }
    return $state
}

function Write-TestEnvironmentState {
    param($State)

    New-Item -ItemType Directory -Path $testStateRoot -Force | Out-Null
    $path = Join-Path $testStateRoot "user-environment.json"
    $content = ($State | ConvertTo-Json -Depth 5) + "`n"
    [IO.File]::WriteAllText($path, $content, [Text.UTF8Encoding]::new($false))
}

function Get-UserEnvironmentValue {
    param([string]$Name)

    if ($isTestMode) {
        $state = Read-TestEnvironmentState
        if ($state.Contains($Name)) {
            return [string]$state[$Name]
        }
        return $null
    }
    return [Environment]::GetEnvironmentVariable($Name, "User")
}

function Set-UserEnvironmentValue {
    param(
        [string]$Name,
        [AllowNull()]
        [string]$Value
    )

    if ($isTestMode) {
        $state = Read-TestEnvironmentState
        if ($null -eq $Value) {
            $state.Remove($Name)
        }
        else {
            $state[$Name] = $Value
        }
        Write-TestEnvironmentState $state
        return
    }
    [Environment]::SetEnvironmentVariable($Name, $Value, "User")
}

function Add-FileSnapshot {
    param([string]$Path)

    $fullPath = Normalize-FullPath $Path
    if ($script:SnapshottedFiles.ContainsKey($fullPath)) {
        return
    }
    $script:SnapshottedFiles[$fullPath] = $true
    $backupRoot = Join-Path $script:StageRoot "backups\files"
    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
    $record = [pscustomobject][ordered]@{
        path = $fullPath
        kind = "missing"
        backup = $null
    }
    if (Test-Path -LiteralPath $fullPath -PathType Leaf) {
        $backup = Join-Path $backupRoot ([guid]::NewGuid().ToString("N"))
        Copy-Item -LiteralPath $fullPath -Destination $backup -Force
        $record.kind = "file"
        $record.backup = $backup
    }
    elseif (Test-Path -LiteralPath $fullPath -PathType Container) {
        throw "expected a file snapshot target but found a directory: $fullPath"
    }
    [void]$script:FileSnapshots.Add($record)
}

function Add-DirectorySnapshot {
    param(
        [string]$Path,
        [switch]$Recursive
    )

    $fullPath = Normalize-FullPath $Path
    $key = "$fullPath|$($Recursive.IsPresent)"
    if ($script:SnapshottedDirectories.ContainsKey($key)) {
        return
    }
    $script:SnapshottedDirectories[$key] = $true
    $backupRoot = Join-Path $script:StageRoot "backups\directories"
    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
    $record = [pscustomobject][ordered]@{
        path = $fullPath
        kind = "missing"
        backup = $null
        recursive = [bool]$Recursive
    }
    if (Test-Path -LiteralPath $fullPath -PathType Container) {
        if ($Recursive) {
            $backup = Join-Path $backupRoot ([guid]::NewGuid().ToString("N"))
            Copy-Item -LiteralPath $fullPath -Destination $backup -Recurse -Force
            $record.kind = "directory"
            $record.backup = $backup
        }
        else {
            $record.kind = "directory"
        }
    }
    elseif (Test-Path -LiteralPath $fullPath -PathType Leaf) {
        throw "expected a directory snapshot target but found a file: $fullPath"
    }
    [void]$script:DirectorySnapshots.Add($record)
    if ($record.kind -eq "missing") {
        [void]$script:CreatedDirectories.Add($fullPath)
    }
}

function Copy-FileAtomically {
    param(
        [string]$Source,
        [string]$Target
    )

    $targetDirectory = Split-Path -Parent $Target
    if (-not (Test-Path -LiteralPath $targetDirectory -PathType Container)) {
        New-Item -ItemType Directory -Path $targetDirectory -Force | Out-Null
    }
    $temporary = Join-Path $targetDirectory (
        "." + (Split-Path -Leaf $Target) + "." + [guid]::NewGuid().ToString("N") + ".tmp"
    )
    try {
        Copy-Item -LiteralPath $Source -Destination $temporary -Force
        if (Test-Path -LiteralPath $Target -PathType Leaf) {
            $replaceBackup = Join-Path $targetDirectory (
                "." + (Split-Path -Leaf $Target) + "." +
                [guid]::NewGuid().ToString("N") + ".replace-backup"
            )
            try {
                [IO.File]::Replace($temporary, $Target, $replaceBackup)
            }
            finally {
                if (Test-Path -LiteralPath $replaceBackup -PathType Leaf) {
                    Remove-Item -LiteralPath $replaceBackup -Force
                }
            }
        }
        else {
            [IO.File]::Move($temporary, $Target)
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

function Invoke-CommandWithEnvironment {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [hashtable]$Environment
    )

    $previous = @{}
    foreach ($name in $Environment.Keys) {
        $previous[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
        [Environment]::SetEnvironmentVariable($name, [string]$Environment[$name], "Process")
    }
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $FilePath @Arguments 2>&1
        $exitCode = $LASTEXITCODE
        return [pscustomobject][ordered]@{
            exit_code = $exitCode
            output = (($output | Out-String -Width 4096).Trim())
        }
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
        foreach ($name in $Environment.Keys) {
            [Environment]::SetEnvironmentVariable($name, $previous[$name], "Process")
        }
    }
}

function Invoke-CodexMcp {
    param(
        [string]$CodexHome,
        [string[]]$Arguments,
        [switch]$AllowFailure
    )

    $result = Invoke-CommandWithEnvironment `
        -FilePath $script:CodexExecutable `
        -Arguments $Arguments `
        -Environment @{ CODEX_HOME = $CodexHome }
    if ($result.exit_code -ne 0 -and -not $AllowFailure) {
        throw "codex $($Arguments -join ' ') failed: $($result.output)"
    }
    return $result
}

function Invoke-PythonJson {
    param(
        [string]$Code,
        [hashtable]$Environment = @{},
        [string[]]$CodeArguments = @()
    )

    $childEnvironment = @{
        PYTHONPATH = Join-Path $projectRoot "src"
        PYTHONIOENCODING = "utf-8"
        PYTHONUTF8 = "1"
    }
    foreach ($name in $Environment.Keys) {
        $childEnvironment[$name] = $Environment[$name]
    }
    $temporary = Join-Path ([IO.Path]::GetTempPath()) (
        "codex-team-python-" + [guid]::NewGuid().ToString("N") + ".py"
    )
    [IO.File]::WriteAllText(
        $temporary,
        $Code,
        [Text.UTF8Encoding]::new($false)
    )
    try {
        $arguments = @($temporary) + $CodeArguments
        $result = Invoke-CommandWithEnvironment `
            -FilePath $script:PythonExecutable `
            -Arguments $arguments `
            -Environment $childEnvironment
        if ($result.exit_code -ne 0) {
            throw "Python probe failed: $($result.output)"
        }
        try {
            return $result.output | ConvertFrom-Json
        }
        catch {
            throw "Python probe returned invalid JSON: $($result.output)"
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

function Get-DaemonStatus {
    param([string]$TeamHome)

    $code = @'
import json
from codex_team.cluster import daemon_status
from codex_team.config import Settings
print(json.dumps(daemon_status(Settings.load()), ensure_ascii=False))
'@
    return Invoke-PythonJson $code @{ CODEX_TEAM_HOME = $TeamHome }
}

function Stop-NewClusterDaemon {
    if ($null -eq $script:StartedDaemonPid) {
        return
    }
    $code = @'
import json
import sys
from codex_team.cluster import daemon_status, stop_daemon
from codex_team.config import Settings
settings = Settings.load()
status = daemon_status(settings)
original = int(sys.argv[1])
result = {"stopped": False, "pid": status.get("pid")}
if status.get("running") and int(status.get("pid") or 0) != original:
    result = stop_daemon(settings)
    result["stopped"] = True
print(json.dumps(result, ensure_ascii=False))
'@
    $childEnvironment = @{
        PYTHONPATH = Join-Path $projectRoot "src"
        PYTHONIOENCODING = "utf-8"
        PYTHONUTF8 = "1"
        CODEX_TEAM_HOME = $script:Preflight.team_home
    }
    [void](Invoke-PythonJson $code $childEnvironment -CodeArguments @(
        [string]$(if ($null -eq $script:OriginalDaemonPid) {
            0
        }
        else {
            $script:OriginalDaemonPid
        })
    ))
}

function Get-DashboardPort {
    param([Uri]$Uri)

    if ($Uri.IsDefaultPort) {
        return 80
    }
    return $Uri.Port
}

function Test-TcpPortOpen {
    param(
        [string]$HostName,
        [int]$Port
    )

    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync($HostName, $Port)
        if (-not $task.Wait(700)) {
            return $false
        }
        return $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Get-DashboardHealth {
    param([int]$TimeoutSeconds = 2)

    try {
        return Invoke-RestMethod `
            -Uri ($script:DashboardBaseUrl + "/api/health") `
            -Method Get `
            -TimeoutSec $TimeoutSeconds `
            -ErrorAction Stop
    }
    catch {
        return $null
    }
}

function Assert-DashboardContract {
    param(
        $Health,
        [string]$Phase
    )

    if ($null -eq $Health) {
        throw "$Phase dashboard health check did not return a response"
    }
    if (-not [bool](Get-ObjectProperty $Health "ok" $false)) {
        throw "$Phase dashboard health is not ok"
    }
    if ([string](Get-ObjectProperty $Health "service" "") -ne "codex-team-board") {
        throw "$Phase dashboard health identifies a different service"
    }
    $actualVersion = [string](Get-ObjectProperty $Health "version" "")
    if ($actualVersion -ne $script:Preflight.version) {
        throw "$Phase dashboard version is '$actualVersion', expected '$($script:Preflight.version)'"
    }
    $actualSchema = [int](Get-ObjectProperty $Health "schema_version" -1)
    if ($actualSchema -ne $script:Preflight.schema_version) {
        throw "$Phase dashboard schema is $actualSchema, expected $($script:Preflight.schema_version)"
    }
    $actualTeamHome = [string](Get-ObjectProperty $Health "team_home" "")
    if (-not (Test-SamePath $actualTeamHome $script:Preflight.team_home)) {
        throw "$Phase dashboard team_home '$actualTeamHome' does not match '$($script:Preflight.team_home)'"
    }
    $actualDatabase = [string](Get-ObjectProperty $Health "database" "")
    if (-not (Test-SamePath $actualDatabase $script:Preflight.database)) {
        throw "$Phase dashboard database '$actualDatabase' does not match '$($script:Preflight.database)'"
    }
    $runtime = Get-ObjectProperty $Health "cluster_runtime" $null
    if ($null -eq $runtime) {
        throw "$Phase dashboard health is missing cluster_runtime"
    }
    $runtimeName = [string](Get-ObjectProperty $runtime "runtime" "")
    if ($runtimeName -notin @("app_server", "exec")) {
        throw "$Phase dashboard cluster runtime is invalid: '$runtimeName'"
    }
    $runtimeVersion = [string](Get-ObjectProperty $runtime "version" "")
    if ($runtimeVersion -ne $script:Preflight.version) {
        throw "$Phase dashboard cluster runtime version is '$runtimeVersion', expected '$($script:Preflight.version)'"
    }
    if ([bool](Get-ObjectProperty $runtime "config_mismatch" $false)) {
        throw "$Phase dashboard reports a cluster runtime configuration mismatch"
    }
    return $Health
}

function Wait-DashboardContract {
    param(
        [int]$Consecutive = 2,
        [int]$TimeoutSeconds = 15,
        [int]$DelayMilliseconds = 350
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $successes = 0
    $attempts = 0
    $lastError = ""
    while ([DateTime]::UtcNow -lt $deadline) {
        $attempts++
        $health = Get-DashboardHealth 2
        if ($null -ne $health) {
            try {
                Assert-DashboardContract $health "post-install" | Out-Null
                $successes++
                if ($isTestMode -and $successes -eq 1) {
                    Invoke-TestFault "dashboard"
                }
                if ($successes -ge $Consecutive) {
                    return [pscustomobject][ordered]@{
                        health = $health
                        attempts = $attempts
                        consecutive = $successes
                    }
                }
            }
            catch {
                $successes = 0
                $lastError = $_.Exception.Message
                $testFault = [Environment]::GetEnvironmentVariable(
                    "CODEX_TEAM_INSTALL_TEST_FAULT",
                    "Process"
                )
                if ($isTestMode -and $testFault -eq "dashboard") {
                    throw
                }
            }
        }
        else {
            $successes = 0
        }
        Start-Sleep -Milliseconds ([Math]::Max(50, $DelayMilliseconds))
    }
    if (-not $lastError) {
        $lastError = "dashboard did not answer /api/health"
    }
    throw "health verification timed out after $attempts attempts: $lastError"
}

function Get-TrackedProcessCreationTime {
    param([System.Diagnostics.Process]$Process)

    try {
        return $Process.StartTime.ToUniversalTime().ToString("o")
    }
    catch {
        return $null
    }
}

function Add-TrackedProcess {
    param(
        [int]$ProcessId,
        [string]$CreationTime,
        [string]$Source
    )

    if ($ProcessId -le 0) {
        return
    }
    foreach ($entry in $script:TrackedProcesses) {
        if ($entry.pid -eq $ProcessId) {
            return
        }
    }
    [void]$script:TrackedProcesses.Add([pscustomobject][ordered]@{
        pid = $ProcessId
        creation_time = $CreationTime
        source = $Source
        stopped = $false
    })
}

function Stop-TrackedProcesses {
    $stopped = New-Object System.Collections.ArrayList
    foreach ($entry in @($script:TrackedProcesses)) {
        $process = Get-Process -Id $entry.pid -ErrorAction SilentlyContinue
        if ($null -eq $process) {
            continue
        }
        $currentCreation = Get-TrackedProcessCreationTime $process
        $identityMatches = (
            [string]::IsNullOrWhiteSpace($entry.creation_time) -or
            [string]::IsNullOrWhiteSpace($currentCreation) -or
            [DateTime]::Parse($entry.creation_time).ToUniversalTime() -eq
                [DateTime]::Parse($currentCreation).ToUniversalTime()
        )
        if ($identityMatches) {
            & taskkill.exe /PID $entry.pid /T /F 2>$null | Out-Null
            $deadline = [DateTime]::UtcNow.AddSeconds(5)
            while ($null -ne (Get-Process -Id $entry.pid -ErrorAction SilentlyContinue) -and
                [DateTime]::UtcNow -lt $deadline) {
                Start-Sleep -Milliseconds 100
            }
            if ($null -ne (Get-Process -Id $entry.pid -ErrorAction SilentlyContinue)) {
                throw "tracked $($entry.source) process $($entry.pid) did not stop"
            }
            $entry.stopped = $true
            [void]$stopped.Add($entry.pid)
        }
    }
    return @($stopped)
}

function Get-ProcessesMatchingCommandLine {
    param([string]$Fragment)

    if ([string]::IsNullOrWhiteSpace($Fragment)) {
        return @()
    }
    return @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $_.CommandLine -and
                $_.CommandLine.IndexOf(
                    $Fragment,
                    [StringComparison]::OrdinalIgnoreCase
                ) -ge 0
            } |
            ForEach-Object {
                [pscustomobject][ordered]@{
                    pid = [int]$_.ProcessId
                    creation_time = ([DateTime]$_.CreationDate).ToUniversalTime().ToString("o")
                }
            }
    )
}

function Get-CodexScheduledTaskRecords {
    $records = @()
    foreach ($task in @(Get-ScheduledTask -ErrorAction SilentlyContinue |
        Where-Object { $_.TaskName -match "(?i)codex" })) {
        try {
            $records += [pscustomobject][ordered]@{
                task_path = [string]$task.TaskPath
                task_name = [string]$task.TaskName
                xml = [string](Export-ScheduledTask `
                    -TaskName $task.TaskName `
                    -TaskPath $task.TaskPath)
            }
        }
        catch {
            throw "failed to snapshot scheduled task '$($task.TaskPath)$($task.TaskName)': $($_.Exception.Message)"
        }
    }
    return $records
}

function Restore-CodexScheduledTaskRecords {
    param($Records)

    $snapshotKeys = @{}
    foreach ($record in @($Records)) {
        $snapshotKeys["$($record.task_path)|$($record.task_name)"] = $record
    }
    $current = Get-CodexScheduledTaskRecords
    foreach ($task in @($current)) {
        $key = "$($task.task_path)|$($task.task_name)"
        if (-not $snapshotKeys.ContainsKey($key)) {
            Unregister-ScheduledTask `
                -TaskName $task.task_name `
                -TaskPath $task.task_path `
                -Confirm:$false `
                -ErrorAction Stop
        }
    }
    foreach ($record in @($Records)) {
        Register-ScheduledTask `
            -TaskName $record.task_name `
            -TaskPath $record.task_path `
            -Xml $record.xml `
            -Force `
            -ErrorAction Stop | Out-Null
    }
}

function Get-CodexRunValueRecords {
    $runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
    if (-not (Test-Path -LiteralPath $runKey)) {
        return @()
    }
    $item = Get-ItemProperty -LiteralPath $runKey
    $records = @()
    foreach ($property in $item.PSObject.Properties) {
        if ($property.Name.StartsWith("PS") -or $property.Name -notmatch "(?i)codex") {
            continue
        }
        $records += [pscustomobject][ordered]@{
            name = [string]$property.Name
            value = [string]$property.Value
        }
    }
    return $records
}

function Restore-CodexRunValueRecords {
    param($Records)

    $runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
    if (-not (Test-Path -LiteralPath $runKey)) {
        New-Item -Path $runKey -Force | Out-Null
    }
    $snapshotNames = @{}
    foreach ($record in @($Records)) {
        $snapshotNames[$record.name] = $record
    }
    foreach ($current in @(Get-CodexRunValueRecords)) {
        if (-not $snapshotNames.ContainsKey($current.name)) {
            Remove-ItemProperty -LiteralPath $runKey -Name $current.name -ErrorAction Stop
        }
    }
    foreach ($record in @($Records)) {
        New-ItemProperty `
            -LiteralPath $runKey `
            -Name $record.name `
            -Value $record.value `
            -PropertyType String `
            -Force `
            -ErrorAction Stop | Out-Null
    }
}

function Get-RolePlan {
    $targets = @()
    foreach ($name in @("team-explorer.toml", "team-worker.toml", "team-reviewer.toml")) {
        $targets += [pscustomobject][ordered]@{
            key = "agent:$name"
            source = Join-Path $projectRoot "templates\agents\$name"
            target = Join-Path (Join-Path $effectiveUserProfile ".codex\agents") $name
        }
    }
    foreach ($name in @("team-explorer.md", "team-worker.md", "team-reviewer.md")) {
        $targets += [pscustomobject][ordered]@{
            key = "role:$name"
            source = Join-Path $projectRoot "templates\roles\$name"
            target = Join-Path (Join-Path $effectiveUserProfile ".codex\team-roles") $name
        }
    }
    return $targets
}

function Get-ProjectInitPaths {
    param([string]$Root)

    $paths = @(
        (Join-Path $Root "AGENTS.md"),
        (Join-Path $Root ".codex\team-project.json")
    )
    foreach ($name in @("team-explorer.toml", "team-worker.toml", "team-reviewer.toml")) {
        $paths += Join-Path $Root ".codex\agents\$name"
    }
    foreach ($name in @("team-explorer.md", "team-worker.md", "team-reviewer.md")) {
        $paths += Join-Path $Root ".codex\team-roles\$name"
    }
    return $paths
}

function Invoke-Rollback {
    if (-not $script:TransactionStarted -or $script:RollbackAttempted) {
        return
    }
    $script:RollbackAttempted = $true
    Write-Human "==> rollback"
    $rollbackStarted = Get-UtcTimestamp
    $stoppedProcesses = @()

    try {
        $stoppedProcesses = Stop-TrackedProcesses
    }
    catch {
        [void]$script:RollbackErrors.Add("processes: $($_.Exception.Message)")
    }

    try {
        Stop-NewClusterDaemon
    }
    catch {
        [void]$script:RollbackErrors.Add("cluster daemon: $($_.Exception.Message)")
    }

    if ($isTestMode) {
        try {
            foreach ($stateFile in @(
                (Join-Path $testStateRoot "scheduled-tasks.json"),
                (Join-Path $testStateRoot "startup-run.json")
            )) {
                if (-not $script:SnapshottedFiles.ContainsKey((Normalize-FullPath $stateFile))) {
                    Add-FileSnapshot $stateFile
                }
            }
        }
        catch {
            [void]$script:RollbackErrors.Add("test scheduler state snapshot: $($_.Exception.Message)")
        }
    }
    else {
        try {
            Restore-CodexScheduledTaskRecords $script:ScheduledTaskSnapshot
        }
        catch {
            [void]$script:RollbackErrors.Add("scheduled tasks: $($_.Exception.Message)")
        }
        try {
            Restore-CodexRunValueRecords $script:RunValueSnapshot
        }
        catch {
            [void]$script:RollbackErrors.Add("startup values: $($_.Exception.Message)")
        }
    }

    foreach ($name in @("CODEX_TEAM_HOME", "CODEX_TEAM_BOARD_ROOT", "CODEX_TEAM_DASHBOARD_URL")) {
        try {
            $value = if ($script:EnvironmentSnapshot.ContainsKey($name)) {
                $script:EnvironmentSnapshot[$name]
            }
            else {
                $null
            }
            Set-UserEnvironmentValue $name $value
        }
        catch {
            [void]$script:RollbackErrors.Add("environment ${name}: $($_.Exception.Message)")
        }
    }

    $directoryRecords = @($script:DirectorySnapshots)
    [array]::Reverse($directoryRecords)
    foreach ($record in $directoryRecords) {
        try {
            if ($record.recursive) {
                if (Test-Path -LiteralPath $record.path) {
                    Remove-Item -LiteralPath $record.path -Recurse -Force
                }
                if ($record.kind -eq "directory") {
                    Copy-Item -LiteralPath $record.backup -Destination $record.path -Recurse -Force
                }
            }
            elseif ($record.kind -eq "missing" -and
                (Test-Path -LiteralPath $record.path -PathType Container)) {
                $children = @(Get-ChildItem -LiteralPath $record.path -Force)
                if ($children.Count -eq 0) {
                    Remove-Item -LiteralPath $record.path -Force
                }
            }
        }
        catch {
            [void]$script:RollbackErrors.Add("directory $($record.path): $($_.Exception.Message)")
        }
    }

    $fileRecords = @($script:FileSnapshots)
    [array]::Reverse($fileRecords)
    foreach ($record in $fileRecords) {
        try {
            if ($record.kind -eq "file") {
                Copy-FileAtomically $record.backup $record.path
            }
            elseif (Test-Path -LiteralPath $record.path -PathType Leaf) {
                Remove-Item -LiteralPath $record.path -Force
            }
        }
        catch {
            [void]$script:RollbackErrors.Add("file $($record.path): $($_.Exception.Message)")
        }
    }

    $script:RollbackSucceeded = $script:RollbackErrors.Count -eq 0
    if (-not $script:RollbackSucceeded) {
        $script:KeepStageForRecovery = $true
    }
    $script:Result.rollback = [ordered]@{
        attempted = $true
        ok = $script:RollbackSucceeded
        started_at = $rollbackStarted
        finished_at = Get-UtcTimestamp
        stopped_processes = @($stoppedProcesses)
        errors = @($script:RollbackErrors)
    }
    if ($script:RollbackSucceeded) {
        Write-Human "Rollback restored the pre-install state."
    }
    else {
        Write-Human ("Rollback completed with errors: " + ($script:RollbackErrors -join "; "))
    }
}

function Write-InstallResult {
    $script:Result.finished_at = Get-UtcTimestamp
    $script:Result.artifacts.stage_root = if ($script:StageRoot -and
        (Test-Path -LiteralPath $script:StageRoot -PathType Container)) {
        $script:StageRoot
    }
    else {
        $null
    }
    $json = $script:Result | ConvertTo-Json -Depth 20
    if ($ResultPath) {
        $resultFullPath = Normalize-FullPath $ResultPath
        $parent = Split-Path -Parent $resultFullPath
        if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
        [IO.File]::WriteAllText(
            $resultFullPath,
            $json + "`n",
            [Text.UTF8Encoding]::new($false)
        )
    }
    Write-Output ("CODEX_TEAM_INSTALL_RESULT=" + ($script:Result | ConvertTo-Json -Depth 20 -Compress))
}

$exitCode = 0
try {
    Invoke-InstallStage "preflight" {
        if (-not (Test-Path -LiteralPath $projectRoot -PathType Container)) {
            throw "project root not found: $projectRoot"
        }
        try {
            $dashboardUri = [Uri]$DashboardUrl
        }
        catch {
            throw "DashboardUrl is invalid: $DashboardUrl"
        }
        if (-not $dashboardUri.IsAbsoluteUri -or
            $dashboardUri.Scheme -ne "http" -or
            -not $dashboardUri.IsLoopback) {
            throw "DashboardUrl must be an absolute loopback http URL"
        }
        if ($dashboardUri.UserInfo) {
            throw "DashboardUrl must not contain credentials"
        }
        $script:DashboardBaseUrl = $DashboardUrl.TrimEnd("/")

        $script:PythonExecutable = Resolve-Python
        $script:CodexExecutable = Resolve-Executable `
            ([Environment]::GetEnvironmentVariable("CODEX_TEAM_INSTALL_CODEX", "Process")) `
            "codex.exe" `
            "Codex CLI"
        $script:PowerShellExecutable = Resolve-PowerShell
        $script:ClusterTaskInstaller = if (
            [Environment]::GetEnvironmentVariable("CODEX_TEAM_INSTALL_CLUSTER_SCRIPT", "Process")
        ) {
            Normalize-FullPath (
                [Environment]::GetEnvironmentVariable(
                    "CODEX_TEAM_INSTALL_CLUSTER_SCRIPT",
                    "Process"
                )
            )
        }
        else {
            Join-Path $PSScriptRoot "install-cluster-task.ps1"
        }
        $script:DashboardScript = if (
            [Environment]::GetEnvironmentVariable("CODEX_TEAM_INSTALL_DASHBOARD_SCRIPT", "Process")
        ) {
            Normalize-FullPath (
                [Environment]::GetEnvironmentVariable(
                    "CODEX_TEAM_INSTALL_DASHBOARD_SCRIPT",
                    "Process"
                )
            )
        }
        else {
            Join-Path $PSScriptRoot "dashboard.ps1"
        }

        $rolePlan = @(Get-RolePlan)
        foreach ($item in $rolePlan) {
            if (-not (Test-Path -LiteralPath $item.source -PathType Leaf)) {
                throw "role template not found: $($item.source)"
            }
            [void](Get-Content -LiteralPath $item.source -Raw)
        }
        foreach ($scriptPath in @(
            $script:ClusterTaskInstaller,
            $script:DashboardScript,
            (Join-Path $PSScriptRoot "ensure-cluster-daemon.ps1"),
            (Join-Path $PSScriptRoot "cluster-watchdog.ps1"),
            (Join-Path $PSScriptRoot "team.ps1")
        )) {
            if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
                throw "installer dependency not found: $scriptPath"
            }
            $tokens = $null
            $parseErrors = $null
            [void][System.Management.Automation.Language.Parser]::ParseFile(
                $scriptPath,
                [ref]$tokens,
                [ref]$parseErrors
            )
            if ($parseErrors.Count -gt 0) {
                throw "PowerShell parse failed for $scriptPath`: $($parseErrors[0].Message)"
            }
        }

        $contextInstaller = $null
        if ($ContextMeterRoot) {
            $contextRootFull = Normalize-FullPath $ContextMeterRoot
            if (-not (Test-Path -LiteralPath $contextRootFull -PathType Container)) {
                throw "context meter root not found: $contextRootFull"
            }
            $contextInstaller = if (
                [Environment]::GetEnvironmentVariable(
                    "CODEX_TEAM_INSTALL_CONTEXT_INSTALLER",
                    "Process"
                )
            ) {
                Normalize-FullPath (
                    [Environment]::GetEnvironmentVariable(
                        "CODEX_TEAM_INSTALL_CONTEXT_INSTALLER",
                        "Process"
                    )
                )
            }
            else {
                Join-Path $contextRootFull "codex-context-meter-plugin\scripts\install.ps1"
            }
            if (-not (Test-Path -LiteralPath $contextInstaller -PathType Leaf)) {
                throw "context meter installer not found: $contextInstaller"
            }
            [void](Get-Content -LiteralPath $contextInstaller -Raw)
            $tokens = $null
            $parseErrors = $null
            [void][System.Management.Automation.Language.Parser]::ParseFile(
                $contextInstaller,
                [ref]$tokens,
                [ref]$parseErrors
            )
            if ($parseErrors.Count -gt 0) {
                throw "PowerShell parse failed for $contextInstaller`: $($parseErrors[0].Message)"
            }
        }

        $repoFull = $null
        if ($Repo) {
            $repoFull = Normalize-FullPath $Repo
            if (-not (Test-Path -LiteralPath $repoFull -PathType Container)) {
                throw "repository not found: $repoFull"
            }
            $gitCheck = Invoke-CommandWithEnvironment `
                -FilePath "git.exe" `
                -Arguments @("-C", $repoFull, "rev-parse", "--show-toplevel") `
                -Environment @{}
            if ($gitCheck.exit_code -ne 0 -or
                -not (Test-SamePath $gitCheck.output $repoFull)) {
                throw "Repo must be the root of a Git worktree: $repoFull"
            }
        }

        $probe = Invoke-PythonJson @'
import json
from codex_team import __version__
from codex_team.cluster import daemon_status
from codex_team.config import Settings
from codex_team.db import SCHEMA_VERSION
settings = Settings.load()
status = daemon_status(settings)
print(json.dumps({
    "version": __version__,
    "schema_version": SCHEMA_VERSION,
    "team_home": str(settings.home.resolve()),
    "database": str(settings.db_path.resolve()),
    "logs_dir": str(settings.logs_dir.resolve()),
    "daemon_pid": status.get("pid"),
    "daemon_running": bool(status.get("running")),
}, ensure_ascii=False))
'@
        $script:OriginalDaemonPid = $probe.daemon_pid
        $script:Preflight = [ordered]@{
            version = [string]$probe.version
            schema_version = [int]$probe.schema_version
            team_home = Normalize-FullPath ([string]$probe.team_home)
            database = Normalize-FullPath ([string]$probe.database)
            logs_dir = Normalize-FullPath ([string]$probe.logs_dir)
            python = $script:PythonExecutable
            codex = $script:CodexExecutable
            powershell = $script:PowerShellExecutable
            dashboard_url = $script:DashboardBaseUrl
            dashboard_port = Get-DashboardPort $dashboardUri
            codex_home = $codexHome
            context_installer = $contextInstaller
            repo = $repoFull
            existing_dashboard = $false
            existing_daemon_pid = $probe.daemon_pid
        }
        $script:Result.preflight = [ordered]@{
            version = $script:Preflight.version
            schema_version = $script:Preflight.schema_version
            team_home = $script:Preflight.team_home
            database = $script:Preflight.database
            python = $script:Preflight.python
            codex = $script:Preflight.codex
            dashboard_url = $script:Preflight.dashboard_url
            existing_dashboard = $false
            existing_daemon_pid = $probe.daemon_pid
        }

        $mcpList = Invoke-CodexMcp `
            -CodexHome $codexHome `
            -Arguments @("mcp", "list", "--json") `
            -AllowFailure
        if ($mcpList.exit_code -ne 0) {
            throw "Codex MCP preflight failed: $($mcpList.output)"
        }
        $script:Result.preflight.existing_mcp = $mcpList.output
        foreach ($commandName in @(
            "Get-ScheduledTask",
            "Export-ScheduledTask",
            "Register-ScheduledTask",
            "Unregister-ScheduledTask",
            "Get-CimInstance",
            "taskkill.exe"
        )) {
            if (-not (Get-Command $commandName -ErrorAction SilentlyContinue)) {
                throw "scheduled task command unavailable: $commandName"
            }
        }

        $existingHealth = Get-DashboardHealth 2
        if ($null -ne $existingHealth) {
            Assert-DashboardContract $existingHealth "preflight" | Out-Null
            $script:Preflight.existing_dashboard = $true
            $script:Result.preflight.existing_dashboard = $true
        }
        elseif (Test-TcpPortOpen $dashboardUri.DnsSafeHost $script:Preflight.dashboard_port) {
            throw "DashboardUrl port $($script:Preflight.dashboard_port) is occupied by an incompatible service"
        }
    }

    Invoke-InstallStage "staging" {
        $script:StageRoot = Join-Path $effectiveLocalAppData (
            "CodexTeam\install-staging-" + [guid]::NewGuid().ToString("N")
        )
        New-Item -ItemType Directory -Path $script:StageRoot -Force | Out-Null
        $stageCodexHome = Join-Path $script:StageRoot "codex-home"
        $stageRoles = Join-Path $script:StageRoot "roles"
        New-Item -ItemType Directory -Path $stageCodexHome -Force | Out-Null
        New-Item -ItemType Directory -Path $stageRoles -Force | Out-Null

        foreach ($name in @("config.toml", "AGENTS.md")) {
            $source = Join-Path $codexHome $name
            if (Test-Path -LiteralPath $source -PathType Leaf) {
                Copy-Item -LiteralPath $source -Destination (Join-Path $stageCodexHome $name) -Force
            }
        }
        $rolePlan = @(Get-RolePlan)
        foreach ($item in $rolePlan) {
            Copy-Item `
                -LiteralPath $item.source `
                -Destination (Join-Path $stageRoles (Split-Path -Leaf $item.target)) `
                -Force
        }

        $stagedConfig = Join-Path $stageCodexHome "config.toml"
        $configHasServer = (
            (Test-Path -LiteralPath $stagedConfig -PathType Leaf) -and
            [bool](Select-String `
                -LiteralPath $stagedConfig `
                -Pattern '^\[mcp_servers\.codexTeamBoard\]' `
                -Quiet)
        )
        if ($configHasServer) {
            [void](Invoke-CodexMcp `
                -CodexHome $stageCodexHome `
                -Arguments @("mcp", "remove", "codexTeamBoard"))
            Write-Human "Staged removal of the previous MCP registration."
        }
        $mcpServer = Join-Path $projectRoot "src\mcp_server.py"
        if (-not (Test-Path -LiteralPath $mcpServer -PathType Leaf)) {
            throw "MCP server entry point not found: $mcpServer"
        }
        [void](Invoke-CodexMcp `
            -CodexHome $stageCodexHome `
            -Arguments @(
                "mcp",
                "add",
                "codexTeamBoard",
                "--env",
                "PYTHONUTF8=1",
                "--env",
                "CODEX_TEAM_HOME=$($script:Preflight.team_home)",
                "--",
                $script:PythonExecutable,
                "-u",
                $mcpServer
            ))

        $bootstrapScript = Join-Path $script:StageRoot "bootstrap.py"
        $bootstrapSource = @'
import json
import sys
from pathlib import Path
from codex_team.bootstrap import install_global_integration
from codex_team.config import Settings
result = install_global_integration(Settings.load(), codex_home=Path(sys.argv[1]))
print(json.dumps(result, ensure_ascii=False))
'@
        [IO.File]::WriteAllText(
            $bootstrapScript,
            $bootstrapSource,
            [Text.UTF8Encoding]::new($false)
        )
        $bootstrap = Invoke-CommandWithEnvironment `
            -FilePath $script:PythonExecutable `
            -Arguments @($bootstrapScript, $stageCodexHome) `
            -Environment @{
                PYTHONPATH = Join-Path $projectRoot "src"
                PYTHONIOENCODING = "utf-8"
                PYTHONUTF8 = "1"
                CODEX_TEAM_HOME = $script:Preflight.team_home
            }
        if ($bootstrap.exit_code -ne 0) {
            throw "staged bootstrap failed: $($bootstrap.output)"
        }
        try {
            $bootstrapResult = $bootstrap.output | ConvertFrom-Json
        }
        catch {
            throw "staged bootstrap returned invalid JSON: $($bootstrap.output)"
        }
        $stagedMcp = Invoke-CodexMcp `
            -CodexHome $stageCodexHome `
            -Arguments @("mcp", "get", "codexTeamBoard", "--json")
        try {
            $stagedMcpConfig = ConvertFrom-MixedCommandJson $stagedMcp.output
        }
        catch {
            throw "staged MCP registration returned invalid JSON: $($stagedMcp.output)"
        }
        $serverCount = @(
            Select-String `
                -LiteralPath (Join-Path $stageCodexHome "config.toml") `
                -Pattern '^\[mcp_servers\.codexTeamBoard\]'
        ).Count
        if ($serverCount -ne 1) {
            throw "staged config contains $serverCount codexTeamBoard MCP registrations"
        }
        $script:StageCodexHome = $stageCodexHome
        $script:StageRoles = $stageRoles
        Invoke-TestFault "bootstrap"
    } "staged roles, AGENTS, Codex config, and one MCP registration"

    Invoke-InstallStage "apply" {
        $script:TransactionStarted = $true
        foreach ($name in @("CODEX_TEAM_HOME", "CODEX_TEAM_BOARD_ROOT", "CODEX_TEAM_DASHBOARD_URL")) {
            $script:EnvironmentSnapshot[$name] = Get-UserEnvironmentValue $name
        }
        Add-DirectorySnapshot (Join-Path $effectiveUserProfile ".codex\agents")
        Add-DirectorySnapshot (Join-Path $effectiveUserProfile ".codex\team-roles")
        Add-DirectorySnapshot $codexHome
        $rolePlan = @(Get-RolePlan)
        foreach ($item in $rolePlan) {
            Add-DirectorySnapshot (Split-Path -Parent $item.target)
            Add-FileSnapshot $item.target
        }
        Add-FileSnapshot (Join-Path $codexHome "config.toml")
        Add-FileSnapshot (Join-Path $codexHome "AGENTS.md")
        if ($ContextMeterRoot) {
            $contextState = Join-Path $effectiveLocalAppData "CodexContextMeter"
            Add-DirectorySnapshot $contextState -Recursive
        }
        if ($script:Preflight.repo) {
            foreach ($path in @(Get-ProjectInitPaths $script:Preflight.repo)) {
                Add-FileSnapshot $path
            }
        }
        if ($isTestMode) {
            Add-FileSnapshot (Join-Path $testStateRoot "scheduled-tasks.json")
            Add-FileSnapshot (Join-Path $testStateRoot "startup-run.json")
        }
        else {
            $script:ScheduledTaskSnapshot = @(Get-CodexScheduledTaskRecords)
            $script:RunValueSnapshot = @(Get-CodexRunValueRecords)
        }

        $oldPythonPath = $env:PYTHONPATH
        $oldPythonUtf8 = $env:PYTHONUTF8
        $oldPythonEncoding = $env:PYTHONIOENCODING
        try {
            $env:PYTHONPATH = Join-Path $projectRoot "src"
            $env:PYTHONUTF8 = "1"
            $env:PYTHONIOENCODING = "utf-8"
            $env:CODEX_TEAM_HOME = $script:Preflight.team_home
            Set-UserEnvironmentValue "CODEX_TEAM_HOME" $script:Preflight.team_home
            Set-UserEnvironmentValue "CODEX_TEAM_BOARD_ROOT" $projectRoot
            Set-UserEnvironmentValue "CODEX_TEAM_DASHBOARD_URL" $script:DashboardBaseUrl
            $env:CODEX_TEAM_BOARD_ROOT = $projectRoot
            $env:CODEX_TEAM_DASHBOARD_URL = $script:DashboardBaseUrl
        }
        finally {
            $env:PYTHONPATH = $oldPythonPath
            $env:PYTHONUTF8 = $oldPythonUtf8
            $env:PYTHONIOENCODING = $oldPythonEncoding
        }

        foreach ($item in $rolePlan) {
            $stagedRole = Join-Path $script:StageRoles (Split-Path -Leaf $item.target)
            Copy-FileAtomically $stagedRole $item.target
            [void]$script:InstalledRoles.Add($item.target)
        }

        Copy-FileAtomically `
            (Join-Path $script:StageCodexHome "config.toml") `
            (Join-Path $codexHome "config.toml")
        $script:McpSwitchCount++
        Copy-FileAtomically `
            (Join-Path $script:StageCodexHome "AGENTS.md") `
            (Join-Path $codexHome "AGENTS.md")

        $actualMcp = Invoke-CodexMcp `
            -CodexHome $codexHome `
            -Arguments @("mcp", "get", "codexTeamBoard", "--json")
        try {
            $actualMcpConfig = ConvertFrom-MixedCommandJson $actualMcp.output
        }
        catch {
            throw "installed MCP registration returned invalid JSON: $($actualMcp.output)"
        }
        if ($script:McpSwitchCount -ne 1) {
            throw "MCP registration switch count is $($script:McpSwitchCount), expected 1"
        }
    } "published roles, environment, and one atomic MCP/config switch"

    Invoke-InstallStage "project" {
        if ($script:Preflight.repo) {
            $init = Invoke-CommandWithEnvironment `
                -FilePath $script:PythonExecutable `
                -Arguments @(
                    "-m",
                    "codex_team.cli",
                    "init",
                    "--repo",
                    $script:Preflight.repo
                ) `
                -Environment @{
                    PYTHONPATH = Join-Path $projectRoot "src"
                    PYTHONIOENCODING = "utf-8"
                    PYTHONUTF8 = "1"
                    CODEX_TEAM_HOME = $script:Preflight.team_home
                }
            if ($init.exit_code -ne 0) {
                throw "project initialization failed: $($init.output)"
            }
            $script:Result.project_initialized = $true
        }
        else {
            $script:Result.project_initialized = $false
        }
    } "project initialization complete or skipped"

    Invoke-InstallStage "watchdog" {
        $clusterResult = Invoke-CommandWithEnvironment `
            -FilePath $script:PowerShellExecutable `
            -Arguments @(
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                $script:ClusterTaskInstaller
            ) `
            -Environment @{}
        if ($clusterResult.exit_code -ne 0) {
            throw "cluster task/watchdog installation failed: $($clusterResult.output)"
        }
        try {
            $registration = $clusterResult.output | ConvertFrom-Json
        }
        catch {
            throw "cluster task/watchdog installer returned invalid JSON: $($clusterResult.output)"
        }
        if (-not [bool](Get-ObjectProperty $registration "ok" $false)) {
            throw "cluster task/watchdog installer did not report success"
        }
        $script:Result.watchdog = $registration
        $processId = Get-ObjectProperty $registration "processId" 0
        if ($processId) {
            Add-TrackedProcess `
                ([int]$processId) `
                ([string](Get-ObjectProperty $registration "processCreationTime" "")) `
                "watchdog"
        }
        if (-not $isTestMode) {
            $deadline = [DateTime]::UtcNow.AddSeconds(12)
            $daemonReady = $false
            $lastStatus = $null
            while ([DateTime]::UtcNow -lt $deadline) {
                $lastStatus = Get-DaemonStatus $script:Preflight.team_home
                if ([bool]$lastStatus.running -and -not [bool]$lastStatus.config_mismatch) {
                    $daemonReady = $true
                    break
                }
                Start-Sleep -Milliseconds 500
            }
            if (-not $daemonReady) {
                throw "cluster daemon did not become healthy after watchdog installation"
            }
            $newPid = [int]$lastStatus.pid
            if ($newPid -and $newPid -ne [int]$script:OriginalDaemonPid) {
                $script:StartedDaemonPid = $newPid
            }
        }
        Invoke-TestFault "watchdog"
    } "scheduled task/watchdog registration verified"

    Invoke-InstallStage "dashboard" {
        if (-not $script:Preflight.existing_dashboard) {
            $started = Start-Process `
                -FilePath $script:PowerShellExecutable `
                -ArgumentList @(
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    ('"' + $script:DashboardScript.Replace('"', '\"') + '"'),
                    "-Port",
                    [string]$script:Preflight.dashboard_port,
                    "-NoBrowser"
                ) `
                -WindowStyle Hidden `
                -PassThru
            $script:DashboardStartedByInstaller = $true
            Add-TrackedProcess `
                $started.Id `
                (Get-TrackedProcessCreationTime $started) `
                "dashboard"
        }
        $timeoutValue = [Environment]::GetEnvironmentVariable(
            "CODEX_TEAM_INSTALL_HEALTH_TIMEOUT_SECONDS",
            "Process"
        )
        $timeout = if ($isTestMode -and $timeoutValue) {
            [int]$timeoutValue
        }
        else {
            20
        }
        $verified = Wait-DashboardContract `
            -Consecutive 2 `
            -TimeoutSeconds ([Math]::Max(1, $timeout)) `
            -DelayMilliseconds $(if ($isTestMode) { 80 } else { 350 })
        $script:Result.dashboard = [ordered]@{
            url = $script:DashboardBaseUrl
            started_by_installer = $script:DashboardStartedByInstaller
            process_pid = $(if ($script:DashboardStartedByInstaller) { $started.Id } else { $null })
            health_attempts = $verified.attempts
            consecutive_health_checks = $verified.consecutive
            version = [string]$verified.health.version
            schema_version = [int]$verified.health.schema_version
            runtime = [string]$verified.health.cluster_runtime.runtime
        }
    } "dashboard health passed twice"

    Invoke-InstallStage "context" {
        if ($script:Preflight.context_installer) {
            $contextProcessesBefore = @{}
            foreach ($process in @(Get-ProcessesMatchingCommandLine $ContextMeterRoot)) {
                $contextProcessesBefore[$process.pid] = $process.creation_time
            }
            try {
                $contextResult = Invoke-CommandWithEnvironment `
                    -FilePath $script:PowerShellExecutable `
                    -Arguments @(
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        $script:Preflight.context_installer,
                        "-TeamBoardRoot",
                        $projectRoot,
                        "-DashboardUrl",
                        $script:DashboardBaseUrl
                    ) `
                    -Environment @{}
            }
            finally {
                foreach ($process in @(Get-ProcessesMatchingCommandLine $ContextMeterRoot)) {
                    if (-not $contextProcessesBefore.ContainsKey($process.pid)) {
                        Add-TrackedProcess `
                            $process.pid `
                            $process.creation_time `
                            "context-meter"
                    }
                }
            }
            if ($contextResult.exit_code -ne 0) {
                throw "context meter installation failed: $($contextResult.output)"
            }
            $script:Result.context_meter = [ordered]@{
                root = Normalize-FullPath $ContextMeterRoot
                installer = $script:Preflight.context_installer
                output = $contextResult.output
            }
        }
        else {
            $script:Result.context_meter = [ordered]@{
                installed = $false
            }
        }
        Invoke-TestFault "context"
    } "context meter installation complete or skipped"

    Invoke-InstallStage "verify" {
        $finalMcp = Invoke-CodexMcp `
            -CodexHome $codexHome `
            -Arguments @("mcp", "get", "codexTeamBoard", "--json")
        try {
            $finalMcpConfig = ConvertFrom-MixedCommandJson $finalMcp.output
        }
        catch {
            throw "final MCP verification returned invalid JSON: $($finalMcp.output)"
        }
        $serverCount = @(
            Select-String `
                -LiteralPath (Join-Path $codexHome "config.toml") `
                -Pattern '^\[mcp_servers\.codexTeamBoard\]'
        ).Count
        if ($serverCount -ne 1) {
            throw "final config contains $serverCount codexTeamBoard MCP registrations"
        }
        $finalHealth = Wait-DashboardContract `
            -Consecutive 2 `
            -TimeoutSeconds $(if ($isTestMode) { 3 } else { 15 }) `
            -DelayMilliseconds $(if ($isTestMode) { 80 } else { 350 })
        $script:Result.verification = [ordered]@{
            mcp_registration_count = $serverCount
            mcp_switch_count = $script:McpSwitchCount
            dashboard_health_attempts = $finalHealth.attempts
            dashboard_version = [string]$finalHealth.health.version
            dashboard_schema_version = [int]$finalHealth.health.schema_version
            dashboard_runtime = [string]$finalHealth.health.cluster_runtime.runtime
        }
    } "final MCP and dashboard contracts verified"

    $script:Result.ok = $true
    $script:Result.status = "succeeded"
    $script:Result.installed = [ordered]@{
        version = $script:Preflight.version
        schema_version = $script:Preflight.schema_version
        team_home = $script:Preflight.team_home
        database = $script:Preflight.database
        codex_home = $codexHome
        dashboard_url = $script:DashboardBaseUrl
        roles = @($script:InstalledRoles)
        mcp_registration = "codexTeamBoard"
        mcp_switches = $script:McpSwitchCount
        started_processes = @(
            $script:TrackedProcesses | ForEach-Object {
                [ordered]@{
                    pid = $_.pid
                    source = $_.source
                }
            }
        )
        requires_new_codex_session = $true
    }
    Write-Human ""
    Write-Human "Installation complete."
    Write-Human "Team board root: $projectRoot"
    Write-Human "Team home: $($script:Preflight.team_home)"
    Write-Human "Team board URL: $script:DashboardBaseUrl"
    Write-Human "Health: version=$($script:Preflight.version) schema=$($script:Preflight.schema_version)"
}
catch {
    $errorMessage = $_.Exception.Message
    if ($null -ne $script:CurrentStage) {
        Fail-InstallStage $_.Exception
    }
    if ($script:TransactionStarted) {
        try {
            Invoke-Rollback
        }
        catch {
            [void]$script:RollbackErrors.Add("rollback coordinator: $($_.Exception.Message)")
            $script:RollbackSucceeded = $false
            $script:KeepStageForRecovery = $true
        }
    }
    $script:Result.ok = $false
    $script:Result.status = "failed"
    $script:Result.failure_stage = $script:FailureStage
    $script:Result.error = $errorMessage
    if ($script:TransactionStarted -and $null -eq $script:Result.rollback) {
        $script:Result.rollback = [ordered]@{
            attempted = $script:RollbackAttempted
            ok = $false
            errors = @($script:RollbackErrors)
        }
    }
    $exitCode = 1
}
finally {
    if ($script:StageRoot -and
        (Test-Path -LiteralPath $script:StageRoot -PathType Container) -and
        -not $script:KeepStageForRecovery) {
        try {
            Remove-Item -LiteralPath $script:StageRoot -Recurse -Force
        }
        catch {
            $script:Result.cleanup_warning = (
                "staging cleanup failed: $($_.Exception.Message)"
            )
            Write-Human $script:Result.cleanup_warning
        }
    }
    Write-InstallResult
}

if ($exitCode -ne 0) {
    exit $exitCode
}
