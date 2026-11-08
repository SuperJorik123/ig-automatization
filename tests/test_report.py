"""Offline tests for /report: the per-platform latest-reel readers (HTTP
faked) and the pure collector/formatter in modules/telegram/report.py."""

import datetime as dt

import pytest

from modules.facebook import poster as fb
from modules.instagram import graph
from modules.telegram import report
from modules.youtube import stats

NOW = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)


# --- formatting helpers ----------------------------------------------------- #

def test_fmt_num():
    assert report.fmt_num(None) == "—"
    assert report.fmt_num(999) == "999"
    assert report.fmt_num(1234) == "1.2K"
    assert report.fmt_num(2000) == "2K"
    assert report.fmt_num(2_500_000) == "2.5M"


def test_age_reads_meta_and_iso_stamps():
    assert report.age("2026-11-01T11:30:00+0000", NOW) == "30m ago"
    assert report.age("2026-11-01T07:00:00Z", NOW) == "5h ago"
    assert report.age("2026-10-29T12:00:00+00:00", NOW) == "3d ago"
    assert report.age("", NOW) == "?"


def test_engagement():
    assert report.engagement({"views": 0}) is None
    assert report.engagement({"views": 1000, "likes": 40, "comments": 5,
                              "shares": 5, "saves": None}) == 0.05


def test_short_error():
    assert "handle" in report.short_error("no handle")
    assert report.short_error("Missing Facebook credential: X") == "not connected"
    assert report.short_error("HTTP Error 404: Not Found") == "channel not found"
    assert report.short_error("(#10) Requires read_insights permission") == \
        "token lacks permission"
    assert report.short_error("Error validating access token: Session has "
                              "expired (code 190)") == "token expired"


# --- collect ---------------------------------------------------------------- #

def test_collect_reads_only_connected_platforms():
    brands = [
        {"name": "a", "ig": "a_ig", "fb": "a_fb", "yt": "a", "yt_handle": "@a"},
        {"name": "b", "ig": "b_ig", "fb": "", "yt": "b", "yt_handle": ""},
        {"name": "c", "ig": "", "fb": "", "yt": "", "yt_handle": ""},
    ]
    seen = []

    def make(p):
        def f(acct):
            seen.append((p, acct))
            if acct == "b_ig":
                raise RuntimeError("boom")
            return {"views": 10}, ""
        return f
    out = report.collect(brands, {p: make(p) for p in ("ig", "fb", "yt")})
    assert sorted(seen) == [("fb", "a_fb"), ("ig", "a_ig"), ("ig", "b_ig"),
                            ("yt", "@a")]
    by = {(r["brand"], r["platform"]): r for r in out}
    assert by[("b", "ig")]["data"] is None and "boom" in by[("b", "ig")]["error"]
    assert by[("b", "yt")]["error"] == "no handle"   # channel but no handle
    assert not any(r["brand"] == "c" for r in out)


# --- render ----------------------------------------------------------------- #

def _r(brand, p, views=None, error="", **kw):
    data = None if error else {"url": f"https://x/{brand}/{p}",
                               "posted_at": "2026-11-01T10:00:00+0000",
                               "views": views, **kw}
    return {"brand": brand, "platform": p, "data": data, "error": error}


def test_render_ranks_accounts_and_sums_totals():
    results = [
        _r("small", "ig", 100, likes=5, comments=1),
        _r("big", "ig", 5000, likes=300, comments=20, shares=10, saves=4),
        _r("big", "fb", 1000, likes=10, comments=2),
        _r("broken", "fb", error="(#10) needs read_insights permission"),
    ]
    pages = report.render(results, NOW)
    assert len(pages) == 1
    text = pages[0]
    assert "Latest-reel report" in text
    assert "IG 2/2 · FB 1/2" in text
    assert "📸 Instagram · 👁 5.1K · ❤️ 305 · 💬 21" in text
    assert "Σ All · 👁 6.1K" in text
    assert "🏆 Most views: <b>big</b> Instagram — 5K" in text
    assert "🐢 Fewest views: <b>small</b>" in text
    assert text.index("1. big") < text.index("2. small") < text.index("3. broken")
    assert "⚠️ token lacks permission" in text
    assert '<a href="https://x/big/ig">IG</a> · 2h ago' in text
    assert "ER 6.7%" in text   # (300+20+10+4)/5000


def test_render_escapes_names_and_splits_long_reports():
    results = [_r(f"brand<{i}>", "ig", i * 10, likes=1, comments=0)
               for i in range(1, 80)]
    pages = report.render(results, NOW)
    assert len(pages) > 1
    assert all(len(p) <= report.MSG_LIMIT for p in pages)
    assert "brand&lt;79&gt;" in pages[0]


def test_render_with_nothing_connected():
    assert "No account" in report.render([], NOW)[0]


# --- platform readers ------------------------------------------------------- #

def test_ig_latest_reel_picks_the_newest_reel(monkeypatch):
    monkeypatch.setenv("IG_GRAPH_ACME_ACCESS_TOKEN", "IGAA")
    monkeypatch.setenv("IG_GRAPH_ACME_USER_ID", "178")
    calls = []

    def fake(method, url, params=None, **kw):
        calls.append(url)
        if url.endswith("/178/media"):
            return {"data": [
                {"id": "p1", "media_type": "IMAGE", "media_product_type": "FEED"},
                {"id": "r1", "media_type": "VIDEO", "media_product_type": "REELS",
                 "permalink": "https://ig/r1", "timestamp": "2026-11-01T10:00:00+0000",
                 "like_count": 50, "comments_count": 3},
            ]}
        assert url.endswith("/r1/insights")
        return {"data": [{"name": "views", "values": [{"value": 900}]},
                         {"name": "shares", "values": [{"value": 7}]},
                         {"name": "saved", "values": [{"value": 2}]}]}
    monkeypatch.setattr(graph, "_call", fake)
    data, why = graph.latest_reel("acme")
    assert why == ""
    assert data["url"] == "https://ig/r1"
    assert (data["views"], data["likes"], data["comments"],
            data["shares"], data["saves"]) == (900, 50, 3, 7, 2)


