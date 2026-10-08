from pathlib import Path
from types import SimpleNamespace
import json, os, sys
out=Path(r"D:\claude_sandbox\pixiv-tracking-tool\.devtools\test-runs\b20261008-reliability-observation-01\integration-01")
os.environ['DATA_ROOT']=str(out/'configuration')
import dotenv
dotenv.load_dotenv=lambda *args,**kwargs:False
app=Path(r"D:\claude_sandbox\pixiv-tracking-tool\.worktrees\v0.2.0-build20261004.3\app")
sys.path[:0]=[str(app/'tests'),str(app)]
import parallel_reliability_probe as probe
args=SimpleNamespace(works=1000,history_steps=1024,json_width=4096,pilot_seconds=12,duration_hours=8,api_interval=.01,remote_delay=.05,download_delay=.15,burst_seconds=45,small_seconds=60)
case=out/'check-workers-1'
case.mkdir()
result=probe.run_case(case,args,1)
print(json.dumps(result,ensure_ascii=False,indent=2))
raise SystemExit(0 if result['status']=='pilot_constraints_passed' else 1)
