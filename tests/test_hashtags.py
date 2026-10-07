"""Offline tests for the hashtag post-count check (modules/instagram/hashtags.py)
and its caption wiring. Apify is never reached: `_fetch` is replaced by a fake
that answers from a table and records every batch it was asked for."""

import json

import pytest

from shared import config
from modules.instagram import caption, hashtags


# Real measurements from 2026-10-07, as the actor's TEXT field gives them.
COUNTS = {
    "valencia": "32.29 M", "roadclosures": "8073", "londonnews": "33.1 K",
    "sagunto": "161.1 K", "ntsb": "8852", "spainweather": None,
    "flooding": "1.29 M", "flashflood": "116.94 K", "texasflood": "37.3 K",
}


@pytest.fixture
def apify(monkeypatch, tmp_path):
    """A token, a throwaway cache, and a fake actor run."""
    monkeypatch.setattr(config, "APIFY_TOKEN", "test-token")
    monkeypatch.setattr(config, "TG_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "HASHTAG_MIN_POSTS", 5_000)
    monkeypatch.setattr(config, "HASHTAG_MAX_POSTS", 200_000)
    monkeypatch.setattr(config, "HASHTAG_TARGET", 10)
    monkeypatch.setattr(config, "HASHTAG_ROUNDS", 3)
    runs = []

    def _fetch(tags):
        runs.append(list(tags))
        out = []
        for t in tags:
            if t in COUNTS:
                item = {"name": t, "posts": COUNTS[t],
                        # the actor's numeric field, 100x off on "K" counts
                        "postsCount": 16110000 if t == "sagunto" else 0}
                if t == "flooding":
                    item["related"] = [{"hash": "#floods", "info": "515.26 k"},
                                       {"hash": "#flooded", "info": "60.72 k"}]
                out.append(item)
        return out

    monkeypatch.setattr(hashtags, "_fetch", _fetch)
    return runs


# --------------------------------------------------------------------------- #
# counts                                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("text,want", [
    ("161.1 K", 161_100), ("95.41 M", 95_410_000), ("8852", 8852),
    ("1,234", 1234), ("2.15 g", 2_150_000_000), ("515.26 k", 515_260),
    (None, None), ("—", None), ("", None),
])
def test_parse_count_reads_the_text_field(text, want):
    assert hashtags.parse_count(text) == want


def test_the_text_count_wins_over_the_buggy_numeric_one(apify):
    """The actor reports #sagunto as postsCount 16,110,000 beside "161.1 K"."""
    counts, _ = hashtags.lookup(["sagunto"])
    assert counts == {"sagunto": 161_100}


def test_an_unused_tag_counts_as_zero(apify):
    counts, _ = hashtags.lookup(["spainweather", "neverseenbefore"])
    assert counts == {"spainweather": 0, "neverseenbefore": 0}


def test_tags_are_normalised(apify):
    counts, _ = hashtags.lookup(["#Valencia", "#ROADCLOSURES"])
    assert counts == {"valencia": 32_290_000, "roadclosures": 8073}


def test_the_cache_answers_a_repeat_tag_without_a_run(apify):
    hashtags.lookup(["ntsb", "valencia"])
    hashtags.lookup(["ntsb", "londonnews"])
    assert apify == [["ntsb", "valencia"], ["londonnews"]]


def test_an_expired_cache_entry_is_measured_again(apify, tmp_path):
    with open(tmp_path / "hashtag_counts.json", "w") as fh:
        json.dump({"ntsb": [8852, 0]}, fh)          # measured in 1970
    hashtags.lookup(["ntsb"])
    assert apify == [["ntsb"]]


def test_in_band_related_tags_come_back_measured(apify):
    _, related = hashtags.lookup(["flooding"])
    assert related == {"flooded": 60_720}            # #floods 515k is too big


def test_no_token_means_no_lookup(apify, monkeypatch):
    monkeypatch.setattr(config, "APIFY_TOKEN", "")
    assert hashtags.lookup(["ntsb"]) is None
    assert apify == []


def test_a_failed_run_is_none(apify, monkeypatch):
    monkeypatch.setattr(hashtags, "_fetch", lambda tags: None)
    assert hashtags.lookup(["ntsb"]) is None


# --------------------------------------------------------------------------- #
# the rounds                                                                  #
# --------------------------------------------------------------------------- #


def test_refine_keeps_only_the_band_in_relevance_order(apify):
    out = hashtags.refine("x", ["valencia", "sagunto", "roadclosures",
                                "flooding", "ntsb"], lambda *a: [])
    assert out == ["sagunto", "roadclosures", "ntsb"]


def test_refine_feeds_the_measured_numbers_back(apify):
    seen = []

    def suggest(text, measured, related, need):
        seen.append((dict(measured), dict(related), need))
        return ["flashflood", "texasflood", "valencia"]   # one already measured

    out = hashtags.refine("caption", ["valencia", "flooding", "ntsb"], suggest)
    assert out == ["ntsb", "flashflood", "texasflood"]
    measured, related, need = seen[0]
    assert measured["valencia"] == 32_290_000 and need == 9
    assert related == {"flooded": 60_720}
    assert apify[1] == ["flashflood", "texasflood"]       # never re-measured


def test_refine_stops_at_the_target(apify, monkeypatch):
    monkeypatch.setattr(config, "HASHTAG_TARGET", 2)
    calls = []
    out = hashtags.refine("x", ["sagunto", "ntsb", "londonnews"],
                          lambda *a: calls.append(a) or [])
    assert out == ["sagunto", "ntsb"] and calls == []


