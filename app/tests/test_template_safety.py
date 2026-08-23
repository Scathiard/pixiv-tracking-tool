from pathlib import Path


TEMPLATES = Path(__file__).parents[1] / "src" / "templates"


def test_confirmation_messages_are_data_not_inline_javascript():
    artists = (TEMPLATES / "artists.html").read_text(encoding="utf-8")
    detail = (TEMPLATES / "illust_detail.html").read_text(encoding="utf-8")

    assert "onsubmit=" not in artists
    assert "onsubmit=" not in detail
    assert artists.count("data-confirm=") == 2
    assert detail.count("data-confirm=") == 1


def test_progress_errors_are_rendered_as_text_nodes():
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")

    assert "row.textContent = String(e)" in base
    assert "errDiv.innerHTML" not in base
