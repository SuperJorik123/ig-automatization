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
                               "posted_at": "2026-10-31T10:00:00+0000",
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
    assert "Latest reels" in text
    assert "👁 <b>6.1K views in total</b>" in text
    assert "📸 5.1K · 📘 1K" in text
    assert "🏆 Best: <b>big</b> on Instagram — 5K views" in text
    assert "🔥 Most engaging: <b>big</b> on Instagram — 6.7%" in text  # 334/5000
    assert "🐢 Weakest: <b>small</b> on Instagram — 100 views" in text
    table = text[text.index("<pre>") + 5:text.index("</pre>")].splitlines()
    assert table[0].split() == ["IG", "FB"]
    assert table[1].split() == ["big", "5K", "1K"]
    assert table[2].split() == ["small", "100", "·"]     # no FB account
    assert table[3].split() == ["broken", "·", "!"]      # FB read failed
    assert "• broken FB: token lacks permission" in text


def test_render_never_calls_a_fresh_reel_the_weakest():
    results = [_r("a", "ig", 5000), _r("b", "ig", 900), _r("c", "ig", 3)]
    results[2]["data"]["posted_at"] = "2026-11-01T11:30:00+0000"   # 30 min old
    assert "🐢 Weakest: <b>b</b>" in report.render(results, NOW)[0]


def test_render_escapes_names_and_splits_long_reports():
    results = [_r(f"brand<{i}>", "ig", i * 10, likes=1, comments=0)
               for i in range(1, 400)]
    pages = report.render(results, NOW)
    assert len(pages) > 1
    assert all(len(p) <= report.MSG_LIMIT for p in pages)
    assert all(p.count("<pre>") == p.count("</pre>") == 1 for p in pages)
    assert "brand&lt;399&gt;" in pages[0]


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


# --- the 24 h report ---------------------------------------------------------- #

def _reel(views, hours_ago, likes=0, url="https://x/r"):
    t = (NOW - dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    return {"url": url, "posted_at": t, "views": views, "likes": likes,
            "comments": 0, "shares": 0}


def _rr(brand, p, reels=None, error=""):
    return {"brand": brand, "platform": p, "reels": reels,
            "error": error if reels is None else ""}


def test_render_recent_sums_every_reel_of_the_window():
    results = [
        _rr("big", "ig", [_reel(5000, 3, likes=400, url="https://ig/a"),
                          _reel(2000, 20)]),
        _rr("big", "fb", [_reel(1000, 10)]),
        _rr("small", "ig", [_reel(50, 12)]),
        _rr("quiet", "ig", []),
        _rr("broken", "fb", error="Session has expired (code 190)"),
    ]
    text = report.render_recent(results, NOW)[0]
    assert "Reels of the last 24 h" in text
    assert "👁 <b>8.1K views</b> on 4 reels" in text
    assert "📸 7K (3) · 📘 1K (1)" in text
    assert '🏆 Best reel: <b>big</b> on Instagram — 5K views (3h ago) · <a href="https://ig/a">open</a>' in text
    assert "🐢 Weakest: <b>small</b> on Instagram — 50 views" in text
    assert "😴 Nothing posted: quiet" in text
    table = [l.split() for l in text[text.index("<pre>") + 5:text.index("</pre>")].splitlines()]
    assert table[0] == ["IG", "FB", "#"]
    assert table[1] == ["big", "7K", "1K", "3"]
    assert table[2] == ["small", "50", "·", "1"]
    assert ["quiet", "—", "·", "0"] in table
    assert ["broken", "·", "!", "0"] in table
    assert "• broken FB: token expired" in text


def test_render_recent_with_no_reels_at_all():
    text = report.render_recent([_rr("a", "ig", [])], NOW)[0]
    assert "<b>0 views</b> on 0 reels" in text and "Best reel" not in text


def test_collect_recent_passes_the_window_and_keeps_failures():
    since = NOW - dt.timedelta(hours=24)
    seen = []

    def ig(acct, s):
        seen.append((acct, s))
        return [_reel(10, 1)], ""
    out = report.collect_recent(
        [{"name": "a", "ig": "a_ig", "fb": "a_fb"}, {"name": "b", "yt": "chan"}],
        since, {"ig": ig, "fb": lambda a, s: (None, "boom"),
                "yt": lambda a, s: ([], "")})
    assert seen == [("a_ig", since)]
    by = {(r["brand"], r["platform"]): r for r in out}
    assert by[("a", "ig")]["reels"][0]["views"] == 10
    assert by[("a", "fb")]["reels"] is None and by[("a", "fb")]["error"] == "boom"
    assert by[("b", "yt")]["error"] == "no handle"     # a channel, no public handle


def test_ig_recent_reels_keeps_only_reels_inside_the_window(monkeypatch):
    monkeypatch.setenv("IG_GRAPH_ACME_ACCESS_TOKEN", "IGAA")
    monkeypatch.setenv("IG_GRAPH_ACME_USER_ID", "178")

    def fake(method, url, params=None, **kw):
        if url.endswith("/178/media"):
            return {"data": [
                {"id": "pin", "media_product_type": "REELS",      # pinned, old
                 "timestamp": "2026-10-01T10:00:00+0000"},
                {"id": "r2", "media_product_type": "REELS",
                 "timestamp": "2026-11-01T10:00:00+0000"},
                {"id": "p1", "media_type": "IMAGE", "media_product_type": "FEED",
                 "timestamp": "2026-11-01T09:00:00+0000"},
                {"id": "r1", "media_type": "VIDEO", "media_product_type": "REELS",
                 "timestamp": "2026-10-31T13:00:00+0000"},
                {"id": "old", "media_product_type": "REELS",
                 "timestamp": "2026-10-31T11:00:00+0000"},
            ]}
        mid = url.split("/")[-2]
        return {"data": [{"name": "views", "values": [{"value": {"r2": 7, "r1": 3}[mid]}]}]}
    monkeypatch.setattr(graph, "_call", fake)
    reels, why = graph.recent_reels("acme", NOW - dt.timedelta(hours=24))
    assert why == "" and [r["views"] for r in reels] == [7, 3]


def test_fb_recent_reels_keeps_only_reels_inside_the_window(monkeypatch, fb_creds):
    def fake(method, url, params=None, **kw):
        if url.endswith("/123/video_reels"):
            return {"data": [
                {"id": "v2", "views": 900, "created_time": "2026-11-01T08:00:00+0000"},
                {"id": "v1", "views": 50, "created_time": "2026-10-30T08:00:00+0000"},
            ]}
        return {"data": []}
    monkeypatch.setattr(fb, "_call", fake)
    reels, why = fb.recent_reels("acme", NOW - dt.timedelta(hours=24))
    assert why == "" and [r["views"] for r in reels] == [900]


def test_youtube_recent_shorts_stops_at_the_first_old_one(monkeypatch):
    import sys
    import types
    infos = {
        "https://www.youtube.com/shorts/a": {"id": "a", "view_count": 40,
                                             "timestamp": NOW.timestamp() - 3600},
        "https://www.youtube.com/shorts/b": {"id": "b", "view_count": 9,
                                             "upload_date": "20261101"},   # date only, today
        "https://www.youtube.com/shorts/c": {"id": "c", "view_count": 1,
                                             "timestamp": NOW.timestamp() - 90000},
    }
    opened = []

    class YDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            if url.endswith("/shorts") and self.opts.get("extract_flat"):
                return {"entries": [{"id": k[-1], "url": k} for k in infos]
                        + [{"id": "d", "url": "https://www.youtube.com/shorts/d"}]}
            opened.append(url)
            return infos[url]
    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=YDL))
    reels, why = stats.recent_shorts("@chan", NOW - dt.timedelta(hours=24))
    assert why == "" and [r["views"] for r in reels] == [40, 9]
    assert opened[-1].endswith("/c")                    # d never opened


