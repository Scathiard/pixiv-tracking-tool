"""Lifecycle evidence tests use only pytest's isolated synthetic directories."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from src.core import lifecycle


@pytest.fixture(autouse=True)
def isolated_recorder():
    lifecycle.finish(clean=False, exit_code=1)
    lifecycle._recorder = None
    yield
    lifecycle.finish(clean=False, exit_code=1)
    lifecycle._recorder = None


def records(directory):
    return [json.loads(line) for line in (directory / "lifecycle.jsonl").read_text("utf-8").splitlines()]


def test_clean_reopen_appends_and_finish_is_idempotent(tmp_path, monkeypatch):
    launch_id = "04a2a127-5396-4ad4-89e9-7c55a51cbd86"
    monkeypatch.setenv("PIXIV_LAUNCH_ID", launch_id)
    lifecycle.emit("service_start", source="before_start")
    first = lifecycle.start(tmp_path, version="0.2.0", build="test.1")
    initial_state = json.loads((tmp_path / "lifecycle-state.json").read_text("utf-8"))
    assert initial_state["clean"] is False
    assert initial_state["launch_id"] == launch_id
    lifecycle.emit("service_ready", port=1919, offline=True)
    lifecycle.finish(clean=True, exit_code=0)
    lifecycle.finish(clean=False, exit_code=99)
    state = json.loads((tmp_path / "lifecycle-state.json").read_text("utf-8"))
    assert state["clean"] is True and state["exit_code"] == 0
    before = (tmp_path / "lifecycle.jsonl").read_bytes()
    assert all(row["launch_id"] == launch_id for row in records(tmp_path))
    monkeypatch.setenv("PIXIV_LAUNCH_ID", "private-invalid-id")
    second = lifecycle.start(tmp_path, version="0.2.0", build="test.2")
    lifecycle.finish(clean=True, exit_code=0)
    assert first is not second
    assert (tmp_path / "lifecycle.jsonl").read_bytes().startswith(before)
    events = [record["event"] for record in records(tmp_path)]
    assert events.count("previous_unknown") == 1
    assert "previous_unclean" not in events
    assert events.count("app_end") == 2
    assert "launch_id" not in records(tmp_path)[-1]
    assert "launch_id" not in json.loads((tmp_path / "lifecycle-state.json").read_text("utf-8"))


def test_process_without_finish_is_identified_on_next_start(tmp_path):
    script = (
        "import sys; from pathlib import Path; from src.core import lifecycle; "
        "lifecycle.start(Path(sys.argv[1]), version='0.2.0', build='child.1')"
    )
    child = subprocess.run([sys.executable, "-c", script, str(tmp_path)],
                           cwd=Path(__file__).resolve().parents[1],
                           env=os.environ.copy(), capture_output=True, timeout=15)
    assert child.returncode == 0, child.stderr
    old = json.loads((tmp_path / "lifecycle-state.json").read_text("utf-8"))
    assert old["clean"] is False
    lifecycle.start(tmp_path, version="0.2.0", build="parent.1")
    lifecycle.finish(clean=True, exit_code=0)
    prior = [record for record in records(tmp_path) if record["event"] == "previous_unclean"]
    assert len(prior) == 1 and prior[0]["previous_run_id"] == old["run_id"]
    assert prior[0]["previous_pid"] == old["pid"]


def test_concurrent_recorder_cannot_overwrite_owner_marker(tmp_path):
    owner = lifecycle.start(tmp_path, version="0.2.0", build="owner.1")
    assert owner.enabled
    state_before = (tmp_path / "lifecycle-state.json").read_bytes()
    log_before = (tmp_path / "lifecycle.jsonl").read_bytes()
    script = (
        "import sys, threading; from pathlib import Path; from src.core import lifecycle; "
        "hook = threading.excepthook; "
        "recorder = lifecycle.start(Path(sys.argv[1]), version='0.2.0', build='other.1'); "
        "print(recorder.enabled); "
        "assert recorder.enabled or threading.excepthook is hook; "
        "lifecycle.emit('service_ready', port=1919); lifecycle.finish(clean=True, exit_code=0); "
        "assert threading.excepthook is hook"
    )
    command = [sys.executable, "-c", script, str(tmp_path)]
    child = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                           env=os.environ.copy(), capture_output=True, timeout=15)
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == b"False"
    assert b"another recorder owns this data directory" in child.stderr
    assert (tmp_path / "lifecycle-state.json").read_bytes() == state_before
    assert (tmp_path / "lifecycle.jsonl").read_bytes() == log_before
    lifecycle.finish(clean=True, exit_code=0)
    child = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                           env=os.environ.copy(), capture_output=True, timeout=15)
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == b"True"
    state = json.loads((tmp_path / "lifecycle-state.json").read_text("utf-8"))
    assert state["build"] == "other.1" and state["clean"] is True


def test_corrupt_marker_is_unknown_instead_of_clean(tmp_path):
    (tmp_path / "lifecycle-state.json").write_text('{"clean":true}', encoding="utf-8")
    lifecycle.start(tmp_path, version="0.2.0", build="test.1")
    lifecycle.finish(clean=True, exit_code=0)
    events = records(tmp_path)
    assert any(row["event"] == "state_warning" for row in events)
    assert any(row["event"] == "previous_unknown" and row["outcome"] == "invalid_marker" for row in events)
    assert not any(row["event"] == "previous_unclean" for row in events)


def test_exception_fields_are_bounded_private_and_hooks_restored(tmp_path, monkeypatch):
    old_calls = []
    old_hook = lambda args: old_calls.append(args.exc_type)
    monkeypatch.setattr(threading, "excepthook", old_hook)
    recorder = lifecycle.start(tmp_path, version="0.2.0", build="test.1")
    lifecycle.emit("service_ready", source="web.start", port=1919, expected=True,
                   token="private-token", outcome="https://secret.example", account={"secret": 1})
    lifecycle.emit("secret-event-with-payload")

    def nested(depth):
        if depth:
            return nested(depth - 1)
        secret_local = "private-local"
        raise ValueError("private-exception-text", secret_local)

    try:
        nested(24)
    except ValueError as exc:
        lifecycle.exception("test.exception", exc)
    worker = threading.Thread(target=lambda: nested(1))
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert old_calls == [ValueError]
    lifecycle.finish(clean=True, exit_code=0)
    assert threading.excepthook is old_hook
    assert recorder.had_unhandled_exception is True
    assert json.loads((tmp_path / "lifecycle-state.json").read_text("utf-8"))["clean"] is False
    text = (tmp_path / "lifecycle.jsonl").read_text("utf-8")
    assert not any(value in text for value in ("private-token", "private-local", "private-exception-text", "secret.example"))
    failures = [row for row in records(tmp_path) if row["event"] in ("exception", "thread_exception")]
    assert {row["event"] for row in failures} == {"exception", "thread_exception"}
    assert failures[0]["exception_type"] == "ValueError"
    assert len(failures[0]["frames"]) == 16
    assert all(set(frame) == {"file", "function", "line"} for row in failures for frame in row["frames"])
    assert all("/" not in frame["file"] and "\\" not in frame["file"] for row in failures for frame in row["frames"])


def test_storage_failure_warns_once_and_does_not_break_application(tmp_path, monkeypatch, capsys):
    def deny_replace(*args):
        raise PermissionError("private-path-and-error")

    monkeypatch.setattr(lifecycle.os, "replace", deny_replace)
    old_hook = threading.excepthook
    recorder = lifecycle.start(tmp_path, version="0.2.0", build="test.1")
    lifecycle.emit("service_ready", port=1919)
    lifecycle.finish(clean=True, exit_code=0)
    stderr = capsys.readouterr().err
    assert recorder.failures == 2
    assert stderr.count("lifecycle recording unavailable") == 1
    assert "private-path-and-error" not in stderr
    assert threading.excepthook is old_hook
    assert not list(tmp_path.glob(".lifecycle-state-*.tmp"))
    assert records(tmp_path)[-1]["event"] == "app_end"
    # A log-open failure after taking ownership must release the owner lock and
    # must not install hooks or modify the existing marker.
    state_before = (tmp_path / "lifecycle-state.json").read_bytes() if (tmp_path / "lifecycle-state.json").exists() else None
    original_open = Path.open

    def deny_log_open(path, *args, **kwargs):
        if path.name == "lifecycle.jsonl":
            raise PermissionError("private-log-open-error")
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(Path, "open", deny_log_open)
        failed = lifecycle.start(tmp_path, version="0.2.0", build="failed.1")
        assert not failed.enabled and failed._owner is None
        assert threading.excepthook is old_hook
        lifecycle.finish(clean=True, exit_code=0)
    state_after = (tmp_path / "lifecycle-state.json").read_bytes() if (tmp_path / "lifecycle-state.json").exists() else None
    assert state_after == state_before
    reopened = lifecycle.start(tmp_path, version="0.2.0", build="reopen.1")
    assert reopened.enabled
    lifecycle.finish(clean=True, exit_code=0)
