"""One small warm synthetic read workload; not a real-library performance test.

Run in app with PYTHONPATH=. and explicit temporary/output paths.
"""
import argparse
import json
from pathlib import Path
import time

from sqlalchemy import text
from src.core import diagnostics
from src.core.database import ArchiveDatabase


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--temporary', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.temporary.mkdir(parents=True, exist_ok=False)
    database = ArchiveDatabase.create(args.temporary / 'synthetic.db')
    def workload():
        started = time.perf_counter()
        for _ in range(100):
            with database.read() as session:
                assert session.scalar(text('SELECT 1')) == 1
        return round((time.perf_counter() - started) * 1000, 3)
    try:
        with database.read() as session:
            assert session.scalar(text('SELECT 1')) == 1
        without = workload()
        log = diagnostics.start(args.temporary / 'capture', build='cost-probe', version='0.2.0', interval=2)
        with_observer = workload()
        recorder = diagnostics._recorder
        diagnostics.stop()
        rows = [json.loads(line) for p in sorted(log.parent.glob('*.jsonl')) for line in p.read_text(encoding='utf-8').splitlines()]
        result = {'scope':'100 warm SELECT 1 reads on one small synthetic SQLite database, off then on',
            'without_observer_ms':without, 'with_observer_ms':with_observer,
            'extra_ms_per_read':round((with_observer-without)/100, 3),
            'log_bytes':sum(p.stat().st_size for p in log.parent.glob('*.jsonl')), 'records':len(rows),
            'queue_dropped':recorder.dropped,'probe_errors':recorder.probe_errors,
            'writer_cpu_ms':round(recorder.writer_cpu_ms,3),
            'log_write_ms':round(recorder.write_ms,3),'log_flush_ms':round(recorder.flush_ms,3),
            'limitations':'Single short sequential sample. Does not establish daily idle/load rates or causal effect on gallery timeouts. Existing daily process may share disk/CPU.'}
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False))
    finally:
        diagnostics.stop()
        database.close()


if __name__ == '__main__':
    main()
