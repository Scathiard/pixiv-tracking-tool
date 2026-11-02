"""Small, always-on process boundary records, independent of diagnostics.

Call start only after obtaining the application's single-instance ownership.
No exception messages, source lines, locals, request data or account data are
recorded. A missing final marker establishes an unclean end, not its cause.
"""
from __future__ import annotations

from datetime import datetime, timezone
import errno
import faulthandler
import json
import os
from pathlib import Path
import re
import sys
import threading
import uuid


_recorder: LifecycleRecorder | None = None
_LABEL = re.compile(r"[A-Za-z0-9_.:\-]{1,96}\Z")
_EVENTS = frozenset((
    "app_start", "app_end", "service_start", "service_ready", "quit_requested",
    "tray_loop_stopped", "service_stopped", "shutdown_requested", "cleanup_completed",
    "previous_unknown", "previous_unclean", "state_warning", "exception", "thread_exception",
))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _label(value: object, fallback: str = "unknown") -> str:
    return value if isinstance(value, str) and _LABEL.fullmatch(value) else fallback


def _fields(fields: dict) -> dict:
    result = {}
    for key in ("source", "outcome"):
        value = fields.get(key)
        if isinstance(value, str) and _LABEL.fullmatch(value):
            result[key] = value
    for key in ("offline", "expected"):
        value = fields.get(key)
        if type(value) is bool:
            result[key] = value
    value = fields.get("port")
    if type(value) is int and 0 <= value <= 65535:
        result["port"] = value
    value = fields.get("exit_code")
    if type(value) is int and -(2 ** 31) <= value <= 2 ** 32 - 1:
        result["exit_code"] = value
    return result


