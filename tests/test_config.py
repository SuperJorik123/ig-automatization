"""TG_DESTINATIONS parsing — including the two-field form that predates
regions, which must keep working untouched."""

from shared.config import _parse_destinations


def test_full_form():
    (d,) = _parse_destinations("@news_eu:en:eu")
    assert d == {"chat_id": "@news_eu", "lang": "en", "regions": {"eu"}}


def test_multi_region():
    (d,) = _parse_destinations("@news_world:en:us+eu")
    assert d["regions"] == {"us", "eu"}


def test_two_field_form_is_a_catch_all():
    """Old config: a language but no region. Empty regions = matches everything."""
    (d,) = _parse_destinations("@news_ru:ru")
    assert d == {"chat_id": "@news_ru", "lang": "ru", "regions": set()}


def test_bare_chat_id():
    (d,) = _parse_destinations("-1001234567890")
    assert d == {"chat_id": "-1001234567890", "lang": "", "regions": set()}


def test_numeric_id_with_lang_and_region():
    (d,) = _parse_destinations("-1001234567890:es:eu")
    assert d["chat_id"] == "-1001234567890"
    assert d["lang"] == "es"
    assert d["regions"] == {"eu"}


def test_list_is_split_and_trimmed():
    out = _parse_destinations(" @a:en:us , @b:ru:ru ,, ")
    assert [d["chat_id"] for d in out] == ["@a", "@b"]


def test_empty():
    assert _parse_destinations("") == []
    assert _parse_destinations(None) == []
