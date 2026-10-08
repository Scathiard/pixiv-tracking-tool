"""One original-size probe; retain process status and instrument identity."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path('D:/claude_sandbox/pixiv-tracking-tool')
APP = ROOT / '.worktrees/v0.2.0-build20261004.3/app'
HERE = Path(__file__).resolve().parent
EXPECTED_PRODUCTION = 'f5401afcc839470959608a354e30a1b77e2914224a890b6bdeac084d33efefaa'


def instrument_identity():
    paths = ('tests/parallel_reliability_probe.py',
             'tests/parallel_reliability_observation.py')
    return {name: hashlib.sha256((APP / name).read_bytes()).hexdigest()
            for name in paths}


def production_identity():
    digest = hashlib.sha256()
    for path in sorted((APP / 'src').rglob('*.py')):
        digest.update(str(path.relative_to(APP)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=('pilot', 'soak'))
    args = parser.parse_args()
    if production_identity() != EXPECTED_PRODUCTION:
        raise RuntimeError('Production source differs from the fixed candidate')
    suffix = '11' if args.phase == 'pilot' else '12'
    artifacts = ROOT / ('.devtools/test-runs/b20261004.3-reliability-20261008-' + suffix)
    if artifacts.exists() or any((HERE / (args.phase + name)).exists()
                                for name in ('.started.json', '.stdout.log', '.stderr.log', '.result.json')):
        raise RuntimeError('Fresh paths required; previous runs must be retained')
    if args.phase == 'soak':
        prior = json.loads((HERE / 'pilot.result.json').read_text(encoding='utf-8'))
        prior_result = json.loads((ROOT / '.devtools/test-runs/'
            'b20261004.3-reliability-20261008-11/check-workers-1/result.json').read_text(encoding='utf-8'))
        if prior['returncode'] != 0 or prior_result['status'] != 'pilot_constraints_passed':
            raise RuntimeError('Pilot has not passed; do not run a soak')
        if not prior['instrument_unchanged'] or prior['instrument_before'] != instrument_identity():
            raise RuntimeError('Instrument changed since the pilot')
        if prior_result['source_tree_sha256'] != EXPECTED_PRODUCTION or \
                prior_result['source_tree_sha256_after'] != EXPECTED_PRODUCTION:
            raise RuntimeError('Pilot did not verify the same fixed production source')
    command = [sys.executable, '-X', 'faulthandler', '-u', 'tests/parallel_reliability_probe.py',
               '--artifacts', str(artifacts), '--check-workers', '1']
    command += ['--pilot-seconds', '120'] if args.phase == 'pilot' else ['--duration-hours', '8']
    record = {'command': command, 'cwd': str(APP),
              'instrument_before': instrument_identity(), 'production_before': production_identity(),
              'started_utc': datetime.now(timezone.utc).isoformat()}
    began = time.perf_counter()
    with (HERE / (args.phase + '.stdout.log')).open('x', encoding='utf-8') as output, \
            (HERE / (args.phase + '.stderr.log')).open('x', encoding='utf-8') as errors:
        child = subprocess.Popen(command, cwd=APP, stdout=output, stderr=errors)
        record['pid'] = child.pid
        (HERE / (args.phase + '.started.json')).write_text(json.dumps(record, indent=2), encoding='utf-8')
        print(json.dumps(record), flush=True)
        record['returncode'] = child.wait()
    record.update(seconds=time.perf_counter()-began,
                  ended_utc=datetime.now(timezone.utc).isoformat(),
                  instrument_after=instrument_identity(), production_after=production_identity())
    record['access_violation_in_output'] = any('access violation' in (HERE / (args.phase + name)).read_text(
        encoding='utf-8', errors='replace').casefold() for name in ('.stdout.log', '.stderr.log'))
    record['instrument_unchanged'] = record['instrument_before'] == record['instrument_after']
    record['production_unchanged'] = record['production_before'] == record['production_after']
    (HERE / (args.phase + '.result.json')).write_text(json.dumps(record, indent=2), encoding='utf-8')
    print(json.dumps(record, indent=2), flush=True)
    return record['returncode'] if record['instrument_unchanged'] and record['production_unchanged'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