def test_refine_gives_up_after_the_last_round(apify):
    rounds = []

    def suggest(*a):
        rounds.append(1)
        return [f"made{len(rounds)}"]                     # always 0 posts

    out = hashtags.refine("x", ["valencia"], suggest)
    # three rounds, nothing in the band -> the only tag with posts stands in
    assert out == ["valencia"] and len(apify) == 3 and len(rounds) == 2


def test_refine_is_none_when_the_first_lookup_fails(apify, monkeypatch):
    monkeypatch.setattr(hashtags, "_fetch", lambda tags: None)
    assert hashtags.refine("x", ["ntsb"], lambda *a: []) is None


def test_a_later_failure_keeps_what_was_found(apify, monkeypatch):
    real = hashtags._fetch

    def suggest(*a):
        monkeypatch.setattr(hashtags, "_fetch", lambda tags: None)
        return ["texasflood"]

    assert real                                           # fixture installed
    assert hashtags.refine("x", ["ntsb", "valencia"], suggest) == ["ntsb"]


def test_a_failing_suggestion_ends_the_search(apify):
    def suggest(*a):
        raise RuntimeError("model down")
    assert hashtags.refine("x", ["ntsb", "valencia"], suggest) == ["ntsb"]


# --------------------------------------------------------------------------- #
# the caption wiring                                                          #
# --------------------------------------------------------------------------- #


BODY = ("Search line\n\nA paragraph.\n\nSend this to someone.\n\n"
        "#valencia #sagunto #flooding #ntsb")


def test_verify_replaces_the_line_with_the_measured_pool(apify, monkeypatch):
    monkeypatch.setattr(caption, "suggest_hashtags", lambda *a: [])
    out = caption.verify_hashtags(BODY)
    assert out.splitlines()[-1] == "#sagunto #ntsb"
    assert out.startswith("Search line\n\nA paragraph.")


def test_verify_with_nothing_in_band_uses_the_nearest_to_the_top(apify, monkeypatch):
    monkeypatch.setattr(caption, "suggest_hashtags", lambda *a: [])
    out = caption.verify_hashtags(
        "Line\n\nText.\n\n#valencia #spainweather #flooding")
    # 1.29M (6.5x over) before 32M (161x over); #spainweather has no posts
    assert out.splitlines()[-1] == "#flooding #valencia"


def test_verify_with_no_posts_anywhere_leaves_no_tag_line(apify, monkeypatch):
    monkeypatch.setattr(caption, "suggest_hashtags", lambda *a: [])
    out = caption.verify_hashtags("Line\n\nText.\n\n#spainweather #madeupthing")
    assert out == "Line\n\nText."
    # ...and the account's own tag still signs it.
    assert caption.with_brand_tag(out, "eur24news").endswith("\n\n#eur24news")


def test_verify_without_a_token_keeps_the_unchecked_pool(monkeypatch):
    line = " ".join(f"#t{i}" for i in range(20))
    out = caption.verify_hashtags("Text.\n\n" + line)
    assert out.splitlines()[-1].split() == [f"#t{i}" for i in
                                            range(caption.POOL_HASHTAGS)]


def test_verify_when_apify_is_down_keeps_the_unchecked_pool(apify, monkeypatch):
    monkeypatch.setattr(hashtags, "_fetch", lambda tags: None)
    assert caption.verify_hashtags(BODY).splitlines()[-1] == \
        "#valencia #sagunto #flooding #ntsb"


def test_suggestions_are_parsed_and_normalised(monkeypatch):
    class _Fake:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    _Fake.kw = kw
                    msg = type("M", (), {"content": "#FlashFlood #texas_flood, txweather"})
                    return type("R", (), {"choices": [type("C", (), {"message": msg})]})
    monkeypatch.setattr(caption, "_client", _Fake)
    monkeypatch.setattr(config, "IG_CAPTION_ENABLED", True)
    out = caption.suggest_hashtags("cap", {"valencia": 32_290_000},
                                   {"flooded": 60_720}, 4)
    assert out == ["flashflood", "texasflood", "txweather"]
    user = _Fake.kw["messages"][-1]["content"]
    assert "#valencia — 32,290,000 posts (too big)" in user
    assert "#flooded — 60,720 posts" in user
    assert ":online" not in _Fake.kw["model"]


# --------------------------------------------------------------------------- #
# nothing in the band: the measured tags nearest its TOP                      #
# --------------------------------------------------------------------------- #


def test_nearest_ranks_by_ratio_to_the_top():
    measured = {"tiny": 3_000, "over": 725_000, "huge": 32_290_000,
                "under": 4_900, "unused": 0, "close": 240_000}
    # 240k 1.2x, 725k 3.6x, 4.9k 41x, 3k 67x, 32M 161x; 0 never
    assert hashtags.nearest(measured, 10) == ["close", "over", "under",
                                              "tiny", "huge"]
    assert hashtags.nearest(measured, 2) == ["close", "over"]


def test_refine_falls_back_to_the_nearest_when_nothing_passes(apify):
    out = hashtags.refine("x", ["valencia", "flooding", "spainweather"],
                          lambda *a: [])
    assert out == ["flooding", "valencia"]


def test_the_fallback_never_takes_unvetted_related_tags(apify):
    """#flooded (60k, in band) came from Apify's related list, not the model:
    it was never checked for relevance, so it is not a fallback."""
    assert hashtags.refine("x", ["flooding"], lambda *a: []) == ["flooding"]


def test_a_partial_band_is_not_topped_up(apify):
    """The fallback is for NOTHING in the band; found tags go out as found."""
    out = hashtags.refine("x", ["ntsb", "valencia", "flooding"], lambda *a: [])
    assert out == ["ntsb"]
