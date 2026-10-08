"""Capture the exact three pytest tests and the child process's actual exit code."""
import json
from pathlib import Path
import subprocess
import sys

artifacts = Path(__file__).resolve().parent
application = Path('D:/claude_sandbox/pixiv-tracking-tool/.worktrees/v0.2.0-build20261004.3/app')
tests = [
    'tests/test_settings_index_preparation.py::test_settings_commit_failure_rolls_back_and_runtime_releases_lease',
    'tests/test_settings_index_preparation.py::test_settings_creation_failure_rolls_back_task_and_settings_ddl',
    'tests/test_settings_index_preparation.py::test_final_receipt_failure_rolls_back_both_settings_indexes',
]
command = [sys.executable, '-m', 'pytest', '-q', *tests,
           '--basetemp=' + str(artifacts/'pytest'),
           '--junitxml=' + str(artifacts/'pytest.xml')]
result = subprocess.run(command, cwd=application, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
(artifacts/'stdout.log').write_text(result.stdout, encoding='utf-8')
(artifacts/'stderr.log').write_text(result.stderr, encoding='utf-8')
access_violation = 'access violation' in (result.stdout + result.stderr).casefold()
record = {'command': command, 'cwd': str(application), 'returncode': result.returncode,
          'access_violation_in_output': access_violation,
          'stdout_bytes': len(result.stdout.encode('utf-8')),
          'stderr_bytes': len(result.stderr.encode('utf-8'))}
(artifacts/'process.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
print(json.dumps({'returncode': result.returncode,
                  'access_violation_in_output': access_violation}))
print(result.stdout)
if result.stderr:
    print(result.stderr, file=sys.stderr)
sys.exit(result.returncode)
