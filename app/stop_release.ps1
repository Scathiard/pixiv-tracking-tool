#Requires -Version 7.0
<#
.SYNOPSIS
Request normal shutdown of one explicitly identified PixivTracker release.
.DESCRIPTION
Run directly in the desktop user's PowerShell host, not through a sandboxed
Python helper. Windows UIPI rejects messages from a lower integrity process.
This script never elevates privileges or terminates a process forcibly.
The returned status is stopped/already_stopped, or inspected with -InspectOnly.
Errors are terminating: do not start a replacement after an error.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string] $ExePath,
    [ValidateRange(1, 600)]
    [int] $TimeoutSeconds = 60,
    [switch] $InspectOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $IsWindows) { throw 'This helper requires Windows.' }
if (-not [IO.Path]::IsPathFullyQualified($ExePath)) { throw 'ExePath must be an absolute path.' }
$exe = Get-Item -LiteralPath $ExePath
if ($exe.PSIsContainer -or $exe.Name -notmatch '^PixivTracker.*\.exe$') {
    throw 'ExePath must identify a PixivTracker executable.'
}
$expectedPath = $exe.FullName
$callerSession = [Diagnostics.Process]::GetCurrentProcess().SessionId
$clock = [Diagnostics.Stopwatch]::StartNew()

