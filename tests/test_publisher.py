"""Caption fitting — an autopilot post must ship, not bounce off a limit."""

from modules.telegram import publisher


def test_short_text_is_untouched():
    assert publisher._fit("hello", has_media=False) == "hello"
    assert publisher._fit("hello", has_media=True) == "hello"


def test_media_captions_use_the_shorter_cap():
    long = "x" * 2000
    assert len(publisher._fit(long, has_media=True)) == publisher.CAPTION_LIMIT
    assert publisher._fit(long, has_media=False) == long  # under the 4096 message cap


def test_message_text_is_capped_too():
    assert len(publisher._fit("x" * 5000, has_media=False)) == publisher.TEXT_LIMIT


def test_trimmed_text_is_marked_with_an_ellipsis():
    assert publisher._fit("x" * 2000, has_media=True).endswith("…")


def test_empty_caption_survives():
    assert publisher._fit("", has_media=True) == ""
    assert publisher._fit(None, has_media=True) is None