def _v(vid, views, hours_ago, caption, url=None):
    r = _reel(views, hours_ago, url=url or f"https://x/{vid}")
    return dict(r, id=vid, caption=caption)


def test_group_videos_ties_copies_through_the_reel_log():
    squid = "Giant squid lunges at the camera\n\nMore text #squid"
    dog = "Dog swept away by a wave #oregon"
    results = [
        _rr("atlas", "ig", [_v("i1", 1200, 3, squid), _v("i2", 40, 2, dog)]),
        # the FB copy went up 2 minutes after atlas's IG squid reel
        _rr("atlas", "fb", [dict(_v("f1", 300, 3, "x"),
                                 posted_at=(NOW - dt.timedelta(hours=3, minutes=-2))
                                 .strftime("%Y-%m-%dT%H:%M:%S+0000"))]),
        _rr("wswire", "ig", [_v("i3", 900, 3, "Squid attacks a submarine camera")]),
        _rr("solo", "ig", [_v("i9", 5, 1, "Hand-posted clip")]),
    ]
    keys = {"i1": "squid.mp4", "i3": "squid.mp4", "i2": "dog.mp4"}
    videos = report.group_videos(results, keys)
    assert [v["views"] for v in videos] == [2400, 40, 5]
    squid_v = videos[0]
    assert squid_v["title"] == "Giant squid lunges at the camera"   # best copy's hook
    assert [(x["brand"], x["platform"]) for x in squid_v["reels"]] == \
        [("atlas", "ig"), ("wswire", "ig"), ("atlas", "fb")]
    assert videos[1]["title"] == "Dog swept away by a wave"          # hashtags dropped

    text = report.render_recent(results, NOW, video_keys=keys)[0]
    assert "🎬 <b>Each video</b> (3)" in text
    assert "1. <b>Giant squid lunges at the camera</b> — 2.4K views, 3 reels" in text
    assert "    atlas 📸1.2K 📘300 · wswire 📸900" in text
    assert "3. <b>Hand-posted clip</b> — 5 views, 1 reel" in text


def test_group_videos_without_the_log_keeps_every_reel_apart():
    results = [_rr("a", "ig", [_v("i1", 10, 1, "Same"), _v("i2", 20, 2, "Same")])]
    assert len(report.group_videos(results, {})) == 2


def test_long_video_list_splits_into_pages():
    results = [_rr(f"b{i}", "ig", [_v(f"i{i}", i, 1, f"Video number {i} " + "x" * 40)])
               for i in range(200)]
    pages = report.render_recent(results, NOW)
    assert len(pages) > 1 and all(len(p) <= report.MSG_LIMIT for p in pages)
    assert sum(p.count("<pre>") for p in pages) >= 1
