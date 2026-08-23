from src.caption_state import (
    decode_caption_state,
    encode_caption_state,
    should_fetch_caption,
)


def test_caption_states_distinguish_empty_unavailable_and_transient_failure():
    empty = encode_caption_state("empty")
    unavailable = encode_caption_state("unavailable", "not found")
    failed = encode_caption_state("failed", "timeout")

    assert decode_caption_state(empty)["status"] == "empty"
    assert decode_caption_state(unavailable)["status"] == "unavailable"
    assert decode_caption_state(failed)["status"] == "failed"
    assert not should_fetch_caption(False, empty)
    assert not should_fetch_caption(False, unavailable)
    assert should_fetch_caption(False, failed)


def test_existing_caption_file_always_wins_and_legacy_values_are_retryable():
    assert not should_fetch_caption(True, "legacy raw caption")
    assert should_fetch_caption(False, "legacy raw caption")
    assert should_fetch_caption(False, None)
