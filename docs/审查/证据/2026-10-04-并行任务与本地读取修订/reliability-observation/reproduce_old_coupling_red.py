"""Execute the exact old sample block with a read fault, without any load or DB."""
import ast
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

probe = Path(__file__).with_name('old-probe-base.py')
source = probe.read_text('utf-8')
tree = ast.parse(source)
sample = next(node for node in ast.walk(tree) if isinstance(node, ast.If)
              and ast.unparse(node.test) == 'now >= next_sample')
class ReadDeadline(Exception):
    pass
class Fault:
    def __enter__(self):
        raise ReadDeadline('mock diagnostic read failure')
    def __exit__(self, *args):
        pass
failures = []
stream = io.StringIO()
env = {'runtime': SimpleNamespace(database=SimpleNamespace(read=lambda: Fault())),
       'telemetry': SimpleNamespace(fail=failures.append, samples=stream), 'now': 10, 'next_sample': 0}
exec(compile(ast.Module(body=[sample], type_ignores=[]), str(probe), 'exec'), env)
record = {'probe_sha256': hashlib.sha256(source.encode()).hexdigest(),
          'injected_fault': 'mock ReadDeadline in the old diagnostic read',
          'expected_resource_record_even_when_other_observation_fails': True,
          'actual_resource_record': bool(stream.getvalue()), 'failures': failures,
          'final_diagnostic_precedes_soak_gates': source.index('result["production_persistence"]') < source.index('if result["elapsed_seconds"] <'),
          'verdict': 'red_old_observation_coupling'}
Path(__file__).with_name('old-coupling-red-reproduced.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
print(json.dumps(record, indent=2))
assert record['actual_resource_record'], 'Old COUNT failure erased the resource observation'
