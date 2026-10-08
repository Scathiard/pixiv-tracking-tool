"""Explicit opt-in Win32 observer for a newly created, synthetic-only tree."""
import argparse
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[2]
APP = ROOT / '.worktrees/v0.2.0-build20261004.3/app'
PYTHON = ROOT / '.devtools/v012-env/Scripts/python.exe'
DWORD = W.DWORD
PTR = C.c_void_p
SIZE = C.c_size_t
DBG_CONTINUE = 0x10002
NOT_HANDLED = 0x80010001
AV = 0xC0000005
BREAKPOINT = 0x80000003


class ExceptionRecord(C.Structure):
    _fields_ = [('code', DWORD), ('flags', DWORD), ('nested', PTR),
                ('address', PTR), ('count', DWORD), ('information', SIZE * 15)]


class ExceptionInfo(C.Structure):
    _fields_ = [('record', ExceptionRecord), ('first_chance', DWORD)]


class CreateProcessInfo(C.Structure):
    _fields_ = [('file', PTR), ('process', PTR), ('thread', PTR), ('base', PTR),
                ('debug_offset', DWORD), ('debug_size', DWORD), ('tls', PTR),
                ('start', PTR), ('image_name', PTR), ('unicode', W.WORD)]


class CreateThreadInfo(C.Structure):
    _fields_ = [('thread', PTR), ('tls', PTR), ('start', PTR)]


class LoadDllInfo(C.Structure):
    _fields_ = [('file', PTR), ('base', PTR), ('debug_offset', DWORD),
                ('debug_size', DWORD), ('image_name', PTR), ('unicode', W.WORD)]


class DebugUnion(C.Union):
    _fields_ = [('exception', ExceptionInfo), ('create', CreateProcessInfo),
                ('thread', CreateThreadInfo), ('dll', LoadDllInfo),
                ('exit_code', DWORD), ('unload_base', PTR)]


class DebugEvent(C.Structure):
    _fields_ = [('kind', DWORD), ('pid', DWORD), ('tid', DWORD), ('data', DebugUnion)]


class Startup(C.Structure):
    _fields_ = [('cb', DWORD), ('reserved', W.LPWSTR), ('desktop', W.LPWSTR),
                ('title', W.LPWSTR), ('x', DWORD), ('y', DWORD), ('cx', DWORD),
                ('cy', DWORD), ('chars_x', DWORD), ('chars_y', DWORD),
                ('fill', DWORD), ('flags', DWORD), ('show', W.WORD),
                ('reserved_size', W.WORD), ('reserved_bytes', PTR),
                ('stdin', PTR), ('stdout', PTR), ('stderr', PTR)]


class ProcessInfo(C.Structure):
    _fields_ = [('process', PTR), ('thread', PTR), ('pid', DWORD), ('tid', DWORD)]


class ModuleInfo(C.Structure):
    _fields_ = [('base', PTR), ('size', DWORD), ('entry', PTR)]


def locate(modules, address):
    matches = [dict(value, offset=address - base) for base, value in modules.items()
               if value.get('size') and base <= address < base + value['size']]
    return matches[0] if len(matches) == 1 else None


def continuation(code, first_chance, initial_seen, loader_breakpoint=False):
    initial = code == BREAKPOINT and first_chance and not initial_seen and loader_breakpoint
    return (DBG_CONTINUE if initial else NOT_HANDLED), bool(initial)


def decode_exception(event):
    value = event.data.exception
    return {'pid': int(event.pid), 'tid': int(event.tid),
            'code': int(value.record.code), 'address': int(value.record.address or 0),
            'first_chance': bool(value.first_chance),
            'access_kind': int(value.record.information[0]) if value.record.count >= 1 else None,
            'access_address': int(value.record.information[1]) if value.record.count >= 2 else None}


