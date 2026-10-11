"""Ordinary log preservation and timestamps; synthetic files only."""
from datetime import datetime
import logging
import re
import sys
import threading
import traceback

import pytest

from src.core.stdio_log import open_stdio_log


PREFIX = re.compile(r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2})\] (.*)$")


def messages(path):
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = PREFIX.fullmatch(line)
        assert match is not None, line
        assert datetime.fromisoformat(match[1]).utcoffset() is not None
        result.append(match[2])
    return result


def test_redirect_preserves_large_existing_log_and_appends_across_restarts(tmp_path, monkeypatch):
    from src import tray

    path = tmp_path / "tracker.log"
    original = b"historical-log\n" + b"x" * 1_048_577
    path.write_bytes(original)
    for message in ("first startup", "second startup"):
        with monkeypatch.context() as context:
            context.setattr(sys, "stdout", sys.stdout)
            context.setattr(sys, "stderr", sys.stderr)
            tray._redirect_stdio(tmp_path)
            assert sys.stdout is sys.stderr
            stream = sys.stdout
            try:
                print(message)
                stream.flush()
            finally:
                stream.close()
        assert path.read_bytes().startswith(original)
    appended = path.read_bytes()[len(original):].decode("utf-8")
    assert appended.startswith("\n")
    lines = appended.splitlines()[1:]
    assert [PREFIX.fullmatch(line)[2] for line in lines] == ["first startup", "second startup"]


def test_split_print_blank_lines_traceback_and_logging_are_timestamped(tmp_path):
    path = tmp_path / "tracker.log"
    with open_stdio_log(path) as stream:
        assert stream.write("split") == 5
        assert stream.write(" output") == 7
        assert stream.write("\n\n") == 2
        assert stream.write("") == 0
        print("中文", "message", file=stream)
        try:
            raise RuntimeError("synthetic error")
        except RuntimeError as error:
            traceback.print_exception(error, file=stream)
        logger = logging.Logger("synthetic-stdio")
        logger.addHandler(logging.StreamHandler(stream))
        logger.warning("logged message")
        assert stream.encoding == "utf-8"
        assert stream.line_buffering
        assert stream.writable() and not stream.isatty()
        assert isinstance(stream.fileno(), int)
        with pytest.raises(TypeError):
            stream.write(b"bytes")
    lines = messages(path)
    assert lines[:3] == ["split output", "", "中文 message"]
    assert "Traceback (most recent call last):" in lines
    assert "RuntimeError: synthetic error" in lines
    assert lines[-1] == "logged message"


def test_flush_publishes_partial_output_and_close_drains_fragments(tmp_path):
    path = tmp_path / "tracker.log"
    stream = open_stdio_log(path)
    stream.write("partial")
    stream.flush()
    assert messages(path) == ["partial"]
    stream.write(" continued\n")
    assert messages(path) == ["partial continued"]
    stream.write("interrupted")
    stream.flush()
    thread = threading.Thread(target=lambda: (
        stream.write("other line\n"), stream.write("thread tail")
    ))
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    stream.write("resumed\n")
    stream.write("main tail")
    stream.close()
    stream.close()
    assert messages(path) == [
        "partial continued", "interrupted", "other line", "resumed",
        "thread tail", "main tail",
    ]
    with pytest.raises(ValueError):
        stream.write("closed")


def test_concurrent_split_writes_keep_whole_lines_and_valid_prefixes(tmp_path):
    path = tmp_path / "tracker.log"
    barrier = threading.Barrier(5)
    failures = []
    with open_stdio_log(path) as stream:
        def writer(index):
            try:
                stream.write(f"thread-{index}")
                barrier.wait(timeout=2)
                barrier.wait(timeout=2)
                stream.write(" complete\n")
                # A flushed fragment must not swallow another thread's prefix.
                stream.write(f"tail-{index}")
                stream.flush()
            except Exception as error:
                failures.append(error)

        threads = [threading.Thread(target=writer, args=(index,)) for index in range(4)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=2)
        logger = logging.Logger("synthetic-concurrent-stdio")
        logger.addHandler(logging.StreamHandler(stream))
        logger.warning("main message")
        barrier.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=2)
            assert not thread.is_alive()
    assert failures == []
    assert sorted(messages(path)) == sorted(
        ["main message"]
        + [f"thread-{index} complete" for index in range(4)]
        + [f"tail-{index}" for index in range(4)]
    )
