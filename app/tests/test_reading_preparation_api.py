"""Preparation waits must leave the event loop and frontend read slots usable."""
import asyncio
from concurrent.futures import Future
from threading import Event

import httpx
from fastapi import FastAPI

from src.core.models import Artist, Work
from src.core.runtime import Runtime
from src.routes.archive_api import create_router


def test_startup_and_waiting_browsers_do_not_block_library(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path / "archive.db", tmp_path / "images", create=True)
    with runtime.database.write() as session:
        artist = Artist(id=1, pixiv_user_id="10", name="Synthetic")
        session.add(Work(id=1, pixiv_illust_id="100", artist=artist, title="Synthetic work", page_count=3))
    entered, release = Event(), Event()
    original_basis = runtime.library.reading_basis
    original_prepare = runtime.reading.prepare
    consumers = []

    def held_basis(ids=None):
        entered.set()
        assert release.wait(10)
        return original_basis(ids)

    def observe_prepare(*args, **kwargs):
        result = original_prepare(*args, **kwargs)
        consumers.append(result)
        return result

    monkeypatch.setattr(runtime.library, "reading_basis", held_basis)
    monkeypatch.setattr(runtime.reading, "prepare", observe_prepare)
    app = FastAPI()
    app.include_router(create_router(lambda: runtime))

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            pending = [asyncio.create_task(client.post("/api/reading-sessions")) for _ in range(4)]
            try:
                async def admitted():
                    while len(consumers) != 4:
                        await asyncio.sleep(.005)
                await asyncio.wait_for(admitted(), 3)
                assert runtime.database.reads.active["frontend"] == 0
                assert runtime.database.reads.pending["frontend"] == 0
                response = await asyncio.wait_for(client.get("/api/library?limit=1"), 3)
                assert response.status_code == 200 and response.json()["items"][0]["id"] == 1
                pending[0].cancel()
                await asyncio.gather(pending[0], return_exceptions=True)
                assert consumers[0].cancelled()
                assert not consumers[1].done()
                release.set()
                responses = await asyncio.wait_for(asyncio.gather(*pending[1:]), 10)
                assert all(item.status_code == 200 for item in responses)
                visits = [item.json() for item in responses]
                assert len({item["session_id"] for item in visits}) == 3
                assert all(item["total"] == 1 and item["page_total"] == 3 for item in visits)
                token = visits[-1]["session_id"]
                located = await client.get(f"/api/reading-sessions/{token}/locate?work_id=1&mode=random")
                assert located.status_code == 200 and located.json()["work_id"] == 1
            finally:
                release.set()
                for task in pending:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

    try:
        runtime.start()
        assert entered.wait(5)  # Startup returned while the shared scan is held.
        asyncio.run(exercise())
    finally:
        release.set()
        assert runtime.close(timeout=10)


def test_preparation_failure_is_explicit_and_filter_validation_is_preserved(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path / "archive.db", tmp_path / "images", create=True)
    app = FastAPI()
    app.include_router(create_router(lambda: runtime))

    def failed(*_args, **_kwargs):
        future = Future()
        future.set_exception(RuntimeError("synthetic failure"))
        return future

    monkeypatch.setattr(runtime.reading, "prepare", failed)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            invalid = await client.post("/api/reading-sessions?period=invalid")
            assert invalid.status_code == 422
            response = await client.post("/api/reading-sessions")
            assert response.status_code == 503 and response.headers["retry-after"] == "1"
            assert response.json()["detail"]["code"] == "reading_prepare_failed"
    try:
        asyncio.run(exercise())
    finally:
        assert runtime.close(timeout=10)
