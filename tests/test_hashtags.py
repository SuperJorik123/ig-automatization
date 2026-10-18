"""Offline tests for the hashtag post-count check (modules/instagram/hashtags.py)
and its caption wiring. Apify is never reached: `_fetch` is replaced by a fake
that answers from a table and records every batch it was asked for."""

import json

import pytest

from shared import config
from modules.instagram import hashtags


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
    monkeypatch.setattr(config, "HASHTAG_FLOOR_POSTS", 500)
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
# select: one pass, in band first, then the most relevant remaining           #
# --------------------------------------------------------------------------- #


MEASURED = {"valencia": 32_290_000, "roadclosures": 8073, "londonnews": 33_100,
            "sagunto": 161_100, "ntsb": 8852, "spainweather": 0,
            "flooding": 1_290_000, "tiny": 120}


def test_select_keeps_the_band_in_relevance_order(apify):
    assert hashtags.select(["valencia", "sagunto", "ntsb", "londonnews",
                            "roadclosures"], MEASURED) ==         ["sagunto", "ntsb", "londonnews", "roadclosures"]


def test_select_caps_at_the_limit(apify):
    out = hashtags.select(["sagunto", "ntsb", "londonnews", "roadclosures"],
                          MEASURED, limit=2)
    assert out == ["sagunto", "ntsb"]


def test_a_thin_band_is_filled_up_to_three_by_relevance(apify):
    out = hashtags.select(["valencia", "flooding", "ntsb", "spainweather"],
                          MEASURED)
    assert out == ["valencia", "flooding", "ntsb"]


def test_zero_and_tiny_tags_are_never_selected(apify):
    assert hashtags.select(["spainweather", "tiny"], MEASURED) == []


def test_an_unmeasured_tag_fills_by_relevance(apify):
    """A tag missing from the counts is one the actor could not count — it
    fills like a fallback rather than being dropped as a 0."""
    assert hashtags.select(["новости", "спасение", "tiny", "чп", "x"],
                           {"tiny": 120}) == ["новости", "спасение", "чп"]


def test_non_latin_tags_are_never_sent_to_the_actor(apify):
    """The actor URL-encodes them and answers postsCount 0 with no text count
    — a 0 that would drop #спасение and sit in the cache for a month."""
    counts, _ = hashtags.lookup(["спасение", "ntsb"])
    assert counts == {"ntsb": 8852}
    assert apify == [["ntsb"]]
    assert hashtags.lookup(["спасение"]) == ({}, {})


def test_every_candidate_is_logged_with_its_verdict(apify, caplog):
    caplog.set_level("INFO", logger="modules.instagram.hashtags")
    hashtags.select(["valencia", "ntsb", "spainweather"], MEASURED, label="wswire")
    text = caplog.text
    assert "[wswire] #ntsb — 8,852 posts — picked: in band" in text
    assert "#valencia — 32,290,000 posts — picked: relevance fallback" in text
    assert "#spainweather — 0 posts — dropped: under 500 posts" in text


def test_norm_keeps_unicode_letters():
    assert hashtags.norm("#Новости") == "новости"
    assert hashtags.norm("#Road_Closures!") == "roadclosures"
