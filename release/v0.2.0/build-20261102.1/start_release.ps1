#Requires -Version 7.0
<#
.SYNOPSIS
Start a release with a hidden, idle process-exit recorder.
.DESCRIPTION
The monitor waits on the exact process handle, without polling or restarting it.
It records the OS exit code even when the app cannot write its own final event.
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
    [string] $ReceiptPath
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
    $running = @(Get-CimInstance Win32_Process -Filter "Name LIKE 'PixivTracker%'" -ErrorAction Stop)
    if ($running.Count -gt 0) { throw 'PixivTracker is already running. Confirm its normal exit before starting a release.' }
    $LaunchId = [guid]::NewGuid().ToString()
    $ReceiptPath = Join-Path $RuntimeDirectory ("launch-" + $LaunchId + '.json')
    # Encode a literal PowerShell invocation, not a shell command assembled from
    # JSON. Each argument is single-quoted with embedded quotes escaped.
    function Quote-Literal([string] $value) { "'" + $value.Replace("'", "''") + "'" }
    $invocation = '& ' + (Quote-Literal $PSCommandPath) + ' -Monitor -ExePath ' + (Quote-Literal $ExePath) +
        ' -LogDirectory ' + (Quote-Literal $LogDirectory) + ' -RuntimeDirectory ' + (Quote-Literal $RuntimeDirectory) +
        ' -LaunchId ' + (Quote-Literal $LaunchId) + ' -ReceiptPath ' + (Quote-Literal $ReceiptPath)
    if ($AppArguments.Count -gt 0) {
        $invocation += ' -AppArguments @(' + (($AppArguments | ForEach-Object { Quote-Literal $_ }) -join ',') + ')'
    }
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($invocation))
    $monitorProcess = Start-Process -FilePath (Join-Path $PSHOME 'pwsh.exe') -ArgumentList @('-NoProfile', '-NonInteractive', '-EncodedCommand', $encoded) -WindowStyle Hidden -PassThru
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    try {
        while ([DateTime]::UtcNow -lt $deadline) {
            if (Test-Path -LiteralPath $ReceiptPath) {
                # The monitor publishes this one receipt atomically.
                $receipt = Get-Content -LiteralPath $ReceiptPath -Raw -Encoding utf8 | ConvertFrom-Json
                if ($receipt.status -ne 'started') { throw "Launcher failed: $($receipt.error_type)." }
                if (-not $receipt.recording_available) { Write-Warning 'Release started, but process-exit recording is unavailable.' }
                return $receipt
            }
            if ($monitorProcess.HasExited) { throw "Exit recorder stopped before startup (code $($monitorProcess.ExitCode))." }
            Start-Sleep -Milliseconds 200
        }
        throw 'Startup receipt is pending; inspect processes before retrying. No process was killed.'
    } finally {
        $monitorProcess.Dispose()
        # Keep the small receipt as startup evidence; it lives in the registered
        # runtime directory and is cleaned with that directory after app exit.
    }
}

$parsedId = [guid]::Empty
if (-not [guid]::TryParse($LaunchId, [ref]$parsedId)) { throw 'Invalid launch identifier.' }
$expectedReceipt = Join-Path $RuntimeDirectory ("launch-" + $LaunchId + '.json')
if (-not [StringComparer]::OrdinalIgnoreCase.Equals($ReceiptPath, $expectedReceipt)) { throw 'Invalid receipt path.' }
$recordPath = Join-Path $LogDirectory 'process-exits.jsonl'
$script:recordingAvailable = $true
function Write-ExitEvent([string] $event, [hashtable] $fields) {
    $row = [ordered]@{timestamp=[DateTimeOffset]::Now.ToString('o'); event=$event; launch_id=$LaunchId; monitor_pid=$PID; exe_name=$exe.Name}
    foreach ($key in $fields.Keys) { $row[$key] = $fields[$key] }
    try { [IO.File]::AppendAllText($recordPath, (($row | ConvertTo-Json -Compress -Depth 3) + [Environment]::NewLine), [Text.UTF8Encoding]::new($false)) }
    catch { $script:recordingAvailable = $false; [Console]::Error.WriteLine('PixivTracker process-exit record could not be written.') }
}
function Write-Receipt([hashtable] $fields) {
    $temporary = $ReceiptPath + '.pending'
    [IO.File]::WriteAllText($temporary, ($fields | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
    [IO.File]::Move($temporary, $ReceiptPath)
}

$process = $null
try {
    if (@(Get-CimInstance Win32_Process -Filter "Name LIKE 'PixivTracker%'" -ErrorAction Stop).Count -gt 0) {
        throw 'An existing instance appeared before launch.'
    }
    Write-ExitEvent 'launch_requested' @{}
    $env:TEMP = $RuntimeDirectory
    $env:TMP = $RuntimeDirectory
    $env:PIXIV_LAUNCH_ID = $LaunchId
    # ArgumentList preserves individual arguments; no shell expansion occurs.
    $info = [Diagnostics.ProcessStartInfo]::new($ExePath)
    $info.WorkingDirectory = $exe.DirectoryName
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    foreach ($argument in $AppArguments) { $info.ArgumentList.Add($argument) }
    $process = [Diagnostics.Process]::Start($info)
    Write-ExitEvent 'process_started' @{pid=$process.Id}
    Write-Receipt @{status='started'; launcher_pid=$process.Id; monitor_pid=$PID; launch_id=$LaunchId; recording_available=$script:recordingAvailable}
} catch {
    Write-ExitEvent 'monitor_error' @{error_type=$_.Exception.GetType().Name}
    if (-not (Test-Path -LiteralPath $ReceiptPath)) {
        Write-Receipt @{status='failed'; error_type=$_.Exception.GetType().Name; monitor_pid=$PID}
    }
    throw
} finally {
    if ($null -ne $process) {
        try {
            # Once a process exists, receipt/log failures must not abandon its
            # exit observation. Waiting does not terminate or restart it.
            $process.WaitForExit()
            $code = $process.ExitCode
            $unsigned = [BitConverter]::ToUInt32([BitConverter]::GetBytes([int]$code), 0)
            Write-ExitEvent 'process_exited' @{pid=$process.Id; exit_code=$code; exit_code_hex=('0x{0:X8}' -f $unsigned)}
        } finally { $process.Dispose() }
    }
}
