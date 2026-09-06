"""shared/credentials.py — credentials/brands/<name>.json -> os.environ."""

import json

import pytest

from shared import credentials


WSWIRE = {
    "lang": "en",
    "group": "JNN",
    "telegram": {"channel": "@wswire"},
    "youtube": {"channel": "wswire"},
    "twitter": {"consumer_key": "ck", "secret_key": "sk", "bearer_token": "bt",
                "access_token": "at", "access_secret": "as"},
    "instagram": {"account": "wswiremedia", "user_id": "178414",
                  "access_token": "IGAA", "token_refreshed": "2026-09-03"},
}
FULL = {"wswire": WSWIRE}


def _dir(tmp_path, **brands):
    """A credentials/brands/ directory holding one file per brand."""
    out = tmp_path / "brands"
    out.mkdir()
    for name, doc in brands.items():
        (out / f"{name}.json").write_text(json.dumps(doc), encoding="utf-8")
    return str(out)


# --- env_from_brands -------------------------------------------------------

def test_expands_every_platform_to_the_vars_the_posters_read():
    env = credentials.env_from_brands(FULL)
    assert env["BRANDS"] == "wswire:en"
    assert env["BRAND_WSWIRE_TG"] == "@wswire"
    assert env["BRAND_WSWIRE_YT"] == "wswire"
    assert env["BRAND_WSWIRE_TW"] == "wswire"
    # modules/twitter/poster.py reads exactly these five.
    assert env["TWITTER_WSWIRE_CONSUMER_KEY"] == "ck"
    assert env["TWITTER_WSWIRE_SECRET_KEY"] == "sk"
    assert env["TWITTER_WSWIRE_BEARER_TOKEN"] == "bt"
    assert env["TWITTER_WSWIRE_ACCESS_TOKEN"] == "at"
    assert env["TWITTER_WSWIRE_ACCESS_SECRET"] == "as"


def test_instagram_account_override_names_the_credential_vars():
    env = credentials.env_from_brands(FULL)
    assert env["BRAND_WSWIRE_IG"] == "wswiremedia"
    assert env["IG_GRAPH_WSWIREMEDIA_ACCESS_TOKEN"] == "IGAA"
    assert env["IG_GRAPH_WSWIREMEDIA_USER_ID"] == "178414"
    assert env["IG_GRAPH_WSWIREMEDIA_TOKEN_REFRESHED"] == "2026-09-03"
    assert env["IG_GRAPH_ACCOUNTS"] == "wswiremedia"


def test_platform_blocks_are_optional():
    env = credentials.env_from_brands(
        {"mir": {"lang": "en", "telegram": {"channel": "@mir"}}})
    assert env["BRAND_MIR_TG"] == "@mir"
    assert "BRAND_MIR_YT" not in env
    assert "BRAND_MIR_TW" not in env
    assert "IG_GRAPH_ACCOUNTS" not in env


def test_brand_without_lang_has_no_colon_in_its_spec():
    env = credentials.env_from_brands({"mir": {"telegram": {"channel": "@m"}}})
    assert env["BRANDS"] == "mir"


def test_blank_field_does_not_overwrite_env():
    # A brand mid-setup must not blank out a value .env still carries.
    env = credentials.env_from_brands({"mir": {"telegram": {"channel": "  "}}})
    assert "BRAND_MIR_TG" not in env


def test_group_expands_to_the_var_config_reads():
    env = credentials.env_from_brands({"mir": {"lang": "en", "group": "GMN"}})
    assert env["BRAND_MIR_GROUP"] == "GMN"


def test_brand_without_a_group_sets_no_group_var():
    env = credentials.env_from_brands({"mir": {"lang": "en"}})
    assert "BRAND_MIR_GROUP" not in env


def test_non_alphanumerics_become_underscores():
    env = credentials.env_from_brands({"mir-news": {"telegram": {"channel": "@m"}}})
    assert env["BRAND_MIR_NEWS_TG"] == "@m"


def test_config_parses_the_expanded_brands():
    # The real consumer: shared.config._parse_brands over our own output.
    from shared import config
    env = credentials.env_from_brands(FULL)
    brand, = config._parse_brands(env["BRANDS"], env)
    assert brand["name"] == "wswire" and brand["lang"] == "en"
    assert brand["group"] == "JNN"
    assert (brand["tg"], brand["yt"], brand["tw"], brand["ig"]) == (
        "@wswire", "wswire", "wswire", "wswiremedia")


# --- merging with a half-migrated .env -------------------------------------

def test_brands_merge_keeps_env_only_brands():
    env = credentials.env_from_brands(FULL, {"BRANDS": "legacy:ru,wswire:de"})
    # wswire replaced in place (JSON wins on lang), legacy left alone.
    assert env["BRANDS"] == "legacy:ru,wswire:en"


def test_ig_accounts_merge_is_case_insensitive_and_appends():
    env = credentials.env_from_brands(
        FULL, {"IG_GRAPH_ACCOUNTS": "other,WSWIREMEDIA"})
    assert env["IG_GRAPH_ACCOUNTS"] == "other,wswiremedia"


# --- strictness ------------------------------------------------------------

def test_unknown_platform_key_is_an_error_not_a_silent_drop():
    with pytest.raises(credentials.CredentialsError, match="twiter"):
        credentials.env_from_brands({"mir": {"twiter": {}}})


def test_unknown_field_inside_a_platform_block_is_an_error():
    with pytest.raises(credentials.CredentialsError, match="consumer_secret"):
        credentials.env_from_brands({"mir": {"twitter": {"consumer_secret": "x"}}})


