from pathlib import Path
import subprocess,sys,json,time
out=Path(r"D:\claude_sandbox\pixiv-tracking-tool\.devtools\test-runs\b20261008-reliability-observation-01")
command=[sys.executable,'-X','faulthandler',str(out/'integration_runner.py')]
started=time.monotonic()
result=subprocess.run(command,cwd=r"D:\claude_sandbox\pixiv-tracking-tool\.worktrees\v0.2.0-build20261004.3\app",capture_output=True,text=True,encoding='utf-8',errors='replace')
for stream in ('stdout','stderr'):
 (out/'integration-01'/f'{stream}.log').write_text(getattr(result,stream),encoding='utf-8')
receipt={'command':command,'returncode':result.returncode,'elapsed_seconds':time.monotonic()-started}
(out/'integration-01'/'receipt.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
print(json.dumps(receipt));print(result.stderr)
raise SystemExit(result.returncode)
