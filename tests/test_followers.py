"""Offline tests for the weekly follower report: the per-platform readers
(HTTP / clients faked), the stored history, and the pure collector/formatter
in modules/telegram/followers.py."""

import types

from modules.facebook import poster as fb
from modules.telegram import followers
from modules.twitter import poster as tw
from modules.youtube import stats

TODAY = "2026-11-23"
WEEK_AGO = "2026-11-16"


# --- readers ---------------------------------------------------------------- #

def test_fb_followers(monkeypatch):
    monkeypatch.setenv("FACEBOOK_ACME_ACCESS_TOKEN", "EAA")
    monkeypatch.setenv("FACEBOOK_ACME_PAGE_ID", "123")
    seen = {}

    def fake(method, url, params=None, **kw):
        seen["url"], seen["fields"] = url, params["fields"]
        return {"followers_count": 1520, "fan_count": 1400}
    monkeypatch.setattr(fb, "_call", fake)
    assert fb.followers("acme") == (1520, "")
    assert seen["url"].endswith("/123") and "followers_count" in seen["fields"]
    monkeypatch.setattr(fb, "_call", lambda *a, **k: {"fan_count": 99})
    assert fb.followers("acme") == (99, "")
    monkeypatch.delenv("FACEBOOK_NOPE_ACCESS_TOKEN", raising=False)
    count, why = fb.followers("nope")
    assert count is None and "Missing" in why


def test_x_followers(monkeypatch):
    class Client:
        def get_me(self, **kw):
            assert kw["user_fields"] == ["public_metrics"]
            return types.SimpleNamespace(data=types.SimpleNamespace(
                public_metrics={"followers_count": 812}))
    monkeypatch.setattr(tw, "_get_clients", lambda acct: (None, Client()))
    assert tw.followers("acme") == (812, "")

    def broken(acct):
        raise EnvironmentError("Missing Twitter credential")
    monkeypatch.setattr(tw, "_get_clients", broken)
    count, why = tw.followers("acme")
    assert count is None and "Missing" in why


def test_youtube_subscribers(monkeypatch):
    class YDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            assert url == "https://www.youtube.com/@altenews/shorts"
            return {"channel_follower_count": 4300, "entries": []}
    import yt_dlp
    monkeypatch.setattr(yt_dlp, "YoutubeDL", YDL)
    assert stats.subscribers("@altenews") == (4300, "")


# --- collect ---------------------------------------------------------------- #

def test_collect_reads_every_connected_platform():
    brands = [
        {"name": "a", "ig": "a_ig", "fb": "a_fb", "yt": "a", "yt_handle": "@a",
         "tw": "a_x"},
        {"name": "b", "ig": "b_ig", "fb": "", "yt": "b", "yt_handle": "", "tw": ""},
        {"name": "c", "ig": "", "fb": "", "yt": "", "yt_handle": "", "tw": ""},
    ]
    seen = []

    def make(p):
        def f(acct):
            seen.append((p, acct))
            if acct == "b_ig":
                return None, "token expired"
            return 100, ""
        return f
    out = followers.collect(brands, {p: make(p) for p in ("ig", "fb", "yt", "x")})
    assert sorted(seen) == [("fb", "a_fb"), ("ig", "a_ig"), ("ig", "b_ig"),
                            ("x", "a_x"), ("yt", "@a")]
    by = {(r["brand"], r["platform"]): r for r in out}
    assert by[("b", "ig")]["count"] is None
    assert by[("b", "yt")]["error"] == "no handle"
    assert followers.counts_of(out) == {("a", "ig"): 100, ("a", "fb"): 100,
                                        ("a", "yt"): 100, ("a", "x"): 100}


# --- stored history --------------------------------------------------------- #

