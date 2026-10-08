"""Record a full isolated regression without swallowing child diagnostics."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ARTIFACTS = Path(__file__).resolve().parent
APP = Path('D:/claude_sandbox/pixiv-tracking-tool/.worktrees/v0.2.0-build20261004.3/app')


def identity():
    digest = hashlib.sha256()
    for path in sorted((APP / 'src').rglob('*.py')):
        digest.update(str(path.relative_to(APP)).encode())
        digest.update(path.read_bytes())
    commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=APP,
                            capture_output=True, text=True, check=True).stdout.strip()
    return {'commit': commit, 'source_tree_sha256': digest.hexdigest()}


def main():
    if any((ARTIFACTS / name).exists() for name in ('pytest', 'stdout.log', 'stderr.log', 'process.json')):
        raise RuntimeError('Fresh artifacts required; previous results must be retained')
    record = {'before': identity(), 'started_utc': datetime.now(timezone.utc).isoformat()}
    command = [sys.executable, '-X', 'faulthandler', '-u', '-m', 'pytest', '-q', 'tests',
               '--basetemp=' + str(ARTIFACTS / 'pytest'),
               '--junitxml=' + str(ARTIFACTS / 'result.xml')]
    record.update(command=command, cwd=str(APP))
    (ARTIFACTS / 'source-reference.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
    environment = dict(os.environ, PYTHONIOENCODING='utf-8',
                       SETTINGS_PROJECTION_ARTIFACTS=str(ARTIFACTS / 'projection'))
    began = time.perf_counter()
    with (ARTIFACTS / 'stdout.log').open('x', encoding='utf-8') as stdout, \
            (ARTIFACTS / 'stderr.log').open('x', encoding='utf-8') as stderr:
        child = subprocess.Popen(command, cwd=APP, env=environment, stdout=stdout, stderr=stderr)
        record['pid'] = child.pid
        (ARTIFACTS / 'source-reference.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
        print(json.dumps({'started': child.pid, 'source': record['before']}), flush=True)
        record['returncode'] = child.wait()
    record.update(seconds=time.perf_counter()-began, after=identity(),
                  ended_utc=datetime.now(timezone.utc).isoformat())
    record['source_unchanged'] = record['before']['source_tree_sha256'] == record['after']['source_tree_sha256']
    record['access_violation_in_output'] = any(
        'access violation' in (ARTIFACTS / name).read_text(encoding='utf-8', errors='replace').casefold()
        for name in ('stdout.log', 'stderr.log'))
    (ARTIFACTS / 'process.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
    print(json.dumps(record, indent=2), flush=True)
    return record['returncode'] or (0 if record['source_unchanged'] else 1)


if __name__ == '__main__':
    raise SystemExit(main())
