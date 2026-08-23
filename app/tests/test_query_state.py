from src.query_state import (
    build_local_url,
    parse_csv_choices,
    parse_csv_ints,
    safe_redirect_target,
)


def test_csv_filters_preserve_difference_between_all_and_empty():
    assert parse_csv_ints(None) is None
    assert parse_csv_ints("") == set()
    assert parse_csv_ints("2,bad,1") == {1, 2}
    assert parse_csv_choices(None, {"illust", "manga"}) is None
    assert parse_csv_choices("", {"illust", "manga"}) == set()
    assert parse_csv_choices("illust,bad", {"illust", "manga"}) == {"illust"}


def test_local_url_encodes_filter_values_and_keeps_explicit_empty():
    assert build_local_url(
        "/", {"artist_ids": "", "tags": "a&b", "drop": None}
    ) == "/?artist_ids=&tags=a%26b"


def test_redirect_target_rejects_external_and_backslash_forms():
    assert safe_redirect_target("/illust/1?show_hidden=1") == "/illust/1?show_hidden=1"
    for unsafe in ("https://evil.example", "//evil.example", "/\\evil.example", ""):
        assert safe_redirect_target(unsafe) == "/"
