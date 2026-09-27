import pytest

from src import auth, client as client_module
from src.client import PixivClient, PixivRequestError


class Response:
    def __init__(self, status, body):
        self.status_code = status
        self.body = body
        self.headers = {}

    def json(self):
        return self.body


def test_bound_write_sends_once_and_uses_matching_token(monkeypatch):
    client = PixivClient()
    monkeypatch.setattr(auth, "_load_token", lambda: {
        "user_id": "123", "access_token": "synthetic-token"})
    monkeypatch.setattr(client, "_throttle", lambda: None)
    calls = []

    def send(endpoint, headers, data):
        calls.append((endpoint, headers["Authorization"], data))
        raise ConnectionError("uncertain")

    monkeypatch.setattr(client, "_do_post", send)
    with pytest.raises(ConnectionError):
        client.write_bookmark("20", True, "123")
    assert len(calls) == 1
    assert calls[0][0] == "/v2/illust/bookmark/add"
    with pytest.raises(PixivRequestError):
        client.write_bookmark("20", True, "other-account")
    assert len(calls) == 1


def test_bound_read_requires_same_illust_and_boolean(monkeypatch):
    client = PixivClient()
    monkeypatch.setattr(auth, "_load_token", lambda: {
        "user_id": "123", "access_token": "synthetic-token"})
    monkeypatch.setattr(client, "_throttle", lambda: None)
    bodies = iter([
        {"illust": {"id": 21, "is_bookmarked": True}},
        {"illust": {"id": 20, "is_bookmarked": None}},
        {"illust": {"id": 20, "is_bookmarked": False}},
    ])
    monkeypatch.setattr(client_module.requests, "get", lambda *a, **k: Response(200, next(bodies)))
    assert client.read_bookmark_bound(20, "123") is None
    assert client.read_bookmark_bound(20, "123") is None
    assert client.read_bookmark_bound(20, "123") is False


def test_expired_bound_read_refreshes_once_without_replaying_get(monkeypatch):
    client = PixivClient()
    monkeypatch.setattr(auth, "_load_token", lambda: {
        "user_id": "123", "access_token": "synthetic-token"})
    monkeypatch.setattr(client, "_throttle", lambda: None)
    reads, refreshes = [], []
    monkeypatch.setattr(client_module.requests, "get", lambda *a, **k:
                        reads.append(1) or Response(401, {"error": "expired"}))
    monkeypatch.setattr(auth, "_try_refresh_token", lambda: refreshes.append(1) or "new")
    with pytest.raises(PixivRequestError):
        client.read_bookmark_bound(20, "123")
    assert reads == [1] and refreshes == [1]


def test_refresh_response_stores_account_identity_without_real_token(monkeypatch):
    monkeypatch.setattr(auth, "_load_token", lambda: {"refresh_token": "fake"})
    monkeypatch.setattr(auth.requests, "post", lambda *a, **k: Response(200, {
        "response": {"access_token": "a", "refresh_token": "b", "user": {"id": 123}}}))
    saved = []
    monkeypatch.setattr(auth, "_save_token", lambda *args: saved.append(args))
    assert auth._try_refresh_token() == "b"
    assert saved == [("a", "b", "123")]


def test_429_sets_shared_cooldown_without_sleeping_or_extra_reads(monkeypatch):
    client = PixivClient()
    monkeypatch.setattr(auth, "_load_token", lambda: {
        "user_id": "123", "access_token": "synthetic-token"})
    monkeypatch.setattr(client, "_throttle", lambda: None)
    calls = []

    def limited(*_args, **_kwargs):
        calls.append(1)
        response = Response(429, {"error": "rate limit"})
        response.headers = {"Retry-After": "120"}
        return response

    monkeypatch.setattr(client_module.requests, "get", limited)
    try:
        with pytest.raises(PixivRequestError):
            client.read_bookmark_bound(20, "123")
        with pytest.raises(PixivRequestError):
            client.read_bookmark_bound(20, "123")
        assert calls == [1]
    finally:
        PixivClient._bookmark_cooldown_until = 0.0
