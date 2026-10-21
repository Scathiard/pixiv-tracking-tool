param([Parameter(Mandatory=$true)][string]$SourceExe,
      [Parameter(Mandatory=$true)][string]$ExpectedHash,
      [Parameter(Mandatory=$true)][string]$Evidence)
$ErrorActionPreference='Stop'
$source=(Resolve-Path -LiteralPath $SourceExe).Path
if ((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash -ne $ExpectedHash) { throw 'Candidate hash mismatch' }
$target=Join-Path 'D:\PixivTracker' ([IO.Path]::GetFileName($source))
if (Test-Path -LiteralPath $target) { throw 'New runtime file already exists; refusing overwrite' }
$old=@(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like 'D:\PixivTracker\PixivTracker*.exe' })
Add-Type @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public static class PixivTrayExit {
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc f, IntPtr p);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint id);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr h, StringBuilder b, int n);
  [DllImport("user32.dll")] static extern bool PostMessage(IntPtr h, uint msg, IntPtr w, IntPtr l);
  public static int Stop(uint id) {
    int count=0;
    EnumWindows((h,p)=> {uint owner; GetWindowThreadProcessId(h,out owner);
      if(owner==id) {var name=new StringBuilder(256); GetClassName(h,name,256);
        if(name.ToString().StartsWith("pixiv_tracker") && name.ToString().EndsWith("SystemTrayIcon")) {
          if(!PostMessage(h,0x40a,IntPtr.Zero,IntPtr.Zero)) throw new Exception("Tray exit rejected"); count++;
        }} return true; },IntPtr.Zero); return count;
  }
}
'@
$started=Get-Date
$posted=0
foreach ($item in $old) { $posted += [PixivTrayExit]::Stop([uint32]$item.ProcessId) }
if ($old.Count -gt 0 -and $posted -eq 0) { throw 'No verified PixivTracker tray window found' }
$deadline=(Get-Date).AddSeconds(180)
while(@($old | Where-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }).Count -gt 0) {
  if((Get-Date) -gt $deadline) { throw 'Old instance did not exit; no force shutdown performed' }
  Start-Sleep -Milliseconds 500
}
$exitSeconds=((Get-Date)-$started).TotalSeconds
if (@(Get-NetTCPConnection -LocalPort 1919 -State Listen -ErrorAction SilentlyContinue).Count) { throw 'Port 1919 still owned; not starting another writer' }
Copy-Item -LiteralPath $source -Destination $target
if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $ExpectedHash) { throw 'Copied runtime hash mismatch' }
$runTemp=Join-Path $PSScriptRoot 'enabled-runtime-20261021.4'
New-Item -ItemType Directory -Path $runTemp | Out-Null
$savedTemp=$env:TEMP; $savedTmp=$env:TMP; $savedRoot=$env:DATA_ROOT
try {
  $env:TEMP=$runTemp; $env:TMP=$runTemp
  Remove-Item Env:DATA_ROOT -ErrorAction SilentlyContinue
  $process=Start-Process -FilePath $target -WorkingDirectory 'D:\PixivTracker' -WindowStyle Hidden -PassThru
} finally {
  $env:TEMP=$savedTemp; $env:TMP=$savedTmp; $env:DATA_ROOT=$savedRoot
}
$record=[ordered]@{build='20261021.4'; public_version='0.2.0'; launched_at=(Get-Date).ToString('o');
  executable=$target; sha256=$ExpectedHash; launcher_pid=$process.Id; normal_mode=$true;
  old_pids=@($old.ProcessId); tray_windows_signaled=$posted; old_exit_seconds=$exitSeconds;
  forced_shutdown=$false; runtime_temp=$runTemp; ready_verified=$false}
$json=$record | ConvertTo-Json -Depth 5
[IO.File]::WriteAllText($Evidence,$json,[Text.UTF8Encoding]::new($false))
$json
