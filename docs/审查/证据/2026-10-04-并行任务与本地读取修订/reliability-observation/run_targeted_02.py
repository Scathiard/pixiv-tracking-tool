from pathlib import Path
import subprocess, sys, json, time
out=Path(r'D:\claude_sandbox\pixiv-tracking-tool\.devtools\test-runs\b20261008-reliability-observation-01')
command=[sys.executable,'-X','faulthandler','-m','pytest','-q','tests/test_parallel_reliability_observation.py','--basetemp',str(out/'pytest-02')]
started=time.monotonic()
result=subprocess.run(command,cwd=r'D:\claude_sandbox\pixiv-tracking-tool\.worktrees\v0.2.0-build20261004.3\app',capture_output=True,text=True,encoding='utf-8',errors='replace')
(out/'pytest-02.stdout.log').write_text(result.stdout,encoding='utf-8')
(out/'pytest-02.stderr.log').write_text(result.stderr,encoding='utf-8')
receipt={'command':command,'returncode':result.returncode,'elapsed_seconds':time.monotonic()-started}
(out/'pytest-02.receipt.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
print(json.dumps(receipt)); print(result.stdout); print(result.stderr)
raise SystemExit(result.returncode)