def test_history_finds_the_closest_older_day(store):
    assert store.follower_counts_on_or_before(TODAY) == (None, {})
    store.save_follower_counts("2026-11-15", {("a", "ig"): 10})
    store.save_follower_counts("2026-11-20", {("a", "ig"): 30})
    store.save_follower_counts("2026-11-20", {("a", "ig"): 35})   # same day: replaced
    assert store.follower_counts_on_or_before(WEEK_AGO) == ("2026-11-15",
                                                            {("a", "ig"): 10})
    assert store.follower_counts_on_or_before(TODAY) == ("2026-11-20",
                                                         {("a", "ig"): 35})
    assert store.first_follower_day() == "2026-11-15"


# --- render ----------------------------------------------------------------- #

def _r(brand, p, count=None, error=""):
    return {"brand": brand, "platform": p, "count": count,
            "error": error if count is None else ""}


def _table(text):
    return [line.split() for line in
            text[text.index("<pre>") + 5:text.index("</pre>")].splitlines()]


def test_render_weekly_change():
    results = [
        _r("big", "ig", 5600), _r("big", "x", 900),
        _r("small", "ig", 210), _r("small", "fb", 1000),
        _r("broken", "fb", error="Session has expired (code 190)"),
        _r("fresh", "ig", 50),
    ]
    then = {("big", "ig"): 5000, ("big", "x"): 904, ("small", "ig"): 200,
            ("small", "fb"): 1000, ("broken", "fb"): 70}
    text = followers.render(results, then, WEEK_AGO, TODAY)[0]
    assert "Followers this week</b> · 16 Nov → 23 Nov" in text
    assert "<b>+606 followers gained in total</b>" in text
    assert "📸 +610 · 📘 0 · 𝕏 −4" in text
    assert "🏆 Grew most: <b>big</b> on Instagram — +600" in text
    assert "📉 Lost most: <b>big</b> on X — −4" in text
    assert "😐 No change: 1 of 4 accounts" in text
    table = _table(text)
    assert table[0] == ["IG", "FB", "X"]
    assert table[1] == ["big", "+600", "·", "−4"]
    assert table[2] == ["small", "+10", "0", "·"]
    assert ["fresh", "new", "·", "·"] in table        # no count from last week
    assert ["broken", "·", "!", "·"] in table
    assert "• broken FB: token expired" in text
    assert "Followers now: 📸 5.9K · 📘 1K · 𝕏 900" in text


def test_render_names_a_short_span():
    text = followers.render([_r("a", "ig", 10)], {("a", "ig"): 5},
                            "2026-11-20", TODAY)[0]
    assert "20 Nov → 23 Nov (3 days)" in text


def test_render_without_history_shows_todays_counts():
    text = followers.render([_r("a", "ig", 1234), _r("a", "yt", 50)], None,
                            None, TODAY)[0]
    assert "Followers now</b> · 23 Nov" in text
    assert "weekly change starts on 30 Nov" in text
    assert _table(text)[1] == ["a", "1.2K", "50"]
    assert "YouTube rounds" in text


def test_render_with_nothing_connected():
    assert "No account" in followers.render([], None, None, TODAY)[0]


def test_youtube_subscribers_from_the_page():
    p = stats.parse_subscribers
    assert p('"metadataParts":[{"text":{"content":"1 subscriber"}}]') == 1
    assert p('{"content":"12.3K subscribers"}') == 12300
    assert p('{"content":"1,204 subscribers"}') == 1204
    assert p('{"content":"No subscribers"}') == 0
    assert p('{"content":"52 videos"}') is None


def test_youtube_subscribers_falls_back_to_the_page(monkeypatch):
    class YDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            raise RuntimeError("ERROR: This channel does not have a shorts tab")
    import yt_dlp
    monkeypatch.setattr(yt_dlp, "YoutubeDL", YDL)
    monkeypatch.setattr(stats, "_subscribers_from_page", lambda h: (1, ""))
    assert stats.subscribers("@x") == (1, "")
    monkeypatch.setattr(stats, "_subscribers_from_page",
                        lambda h: (None, "count not shown"))
    assert stats.subscribers("@x") == (None, "count not shown")
