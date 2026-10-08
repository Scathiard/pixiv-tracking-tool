$ErrorActionPreference = 'Stop'
$diagnosisDirectory = $PSScriptRoot
$interpreterPath = 'D:\claude_sandbox\pixiv-tracking-tool\.devtools\v012-env\Scripts\python.exe'
$probePath = Join-Path $diagnosisDirectory 'import_probe.py'
$results = @()
for ($trialNumber = 1; $trialNumber -le 3; $trialNumber++) {
    $label = 'import-{0:D2}' -f $trialNumber
    $trialProcess = [System.Diagnostics.Process]::new()
    $trialProcess.StartInfo.FileName = $interpreterPath
    $trialProcess.StartInfo.WorkingDirectory = $diagnosisDirectory
    $trialProcess.StartInfo.UseShellExecute = $false
    $trialProcess.StartInfo.CreateNoWindow = $true
    $trialProcess.StartInfo.RedirectStandardOutput = $true
    $trialProcess.StartInfo.RedirectStandardError = $true
    foreach ($argument in @('-X', 'faulthandler', '-u', '-B', $probePath)) {
        $trialProcess.StartInfo.ArgumentList.Add($argument)
    }
    $startedUtc = [DateTime]::UtcNow.ToString('o')
    $elapsed = [System.Diagnostics.Stopwatch]::StartNew()
    if (-not $trialProcess.Start()) { throw 'Import process did not start' }
    $pidValue = $trialProcess.Id
    $stdoutTask = $trialProcess.StandardOutput.ReadToEndAsync()
    $stderrTask = $trialProcess.StandardError.ReadToEndAsync()
    $normalExit = $trialProcess.WaitForExit(30000)
    if (-not $normalExit) {
        $trialProcess.Kill($true)
        $trialProcess.WaitForExit()
    }
    $stdoutText = $stdoutTask.GetAwaiter().GetResult()
    $stderrText = $stderrTask.GetAwaiter().GetResult()
    $elapsed.Stop()
    [IO.File]::WriteAllText((Join-Path $diagnosisDirectory "$label.stdout.log"), $stdoutText)
    [IO.File]::WriteAllText((Join-Path $diagnosisDirectory "$label.stderr.log"), $stderrText)
    $results += [ordered]@{
        trial = $label; pid = $pidValue; interpreter = $interpreterPath
        arguments = @('-X', 'faulthandler', '-u', '-B', $probePath)
        cwd = $diagnosisDirectory; started_utc = $startedUtc
        ended_utc = [DateTime]::UtcNow.ToString('o'); seconds = $elapsed.Elapsed.TotalSeconds
        timed_out = (-not $normalExit); actual_exit_code = $trialProcess.ExitCode
        fatal_access_violation_count = ([regex]::Matches($stderrText, 'Windows fatal exception: access violation')).Count
        completion_marker = $stdoutText.Contains('"event": "complete"')
        stdout = "$label.stdout.log"; stderr = "$label.stderr.log"
    }
    $trialProcess.Dispose()
    $results | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $diagnosisDirectory 'result.json') -Encoding utf8
    Write-Output ($results[-1] | ConvertTo-Json -Compress)
    if (-not $normalExit) { break }
}
