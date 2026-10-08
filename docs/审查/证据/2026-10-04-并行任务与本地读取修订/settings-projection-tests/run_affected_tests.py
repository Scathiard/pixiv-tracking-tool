"""Capture only the requested affected regression files and child exit code."""
import json
import os
from pathlib import Path
import subprocess
import sys

artifacts = Path(__file__).resolve().parent
application = Path('D:/claude_sandbox/pixiv-tracking-tool/.worktrees/v0.2.0-build20261004.3/app')
tests = ['tests/test_settings_projection.py', 'tests/test_settings_scale.py',
         'tests/test_daily_management.py', 'tests/test_snapshot_read_sharing.py',
         'tests/test_settings_index_preparation.py', 'tests/test_task_index_preparation.py',
         'tests/test_upgrade_revisions.py']
command = [sys.executable, '-m', 'pytest', '-q', *tests,
           '--basetemp=' + str(artifacts/'pytest'), '--junitxml=' + str(artifacts/'pytest.xml')]
environment = dict(os.environ, SETTINGS_PROJECTION_ARTIFACTS=str(artifacts/'projection'))
result = subprocess.run(command, cwd=application, env=environment, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
(artifacts/'stdout.log').write_text(result.stdout, encoding='utf-8')
(artifacts/'stderr.log').write_text(result.stderr, encoding='utf-8')
access_violation = 'access violation' in (result.stdout + result.stderr).casefold()
record = {'command': command, 'cwd': str(application), 'returncode': result.returncode,
          'access_violation_in_output': access_violation,
          'stdout_bytes': len(result.stdout.encode('utf-8')), 'stderr_bytes': len(result.stderr.encode('utf-8'))}
(artifacts/'process.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
print(json.dumps({'returncode': result.returncode, 'access_violation_in_output': access_violation}))
print(result.stdout)
if result.stderr:
    print(result.stderr, file=sys.stderr)
sys.exit(result.returncode)
