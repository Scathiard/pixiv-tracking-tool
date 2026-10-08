"""Pure dependency imports only; no project, configuration, database or network."""
import faulthandler
import importlib
import json
import sys
import time

faulthandler.enable(all_threads=True)
started = time.perf_counter()

def emit(event, **fields):
    payload = dict(event=event, elapsed_seconds=time.perf_counter()-started,
                   wall_epoch_seconds=time.time(), **fields)
    line = json.dumps(payload, ensure_ascii=True)
    print(line, flush=True)
    if event in ('import_begin', 'import_end', 'import_error', 'complete'):
        print('PROBE_PHASE '+line, file=sys.stderr, flush=True)

emit('identity', executable=sys.executable, version=sys.version,
     prefix=sys.prefix, base_prefix=sys.base_prefix,
     faulthandler_enabled=faulthandler.is_enabled(), sys_path=sys.path,
     flags=str(sys.flags))
for name in ('zoneinfo', 'alembic', 'gallery_dl'):
    emit('import_begin', module=name)
    began = time.perf_counter()
    try:
        module = importlib.import_module(name)
    except BaseException as exc:
        emit('import_error', module=name, error_type=type(exc).__name__)
        raise
    emit('import_end', module=name, seconds=time.perf_counter()-began,
         path=getattr(module, '__file__', None),
         version=getattr(module, '__version__', None))
emit('complete', project_modules=[name for name in sys.modules if name == 'src' or name.startswith('src.')],
     native_zoneinfo_path=getattr(sys.modules.get('_zoneinfo'), '__file__', None))
