"""Isolated API/image responses; no account, network, or real media."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
import requests

from src import client as client_module
from src.client import PixivClient, PixivRequestError
from src.core.download import AssetDownloader, DownloadFailure
from src.core.tasks import safe_failure
from src.request_policy import retry_after_seconds


class Response:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self.body = body or {}
        self.headers = headers or {}
        self.closed = False

    def json(self):
        return self.body

    def close(self):
        self.closed = True


@pytest.fixture
def clock(monkeypatch):
    import time
    current = [1000.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        current[0] += seconds

    monkeypatch.setattr(time, "monotonic", lambda: current[0])
    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr(PixivClient, "_last_request", 0.0)
    monkeypatch.setattr(PixivClient, "_api_cooldown_until", 0.0)
    monkeypatch.setattr(PixivClient, "_bookmark_cooldown_until", 0.0)
    monkeypatch.setattr(AssetDownloader, "_last_request", 0.0)
    monkeypatch.setattr(AssetDownloader, "_cooldown_until", 0.0)
    return current, sleeps


def _api(monkeypatch, responses):
    calls = []
    client = PixivClient()
    monkeypatch.setattr(client_module, "_get_auth_header", lambda: {})

    def get(*args):
        calls.append(args)
        return responses.pop(0)

    monkeypatch.setattr(client, "_do_get", get)
    return client, calls


def test_http_429_defers_all_api_clients_with_retry_after(monkeypatch, clock):
    client, calls = _api(monkeypatch, [Response(429, headers={"Retry-After": "120"})])
    with pytest.raises(PixivRequestError) as caught:
        client._get("/v1/test")
    assert safe_failure(caught.value) == {
        "code": "PixivRequestError", "message": "Pixiv 请求受限，请稍后重试",
        "status": 429, "retry_after": 120, "retryable": True,
        "failure_code": "api_rate_limit"}
    with pytest.raises(PixivRequestError) as cooldown:
        PixivClient()._get("/v1/other")
    assert cooldown.value.failure_code == "api_rate_limit"
    assert cooldown.value.status is None  # No second HTTP response occurred.
    assert len(calls) == 1 and clock[1] == []


def test_body_rate_limit_keeps_http_200_and_next_request_obeys_shared_clock(monkeypatch, clock):
    client, calls = _api(monkeypatch, [Response(200, {"error": "Rate Limit"}),
                                          Response(200, {"ok": True})])
    with pytest.raises(PixivRequestError) as caught:
        client._get("/v1/test")
    assert caught.value.status == 200
    assert caught.value.failure_code == "api_rate_limit"
    assert caught.value.retryable is True and caught.value.retry_after == 60
    assert len(calls) == 1 and clock[1] == []
    clock[0][0] += 60
    assert client._get("/v1/next") == {"ok": True}
    assert len(calls) == 2
    assert PixivClient._last_request == clock[0][0]


@pytest.mark.parametrize("status,code", [(401, "api_unauthorized"), (403, "api_forbidden")])
def test_api_auth_and_forbidden_are_not_rate_limit(monkeypatch, clock, status, code):
    client, calls = _api(monkeypatch, [Response(status), Response(200, {"ok": True})])
    monkeypatch.setattr(client_module, "_token_expired", lambda _response: False)
    with pytest.raises(PixivRequestError) as caught:
        client._get("/v1/test")
    assert caught.value.status == status
    assert caught.value.failure_code == code and caught.value.retryable is False
    assert "限流" not in caught.value.user_message
    clock[0][0] += 1
    assert client._get("/v1/next") == {"ok": True}
    assert len(calls) == 2


def test_retry_after_accepts_http_date_and_ignores_invalid_values():
    now = datetime(2026, 9, 29, tzinfo=timezone.utc)
    value = format_datetime(now + timedelta(seconds=125), usegmt=True)
    assert retry_after_seconds({"Retry-After": value}, now=now) == 125
    assert retry_after_seconds({"Retry-After": "not a date"}, now=now) is None
    assert retry_after_seconds({"Retry-After": "-1"}, now=now) is None


def test_network_error_is_retryable_and_persisted_without_request_details(monkeypatch, clock):
    client = PixivClient()
    monkeypatch.setattr(client_module, "_get_auth_header", lambda: {})
    monkeypatch.setattr(client, "_do_get", lambda *_: (_ for _ in ()).throw(
        requests.ConnectionError("https://secret.invalid/?token=example")))
    with pytest.raises(PixivRequestError) as caught:
        client._get("/v1/test")
    failure = safe_failure(caught.value)
    assert failure["failure_code"] == "api_network" and failure["retryable"] is True
    assert "secret" not in failure["message"] and "token" not in failure["message"]


class Session:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_image_429_is_independent_and_never_sends_bearer(monkeypatch, clock, tmp_path):
    response = Response(429, headers={"Retry-After": "180"})
    session = Session([response])
    downloader = AssetDownloader(session=session)
    with pytest.raises(DownloadFailure) as caught:
        downloader.download("https://i.pximg.net/a.jpg", tmp_path / "a.jpg")
    assert safe_failure(caught.value)["status"] == 429
    assert caught.value.failure_code == "image_rate_limit"
    assert caught.value.retry_after == 180 and caught.value.retryable is True
    assert response.closed
    assert "Authorization" not in session.calls[0][1]["headers"]
    other = AssetDownloader(session=Session([]))
    with pytest.raises(DownloadFailure) as cooldown:
        other.download("https://i.pximg.net/b.jpg", tmp_path / "b.jpg")
    assert cooldown.value.status is None and cooldown.value.failure_code == "image_rate_limit"
    assert other.session.calls == [] and clock[1] == []
    assert PixivClient._api_cooldown_until == 0


@pytest.mark.parametrize("status,code,retryable", [
    (401, "image_unauthorized", False),
    (403, "image_forbidden", False),
    (503, "image_http_status", True),
])
def test_image_failure_preserves_exact_status(monkeypatch, clock, tmp_path, status, code, retryable):
    response = Response(status, headers={"Retry-After": "90"} if status == 503 else None)
    session = Session([response])
    with pytest.raises(DownloadFailure) as caught:
        AssetDownloader(session=session).download("https://i.pximg.net/a.jpg", tmp_path / "a.jpg")
    assert caught.value.status == status
    assert caught.value.failure_code == code
    assert caught.value.retryable is retryable
    assert caught.value.retry_after == (90 if status == 503 else None)
    assert "https://" not in caught.value.user_message
    assert response.closed
