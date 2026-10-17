"""Read log boundaries once; retain timing/diagnostic metadata, never payloads."""
from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path

ROOT = Path('D:/PixivTracker/data')
OUT = Path(__file__).resolve().parent
PREFIX = 'diagnostic-20261013-215839-6296-7c2dedefc7d6-part'
BLOCK = 256 * 1024


def parse_lines(data):
    values = []
    for line in data.splitlines():
        try:
            values.append(json.loads(line))
        except (ValueError, UnicodeError):
            continue
    return values


def main():
    all_files = list(ROOT.glob('diagnostic-*.jsonl'))
    paths = sorted(ROOT.glob(PREFIX + '*.jsonl'))
    inventory = []
    metrics = []
    active = []
    interesting = []
    stacks = {}
    counts = Counter()
    aggregates = defaultdict(Counter)
    read_bytes = 0
    for path in paths:
        with path.open('rb') as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(0)
            head = stream.read(min(BLOCK, size))
            head = head[:head.rfind(b'\n') + 1]
            tail_start = max(len(head), size - BLOCK)
            stream.seek(tail_start)
            if tail_start:
                stream.readline()
            tail = stream.read()
        read_bytes += len(head) + len(tail)
        rows = parse_lines(head) + parse_lines(tail)
        # The ranges do not overlap. Writer-owned part markers have no seq,
        # so deduplicating by seq would incorrectly replace the first marker.
        first, last = rows[0], rows[-1]
        inventory.append({'file': path.name, 'bytes': size, 'first_utc': first.get('utc'),
            'last_utc': last.get('utc'), 'first_seq': first.get('seq'), 'last_seq': last.get('seq'),
            'sample_records': len(rows), 'last_complete_line': tail.endswith(b'\n')})
        for row in rows:
            event = row.get('event', '')
            counts[event] += 1
            context = {'file': path.name, **row}
            if event == 'process.sample':
                metrics.append(context)
            elif event == 'watch.active':
                active.append(context)
            elif event == 'thread.stack':
                stacks[row.get('stack_id')] = context
            elif event == 'event.aggregate':
                aggregates[row.get('observed_event', '')]['count'] += row.get('count', 0)
            elif (event in ('diagnostic.start', 'diagnostic.stop', 'read.completed', 'sql.slow',
                            'sql.error', 'http.request.end') or event.startswith('task.') or
                  'error' in event or 'shutdown' in event or event.startswith('wal.')):
                if event.endswith('.begin'):
                    continue
                if event in ('wal.wait.end', 'wal.cycle.end', 'wal.checkpoint.end') and row.get('duration_ms', 0) < 1000:
                    continue
                if event == 'task.scheduler_step.end' and row.get('outcome') in ('idle', 'empty') and row.get('duration_ms', 0) < 1000:
                    continue
                interesting.append(context)
    def save(name, data):
        (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    save('run-boundaries.json', {'observed_at': datetime.now().astimezone().isoformat(),
        'run_id': '7c2dedefc7d6', 'method': 'first and last 256 KiB of every volume; not whole-run event statistics',
        'all_log_files': len(all_files), 'all_log_bytes': sum(p.stat().st_size for p in all_files),
        'run_files': len(paths), 'run_bytes': sum(x['bytes'] for x in inventory),
        'sample_bytes': read_bytes, 'sample_events': counts, 'sample_aggregate_counts': dict(aggregates),
        'files': inventory})
    save('sample-process.json', metrics)
    save('sample-active.json', active)
    save('sample-events.json', interesting)
    save('sample-stacks.json', list(stacks.values()))
    print(json.dumps({'run_files': len(paths), 'run_bytes': sum(x['bytes'] for x in inventory),
        'sample_bytes': read_bytes, 'metrics': len(metrics), 'active': len(active),
        'interesting': len(interesting), 'stacks': len(stacks), 'top_sample_events': counts.most_common(12)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