def test_the_error_names_the_file_it_came_from():
    with pytest.raises(credentials.CredentialsError, match=r"mir\.json"):
        credentials.env_from_brands({"mir": {"services": {}}})


# --- load / apply ----------------------------------------------------------

def test_missing_directory_is_a_no_op(tmp_path):
    env = {}
    assert credentials.apply(str(tmp_path / "nope"), env) == []
    assert env == {}


def test_apply_loads_every_file_in_the_directory(tmp_path):
    path = _dir(tmp_path, wswire=WSWIRE, mir={"lang": "ru"})
    env = {}
    assert credentials.apply(path, env) == ["mir", "wswire"]   # filename order
    assert env["TWITTER_WSWIRE_BEARER_TOKEN"] == "bt"
    assert env["BRANDS"] == "mir:ru,wswire:en"


def test_underscore_and_example_files_are_not_brands(tmp_path):
    path = _dir(tmp_path, wswire=WSWIRE)
    (tmp_path / "brands" / "_example.json").write_text('{"lang": "en"}',
                                                       encoding="utf-8")
    (tmp_path / "brands" / "old.example.json").write_text('{"lang": "en"}',
                                                          encoding="utf-8")
    assert credentials.apply(path, {}) == ["wswire"]


def test_malformed_file_raises_rather_than_dropping_the_brand(tmp_path):
    path = _dir(tmp_path, wswire=WSWIRE)
    (tmp_path / "brands" / "broken.json").write_text("{not json",
                                                     encoding="utf-8")
    with pytest.raises(credentials.CredentialsError, match="broken.json"):
        credentials.apply(path, {})


# --- Instagram token write-back --------------------------------------------

def test_update_instagram_token_rewrites_only_the_owning_file(tmp_path):
    path = _dir(tmp_path, wswire=WSWIRE, mir={"lang": "ru"})
    assert credentials.update_instagram_token(
        "wswiremedia", "IGAA-new", "2026-09-10", path) is True
    doc = json.loads((tmp_path / "brands" / "wswire.json").read_text(
        encoding="utf-8"))
    ig = doc["instagram"]
    assert (ig["access_token"], ig["token_refreshed"]) == ("IGAA-new", "2026-09-10")
    assert ig["user_id"] == "178414"          # untouched
    assert doc["twitter"] == WSWIRE["twitter"]
    assert json.loads((tmp_path / "brands" / "mir.json").read_text(
        encoding="utf-8")) == {"lang": "ru"}


def test_update_instagram_token_reports_an_account_no_file_holds(tmp_path):
    # False = caller falls back to rewriting the .env line.
    path = _dir(tmp_path, wswire=WSWIRE)
    assert credentials.update_instagram_token(
        "someoneelse", "t", "2026-09-10", path) is False


def test_update_instagram_token_with_no_directory_is_false(tmp_path):
    assert credentials.update_instagram_token(
        "wswire", "t", "2026-09-10", str(tmp_path / "nope")) is False


# --- migration off .env ----------------------------------------------------

def test_import_env_round_trips_through_expansion():
    env = credentials.env_from_brands(FULL)
    assert credentials._from_env(env) == FULL


def test_import_env_recovers_the_group():
    brands = credentials._from_env({"BRANDS": "mir:en", "BRAND_MIR_GROUP": "GMN"})
    assert brands == {"mir": {"lang": "en", "group": "GMN"}}


def test_import_env_skips_platforms_the_brand_has_no_account_for():
    brands = credentials._from_env({"BRANDS": "mir:en", "BRAND_MIR_TG": "@mir"})
    assert brands == {"mir": {"lang": "en", "telegram": {"channel": "@mir"}}}


def test_import_env_recovers_credentials_no_brand_line_points_at():
    # TWITTER_MIR_* with no BRAND_MIR_TW line: the picker never offered X for
    # this brand, but the keys are real and must survive the migration.
    brands = credentials._from_env({
        "BRANDS": "mir:en",
        "TWITTER_MIR_CONSUMER_KEY": "ck", "TWITTER_MIR_SECRET_KEY": "sk",
        "TWITTER_MIR_BEARER_TOKEN": "bt", "TWITTER_MIR_ACCESS_TOKEN": "at",
        "TWITTER_MIR_ACCESS_SECRET": "as",
    })
    assert brands["mir"]["twitter"] == {
        "consumer_key": "ck", "secret_key": "sk", "bearer_token": "bt",
        "access_token": "at", "access_secret": "as"}
    # ...and it wires X on, which is the point.
    assert credentials.env_from_brands(brands)["BRAND_MIR_TW"] == "mir"


def test_import_env_keeps_an_explicit_pointer_with_no_credentials_behind_it():
    brands = credentials._from_env({"BRANDS": "mir", "BRAND_MIR_TW": "elsewhere"})
    assert brands["mir"]["twitter"] == {"account": "elsewhere"}


def test_import_env_adds_no_block_for_a_brand_with_no_accounts():
    assert credentials._from_env({"BRANDS": "mir:en"}) == {"mir": {"lang": "en"}}


# --- the shipped template --------------------------------------------------

def test_example_template_is_valid_and_skipped_by_the_loader():
    import os
    path = os.path.join(credentials.ACCOUNTS_DIR, "_example.json")
    doc = credentials.load_brand(path)
    env = credentials.env_from_brands({"_example": doc})
    assert env["BRAND__EXAMPLE_TG"] == "@YourChannel"
    assert "_example" not in credentials.load(credentials.ACCOUNTS_DIR)
