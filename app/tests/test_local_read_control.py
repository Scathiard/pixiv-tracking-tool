"""Real SQLite work and localhost disconnects; no account or user library."""
import asyncio
from contextlib import contextmanager
import socket
import re
from threading import Event, Thread
from time import monotonic, sleep

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import text
import uvicorn

from src.core.database import ArchiveDatabase
from src.core.read_control import ReadBudget, ReadCancelled, ReadDeadline, current_read, run_read


LONG_SQL = text("WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<1000000000) SELECT sum(x) FROM n")


@pytest.fixture
def database(tmp_path):
    value = ArchiveDatabase.create(tmp_path / "reads.db")
    yield value
    value.close()


def long_read(database, started=None):
    with database.read() as session:
        if started:
            started.set()
        return session.scalar(LONG_SQL)


def test_sql_deadline_releases_and_old_cancel_cannot_abort_next_borrower(database):
    budget = ReadBudget(.08, "frontend")
    token = current_read.set(budget)
    began = monotonic()
    try:
        with pytest.raises(ReadDeadline):
            long_read(database)
    finally:
        current_read.reset(token)
    assert monotonic() - began < 1
    assert database.engine.pool.checkedout() == 0
    with database.read() as session:
        budget.cancel()
        assert session.scalar(text("SELECT 42")) == 42


def test_writer_succeeds_while_every_read_connection_is_borrowed(database):
    connections = [database.engine.connect() for _ in range(7)]
    try:
        began = monotonic()
        with database.write() as session:
            assert session.scalar(text("SELECT 42")) == 42
        assert monotonic() - began < .5
        assert database.writer_engine.pool.checkedout() == 0
    finally:
        for connection in connections:
            connection.close()


def test_failed_read_records_stage_cost_without_query_or_exception_text(database, caplog):
    def fail():
        with database.read() as session:
            driver = session.connection().connection.driver_connection
            # Python 3.12 monotonic on Windows may advance in ~16 ms ticks.
            driver.create_function("isolated_delay", 1, lambda _: (sleep(.04), 42)[1])
            assert session.scalar(text("SELECT isolated_delay('private-query-value')")) == 42
        raise ValueError("private-token-and-query")
    async def scenario():
        with pytest.raises(ValueError):
            await database.reads.run(None, fail, label="library")
    asyncio.run(scenario())
    record = next(item.message for item in caplog.records if "outcome=read_error" in item.message)
    assert "endpoint=library" in record and "lane=frontend" in record
    timings = dict(re.findall(r"(\w+_ms)=([\d.]+)", record))
    assert set(timings) == {"total_ms", "queue_ms", "pool_ms", "database_ms", "execute_ms"}
    assert float(timings["database_ms"]) > 0 and float(timings["execute_ms"]) > 0
    assert "isolated_delay" not in record and str(database.path) not in record
    assert "private-query-value" not in record and "private-token-and-query" not in record


def test_queued_timeout_is_logged_as_queue_cost(database, caplog):
    async def scenario():
        # No thread or SQL starts when admission never becomes available.
        database.reads.capacities["frontend"] = 0
        with pytest.raises(ReadDeadline):
            await database.reads.run(None, lambda: pytest.fail("queued work must not run"),
                                     timeout=.03, label="library")
    asyncio.run(scenario())
    record = next(item.message for item in caplog.records if "outcome=read_timeout" in item.message)
    assert "database_ms=0.0" in record and "execute_ms=0.0" in record and "statements=0" in record
    timings = dict(re.findall(r"(\w+_ms)=([\d.]+)", record))
    assert float(timings["queue_ms"]) > 0


def test_consumer_deadline_interrupt_is_not_silenced_as_navigation_cancel(database, caplog):
    entered = Event()
    def wait_for_interrupt():
        budget = current_read.get()
        entered.set()
        assert budget.cancelled.wait(2)
        budget.checkpoint()
    async def scenario():
        with pytest.raises(ReadDeadline):
            await database.reads.run(None, wait_for_interrupt, timeout=.5, label="library")
    asyncio.run(scenario())
    assert entered.is_set()
    messages = [item.message for item in caplog.records if "endpoint=library" in item.message]
    assert len(messages) == 1 and "outcome=read_timeout" in messages[0]
    assert database.reads.active["frontend"] == 0


def test_nested_writer_must_pass_existing_session(database):
    with database.write():
        with pytest.raises(RuntimeError, match="嵌套"):
            with database.write():
                pass


def test_maintenance_read_slots_keep_frontend_and_writer_available(database):
    release, first, second = Event(), Event(), Event()
    def maintenance(entered):
        with database.read() as session:
            session.scalar(text("SELECT 1"))
            entered.set()
            release.wait(2)
    threads = [Thread(target=maintenance, args=(entered,)) for entered in (first, second)]
    for thread in threads:
        thread.start()
    try:
        assert first.wait(1) and second.wait(1)
        assert database.reads.active["background"] == 2
        def view():
            with database.read() as session:
                return session.scalar(text("SELECT 42"))
        async def mixed():
            values = await asyncio.gather(*[database.reads.run(None, view,
                lane="logs" if index % 5 == 0 else "frontend") for index in range(40)])
            assert values == [42] * 40
        began = monotonic()
        asyncio.run(mixed())
        with database.write() as session:
            assert session.scalar(text("SELECT 42")) == 42
        assert monotonic() - began < 1
    finally:
        release.set()
        for thread in threads:
            thread.join(2)
        assert all(not thread.is_alive() for thread in threads)


