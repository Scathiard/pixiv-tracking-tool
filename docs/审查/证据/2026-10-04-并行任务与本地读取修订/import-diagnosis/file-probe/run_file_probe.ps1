$ErrorActionPreference = 'Stop'
$diagnosisDirectory = $PSScriptRoot
$interpreterPath = 'D:\claude_sandbox\pixiv-tracking-tool\.devtools\v012-env\Scripts\python.exe'
$probePath = Join-Path $diagnosisDirectory 'file_probe.py'
$stdoutPath = Join-Path $diagnosisDirectory 'probe.stdout.log'
$stderrPath = Join-Path $diagnosisDirectory 'probe.stderr.log'
$resultPath = Join-Path $diagnosisDirectory 'result.json'
$startPath = Join-Path $diagnosisDirectory 'started.json'
foreach ($evidencePath in @($stdoutPath, $stderrPath, $resultPath, $startPath)) {
    if (Test-Path -LiteralPath $evidencePath) { throw 'This single-run evidence directory has already been used; do not repeat or overwrite.' }
}
$arguments = @('-X', 'faulthandler', '-u', '-B', $probePath)
$trialProcess = [System.Diagnostics.Process]::new()
$trialProcess.StartInfo.FileName = $interpreterPath
$trialProcess.StartInfo.WorkingDirectory = $diagnosisDirectory
$trialProcess.StartInfo.UseShellExecute = $false
$trialProcess.StartInfo.CreateNoWindow = $true
$trialProcess.StartInfo.RedirectStandardOutput = $true
$trialProcess.StartInfo.RedirectStandardError = $true
foreach ($argument in $arguments) { $trialProcess.StartInfo.ArgumentList.Add($argument) }
$startedUtc = [DateTime]::UtcNow.ToString('o')
[ordered]@{interpreter=$interpreterPath;arguments=$arguments;cwd=$diagnosisDirectory;started_utc=$startedUtc} |
    ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $startPath -Encoding utf8
$elapsed = [System.Diagnostics.Stopwatch]::StartNew()
if (-not $trialProcess.Start()) { throw 'File probe process did not start' }
$pidValue = $trialProcess.Id
$stdoutTask = $trialProcess.StandardOutput.ReadToEndAsync()
$stderrTask = $trialProcess.StandardError.ReadToEndAsync()
$normalExit = $trialProcess.WaitForExit(10000)
if (-not $normalExit) {
    $trialProcess.Kill($true)
    $trialProcess.WaitForExit()
}
$stdoutText = $stdoutTask.GetAwaiter().GetResult()
$stderrText = $stderrTask.GetAwaiter().GetResult()
$elapsed.Stop()
[IO.File]::WriteAllText($stdoutPath, $stdoutText)
[IO.File]::WriteAllText($stderrPath, $stderrText)
$result = [ordered]@{
    pid=$pidValue;interpreter=$interpreterPath;arguments=$arguments;cwd=$diagnosisDirectory
    started_utc=$startedUtc;ended_utc=[DateTime]::UtcNow.ToString('o');seconds=$elapsed.Elapsed.TotalSeconds
    timed_out=(-not $normalExit);actual_exit_code=$trialProcess.ExitCode
    fatal_access_violation_count=([regex]::Matches($stderrText,'Windows fatal exception: access violation')).Count
    completion_marker=$stdoutText.Contains('"event": "complete"')
    stdout='probe.stdout.log';stderr='probe.stderr.log'
}
$trialProcess.Dispose()
$result | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $resultPath -Encoding utf8
Write-Output ($result | ConvertTo-Json -Compress)