class LifecycleRecorder:
    """Best-effort synchronous writes only at rare lifecycle boundaries."""

    def __init__(self, data_dir: Path, *, version: str, build: str):
        self._lock = threading.RLock()
        self._log = None
        self._owner = None
        self._fault = None
        self._old_thread_hook = None
        self._thread_hook = None
        self.closed = False
        self.enabled = False
        self.failures = 0
        self.had_unhandled_exception = False
        self._warned = False
        self._state_path = Path(data_dir) / "lifecycle-state.json"
        self._state = {
            "run_id": uuid.uuid4().hex, "pid": os.getpid(), "ppid": os.getppid(),
            "version": _label(version), "build": _label(build),
            "started_at": _now(), "clean": False, "ended_at": None, "exit_code": None,
        }
        try:
            launch_id = os.environ.get("PIXIV_LAUNCH_ID", "")
            if len(launch_id) <= 45:
                self._state["launch_id"] = str(uuid.UUID(launch_id))
        except (ValueError, AttributeError):
            pass
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            if not self._acquire_owner():
                return
            previous = self._previous()
            self._log = (self._state_path.parent / "lifecycle.jsonl").open(
                "a", encoding="utf-8", newline="\n")
            self.enabled = True
            self._write_state()
            self._record("app_start", version=self._state["version"], build=self._state["build"],
                         pid=self._state["pid"], ppid=self._state["ppid"])
            if previous is None:
                self._record("previous_unknown", outcome="no_marker")
            elif previous == "invalid":
                self._record("state_warning", outcome="invalid_previous_marker")
                self._record("previous_unknown", outcome="invalid_marker")
            elif previous["clean"] is False:
                self._record("previous_unclean", previous_run_id=previous["run_id"],
                             previous_pid=previous["pid"],
                             previous_exit_code=previous.get("exit_code"))
        except Exception:
            self._failure()
            if self._log is not None:
                try:
                    self._log.close()
                except Exception:
                    self._failure()
                self._log = None
            self.enabled = False
            self._release_owner()
            return
        self._install_hooks()

    def _failure(self, *, occupied: bool = False) -> None:
        self.failures += 1
        if not self._warned:
            self._warned = True
            try:
                warning = ("PixivTracker: lifecycle recording unavailable; another recorder owns this data directory."
                           if occupied else
                           "PixivTracker: lifecycle recording unavailable; evidence may be incomplete.")
                print(warning,
                      file=sys.stderr, flush=True)
            except Exception:
                pass

    def _acquire_owner(self) -> bool:
        """The data-directory marker has one writer, even across IPC roots."""
        stream = None
        acquired = False
        try:
            stream = (self._state_path.parent / "lifecycle-owner.lock").open("a+b")
            stream.seek(0, 2)
            if not stream.tell():
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                self._failure(occupied=exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK))
                return False
            acquired = True
            self._owner = stream
            return True
        except Exception:
            self._failure()
            return False
        finally:
            if stream is not None and not acquired:
                try:
                    stream.close()
                except Exception:
                    self._failure()

    def _release_owner(self) -> None:
        stream, self._owner = self._owner, None
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except Exception:
            self._failure()
        finally:
            try:
                stream.close()
            except Exception:
                self._failure()

    def _previous(self):
        try:
            with self._state_path.open("r", encoding="utf-8") as stream:
                raw = stream.read(16385)
        except FileNotFoundError:
            return None
        except Exception:
            self._failure()
            return "invalid"
        try:
            value = json.loads(raw) if len(raw) <= 16384 else None
            if (not isinstance(value, dict) or type(value.get("clean")) is not bool
                    or not isinstance(value.get("run_id"), str)
                    or not re.fullmatch(r"[a-f0-9]{32}", value["run_id"])
                    or type(value.get("pid")) is not int or value["pid"] <= 0
                    or type(value.get("ppid")) is not int or value["ppid"] < 0
                    or _label(value.get("version"), "") == ""
                    or _label(value.get("build"), "") == ""
                    or not isinstance(value.get("started_at"), str)
                    or len(value["started_at"]) > 64
                    or (value["clean"] and (value.get("exit_code") != 0
                                            or not isinstance(value.get("ended_at"), str)
                                            or len(value["ended_at"]) > 64))
                    or (value.get("exit_code") is not None
                        and (type(value["exit_code"]) is not int
                             or not -(2 ** 31) <= value["exit_code"] <= 2 ** 32 - 1))):
                return "invalid"
            return value
        except Exception:
            return "invalid"

    def _write_state(self) -> None:
        candidate = self._state_path.with_name(f".lifecycle-state-{self._state['run_id']}.tmp")
        created = False
        try:
            with candidate.open("x", encoding="utf-8", newline="\n") as stream:
                created = True
                json.dump(self._state, stream, ensure_ascii=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(candidate, self._state_path)
        except Exception:
            self._failure()
        finally:
            try:
                if created:
                    candidate.unlink(missing_ok=True)
            except Exception:
                self._failure()

    def _record(self, event: str, **fields) -> None:
        with self._lock:
            if self.closed or self._log is None:
                return
            try:
                record = {"time": _now(), "run_id": self._state["run_id"], "event": event, **fields}
                if "launch_id" in self._state:
                    record["launch_id"] = self._state["launch_id"]
                self._log.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")
                self._log.flush()
            except Exception:
                self._failure()

    def emit(self, event: str, **safe_fields) -> None:
        if isinstance(event, str) and event in _EVENTS:
            self._record(event, **_fields(safe_fields))

    def exception(self, where: str, exc: BaseException, *, unhandled: bool = False) -> None:
        try:
            frames = []
            tb = exc.__traceback__
            # Walk only bounded frame metadata, without formatting source lines or values.
            while tb is not None:
                code = tb.tb_frame.f_code
                filename = code.co_filename.replace("\\", "/").rsplit("/", 1)[-1]
                frames.append({"file": filename[:96], "function": _label(code.co_name),
                               "line": tb.tb_lineno})
                if len(frames) > 16:
                    frames.pop(0)
                tb = tb.tb_next
            if unhandled:
                self.had_unhandled_exception = True
            self._record("thread_exception" if unhandled else "exception",
                         source=_label(where), exception_type=_label(type(exc).__name__), frames=frames)
        except Exception:
            self._failure()

    def _install_hooks(self) -> None:
        self._old_thread_hook = threading.excepthook

        def thread_hook(args):
            try:
                self.exception("thread", args.exc_value, unhandled=True)
            finally:
                self._old_thread_hook(args)

        self._thread_hook = thread_hook
        threading.excepthook = thread_hook
        try:
            # Preserve an existing faulthandler destination/owner. No timed probes.
            if not faulthandler.is_enabled():
                self._fault = (self._state_path.parent / "lifecycle-fault.log").open("ab")
                faulthandler.enable(file=self._fault, all_threads=True)
        except Exception:
            self._failure()
            if self._fault is not None:
                try:
                    self._fault.close()
                except Exception:
                    self._failure()
                self._fault = None

    def finish(self, *, clean: bool, exit_code: int) -> None:
        with self._lock:
            if self.closed:
                return
            if not self.enabled:
                self.closed = True
                self._release_owner()
                return
            code = _fields({"exit_code": exit_code}).get("exit_code", 1)
            final_clean = bool(clean) and not self.had_unhandled_exception and code == 0
            self._record("app_end", clean=final_clean, exit_code=code)
            self._state.update(clean=final_clean, exit_code=code, ended_at=_now())
            self._write_state()
            self.closed = True
            if threading.excepthook is self._thread_hook:
                threading.excepthook = self._old_thread_hook
            if self._fault is not None:
                try:
                    faulthandler.disable()
                    self._fault.close()
                except Exception:
                    self._failure()
                self._fault = None
            if self._log is not None:
                try:
                    self._log.close()
                except Exception:
                    self._failure()
                self._log = None
            self.enabled = False
            self._release_owner()


def start(data_dir: Path, *, version: str, build: str) -> LifecycleRecorder:
    global _recorder
    if _recorder is None or _recorder.closed:
        _recorder = LifecycleRecorder(data_dir, version=version, build=build)
    return _recorder


def emit(event: str, **safe_fields) -> None:
    if _recorder is not None:
        _recorder.emit(event, **safe_fields)


def exception(where: str, exc: BaseException) -> None:
    if _recorder is not None:
        _recorder.exception(where, exc)


def finish(*, clean: bool, exit_code: int) -> None:
    if _recorder is not None:
        _recorder.finish(clean=clean, exit_code=exit_code)