def self_test():
    assert C.sizeof(PTR) == 8, 'Observer requires the existing x64 interpreter'
    expected = {ExceptionRecord: 152, ExceptionInfo: 160, CreateProcessInfo: 72,
                CreateThreadInfo: 24, LoadDllInfo: 40, DebugUnion: 160,
                DebugEvent: 176, Startup: 104, ProcessInfo: 24, ModuleInfo: 24}
    for kind, size in expected.items():
        assert C.sizeof(kind) == size, (kind.__name__, C.sizeof(kind), size)
    assert DebugEvent.data.offset == 16
    event = DebugEvent(kind=1, pid=11, tid=22)
    event.data.exception.record.code = AV
    event.data.exception.record.address = 0x1012
    event.data.exception.record.count = 2
    event.data.exception.record.information[0] = 1
    event.data.exception.record.information[1] = 0xBAD
    event.data.exception.first_chance = 1
    assert decode_exception(event) == {'pid': 11, 'tid': 22, 'code': AV, 'address': 0x1012,
            'first_chance': True, 'access_kind': 1, 'access_address': 0xBAD}
    event.data.exception.first_chance = 0
    assert not decode_exception(event)['first_chance']
    assert locate({0x1000: {'size': 0x20, 'path': 'synthetic.dll'}}, 0x1012)['offset'] == 0x12
    assert locate({0x1000: {'size': 0x20}}, 0x1020) is None
    assert locate({0x1000: {'size': None}}, 0x1012) is None
    assert continuation(AV, True, False) == (NOT_HANDLED, False)
    assert continuation(AV, False, False) == (NOT_HANDLED, False)
    assert continuation(BREAKPOINT, True, False) == (NOT_HANDLED, False)
    assert continuation(BREAKPOINT, True, False, True) == (DBG_CONTINUE, True)
    assert continuation(BREAKPOINT, True, True) == (NOT_HANDLED, False)
    assert continuation(BREAKPOINT, False, False) == (NOT_HANDLED, False)
    print(json.dumps({'self_test': 'passed', 'win32_debug_api_calls': 0,
                      'sizes': {kind.__name__: size for kind, size in expected.items()}}))


