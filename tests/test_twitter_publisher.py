"""Offline tests for modules/twitter/publisher — no X API, no OpenRouter.

The pure logic worth breaking: X's length accounting (every URL = 23 chars),
the word-boundary trim that never splits a URL, and the per-language
translation cache in publish_tweets.
"""

import pytest

from modules.twitter import publisher
from modules.telegram import translator


# --------------------------------------------------------------------------- #
# tweet_len — X's counting rules                                              #
# --------------------------------------------------------------------------- #


def test_plain_text_counts_itself():
    assert publisher.tweet_len("hello world") == 11


def test_url_counts_as_23_no_matter_its_length():
    long_url = "https://example.com/" + "a" * 200
    assert publisher.tweet_len(f"read this {long_url}") == 10 + 23
    assert publisher.tweet_len("https://x.co/1") == 23


# --------------------------------------------------------------------------- #
# trim_tweet                                                                  #
# --------------------------------------------------------------------------- #


def test_short_text_passes_through_untouched():
    assert publisher.trim_tweet("breaking news") == "breaking news"


def test_long_text_cut_on_word_boundary_with_ellipsis():
    text = " ".join(["word"] * 100)  # 499 chars as X counts them
    out = publisher.trim_tweet(text)
    assert out.endswith("…")
    assert publisher.tweet_len(out) <= publisher.TWEET_MAX
    assert " wor…" not in out  # no split words


def test_trim_never_splits_a_url():
    text = "x" * 270 + " https://example.com/article"
    out = publisher.trim_tweet(text)
    # the URL would overflow, so the cut lands before it — never inside it
    assert "https://" not in out
    assert publisher.tweet_len(out) <= publisher.TWEET_MAX


def test_one_giant_token_is_hard_cut():
    out = publisher.trim_tweet("x" * 500)
    assert out.endswith("…") and len(out) == publisher.TWEET_MAX


# --------------------------------------------------------------------------- #
# publish_tweets                                                              #
# --------------------------------------------------------------------------- #


def _dest(account, lang):
    return {"chat_id": account, "lang": lang, "regions": set()}


def test_translation_cached_per_language(monkeypatch):
    calls = []
    monkeypatch.setattr(translator, "translate",
                        lambda text, lang, src: (calls.append(lang), f"[{lang}] {text}")[-1])
    posted_captions = {}

    def fake_post(path, caption, account):
        posted_captions[account] = caption
        return {"account": account, "tweet_id": "1", "status": "success"}

    monkeypatch.setattr(publisher.poster, "post_media", fake_post)
    dests = [_dest("a", "en"), _dest("b", "en"), _dest("c", "ru")]
    posted, errors = publisher.publish_tweets("clip.mp4", "hello", dests)

    assert posted == ["a", "b", "c"] and errors == []
    assert calls == ["en", "ru"]  # two languages -> exactly two API calls
    assert posted_captions["a"] == posted_captions["b"] == "[en] hello"


def test_empty_lang_skips_translation(monkeypatch):
    monkeypatch.setattr(translator, "translate",
                        lambda *a: pytest.fail("must not translate raw destinations"))
    monkeypatch.setattr(publisher.poster, "post_media",
                        lambda p, c, a: {"account": a, "tweet_id": "1", "status": "success"})
    posted, errors = publisher.publish_tweets("pic.jpg", "hi", [_dest("a", "")])
    assert posted == ["a"] and errors == []


def test_one_account_failing_does_not_abort_the_rest(monkeypatch):
    monkeypatch.setattr(translator, "translate", lambda text, lang, src: text)

    def fake_post(path, caption, account):
        if account == "bad":
            return {"account": account, "tweet_id": None, "status": "failed",
                    "error": "402 payment required"}
        return {"account": account, "tweet_id": "1", "status": "success"}

    monkeypatch.setattr(publisher.poster, "post_media", fake_post)
    posted, errors = publisher.publish_tweets(
        "clip.mp4", "hi", [_dest("bad", "en"), _dest("good", "en")])
    assert posted == ["good"]
    assert errors == [("bad", "402 payment required")]


def test_poster_exception_becomes_an_error_row(monkeypatch):
    monkeypatch.setattr(translator, "translate", lambda text, lang, src: text)

    def boom(path, caption, account):
        raise EnvironmentError("Missing Twitter credential in environment: TWITTER_A_CONSUMER_KEY")

    monkeypatch.setattr(publisher.poster, "post_media", boom)
    posted, errors = publisher.publish_tweets("clip.mp4", "hi", [_dest("a", "en")])
    assert posted == []
    assert errors[0][0] == "a" and "Missing Twitter credential" in errors[0][1]