if (-not ('PixivTracker.ReleaseControl' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

namespace PixivTracker {
    public sealed class TrayWindow {
        public IntPtr Handle;
        public uint ProcessId;
        public string ClassName;
    }

    public static class ReleaseControl {
        private delegate bool EnumWindowsProc(IntPtr window, IntPtr data);
        [DllImport("user32.dll", SetLastError = true)]
        private static extern bool EnumWindows(EnumWindowsProc callback, IntPtr data);
        [DllImport("user32.dll")]
        private static extern uint GetWindowThreadProcessId(IntPtr window, out uint pid);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern int GetClassNameW(IntPtr window, StringBuilder name, int size);
        [DllImport("user32.dll", SetLastError = true)]
        private static extern bool PostMessageW(IntPtr window, uint message, IntPtr wp, IntPtr lp);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool QueryFullProcessImageNameW(IntPtr process, uint flags, StringBuilder path, ref uint size);

        public static string ImagePath(IntPtr process) {
            var path = new StringBuilder(32768);
            uint size = (uint)path.Capacity;
            if (!QueryFullProcessImageNameW(process, 0, path, ref size))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            return path.ToString();
        }

        private static string ClassName(IntPtr window) {
            var name = new StringBuilder(256);
            GetClassNameW(window, name, name.Capacity);
            return name.ToString();
        }

        private static bool IsTray(string name) {
            return name.StartsWith("pixiv_tracker", StringComparison.Ordinal)
                && name.EndsWith("SystemTrayIcon", StringComparison.Ordinal);
        }

        public static TrayWindow[] FindWindows(int[] processIds) {
            var ids = new HashSet<int>(processIds);
            var found = new List<TrayWindow>();
            EnumWindowsProc callback = (window, data) => {
                uint pid;
                GetWindowThreadProcessId(window, out pid);
                if (ids.Contains((int)pid)) {
                    var name = ClassName(window);
                    if (IsTray(name)) found.Add(new TrayWindow { Handle = window, ProcessId = pid, ClassName = name });
                }
                return true;
            };
            if (!EnumWindows(callback, IntPtr.Zero))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            return found.ToArray();
        }

        public static bool RequestStop(TrayWindow target) {
            uint pid;
            GetWindowThreadProcessId(target.Handle, out pid);
            if (pid == 0) return false; // Already destroyed during normal exit.
            if (pid != target.ProcessId || !IsTray(ClassName(target.Handle)))
                throw new InvalidOperationException("Tray window changed; no message sent.");
            // pystray WM_STOP (WM_USER + 10). run_tray() then requests service
            // shutdown and joins its worker; this is NOT TerminateProcess.
            if (!PostMessageW(target.Handle, 0x040A, IntPtr.Zero, IntPtr.Zero)) {
                int error = Marshal.GetLastWin32Error();
                if (error == 1400) return false; // Window closed after identity check.
                throw new Win32Exception(error,
                    "Normal tray shutdown was rejected. Use the same desktop user's PowerShell at the same integrity level; do not relay through Python or force-kill.");
            }
            return true;
        }
    }
}
'@
}

$processes = [Collections.Generic.List[Diagnostics.Process]]::new()
try {
    # CIM failures must surface. An inaccessible process is not an exited one.
    $candidates = @(Get-CimInstance Win32_Process -Filter "Name LIKE 'PixivTracker%'" -ErrorAction Stop)
    foreach ($candidate in $candidates) {
        if (-not $candidate.ExecutablePath -and $candidate.Name -ieq $exe.Name) {
            throw "Cannot inspect PID $($candidate.ProcessId); shutdown cannot be confirmed."
        }
        if (-not [StringComparer]::OrdinalIgnoreCase.Equals($candidate.ExecutablePath, $expectedPath)) { continue }
        if ($candidate.SessionId -ne $callerSession) {
            throw "PID $($candidate.ProcessId) belongs to another desktop session; no message sent."
        }
        $process = [Diagnostics.Process]::GetProcessById($candidate.ProcessId)
        $processes.Add($process)
        # Retain a handle to the original process, not just a reusable PID.
        $handle = $process.SafeHandle.DangerousGetHandle()
        if ($process.HasExited) { continue }
        if (-not [StringComparer]::OrdinalIgnoreCase.Equals(
            [PixivTracker.ReleaseControl]::ImagePath($handle), $expectedPath)) {
            throw "PID $($process.Id) changed identity; no message sent."
        }
    }
    $processIds = @($processes | ForEach-Object { $_.Id })
    $listenersBefore = @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Where-Object { $_.OwningProcess -in $processIds } |
        Select-Object LocalAddress, LocalPort, OwningProcess)
    $windows = @([PixivTracker.ReleaseControl]::FindWindows([int[]]$processIds))
    $result = [ordered]@{
        status = 'inspected'
        exe = $expectedPath
        caller_pid = $PID
        session_id = $callerSession
        process_ids = $processIds
        tray_process_ids = @($windows | ForEach-Object { $_.ProcessId } | Sort-Object -Unique)
        listeners_before = $listenersBefore
        messages_sent = 0
        forced = $false
    }
    if ($InspectOnly) { return [pscustomobject]$result }
    if ($processIds.Count -eq 0) {
        $result.status = 'already_stopped'
        $result.elapsed_seconds = [Math]::Round($clock.Elapsed.TotalSeconds, 3)
        return [pscustomobject]$result
    }
    if ($windows.Count -eq 0 -and @($processes | Where-Object { -not $_.HasExited }).Count -gt 0) {
        throw 'Target process exists but its tray window is unavailable. Shutdown is NOT confirmed; do not start a replacement.'
    }
    # pystray's main and menu windows share a class, but only the main window
    # dispatches WM_STOP. Enumeration order cannot identify it. Address every
    # verified tray window; a window already destroyed by normal exit is safe
    # to skip. Permission/identity errors still fail, and completion still
    # requires the captured processes and listeners to disappear.
    foreach ($window in $windows) {
        if ([PixivTracker.ReleaseControl]::RequestStop($window)) { $result.messages_sent++ }
    }
    if ($result.messages_sent -eq 0 -and @($processes | Where-Object { -not $_.HasExited }).Count -gt 0) {
        throw 'No normal stop message was delivered; shutdown is NOT confirmed.'
    }
    Write-Verbose 'Normal stop requested; waiting for service cleanup and process exit.'
    $wait = [Diagnostics.Stopwatch]::StartNew()
    do {
        $remaining = @($processes | Where-Object { -not $_.HasExited })
        if ($remaining.Count -eq 0) { break }
        if ($wait.Elapsed.TotalSeconds -ge $TimeoutSeconds) {
            throw "Shutdown is still pending for PID(s) $($remaining.Id -join ', ') after $TimeoutSeconds seconds. No process was killed; do not start a replacement."
        }
        Start-Sleep -Milliseconds 200
    } while ($true)

    $remainingListeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Where-Object { $_.OwningProcess -in $processIds })
    if ($remainingListeners.Count -gt 0) { throw 'A captured PID still owns a listener; shutdown cannot be confirmed.' }
    $result.status = 'stopped'
    $result.exit_codes = @($processes | ForEach-Object { [pscustomobject]@{ pid = $_.Id; code = $_.ExitCode } })
    $result.listeners_released = $true
    $result.elapsed_seconds = [Math]::Round($clock.Elapsed.TotalSeconds, 3)
    [pscustomobject]$result
} finally {
    foreach ($process in $processes) { $process.Dispose() }
}
