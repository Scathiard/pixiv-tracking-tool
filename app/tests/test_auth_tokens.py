import json

from src import auth
from src.client import _get_auth_header


def test_corrupt_token_file_falls_back_to_logged_out(monkeypatch, tmp_path):
    token_file = tmp_path / "pixiv_token.json"
    token_file.write_text('{"access_token":', encoding="utf-8")
    monkeypatch.setattr(auth, "_token_file", lambda: token_file)

    assert auth._load_token() == {}
    assert _get_auth_header() == {}


def test_token_write_is_atomic_and_round_trips(monkeypatch, tmp_path):
    token_file = tmp_path / "pixiv_token.json"
    monkeypatch.setattr(auth, "_token_file", lambda: token_file)

    auth._save_token("access", "refresh")

    assert auth._load_token() == {
        "access_token": "access",
        "refresh_token": "refresh",
    }
    assert json.loads(token_file.read_text(encoding="utf-8"))["refresh_token"] == "refresh"
    assert list(tmp_path.glob("*.tmp")) == []


def test_oauth_input_rejects_unbounded_payload():
    code, error = auth.validate_oauth_input("x" * 9000)
    assert code is None
    assert "过长" in error
