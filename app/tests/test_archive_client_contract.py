import pytest

from src import auth, client as client_module
from src.client import PixivClient, PixivRequestError


class Response:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        return self.body


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("status", [200, 400, 401])
def test_expired_auth_is_refreshed_at_most_once_without_real_tokens(monkeypatch, method, status):
    client = PixivClient()
    responses = iter([Response(status, {"error": "oauth invalid_grant"}), Response(200, {"ok": True})])
    refreshes = []
    monkeypatch.setattr(client_module, "_get_auth_header", lambda: {})
    monkeypatch.setattr(client, "_throttle", lambda: None)
    monkeypatch.setattr(client, "_do_" + method, lambda *_: next(responses))
    monkeypatch.setattr(auth, "_try_refresh_token", lambda: refreshes.append("called") or True)
    result = client._get("/test") if method == "get" else client._post("/test")
    assert result == {"ok": True} and refreshes == ["called"]


def test_failed_refresh_does_not_loop_or_claim_success(monkeypatch):
    client = PixivClient()
    calls = []
    monkeypatch.setattr(client_module, "_get_auth_header", lambda: {})
    monkeypatch.setattr(client, "_throttle", lambda: None)
    monkeypatch.setattr(client, "_do_get", lambda *_: Response(200, {"error": "oauth invalid_grant"}))
    monkeypatch.setattr(auth, "_try_refresh_token", lambda: calls.append("called") or False)
    with pytest.raises(PixivRequestError):
        client._get("/test")
    assert calls == ["called"]


def test_preview_url_is_never_passed_off_as_an_original():
    parsed = PixivClient._parse_illust({"id": 1, "title": "preview only",
        "image_urls": {"large": "https://i.pximg.net/preview.jpg"}})
    assert parsed["urls"][0]["original"] is None


def test_multipage_original_urls_preserve_page_identity():
    parsed = PixivClient._parse_illust({"id": 1, "title": "multipage", "page_count": 2,
        "meta_pages": [{"image_urls": {"original": "https://i.pximg.net/1_p0.png"}},
                       {"image_urls": {"original": "https://i.pximg.net/1_p1.jpg"}}]})
    assert [page["original"] for page in parsed["urls"]] == ["https://i.pximg.net/1_p0.png", "https://i.pximg.net/1_p1.jpg"]
