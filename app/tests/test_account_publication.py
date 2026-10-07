"""Account publication barriers, with synthetic libraries and external sockets denied."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import socket
import threading

import pytest
import requests

from src import auth
from src.client import PixivClient
from src.core.models import Artist, ArchiveIntent, RemoteBookmark, Work, utcnow
from src.core.runtime import Runtime
from src.request_policy import AccountChanged


class SyntheticClient:
    def __init__(self, account):
        self.account = account

    def get_account_id(self):
        return self.account

    def identity_generation(self):
        return (0, self.account)


@pytest.fixture(autouse=True)
def isolated_credentials_and_network(tmp_path, monkeypatch):
    original_connect = socket.socket.connect
    original_socketpair = socket.socketpair
    self_pipe = threading.local()

    def local_socketpair(*args, **kwargs):
        # Windows asyncio uses a loopback TCP pair for its private wakeup pipe.
        # Permit only that synchronous stdlib setup, never application sockets.
        self_pipe.creating = True
        try:
            return original_socketpair(*args, **kwargs)
        finally:
            self_pipe.creating = False

    def forbidden(*args, **kwargs):
        pytest.fail("Account publication tests must never send network traffic")

    def guarded_connect(sock, address):
        if getattr(self_pipe, "creating", False) and address[0] == "127.0.0.1":
            return original_connect(sock, address)
        return forbidden()

    monkeypatch.setattr(socket, "socketpair", local_socketpair)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(auth, "_token_file", lambda: tmp_path / "synthetic-token.json")
    monkeypatch.setattr(auth, "_credential_generation", 0)
    monkeypatch.setattr(auth, "_login_generation", 0)
    monkeypatch.setattr(auth, "_refresh_flights", {})
    auth._cancel_event.clear()
    auth._save_token("synthetic-access", "synthetic-refresh", "10001")
    yield
    auth._cancel_event.clear()


def make_runtime(tmp_path, client):
    runtime = Runtime(tmp_path / "synthetic.db", tmp_path / "images", create=True, client=client)
    assert runtime.remote.get_account_id() == "10001"
    with runtime.database.write() as session:
        session.add(Artist(id=1, pixiv_user_id="30003", name="Synthetic artist"))
        session.flush()
        session.add(Work(id=1, pixiv_illust_id="40004", artist_id=1, title="Synthetic work", source_metadata={
            "remote_availability": {"state": "private", "confirmed": True, "policy": "source-v2",
                "account_id": "10001", "reason": "Synthetic A observation", "checked_at": "2026-10-04T00:00:00Z"}}))
        session.add(ArchiveIntent(pixiv_illust_id="40004", mode="keep"))
        session.flush()
        session.add(RemoteBookmark(work_id=1, value=True, source="readback", account_id="10001",
            version=1, confirmed_at=utcnow()))
    runtime.bookmarks._mark_fresh(1, 1, "10001")
    return runtime


def assert_b_context(runtime):
    assert runtime.client.account == "20002"
    assert runtime.account_id == runtime.library.account_id == "20002"
    assert runtime.bookmarks._account_context()[0] == "20002"
    bookmark = runtime.bookmarks.state(1)
    detail = runtime.library.detail(1)
    assert bookmark["current_account_id"] == "20002" and bookmark["cached"] is True
    assert detail["bookmark"]["current_account_id"] == "20002"
    assert detail["bookmark"]["cached"] is True
    assert detail["availability"]["effective"] is False
    assert detail["availability"]["confirmed"] is False


def test_old_validated_account_cannot_overwrite_new_publication(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path, SyntheticClient("10001"))
    validated, resume = threading.Event(), threading.Event()
    original = runtime.publish_account

    def publication_barrier(expected, account):
        if threading.current_thread().name == "old-account-A":
            # Precisely reproduce the former final-check/publication gap.
            runtime.remote.assert_generation(expected)
            validated.set()
            assert resume.wait(10)
        return original(expected, account)

    monkeypatch.setattr(runtime, "publish_account", publication_barrier)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="old-account-A") as pool:
            # Exact thread name keeps the publication barrier isolated to A.
            def old_call():
                threading.current_thread().name = "old-account-A"
                return runtime.remote.get_account_id()
            pending = pool.submit(old_call)
            try:
                assert validated.wait(10)
                runtime.client = SyntheticClient("20002")
                assert runtime.remote.get_account_id() == "20002"
                before = runtime.bookmarks._account_context()
                assert_b_context(runtime)
            finally:
                resume.set()
            with pytest.raises(AccountChanged):
                pending.result(timeout=10)
        assert_b_context(runtime)
        assert runtime.bookmarks._account_context() == before
    finally:
        resume.set()
        assert runtime.close()


def test_client_replacement_invalidates_account_and_old_freshness(tmp_path, monkeypatch):
    client_a = SyntheticClient("10001")
    runtime = make_runtime(tmp_path, client_a)
    original = runtime.bookmarks.mark_disconnected
    cleanup_calls = []

    def memory_cleanup():
        assert not runtime._client_lock.locked()
        assert not auth._token_lock._is_owned()
        cleanup_calls.append(True)
        original()

    monkeypatch.setattr(runtime.bookmarks, "mark_disconnected", memory_cleanup)
    try:
        assert runtime.bookmarks.state(1)["cached"] is False
        runtime.client = SyntheticClient("20002")
        assert runtime.account_id is None and runtime.library.account_id is None
        bookmark = runtime.bookmarks.state(1)
        assert bookmark["current_account_id"] is None and bookmark["cached"] is True
        assert runtime.library.detail(1)["availability"]["cached"] is True
        runtime.client = client_a
        assert runtime.remote.get_account_id() == "10001"
        # Reusing an account/client cannot revive its previous connection's
        # marker; a new remote observation must establish freshness again.
        assert runtime.bookmarks.state(1)["cached"] is True
        assert runtime.library.detail(1)["bookmark"]["cached"] is True
        assert len(cleanup_calls) == 3
    finally:
        assert runtime.close()


@pytest.mark.parametrize("return_to_a", [False, True])
def test_real_client_late_account_rejected_after_new_login(tmp_path, monkeypatch, return_to_a):
    runtime = make_runtime(tmp_path, PixivClient())
    validated, resume = threading.Event(), threading.Event()
    original = runtime.publish_account

    def publication_barrier(expected, account):
        if threading.current_thread().name == "old-login-A":
            runtime.remote.assert_generation(expected)
            validated.set()
            assert resume.wait(10)
        return original(expected, account)

    monkeypatch.setattr(runtime, "publish_account", publication_barrier)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            def old_call():
                threading.current_thread().name = "old-login-A"
                return runtime.remote.get_account_id()
            pending = pool.submit(old_call)
            try:
                assert validated.wait(10)
                auth._save_token("synthetic-later-access", "synthetic-later-refresh", "20002")
                assert runtime.remote.get_account_id() == "20002"
                assert runtime.account_id == runtime.library.account_id == "20002"
                assert runtime.bookmarks.state(1)["cached"] is True
                assert runtime.library.detail(1)["availability"]["effective"] is False
                if return_to_a:
                    auth._save_token("synthetic-return-access", "synthetic-return-refresh", "10001")
                    assert runtime.remote.get_account_id() == "10001"
                    assert runtime.bookmarks.state(1)["cached"] is True
                before = runtime.bookmarks._account_context()
            finally:
                resume.set()
            with pytest.raises(AccountChanged):
                pending.result(timeout=10)
        assert runtime.account_id == runtime.library.account_id == ("10001" if return_to_a else "20002")
        assert runtime.bookmarks._account_context() == before
    finally:
        resume.set()
        assert runtime.close()


def test_displaced_disconnect_preserves_later_account(tmp_path):
    client_a = SyntheticClient("10001")
    runtime = make_runtime(tmp_path, client_a)
    try:
        runtime.client = SyntheticClient("20002")
        assert runtime.remote.get_account_id() == "20002"
        assert runtime.disconnect_client(client_a) is False
        assert_b_context(runtime)
        current = runtime.client
        assert runtime.disconnect_client(current) is True
        assert runtime.client is None
        assert runtime.account_id is None and runtime.library.account_id is None
    finally:
        assert runtime.close()


def test_same_client_same_account_new_login_drops_previous_freshness(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path, PixivClient())
    original = runtime.bookmarks.mark_disconnected
    cleanups = []

    def cleanup_outside_publication_locks():
        assert not runtime._client_lock.locked()
        assert not auth._token_lock._is_owned()
        cleanups.append(True)
        original()

    monkeypatch.setattr(runtime.bookmarks, "mark_disconnected", cleanup_outside_publication_locks)
    try:
        assert runtime.bookmarks.state(1)["cached"] is False
        assert runtime.remote.get_account_id() == "10001"
        assert cleanups == []
        auth._save_token("synthetic-new-login", "synthetic-new-refresh", "10001")
        assert runtime.remote.get_account_id() == "10001"
        assert runtime.bookmarks.state(1)["cached"] is True
        assert cleanups == [True]
    finally:
        assert runtime.close()


@pytest.mark.parametrize("blocked_boundary", ["token_read", "token_lock"])
def test_credential_wait_never_holds_runtime_client_lock(tmp_path, monkeypatch, blocked_boundary):
    runtime = make_runtime(tmp_path, PixivClient())
    expected = runtime.remote.generation_context()
    entered, release = threading.Event(), threading.Event()
    original_load = auth._load_token

    def blocked_load():
        entered.set()
        assert release.wait(10)
        return original_load()

    def hold_token():
        with auth._token_lock:
            entered.set()
            assert release.wait(10)

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            holder = None
            if blocked_boundary == "token_read":
                monkeypatch.setattr(auth, "_load_token", blocked_load)
            else:
                holder = pool.submit(hold_token)
                assert entered.wait(10)
            publication = pool.submit(runtime.publish_account, expected, "10001")
            try:
                assert entered.wait(10)
                assert not publication.done()
                assert pool.submit(lambda: runtime.client).result(timeout=1) is not None
                detail = pool.submit(runtime.library.detail, 1).result(timeout=1)
                assert detail["id"] == 1
                assert not runtime._client_lock.locked()
            finally:
                release.set()
            if holder is not None:
                holder.result(timeout=10)
            assert publication.result(timeout=10) == "10001"
    finally:
        release.set()
        assert runtime.close()


def test_web_failure_disconnect_is_a_compare_and_clear(tmp_path, monkeypatch):
    from src import web
    runtime = make_runtime(tmp_path, SyntheticClient("10001"))
    runtime.client = None
    later = SyntheticClient("20002")

    class FailedClient:
        def get_account_id(self):
            raise RuntimeError("Synthetic connection failure")

    original_disconnect = runtime.disconnect_client

    def replaced_before_clear(expected_client):
        runtime.client = later
        assert runtime.remote.get_account_id() == "20002"
        return original_disconnect(expected_client)

    monkeypatch.setattr(runtime, "disconnect_client", replaced_before_clear)
    monkeypatch.setattr(auth, "_try_refresh_token", lambda: True)
    monkeypatch.setattr(web, "PixivClient", FailedClient)
    monkeypatch.setattr(web, "_shutdown_requested", threading.Event())
    application = web.create_app(lambda: runtime, authenticate=True, schedule=False)

    async def exercise():
        async with application.router.lifespan_context(application):
            await application.state.auth_task
            assert_b_context(runtime)
            assert application.state.login_error is None
            assert application.state.auth_task.exception() is None

    asyncio.run(exercise())
