import pytest

from src import config


def test_set_port_creates_env_atomically_and_validates_range(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(config, "env_file", env_file)
    monkeypatch.setattr(config, "PORT", 1919)
    monkeypatch.setenv("PORT", "1919")

    config.set_port(2919)

    assert config.PORT == 2919
    assert env_file.read_text(encoding="utf-8") == "PORT=2919\n"
    assert not (tmp_path / ".env.tmp").exists()
    with pytest.raises(ValueError, match="1024"):
        config.set_port(80)


def test_set_pagination_replaces_only_its_own_keys(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("PORT=1919\nPAGE_SIZE=200\nKEEP=this\n", encoding="utf-8")
    monkeypatch.setattr(config, "env_file", env_file)
    monkeypatch.setattr(config, "PAGE_SIZE", 200)
    monkeypatch.setattr(config, "PAGINATION_MODE", "scroll")
    monkeypatch.setattr(config, "MAX_VISIBLE_ITEMS", 0)

    config.set_pagination(100, "paginate", 500)

    assert env_file.read_text(encoding="utf-8") == (
        "PORT=1919\nPAGE_SIZE=100\nKEEP=this\n"
        "PAGINATION_MODE=paginate\nMAX_VISIBLE_ITEMS=500\n"
    )
    assert config.PAGE_SIZE == 100
    assert config.PAGINATION_MODE == "paginate"
    assert config.MAX_VISIBLE_ITEMS == 500


@pytest.mark.parametrize(
    "args",
    [
        (0, "scroll", 0),
        (200, "unknown", 0),
        (200, "scroll", -1),
    ],
)
def test_set_pagination_rejects_invalid_values(monkeypatch, tmp_path, args):
    monkeypatch.setattr(config, "env_file", tmp_path / ".env")
    with pytest.raises(ValueError):
        config.set_pagination(*args)
