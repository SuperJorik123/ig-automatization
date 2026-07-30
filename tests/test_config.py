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


from shared import config


def test_parse_brands_names_langs_and_env_pointers():
    env = {
        "BRAND_MIRNEWS_TG": "@mir_news",
        "BRAND_MIRNEWS_YT": "mirnews",
        "BRAND_RUS_NEWS_TW": "rusnews",
    }
    brands = config._parse_brands("mirnews:en, rus-news:ru", env)
    assert [b["name"] for b in brands] == ["mirnews", "rus-news"]
    assert brands[0]["lang"] == "en" and brands[1]["lang"] == "ru"
    # env key: name uppercased, non-alphanumerics -> "_"
    assert brands[0]["tg"] == "@mir_news"
    assert brands[0]["yt"] == "mirnews"
    assert brands[0]["tw"] == ""            # unset platform -> empty string
    assert brands[1]["tw"] == "rusnews"     # rus-news -> BRAND_RUS_NEWS_TW
    assert brands[1]["tg"] == "" and brands[1]["yt"] == ""


def test_parse_brands_logo_path_and_empty_input():
    brands = config._parse_brands("mirnews:en", {})
    assert brands[0]["logo"] == config.os.path.join(
        config.ROOT_DIR, "brands", "mirnews", "logo.png"
    )
    assert config._parse_brands("", {}) == []
    assert config._parse_brands("  ,  ", {}) == []


def test_parse_brands_lang_optional():
    brands = config._parse_brands("solo", {})
    assert brands[0]["name"] == "solo" and brands[0]["lang"] == ""
