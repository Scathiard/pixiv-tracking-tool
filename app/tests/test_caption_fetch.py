from types import SimpleNamespace

from src.caption_state import decode_caption_state
from src.tracker import Tracker


class _Client:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def get_illust_detail(self, _illust_id):
        if self.error:
            raise self.error
        return self.result


def _illust():
    return SimpleNamespace(
        pixiv_illust_id="100",
        artist=SimpleNamespace(name="Artist", pixiv_user_id="1"),
        caption="",
    )


def test_empty_caption_is_cached_as_a_real_result(monkeypatch, tmp_path):
    caption_file = tmp_path / "100.caption.html"
    monkeypatch.setattr("src.tracker._caption_path", lambda *_: caption_file)
    tracker = Tracker.__new__(Tracker)
    tracker.client = _Client({"caption": ""})
    illust = _illust()

    assert tracker._fetch_and_save_caption(illust) == "empty"
    assert caption_file.exists()
    assert caption_file.read_text(encoding="utf-8") == ""
    assert decode_caption_state(illust.caption)["status"] == "empty"


def test_unavailable_caption_is_terminal_without_creating_a_file(monkeypatch, tmp_path):
    caption_file = tmp_path / "100.caption.html"
    monkeypatch.setattr("src.tracker._caption_path", lambda *_: caption_file)
    tracker = Tracker.__new__(Tracker)
    tracker.client = _Client(error=RuntimeError("not found"))
    illust = _illust()

    assert tracker._fetch_and_save_caption(illust) == "unavailable"
    assert not caption_file.exists()
    assert decode_caption_state(illust.caption)["status"] == "unavailable"


def test_transient_caption_failure_remains_retryable(monkeypatch, tmp_path):
    monkeypatch.setattr("src.tracker._caption_path", lambda *_: tmp_path / "100.caption.html")
    tracker = Tracker.__new__(Tracker)
    tracker.client = _Client(error=RuntimeError("timeout"))
    illust = _illust()

    assert tracker._fetch_and_save_caption(illust) == "failed"
    assert decode_caption_state(illust.caption)["status"] == "failed"