def observe(label):
    if C.sizeof(PTR) != 8 or Path(sys.executable).resolve() != PYTHON.resolve():
        raise RuntimeError('Use only the approved existing x64 v012-env interpreter')
    if label != 'stdlib':
        standard_result = json.loads((BASE / 'stdlib/result.json').read_text(encoding='utf-8'))
        if not standard_result['completed_tree'] or standard_result['root_actual_exit_code'] != 0:
            raise RuntimeError('Standard library child must first prove normal complete exit')
    for prior in BASE.glob('*/result.json'):
        if json.loads(prior.read_text(encoding='utf-8')).get('live_at_detach'):
            raise RuntimeError('A prior detached child tree has not been confirmed stopped; do not launch another')
    out = BASE / label
    out.mkdir(exist_ok=False)
    (out / 'synthetic-config').mkdir()
    (out / 'temp').mkdir()
    api = C.WinDLL('kernel32', use_last_error=True)

    def bind(name, arguments, result=W.BOOL):
        value = getattr(api, name)
        value.argtypes, value.restype = arguments, result
        return value

    create = bind('CreateProcessW', [W.LPCWSTR, W.LPWSTR, PTR, PTR, W.BOOL, DWORD,
                  PTR, W.LPCWSTR, C.POINTER(Startup), C.POINTER(ProcessInfo)])
    kill_on_exit = bind('DebugSetProcessKillOnExit', [W.BOOL])
    wait = bind('WaitForDebugEvent', [C.POINTER(DebugEvent), DWORD])
    resume = bind('ContinueDebugEvent', [DWORD, DWORD, DWORD])
    detach = bind('DebugActiveProcessStop', [DWORD])
    close = bind('CloseHandle', [PTR])
    duplicate = bind('DuplicateHandle', [PTR, PTR, PTR, C.POINTER(PTR), DWORD, W.BOOL, DWORD])
    current = bind('GetCurrentProcess', [], PTR)
    final_path = bind('GetFinalPathNameByHandleW', [PTR, W.LPWSTR, DWORD, DWORD], DWORD)
    module_info = bind('K32GetModuleInformation', [PTR, PTR, C.POINTER(ModuleInfo), DWORD])
    module_path = bind('K32GetModuleFileNameExW', [PTR, PTR, W.LPWSTR, DWORD], DWORD)
    get_module = bind('GetModuleHandleW', [W.LPCWSTR], PTR)
    get_proc = bind('GetProcAddress', [PTR, C.c_char_p], PTR)
    set_inherit = bind('SetHandleInformation', [PTR, DWORD, DWORD])
    query_exit = bind('GetExitCodeProcess', [PTR, C.POINTER(DWORD)])
    started = time.monotonic()
    alive, owned_handles, modules, initial_breaks = set(), {}, {}, set()
    exits, detach_results, first_av, last_av = {}, [], None, None
    av_count = event_count = 0
    observer_error = None
    local_ntdll = get_module('ntdll.dll')
    local_breakpoint = get_proc(local_ntdll, b'DbgBreakPoint') if local_ntdll else None
    if not local_breakpoint:
        raise RuntimeError('Cannot identify the loader breakpoint; no child launched')
    loader_breakpoint_offset = int(local_breakpoint) - int(local_ntdll)

    def checked(ok):
        if not ok:
            raise C.WinError(C.get_last_error())

    with (out / 'events.jsonl').open('x', encoding='utf-8') as log, \
         (out / 'stdout.log').open('xb', buffering=0) as stdout, \
         (out / 'stderr.log').open('xb', buffering=0) as stderr, \
         open(os.devnull, 'rb', buffering=0) as stdin:
        def record(kind, **fields):
            log.write(json.dumps({'event': kind, 'elapsed': time.monotonic() - started,
                                  'wall_time': time.time(), **fields}) + '\n')
            log.flush()

        def image_path(file_handle):
            if not file_handle:
                return None
            try:
                buffer = C.create_unicode_buffer(32768)
                length = final_path(file_handle, buffer, len(buffer), 0)
                return buffer.value if 0 < length < len(buffer) else None
            finally:
                checked(close(file_handle))

        def refresh_module(pid, base):
            value = ModuleInfo()
            if module_info(owned_handles[pid], PTR(base), C.byref(value), C.sizeof(value)):
                modules[pid][base]['size'] = int(value.size)
            if not modules[pid][base]['path']:
                buffer = C.create_unicode_buffer(32768)
                length = module_path(owned_handles[pid], PTR(base), buffer, len(buffer))
                if 0 < length < len(buffer):
                    modules[pid][base]['path'] = buffer.value

        import msvcrt
        stdin_handle = msvcrt.get_osfhandle(stdin.fileno())
        stdout_handle, stderr_handle = msvcrt.get_osfhandle(stdout.fileno()), msvcrt.get_osfhandle(stderr.fileno())
        checked(set_inherit(stdin_handle, 1, 1))
        checked(set_inherit(stdout_handle, 1, 1))
        checked(set_inherit(stderr_handle, 1, 1))
        startup = Startup(cb=C.sizeof(Startup), flags=0x100, stdin=stdin_handle,
                          stdout=stdout_handle, stderr=stderr_handle)
        info = ProcessInfo()
        target = BASE / ('stdlib_target.py' if label == 'stdlib' else 'collect_target.py')
        arguments = [str(PYTHON), '-X', 'faulthandler', '-u', '-B', str(target), str(out)]
        command = C.create_unicode_buffer(subprocess.list2cmdline(arguments))
        environment = dict(os.environ, DATA_ROOT=str(out / 'synthetic-config'),
                           TEMP=str(out / 'temp'), TMP=str(out / 'temp'), PIXIV_REFRESH_TOKEN='')
        env_block = C.create_unicode_buffer('\0'.join(f'{key}={value}' for key, value in
                                  sorted(environment.items(), key=lambda item: item[0].upper())) + '\0\0')
        try:
            # DEBUG_PROCESS follows only descendants created by this new tree.
            checked(create(str(PYTHON), command, None, None, True, 0x1 | 0x400 | 0x08000000,
                           env_block, str(APP), C.byref(startup), C.byref(info)))
            alive.add(int(info.pid))
            record('launch', pid=int(info.pid), arguments=arguments, cwd=str(APP))
            # The initial debug event still suspends the child; set before any continue.
            checked(kill_on_exit(False))
            checked(close(info.thread))
            info.thread = None
            while alive and time.monotonic() - started < 60 and event_count < 10000:
                event = DebugEvent()
                remaining = max(1, min(100, int((60 - (time.monotonic() - started)) * 1000)))
                if not wait(C.byref(event), remaining):
                    error = C.get_last_error()
                    if error in (121, 258):
                        continue
                    raise C.WinError(error)
                event_count += 1
                pid, tid, kind = int(event.pid), int(event.tid), int(event.kind)
                status = NOT_HANDLED if kind == 1 else DBG_CONTINUE
                try:
                    if kind == 3:
                        alive.add(pid)
                        path = image_path(event.data.create.file)
                        process_copy = PTR()
                        checked(duplicate(current(), event.data.create.process, current(),
                                          C.byref(process_copy), 0, False, 2))
                        owned_handles[pid] = process_copy.value
                        modules[pid] = {}
                        base = int(event.data.create.base or 0)
                        modules[pid][base] = {'path': path, 'base': base, 'size': None}
                        refresh_module(pid, base)
                        record('create_process', pid=pid, tid=tid, image=modules[pid][base],
                               thread_start=int(event.data.create.start or 0))
                    elif pid not in alive:
                        raise RuntimeError('Unexpected event outside this newly created tree')
                    elif kind == 6:
                        base = int(event.data.dll.base or 0)
                        modules[pid][base] = {'path': image_path(event.data.dll.file), 'base': base, 'size': None}
                        refresh_module(pid, base)
                        record('load_module', pid=pid, tid=tid, module=modules[pid][base])
                    elif kind == 7:
                        base = int(event.data.unload_base or 0)
                        record('unload_module', pid=pid, base=base)
                        modules[pid].pop(base, None)
                    elif kind == 1:
                        value = decode_exception(event)
                        for base in modules[pid]:
                            if not modules[pid][base]['size'] or not modules[pid][base]['path']:
                                refresh_module(pid, base)
                        loader_breakpoint = any(value['address'] == base + loader_breakpoint_offset and
                            (module['path'] or '').replace('\\', '/').rsplit('/', 1)[-1].lower() == 'ntdll.dll'
                            for base, module in modules[pid].items())
                        status, initial = continuation(value['code'], value['first_chance'],
                                                       pid in initial_breaks, loader_breakpoint)
                        if initial:
                            initial_breaks.add(pid)
                        if value['code'] == AV:
                            value['module'] = locate(modules[pid], value['address'])
                            value['elapsed'] = time.monotonic() - started
                            value['wall_time'] = time.time()
                            av_count += 1
                            if first_av is None:
                                first_av = value
                            last_av = value
                        record('exception', **value, initial_breakpoint=initial, continue_status=status)
                    elif kind == 5:
                        exits[pid] = int(event.data.exit_code)
                        record('exit_process', pid=pid, tid=tid, actual_exit_code=exits[pid])
                        alive.remove(pid)
                    elif kind == 2:
                        record('create_thread', pid=pid, tid=tid, thread_start=int(event.data.thread.start or 0))
                    elif kind == 4:
                        record('exit_thread', pid=pid, tid=tid, exit_code=int(event.data.exit_code))
                    # Debug string content is deliberately not read.
                finally:
                    checked(resume(pid, tid, status))
                if kind == 5 and pid in owned_handles:
                    checked(close(owned_handles.pop(pid)))
        except Exception as exc:
            observer_error = f'{type(exc).__name__}: {exc}'
            record('observer_error', error=observer_error)
        finally:
            # Never terminate/attach/inject. Only detach the IDs received from this launch.
            for pid in sorted(alive, reverse=True):
                ok = bool(detach(pid))
                value = {'pid': pid, 'detached': ok, 'error': None if ok else C.get_last_error()}
                detach_results.append(value)
                record('detach', **value)
            for handle in owned_handles.values():
                checked(close(handle))
            if info.thread:
                checked(close(info.thread))
            if info.process:
                code = DWORD()
                if query_exit(info.process, C.byref(code)) and code.value != 259:
                    exits.setdefault(int(info.pid), int(code.value))
                checked(close(info.process))
            checked(set_inherit(stdout_handle, 1, 0))
            checked(set_inherit(stderr_handle, 1, 0))
            checked(set_inherit(stdin_handle, 1, 0))
            summary = {'label': label, 'root_pid': int(info.pid), 'seconds': time.monotonic() - started,
                       'event_count': event_count, 'av_count': av_count, 'first_av': first_av,
                       'last_av': last_av, 'actual_exit_codes': exits,
                       'root_actual_exit_code': exits.get(int(info.pid)),
                       'live_at_detach': sorted(alive), 'detach': detach_results,
                       'observer_error': observer_error, 'completed_tree': not alive and observer_error is None}
            (out / 'result.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
            print(json.dumps(summary))
    return 0 if summary['completed_tree'] and summary['root_actual_exit_code'] == 0 else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--observe', choices=('stdlib', 'collect-01', 'collect-02'))
    args = parser.parse_args()
    if args.observe:
        raise SystemExit(observe(args.observe))
    self_test()
