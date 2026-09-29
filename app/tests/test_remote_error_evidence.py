"""Exact remote facts, not guessed deletion; all responses are synthetic."""
import pytest

from src import client as module
from src.client import PixivClient, PixivRequestError
from src.core.activity_log import failure_reason
from src.core.tasks import safe_failure


class Response:
    headers = {}

    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        return self.body


def request_client(monkeypatch, status, body):
    client = PixivClient()
    monkeypatch.setattr(module, "_get_auth_header", lambda: {})
    monkeypatch.setattr(client, "_throttle", lambda: None)
    monkeypatch.setattr(module, "_token_expired", lambda _: False)
    calls = []
    monkeypatch.setattr(client, "_do_get", lambda *args: calls.append(args) or Response(status, body))
    return client, calls


@pytest.mark.parametrize("message", ["尚无此页", "Page not found", "Deleted or private", ""])
def test_ambiguous_404_never_claims_deletion(monkeypatch, message):
    client, calls = request_client(monkeypatch, 404, {"error": {"user_message": message, "reason": ""}})
    for _ in range(2):
        with pytest.raises(PixivRequestError) as caught:
            client.get_illust_detail("74933612")
        detail = safe_failure(caught.value)
        assert detail["failure_code"] == "work_unavailable"
        assert detail["retryable"] is False
        assert "已删除" not in detail["message"]
    assert len(calls) == 2
    assert all(call[2] == {"illust_id": "74933612"} for call in calls)


@pytest.mark.parametrize("status", [200, 404])
def test_explicit_private_is_distinct_from_unknown_404(monkeypatch, status):
    client, _ = request_client(monkeypatch, status, {"error": {"user_message": "Artist has made their work private."}})
    with pytest.raises(PixivRequestError) as caught:
        client.get_illust_detail("11")
    assert caught.value.failure_code == "work_private"
    assert "非公开" in caught.value.user_message
    assert "删除" not in caught.value.user_message


def test_ugoira_subresource_404_does_not_declare_whole_work_gone(monkeypatch):
    client, calls = request_client(monkeypatch, 404, {"error": {"user_message": "尚无此页"}})
    with pytest.raises(PixivRequestError) as caught:
        client.get_ugoira_metadata("22")
    assert caught.value.failure_code == "ugoira_metadata_unavailable"
    assert "动图" in caught.value.user_message
    assert len(calls) == 1


@pytest.mark.parametrize("status,expected", [(401, "api_unauthorized"), (403, "api_forbidden"), (429, "api_rate_limit"), (503, "api_unavailable")])
def test_auth_throttle_and_service_errors_are_not_work_availability(monkeypatch, status, expected):
    client, _ = request_client(monkeypatch, status, {"error": {"user_message": "Artist has made their work private."}})
    with pytest.raises(PixivRequestError) as caught:
        client.get_illust_detail("22")
    assert caught.value.failure_code == expected


def test_log_reports_safe_reason_without_echoing_arbitrary_remote_message():
    secret = "private-token=synthetic-dont-show"
    assert "无法获取" in failure_reason({"code": "PixivRequestError", "status": 404, "message": secret, "failure_code": "work_unavailable"})
    assert "核对" in failure_reason({"code": "WorkMetadataConflict", "message": secret, "failure_code": "work_metadata_conflict"})
    for detail in ({"code": "PixivRequestError", "status": 404, "message": secret},
                   {"code": "InvalidMedia", "message": secret},
                   {"code": "DownloadFailure", "status": 403, "message": secret}):
        shown = failure_reason(detail)
        assert shown != "此项未完成，请重试" and secret not in shown


def test_wrong_or_missing_illust_id_is_not_available(monkeypatch):
    client, _ = request_client(monkeypatch, 200, {"illust": {"id": 999, "title": "synthetic", "image_urls": {}}})
    with pytest.raises(PixivRequestError) as caught:
        client.get_illust_detail("22")
    assert caught.value.failure_code == "api_invalid_response"


@pytest.mark.parametrize("metadata", [{}, {"frames": [], "zip_urls": {"medium": "https://i.pximg.net/a.zip"}},
    {"frames": [{"file": "0.jpg", "delay": 60}], "zip_urls": {}},
    {"frames": [{"file": "0.jpg", "delay": None}], "zip_urls": {"medium": "https://i.pximg.net/a.zip"}}])
def test_incomplete_ugoira_response_cannot_resume_a_stopped_repair(monkeypatch, metadata):
    client, _ = request_client(monkeypatch, 200, {"ugoira_metadata": metadata})
    with pytest.raises(PixivRequestError) as caught:
        client.get_ugoira_metadata("22")
    assert caught.value.failure_code == "api_invalid_response"


def test_valid_ugoira_metadata_can_resume_repair(monkeypatch):
    metadata = {"frames": [{"file": "0.jpg", "delay": 60}],
                "zip_urls": {"medium": "https://i.pximg.net/a.zip"}}
    client, _ = request_client(monkeypatch, 200, {"ugoira_metadata": metadata})
    assert client.get_ugoira_metadata("22") == metadata
