"""Run exactly one selected original acceptance probe and retain its exit."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path('D:/claude_sandbox/pixiv-tracking-tool')
APP = ROOT / '.worktrees/v0.2.0-build20261004.3/app'
HERE = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument('phase', choices=('pilot', 'soak'))
args = parser.parse_args()
suffix = '09' if args.phase == 'pilot' else '10'
artifacts = ROOT / ('.devtools/test-runs/b20261004.3-reliability-20261008-' + suffix)
if artifacts.exists() or any((HERE / (args.phase + name)).exists()
                            for name in ('.started.json', '.stdout.log', '.stderr.log', '.result.json')):
    raise RuntimeError('Fresh paths required; previous runs must be retained')
command = [sys.executable, '-X', 'faulthandler', '-u', 'tests/parallel_reliability_probe.py',
           '--artifacts', str(artifacts), '--check-workers', '1']
command += ['--pilot-seconds', '120'] if args.phase == 'pilot' else ['--duration-hours', '8']
record = {'command': command, 'cwd': str(APP), 'started_utc': datetime.now(timezone.utc).isoformat()}
began = time.perf_counter()
with (HERE / (args.phase + '.stdout.log')).open('x', encoding='utf-8') as output, \
        (HERE / (args.phase + '.stderr.log')).open('x', encoding='utf-8') as errors:
    child = subprocess.Popen(command, cwd=APP, stdout=output, stderr=errors)
    record['pid'] = child.pid
    (HERE / (args.phase + '.started.json')).write_text(json.dumps(record, indent=2), encoding='utf-8')
    print(json.dumps(record), flush=True)
    record['returncode'] = child.wait()
record.update(seconds=time.perf_counter()-began, ended_utc=datetime.now(timezone.utc).isoformat())
record['access_violation_in_output'] = any('access violation' in (HERE / (args.phase + name)).read_text(
    encoding='utf-8', errors='replace').casefold() for name in ('.stdout.log', '.stderr.log'))
(HERE / (args.phase + '.result.json')).write_text(json.dumps(record, indent=2), encoding='utf-8')
print(json.dumps(record, indent=2), flush=True)
raise SystemExit(record['returncode'])