def test_request_cancel_never_interrupts_accepted_writer(database):
    budget = ReadBudget(.01, "frontend")
    token = current_read.set(budget)
    try:
        with database.write() as session:
            budget.cancel()
            assert session.scalar(text("SELECT 42")) == 42
    finally:
        current_read.reset(token)


def test_queue_wait_counts_against_one_deadline_and_does_not_start_worker(database):
    async def exercise():
        entered, release, queued_called = Event(), Event(), Event()
        database.reads.capacities["frontend"] = 1
        def hold():
            entered.set()
            release.wait(2)
            return 42
        first = asyncio.create_task(database.reads.run(None, hold, timeout=2))
        while not entered.is_set():
            await asyncio.sleep(.005)
        began = monotonic()
        with pytest.raises(ReadDeadline):
            await database.reads.run(None, lambda: queued_called.set(), timeout=.08)
        assert monotonic() - began < .5
        assert not queued_called.is_set()
        assert database.reads.active["frontend"] == 1
        release.set()
        assert await first == 42
        assert database.reads.active["frontend"] == 0
        assert database.reads.pending["frontend"] == 0
    asyncio.run(exercise())


def test_shared_read_survives_one_consumer_and_cancels_after_last(database):
    async def exercise():
        began, finished = Event(), Event()
        calls = []
        def work():
            calls.append(1)
            began.set()
            try:
                return long_read(database)
            finally:
                finished.set()
        first = asyncio.create_task(database.reads.run(None, work, lane="logs", key="same"))
        second = asyncio.create_task(database.reads.run(None, work, lane="logs", key="same"))
        while not began.is_set():
            await asyncio.sleep(.005)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.sleep(.04)
        assert not finished.is_set() and len(calls) == 1
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        assert finished.is_set()
        assert database.reads.active["logs"] == 0
        assert database.engine.pool.checkedout() == 0
    asyncio.run(exercise())


@contextmanager
def server_for(app):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    until = monotonic() + 5
    while not server.started and monotonic() < until:
        sleep(.01)
    assert server.started
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(5)
        listener.close()
        assert not thread.is_alive()


def test_real_http_disconnect_interrupts_sql_and_reopen_returns_content(database):
    app = FastAPI()
    started, finished = Event(), Event()
    @app.get("/slow")
    async def slow(request: Request):
        def work():
            try:
                return long_read(database, started)
            finally:
                finished.set()
        return await run_read(request, database, work)
    @app.get("/content")
    async def content(request: Request):
        def work():
            with database.read() as session:
                return {"value": session.scalar(text("SELECT 42"))}
        return await run_read(request, database, work)
    with server_for(app) as port:
        client = socket.create_connection(("127.0.0.1", port))
        client.sendall(b"GET /slow HTTP/1.1\r\nHost: localhost\r\n\r\n")
        assert started.wait(2)
        began = monotonic()
        client.close()
        assert finished.wait(1)
        assert monotonic() - began < 1
        response = httpx.get(f"http://127.0.0.1:{port}/content", timeout=2)
        assert response.status_code == 200 and response.json() == {"value": 42}
        assert database.engine.pool.checkedout() == 0
        assert all(value == 0 for value in database.reads.active.values())


def test_real_http_queued_disconnect_starts_no_worker_and_deadline_is_safe(database):
    app = FastAPI()
    hold_started, release, unexpected = Event(), Event(), Event()
    database.reads.capacities["frontend"] = 1
    @app.get("/hold")
    async def hold(request: Request):
        def work():
            hold_started.set()
            release.wait(2)
            return {"held": True}
        return await run_read(request, database, work)
    @app.get("/queued")
    async def queued(request: Request):
        return await run_read(request, database, lambda: unexpected.set())
    @app.get("/deadline")
    async def deadline(request: Request):
        return await run_read(request, database, lambda: long_read(database), timeout=.08)
    with server_for(app) as port:
        holder = socket.create_connection(("127.0.0.1", port))
        pending = socket.create_connection(("127.0.0.1", port))
        try:
            holder.sendall(b"GET /hold HTTP/1.1\r\nHost: localhost\r\n\r\n")
            assert hold_started.wait(1)
            pending.sendall(b"GET /queued HTTP/1.1\r\nHost: localhost\r\n\r\n")
            until = monotonic() + 1
            while not database.reads.pending["frontend"] and monotonic() < until:
                sleep(.01)
            assert database.reads.pending["frontend"] == 1
            pending.close()
            until = monotonic() + 1
            while database.reads.pending["frontend"] and monotonic() < until:
                sleep(.01)
            assert database.reads.pending["frontend"] == 0
            assert not unexpected.is_set()
        finally:
            release.set()
            holder.close()
            pending.close()
        response = httpx.get(f"http://127.0.0.1:{port}/deadline", timeout=2)
        assert response.status_code == 504
        assert response.json()["detail"] == {"code": "read_timeout", "message": "本地读取超时，请重试"}
        assert database.engine.pool.checkedout() == 0