def test_ig_latest_reel_without_insights_keeps_likes(monkeypatch):
    monkeypatch.setenv("IG_GRAPH_ACME_ACCESS_TOKEN", "IGAA")
    monkeypatch.setenv("IG_GRAPH_ACME_USER_ID", "178")

    def fake(method, url, params=None, **kw):
        if url.endswith("/insights"):
            raise graph.GraphError("(#10) permission")
        return {"data": [{"id": "r1", "media_product_type": "REELS",
                          "like_count": 5, "comments_count": 1}]}
    monkeypatch.setattr(graph, "_call", fake)
    data, _ = graph.latest_reel("acme")
    assert data["views"] is None and data["likes"] == 5


def test_ig_latest_reel_none_and_errors(monkeypatch):
    monkeypatch.setenv("IG_GRAPH_ACME_ACCESS_TOKEN", "IGAA")
    monkeypatch.setenv("IG_GRAPH_ACME_USER_ID", "178")
    monkeypatch.setattr(graph, "_call", lambda *a, **k: {"data": []})
    assert graph.latest_reel("acme") == (None, "no reels yet")
    monkeypatch.delenv("IG_GRAPH_NOPE_ACCESS_TOKEN", raising=False)
    data, why = graph.latest_reel("nope")
    assert data is None and "Missing" in why


@pytest.fixture
def fb_creds(monkeypatch):
    monkeypatch.setenv("FACEBOOK_ACME_ACCESS_TOKEN", "EAA")
    monkeypatch.setenv("FACEBOOK_ACME_PAGE_ID", "123")


def test_fb_latest_reel_reads_each_number(monkeypatch, fb_creds):
    def fake(method, url, params=None, **kw):
        if url.endswith("/123/video_reels"):
            return {"data": [{"id": "v9", "permalink_url": "/reel/v9",
                              "created_time": "2026-11-01T09:00:00+0000"}]}
        if url.endswith("/v9/video_insights"):
            m = params["metric"]
            if m == "blue_reels_play_count":   # replays left out — never preferred
                return {"data": [{"name": m, "values": [{"value": 2500}]}]}
            if m == "fb_reels_total_plays":
                return {"data": [{"name": m, "values": [{"value": 4321}]}]}
            return {"data": [{"name": m, "values": [{"value": {"SHARE": 6}}]}]}
        if params["fields"].startswith("likes"):
            return {"likes": {"summary": {"total_count": 40}}}
        raise fb.GraphError("(#10) comments need permission")
    monkeypatch.setattr(fb, "_call", fake)
    data, why = fb.latest_reel("acme")
    assert why == ""
    assert data["url"] == "https://www.facebook.com/reel/v9"
    assert (data["views"], data["likes"], data["comments"], data["shares"]) == \
        (4321, 40, None, 6)


def test_fb_views_are_the_count_facebook_shows(monkeypatch, fb_creds):
    """The listing's `views` field is the number on the Page (replays
    included); no insights call is needed for it."""
    metrics = []

    def fake(method, url, params=None, **kw):
        if url.endswith("/123/video_reels"):
            assert "views" in params["fields"]
            return {"data": [{"id": "v9", "views": 77938}]}
        if url.endswith("/video_insights"):
            metrics.append(params["metric"])
            return {"data": []}
        return {}
    monkeypatch.setattr(fb, "_call", fake)
    data, _ = fb.latest_reel("acme")
    assert data["views"] == 77938
    assert metrics == ["post_video_social_actions"]


def test_fb_listing_without_the_views_field_still_works(monkeypatch, fb_creds):
    def fake(method, url, params=None, **kw):
        if url.endswith("/123/video_reels"):
            if "views" in params["fields"]:
                raise fb.GraphError("(#100) Tried accessing nonexisting field (views)")
            return {"data": [{"id": "v9"}]}
        if url.endswith("/video_insights") and params["metric"] == "fb_reels_total_plays":
            return {"data": [{"name": "fb_reels_total_plays", "values": [{"value": 12}]}]}
        return {"data": []}
    monkeypatch.setattr(fb, "_call", fake)
    data, why = fb.latest_reel("acme")
    assert why == "" and data["views"] == 12


def test_fb_latest_reel_no_reels(monkeypatch, fb_creds):
    monkeypatch.setattr(fb, "_call", lambda *a, **k: {"data": []})
    assert fb.latest_reel("acme") == (None, "no reels yet")


def test_youtube_channel_url():
    assert stats.channel_url("@news") == "https://www.youtube.com/@news/shorts"
    assert stats.channel_url("news") == "https://www.youtube.com/@news/shorts"
    assert stats.channel_url("UC" + "x" * 22) == \
        "https://www.youtube.com/channel/UC" + "x" * 22 + "/shorts"
    assert stats.channel_url("https://www.youtube.com/@news/videos") == \
        "https://www.youtube.com/@news/shorts"
