#Requires -Version 7.0
<#
.SYNOPSIS
Start a release normally and attach a hidden, idle process-exit recorder.
.DESCRIPTION
The monitor waits on the exact process handle, without polling or restarting it.
If both are killed together, only the app's unfinished state survives.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string] $ExePath,
    [Parameter(Mandatory)][string] $LogDirectory,
    [Parameter(Mandatory)][string] $RuntimeDirectory,
    [string[]] $AppArguments = @(),
    [switch] $Monitor,
    [string] $LaunchId,
    [string] $ReceiptPath,
    [int] $TargetProcessId,
    [long] $TargetStartTicks
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $IsWindows) { throw 'This launcher requires Windows.' }
foreach ($value in @($ExePath, $LogDirectory, $RuntimeDirectory)) {
    if (-not [IO.Path]::IsPathFullyQualified($value)) { throw 'All paths must be absolute.' }
}
$exe = Get-Item -LiteralPath $ExePath
if ($exe.PSIsContainer -or $exe.Name -notmatch '^PixivTracker.*\.exe$') { throw 'Expected an explicit PixivTracker EXE.' }
$ExePath = $exe.FullName
$LogDirectory = [IO.Path]::GetFullPath($LogDirectory)
$RuntimeDirectory = [IO.Path]::GetFullPath($RuntimeDirectory)
$null = [IO.Directory]::CreateDirectory($RuntimeDirectory)
$null = [IO.Directory]::CreateDirectory($LogDirectory)
if (-not $Monitor) {
    if (@(Get-CimInstance Win32_Process -Filter "Name LIKE 'PixivTracker%'" -ErrorAction Stop).Count -gt 0) {
        throw 'PixivTracker is already running. Confirm its normal exit before starting a release.'
    }
    $LaunchId = [guid]::NewGuid().ToString()
    $ReceiptPath = Join-Path $RuntimeDirectory ("launch-" + $LaunchId + '.json')
}
$parsedId = [guid]::Empty
if (-not [guid]::TryParse($LaunchId, [ref]$parsedId)) { throw 'Invalid launch identifier.' }
$expectedReceipt = Join-Path $RuntimeDirectory ("launch-" + $LaunchId + '.json')
if (-not [StringComparer]::OrdinalIgnoreCase.Equals($ReceiptPath, $expectedReceipt)) { throw 'Invalid receipt path.' }
$recordPath = Join-Path $LogDirectory 'process-exits.jsonl'
$script:recordingAvailable = $true
function Write-ExitEvent([string] $event, [hashtable] $fields) {
    $row = [ordered]@{timestamp=[DateTimeOffset]::Now.ToString('o'); event=$event; launch_id=$LaunchId; recorder_pid=$PID; exe_name=$exe.Name}
    foreach ($key in $fields.Keys) { $row[$key] = $fields[$key] }
    try { [IO.File]::AppendAllText($recordPath, (($row | ConvertTo-Json -Compress -Depth 3) + [Environment]::NewLine), [Text.UTF8Encoding]::new($false)) }
    catch { $script:recordingAvailable = $false; [Console]::Error.WriteLine('PixivTracker process-exit record could not be written.') }
}
function Write-ProcessExit([Diagnostics.Process] $process) {
    $process.WaitForExit()
    $code = $process.ExitCode
    $unsigned = [BitConverter]::ToUInt32([BitConverter]::GetBytes([int]$code), 0)
    Write-ExitEvent 'process_exited' @{pid=$process.Id; exit_code=$code; exit_code_hex=('0x{0:X8}' -f $unsigned)}
}
function Write-Receipt([hashtable] $fields) {
    $temporary = $ReceiptPath + '.pending'
    [IO.File]::WriteAllText($temporary, ($fields | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
    [IO.File]::Move($temporary, $ReceiptPath)
}

if ($Monitor) {
    $process = $null
    $attached = $false
    try {
        $process = [Diagnostics.Process]::GetProcessById($TargetProcessId)
        $null = $process.Handle
        if ($process.StartTime.ToUniversalTime().Ticks -ne $TargetStartTicks) { throw 'Process identity changed.' }
        $attached = $true
        Write-ExitEvent 'observer_attached' @{pid=$process.Id}
        Write-Receipt @{status='observing'; monitor_pid=$PID; recording_available=$script:recordingAvailable}
    } catch {
        if (-not $attached -and $null -ne $process) { $process.Dispose(); $process = $null }
        Write-ExitEvent 'monitor_error' @{error_type=$_.Exception.GetType().Name}
        if (-not (Test-Path -LiteralPath $ReceiptPath)) {
            try { Write-Receipt @{status='failed'; error_type=$_.Exception.GetType().Name; monitor_pid=$PID} }
            catch { Write-ExitEvent 'receipt_error' @{error_type=$_.Exception.GetType().Name} }
        }
    } finally {
        if ($null -ne $process) {
            try { Write-ProcessExit $process } finally { $process.Dispose() }
        }
    }
    return
}

# Start in the caller's desktop PowerShell context. The observer never launches
# the application, so its own context cannot change tray-window permissions.
function Quote-NativeArgument([string] $value) {
    if ($value -ne '' -and $value -notmatch '[\s"]') { return $value }
    return '"' + (($value -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
}
function Quote-Literal([string] $value) { "'" + $value.Replace("'", "''") + "'" }
$savedTemp = $env:TEMP
$savedTmp = $env:TMP
$savedLaunch = $env:PIXIV_LAUNCH_ID
$process = $null
$monitorProcess = $null
try {
    Write-ExitEvent 'launch_requested' @{}
    $env:TEMP = $RuntimeDirectory
    $env:TMP = $RuntimeDirectory
    $env:PIXIV_LAUNCH_ID = $LaunchId
    $startOptions = @{FilePath=$ExePath; WorkingDirectory=$exe.DirectoryName; WindowStyle='Hidden'; PassThru=$true}
    if ($AppArguments.Count -gt 0) { $startOptions.ArgumentList = @($AppArguments | ForEach-Object { Quote-NativeArgument $_ }) }
    $process = Start-Process @startOptions
    $null = $process.Handle
    Write-ExitEvent 'process_started' @{pid=$process.Id}
    if ($process.HasExited) {
        Write-ProcessExit $process
        return @{status='exited'; launcher_pid=$process.Id; exit_code=$process.ExitCode; launch_id=$LaunchId; recording_available=$script:recordingAvailable}
    }
    $startTicks = $process.StartTime.ToUniversalTime().Ticks
    $invocation = '& ' + (Quote-Literal $PSCommandPath) + ' -Monitor -ExePath ' + (Quote-Literal $ExePath) +
        ' -LogDirectory ' + (Quote-Literal $LogDirectory) + ' -RuntimeDirectory ' + (Quote-Literal $RuntimeDirectory) +
        ' -LaunchId ' + (Quote-Literal $LaunchId) + ' -ReceiptPath ' + (Quote-Literal $ReceiptPath) +
        ' -TargetProcessId ' + $process.Id + ' -TargetStartTicks ' + $startTicks
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($invocation))
    $monitorProcess = Start-Process -FilePath (Join-Path $PSHOME 'pwsh.exe') -ArgumentList @('-NoProfile', '-NonInteractive', '-EncodedCommand', $encoded) -WindowStyle Hidden -PassThru
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-Path -LiteralPath $ReceiptPath) {
            $receipt = Get-Content -LiteralPath $ReceiptPath -Raw -Encoding utf8 | ConvertFrom-Json
            if ($receipt.status -eq 'observing') {
                return @{status='started'; launcher_pid=$process.Id; monitor_pid=$monitorProcess.Id; launch_id=$LaunchId; recording_available=($receipt.recording_available -and $script:recordingAvailable)}
            }
            break
        }
        if ($monitorProcess.HasExited) { break }
        Start-Sleep -Milliseconds 200
    }
    # A very fast failure may precede observer attachment; this original process
    # handle still supplies the actual exit code without guessing from a PID.
    if ($process.HasExited) {
        Write-ProcessExit $process
        return @{status='exited'; launcher_pid=$process.Id; exit_code=$process.ExitCode; launch_id=$LaunchId; recording_available=$script:recordingAvailable}
    }
    Write-Warning 'Release started, but process-exit observer did not confirm attachment. Inspect before retrying.'
    return @{status='started'; launcher_pid=$process.Id; monitor_pid=$monitorProcess.Id; launch_id=$LaunchId; recording_available=$false}
} catch {
    Write-ExitEvent 'launcher_error' @{error_type=$_.Exception.GetType().Name}
    throw
} finally {
    $env:TEMP = $savedTemp
    $env:TMP = $savedTmp
    $env:PIXIV_LAUNCH_ID = $savedLaunch
    if ($null -ne $monitorProcess) { $monitorProcess.Dispose() }
    if ($null -ne $process) { $process.Dispose() }
}
