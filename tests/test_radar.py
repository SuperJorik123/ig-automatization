"""Radar (modules/radar) — offline: no Telegram, Reddit, OpenRouter or feeds."""
import asyncio
import datetime as dt
import json

import pytest

from modules.radar import alert as A, calendar_, cluster, score as SC, settings as S
from modules.radar import spikes, text as T
from modules.radar.items import Item
from modules.radar.pipeline import Pipeline
from modules.radar.sources import hazards, reddit
from modules.radar.sources import telegram as tgsrc
from modules.radar.store import Store

NOW = 1_800_000_000.0


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


class FakeNotifier:
    def __init__(self):
        self.alerts, self.updates, self.hazards, self.texts = [], [], [], []
        self._id = 100

    async def alert(self, inc, items, video):
        self._id += 1
        self.alerts.append((dict(inc), len(items), video["source"] if video else None))
        return self._id

    async def update(self, inc, items, note, new_video, ping):
        self.updates.append((inc["id"], note, ping))

    async def hazard(self, item):
        self._id += 1
        self.hazards.append(item.text)
        return self._id

    async def text(self, html):
        self.texts.append(html)
        return 1


def _label(newsworthy=75, **kw):
    base = {"incident": True, "category": "fire", "place": "Brooklyn", "country": "United States",
            "us": True, "english": "yes", "graphic": False,
            "title": "Warehouse fire in Brooklyn", "newsworthy": newsworthy}
    base.update(kw)
    return base


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(S, "AI_MAX_PER_DAY", 100)
    monkeypatch.setattr(S, "ALERT_MIN_SCORE", 60)
    monkeypatch.setattr(S, "MAX_PER_HOUR", 0)
    monkeypatch.setattr(S, "BLOCK_CATEGORIES", set())
    monkeypatch.setattr(S, "US_BONUS", 10)
    clock = Clock()
    labels = []

    def labeler(text, thumb, source):
        labels.append(text)
        return _label()

    note = FakeNotifier()
    pipe = Pipeline(Store(":memory:"), note, labeler=labeler, clock=clock)
    return pipe, note, clock, labels


def _tg(ext, text, video=True, size=5_000_000, t=NOW - 60, chan="BNONews"):
    return Item(source=f"tg:@{chan}", ext_id=ext, time=t, text=text,
                link=f"https://t.me/{chan}/{ext}", has_video=video,
                media_size=size if video else 0, ref={"chat_id": -1, "msg_id": 1})


def run(coro):
    return asyncio.run(coro)


# --- text / cluster -----------------------------------------------------------
def test_tokens_drop_filler_links_and_handles():
    toks = T.tokens("BREAKING: Huge warehouse fire in Brooklyn https://t.me/x @bno video")
    assert toks == {"huge", "warehouse", "fire", "brooklyn"}


def test_cluster_joins_on_shared_words_not_on_one_word():
    inc = {"kind": "post", "place": "", "category": "", "tokens": "warehouse fire brooklyn huge"}
    assert cluster.matches(T.tokens("Massive fire at a Brooklyn warehouse"), None, inc)
    assert not cluster.matches(T.tokens("Forest fire in Spain"), None, inc)


def test_cluster_joins_on_same_place_and_category():
    inc = {"kind": "post", "place": "Brooklyn", "category": "fire", "tokens": "x y z"}
    assert cluster.matches({"other"}, {"place": "brooklyn", "category": "Fire"}, inc)


def test_cluster_never_joins_a_hazard():
    inc = {"kind": "hazard", "place": "Brooklyn", "category": "fire", "tokens": "fire brooklyn"}
    assert not cluster.matches({"fire", "brooklyn"}, {"place": "Brooklyn", "category": "fire"}, inc)


def test_merge_tokens_keeps_the_first_words_under_the_cap():
    old = " ".join(f"w{i:02d}" for i in range(cluster.MAX_TOKENS))
    merged = cluster.merge_tokens(old, {"zzz"})
    assert "zzz" not in merged and len(merged) == cluster.MAX_TOKENS


# --- score --------------------------------------------------------------------
def test_score_rewards_video_freshness_sources_us_and_velocity():
    low = SC.score(label=_label(50), has_video=False, sources=1, newest_age_s=7200, us=False)
    high = SC.score(label=_label(50), has_video=True, sources=3, newest_age_s=60, us=True,
                    velocity=3)
    assert low == 50 and high == 50 + 10 + 10 + 10 + 10 + 20


def test_score_caps_a_non_incident():
    assert SC.score(label=_label(90, incident=False), has_video=True, sources=1,
                    newest_age_s=7200, us=False) == 40


# --- pipeline -----------------------------------------------------------------
def test_video_alerts_once_and_a_repost_updates_it(env):
    pipe, note, clock, labels = env
    run(pipe.ingest(_tg("1", "Huge warehouse fire in Brooklyn")))
    assert len(note.alerts) == 1
    inc, n_items, video_src = note.alerts[0]
    assert inc["category"] == "fire" and inc["us"] == 1 and video_src == "tg:@BNONews"
    # Same video (same size) from another channel: no second alert, an update.
    run(pipe.ingest(_tg("9", "😳😳", chan="spectatorindex")))
    assert len(note.alerts) == 1
    assert note.updates and note.updates[0][2] is False   # not watched: no ping


def test_a_watched_incident_pings_when_it_grows(env):
    pipe, note, clock, _ = env
    run(pipe.ingest(_tg("1", "Huge warehouse fire in Brooklyn")))
    pipe.on_button("w", 1, None)
    run(pipe.ingest(_tg("2", "Brooklyn warehouse fire spreading", size=7_000_000, chan="OsintTv")))
    assert note.updates[-1][2] is True


def test_text_only_posts_never_alert_and_cost_no_ai(env):
    pipe, note, clock, labels = env
    run(pipe.ingest(_tg("1", "Explosion reported in Lyon city centre", video=False)))
    assert note.alerts == [] and labels == []


def test_text_post_strengthens_a_later_video(env):
    pipe, note, clock, _ = env
    run(pipe.ingest(_tg("1", "Huge warehouse fire in Brooklyn right now", video=False,
                        chan="spectatorindex")))
    run(pipe.ingest(_tg("2", "Huge warehouse fire Brooklyn footage")))
    assert len(note.alerts) == 1 and note.alerts[0][1] == 2


def test_low_score_does_not_alert(env, monkeypatch):
    pipe, note, clock, _ = env
    pipe.labeler = lambda *a: _label(10, us=False)
    run(pipe.ingest(_tg("1", "Something", t=NOW - 7200)))
    assert note.alerts == []


def test_blocked_category_never_alerts(env, monkeypatch):
    pipe, note, clock, _ = env
    monkeypatch.setattr(S, "BLOCK_CATEGORIES", {"fire"})
    run(pipe.ingest(_tg("1", "Huge warehouse fire in Brooklyn")))
    assert note.alerts == []


def test_ignored_incident_stays_quiet(env):
    pipe, note, clock, _ = env
    run(pipe.ingest(_tg("1", "Huge warehouse fire in Brooklyn")))
    pipe.on_button("xr", 1, "boring")
    run(pipe.ingest(_tg("2", "Brooklyn warehouse fire", size=8_000_000, chan="OsintTv")))
    assert note.updates == []


def test_ai_budget_falls_back_to_rules(env, monkeypatch):
    pipe, note, clock, labels = env
    monkeypatch.setattr(S, "AI_MAX_PER_DAY", 1)
    run(pipe.ingest(_tg("1", "Huge warehouse fire in Brooklyn")))
    run(pipe.ingest(_tg("2", "Riot downtown Paris tonight", size=1)))
    assert len(labels) == 1


def test_hourly_cap_holds_alerts(env, monkeypatch):
    pipe, note, clock, _ = env
    monkeypatch.setattr(S, "MAX_PER_HOUR", 1)
    run(pipe.ingest(_tg("1", "Huge warehouse fire in Brooklyn")))
    pipe.labeler = lambda *a: _label(80, category="riot", place="Paris", title="Riot in Paris")
    run(pipe.ingest(_tg("2", "Riot downtown Paris tonight", size=1)))
    assert len(note.alerts) == 1


def test_reddit_post_waits_until_it_rises(env):
    pipe, note, clock, labels = env
    it = Item(source="reddit:PublicFreakout", ext_id="abc", time=NOW - 600,
              text="Man fights cashier in Houston", has_video=True,
              metrics={"ups": 5, "velocity": 0.5})
    run(pipe.ingest(it))
    assert labels == []
    it.metrics = {"ups": 400, "velocity": 4.0}
    run(pipe.ingest(it))
    assert len(labels) == 1 and len(note.alerts) == 1


def test_hazard_alerts_once_when_it_qualifies(env):
    pipe, note, clock, _ = env
    q = Item(source="usgs", kind="hazard", ext_id="q1", time=NOW, text="M5.1 — LA, CA",
             metrics={"alert": False, "category": "earthquake"})
    run(pipe.ingest(q))
    assert note.hazards == []
    q.metrics = {"alert": True, "category": "earthquake"}
    run(pipe.ingest(q))
    run(pipe.ingest(q))
    assert note.hazards == ["M5.1 — LA, CA"]


def test_seed_records_without_alerting(env):
    pipe, note, clock, _ = env
    q = Item(source="gdacs", kind="hazard", ext_id="g1", time=NOW, text="Red flood",
             metrics={"alert": True})
    pipe.seed([q])
    run(pipe.ingest(q))
    assert note.hazards == []


# --- spikes -------------------------------------------------------------------
def test_spike_needs_several_sources_and_beats_the_baseline(env, monkeypatch):
    pipe, note, clock, _ = env
    monkeypatch.setattr(S, "SPIKE_MIN_HITS", 6)
    monkeypatch.setattr(S, "SPIKE_MIN_SOURCES", 3)
    hour = int(NOW // 3600)
    st = pipe.store
    for h in range(hour - 100, hour - 2):          # history: "lyon" ~ once a day
        if h % 24 == 0:
            st.add_terms({"lyon"}, h, "tg:@a")
    st.add_terms({"quiet"}, hour - 100, "tg:@a")
    for i in range(9):
        st.add_terms({"lyon"}, hour, f"tg:@c{i % 4}")
    found = spikes.check(st, NOW)
    assert [f["term"] for f in found] == ["lyon"]
    # One source alone never spikes.
    for _ in range(20):
        st.add_terms({"spam"}, hour, "tg:@one")
    assert "spam" not in [f["term"] for f in spikes.check(st, NOW)]


def test_spike_is_sent_once_then_cools_down(env, monkeypatch):
    pipe, note, clock, _ = env
    monkeypatch.setattr(S, "SPIKE_MIN_HITS", 2)
    hour = int(NOW // 3600)
    for i in range(6):
        pipe.store.add_terms({"lyon"}, hour, f"tg:@c{i}")
    assert run(pipe.check_spikes()) == ["lyon"]
    assert run(pipe.check_spikes()) == []
    assert "lyon" in note.texts[0]


def test_spike_about_an_alerted_incident_stays_quiet(env, monkeypatch):
    pipe, note, clock, _ = env
    monkeypatch.setattr(S, "SPIKE_MIN_HITS", 2)
    run(pipe.ingest(_tg("1", "Huge warehouse fire in Brooklyn")))
    hour = int(NOW // 3600)
    for i in range(6):
        pipe.store.add_terms({"brooklyn"}, hour, f"tg:@c{i}")
    assert run(pipe.check_spikes()) == []


# --- alert text / buttons -----------------------------------------------------
def test_callbacks_round_trip():
    assert A.parse_callback("r:w:5") == ("w", 5, None)
    assert A.parse_callback("r:xr:5:boring") == ("xr", 5, "boring")
    assert A.parse_callback("r:xr:5:nonsense") is None
    assert A.parse_callback("b:go") is None


def test_incident_text_escapes_and_names_sources(env):
    pipe, note, clock, _ = env
    run(pipe.ingest(_tg("1", "Fire <b>Brooklyn</b> warehouse huge")))
    inc = pipe.store.incident(1)
    txt = A.incident_text(inc, pipe.store.incident_items(1), NOW)
    assert "FIRE" in txt and "BNONews" in txt and "<b>Brooklyn</b>" not in txt.split("\n", 1)[1]
    assert "forward the video" in txt


def test_reasons_keyboard_has_every_reason_and_back():
    datas = [d for row in A.reasons_keyboard(3) for _, d in row]
    assert datas[-1] == "r:b:3" and len(datas) == len(A.REASONS) + 1


# --- sources ------------------------------------------------------------------
def test_reddit_parse_keeps_young_video_posts(monkeypatch):
    monkeypatch.setattr(S, "REDDIT_MAX_AGE_H", 4)
    listing = {"data": {"children": [
        {"data": {"id": "a", "subreddit": "PublicFreakout", "title": "Fight", "is_video": True,
                  "created_utc": NOW - 1200, "ups": 200, "permalink": "/r/x/a"}},
        {"data": {"id": "b", "subreddit": "news", "title": "Text", "is_video": False,
                  "domain": "self.news", "created_utc": NOW - 60, "ups": 900}},
        {"data": {"id": "c", "subreddit": "news", "title": "Old", "is_video": True,
                  "created_utc": NOW - 6 * 3600, "ups": 9000}},
    ]}}
    items = reddit.parse(listing, NOW)
    assert [i.ext_id for i in items] == ["a"]
    assert items[0].metrics["velocity"] == 10.0


def test_quake_bar_wants_people_not_seismographs():
    assert not hazards.quake_alert({"mag": 6.1, "place": "Pacific-Antarctic Ridge", "alert": "green"})
    assert not hazards.quake_alert({"mag": 5.2, "place": "169 km S of Attu Station, Alaska"})
    assert hazards.quake_alert({"mag": 4.9, "place": "5 km N of Los Angeles, CA", "felt": 40})
    assert hazards.quake_alert({"mag": 6.4, "place": "Turkey", "alert": "orange"})
    assert hazards.quake_alert({"mag": 7.2, "place": "somewhere at sea"})


def test_nws_only_confirmed_tornadoes_and_dangerous_floods():
    assert hazards.nws_alert({"event": "Tornado Warning",
                              "parameters": {"tornadoDetection": ["OBSERVED"]}})
    assert not hazards.nws_alert({"event": "Tornado Warning",
                                  "parameters": {"tornadoDetection": ["RADAR INDICATED"]}})
    assert hazards.nws_alert({"event": "Flash Flood Warning",
                              "parameters": {"flashFloodDamageThreat": ["CATASTROPHIC"]}})
    assert not hazards.nws_alert({"event": "Flash Flood Warning", "parameters": {}})


def test_nws_groups_an_outbreak_per_state_and_3_hours():
    feat = lambda i: {"properties": {"event": "Tornado Warning", "sent": "2026-11-19T10:0%d:00-05:00" % i,
                                     "areaDesc": "X", "geocode": {"UGC": ["OKC001"]}}}
    items = hazards.parse_nws({"features": [feat(1), feat(2)]})
    assert items[0].ext_id == items[1].ext_id and items[0].ext_id.startswith("Tornado Warning|OK|")


def test_gdacs_red_alerts_but_drought_never():
    xml = """<?xml version="1.0"?><rss xmlns:gdacs="http://www.gdacs.org"
    xmlns:geo="http://www.w3.org/2003/01/geo/wgs84_pos#"><channel>
    <item><title>Red flood</title><guid>FL1</guid><gdacs:alertlevel>Red</gdacs:alertlevel>
    <gdacs:eventtype>FL</gdacs:eventtype><gdacs:country>Spain</gdacs:country>
    <geo:Point><geo:lat>39.5</geo:lat><geo:long>-0.4</geo:long></geo:Point></item>
    <item><title>Drought</title><guid>DR1</guid><gdacs:alertlevel>Red</gdacs:alertlevel>
    <gdacs:eventtype>DR</gdacs:eventtype></item></channel></rss>"""
    items = hazards.parse_gdacs(xml)
    assert [i.metrics["alert"] for i in items] == [True, False]
    assert items[0].lat == 39.5 and "Snap Map" in A.hazard_text(items[0], NOW)


def test_nhc_alerts_hurricanes_and_again_at_major():
    storms = lambda kt: {"activeStorms": [{"id": "al05", "name": "Erin", "classification": "HU",
                                           "intensity": str(kt)}]}
    a = hazards.parse_nhc(storms(80), NOW)[0]
    b = hazards.parse_nhc(storms(115), NOW)[0]
    assert a.metrics["alert"] and a.ext_id != b.ext_id


def test_telegram_message_to_item_spots_video_and_albums():
    class Attr:
        pass

    video_attr = type("DocumentAttributeVideo", (), {"duration": 12})()
    doc = type("Doc", (), {"mime_type": "video/mp4", "size": 999, "attributes": [video_attr]})()
    msg = type("Msg", (), {"id": 7, "grouped_id": 55, "message": "hi", "date": None,
                           "document": doc, "video": doc})()
    it = tgsrc.to_item(msg, "BNONews", -100)
    assert it.has_video and it.media_size == 999 and it.ext_id == "g55"
    assert it.link == "https://t.me/BNONews/7"
    gif_attr = type("DocumentAttributeAnimated", (), {})()
    gif = type("Doc", (), {"mime_type": "video/mp4", "size": 5, "attributes": [gif_attr]})()
    msg.document = msg.video = gif
    assert not tgsrc.to_item(msg, "BNONews", -100).has_video


def test_channels_file_loads():
    handles = tgsrc.load_channels()
    assert "BNONews" in handles and all(not h.startswith("@") for h in handles)


# --- calendar -----------------------------------------------------------------
def test_black_friday_follows_thanksgiving():
    dates = dict((t.split(" —")[0], d) for d, t in calendar_.recurring(2026))
    assert dates["Thanksgiving"] == dt.date(2026, 11, 26)
    assert dates["Black Friday"] == dt.date(2026, 11, 27)


def test_upcoming_merges_custom_dates(tmp_path):
    f = tmp_path / "cal.json"
    f.write_text(json.dumps({"events": [{"date": "2026-11-21", "title": "Derby"},
                                        {"date": "bad", "title": "x"}]}))
    up = calendar_.upcoming(dt.date(2026, 11, 19), path=str(f))
    titles = [t for _, t in up]
    assert titles[0] == "Derby" and any("Thanksgiving" in t for t in titles)


def _jar(tmp_path, monkeypatch):
    monkeypatch.setattr(reddit, "ARCTIC_PAUSE_S", 0)
    monkeypatch.setattr(reddit, "LIVE_PAUSE_S", 0)
    monkeypatch.setattr(reddit, "_dead", {})
    monkeypatch.setattr(reddit, "_turn", {"n": 0})
    monkeypatch.setattr(reddit, "notices", [])
    monkeypatch.setattr(S, "REDDIT_CHECKS_MAX", 3)
    monkeypatch.setattr(S, "REDDIT_RECHECK_M", 30)
    jar = tmp_path / "reddit_cookies.txt"
    jar.write_text("# Netscape HTTP Cookie File\n"
                   ".reddit.com\tTRUE\t/\tTRUE\t0\treddit_session\tspare\n"
                   ".example.com\tTRUE\t/\tTRUE\t0\tother\tx\n")
    monkeypatch.setattr(S, "REDDIT_COOKIES_FILE", str(jar))
    return jar


def test_reddit_is_off_without_the_spare_accounts_cookies(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "REDDIT_COOKIES_FILE", str(tmp_path / "none.txt"))
    assert not reddit.enabled()
    _jar(tmp_path, monkeypatch)
    assert reddit.enabled()
    assert reddit._cookies() == {"reddit_session": "spare"}


def test_reddit_never_defaults_to_the_news_bots_prod_session():
    assert "credentials" not in S.REDDIT_COOKIES_FILE.replace("\\", "/").split("/")


class _Resp:
    def __init__(self, data, status=200, text=None):
        self._d, self.status_code = data, status
        self.text = text if text is not None else "{}"
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)
    def json(self):
        return self._d


def _post(pid, ups, age_s, video=True):
    return {"id": pid, "subreddit": "news", "title": f"Post {pid}", "is_video": video,
            "domain": "v.redd.it" if video else "self.news",
            "created_utc": NOW - age_s, "ups": ups, "permalink": f"/r/news/{pid}"}


def _wire(monkeypatch, arctic_posts, live_ups, calls):
    """Arctic Shift returns `arctic_posts`; Reddit says post id has live_ups[id]."""
    monkeypatch.setattr(reddit.requests, "get",
                        lambda url, params, **kw: _Resp({"data": arctic_posts}))

    def post(url, params, cookies, **kw):
        pid = url.split("/comments/")[1].split(".")[0]
        calls.append((pid, cookies))
        d = next(p for p in arctic_posts if p["id"] == pid)
        body = [{"data": {"children": [{"data": dict(d, ups=live_ups[pid])}]}}, {}]
        return _Resp(body, text="[...]")

    monkeypatch.setattr(reddit, "_get", post)


def test_reddit_finds_on_arctic_shift_and_reads_live_upvotes(tmp_path, monkeypatch):
    _jar(tmp_path, monkeypatch)
    monkeypatch.setattr(reddit, "_track", {})
    monkeypatch.setattr(S, "REDDIT_SUBS", ["news"])
    monkeypatch.setattr(S, "REDDIT_MAX_AGE_H", 4)
    calls = []
    _wire(monkeypatch, [_post("a", 1, 1200), _post("b", 1, 600, video=False)], {"a": 400}, calls)
    items = reddit.fetch(NOW)
    assert calls == [("a", {"reddit_session": "spare"})]       # the text post is never read
    assert [i.ext_id for i in items] == ["a"]
    assert items[0].metrics["ups"] == 400 and items[0].metrics["velocity"] == 20.0


def test_reddit_spares_the_account_between_polls(tmp_path, monkeypatch):
    _jar(tmp_path, monkeypatch)
    monkeypatch.setattr(reddit, "_track", {})
    monkeypatch.setattr(S, "REDDIT_SUBS", ["news"])
    monkeypatch.setattr(S, "REDDIT_MAX_AGE_H", 4)
    calls = []
    _wire(monkeypatch, [_post("slow", 1, 600), _post("hot", 1, 600)],
          {"slow": 2, "hot": 500}, calls)
    reddit.fetch(NOW)
    assert sorted(c[0] for c in calls) == ["hot", "slow"]
    calls.clear()
    reddit.fetch(NOW + 60)                       # too soon for a recheck
    assert calls == []
    recheck = S.REDDIT_RECHECK_M * 60
    reddit.fetch(NOW + recheck)                  # "hot" already passed the bar
    assert [c[0] for c in calls] == ["slow"]
    for k in range(2, 7):
        reddit.fetch(NOW + k * recheck)
    assert sum(c[0] == "slow" for c in calls) == S.REDDIT_CHECKS_MAX - 1   # 3 in all


def test_reddit_names_a_dead_spare_session(tmp_path, monkeypatch):
    _jar(tmp_path, monkeypatch)
    monkeypatch.setattr(reddit, "_track", {})
    monkeypatch.setattr(S, "REDDIT_SUBS", ["news"])
    monkeypatch.setattr(reddit.requests, "get",
                        lambda *a, **k: _Resp({"data": [_post("a", 1, 60)]}))
    monkeypatch.setattr(reddit, "_get", lambda *a, **k: _Resp({}, 403, "<html>blocked"))
    assert reddit.fetch(NOW) == []
    assert len(reddit.notices) == 1 and "reddit_cookies.txt" in reddit.notices[0]
    with pytest.raises(RuntimeError, match="every spare account"):
        reddit.fetch(NOW + 60)


def test_reddit_requests_take_turns_across_spare_accounts(tmp_path, monkeypatch):
    _jar(tmp_path, monkeypatch)
    (tmp_path / "reddit_cookies_2.txt").write_text(
        "# Netscape HTTP Cookie File\n"
        ".reddit.com\tTRUE\t/\tTRUE\t0\treddit_session\ttwo\n")
    monkeypatch.setattr(reddit, "_track", {})
    monkeypatch.setattr(S, "REDDIT_SUBS", ["news"])
    posts = [_post(x, 1, 60) for x in "abcd"]
    calls = []
    _wire(monkeypatch, posts, {x: 1 for x in "abcd"}, calls)
    reddit.fetch(NOW)
    assert [c[1]["reddit_session"] for c in calls] == ["spare", "two", "spare", "two"]


def test_reddit_carries_on_when_one_spare_account_is_refused(tmp_path, monkeypatch):
    _jar(tmp_path, monkeypatch)
    (tmp_path / "reddit_cookies_2.txt").write_text(
        "# Netscape HTTP Cookie File\n"
        ".reddit.com\tTRUE\t/\tTRUE\t0\treddit_session\ttwo\n")
    monkeypatch.setattr(reddit, "_track", {})
    monkeypatch.setattr(S, "REDDIT_SUBS", ["news"])
    posts = [_post(x, 1, 60) for x in "abc"]
    monkeypatch.setattr(reddit.requests, "get", lambda *a, **k: _Resp({"data": posts}))
    used = []

    def post(url, params, cookies, **kw):
        used.append(cookies["reddit_session"])
        if cookies["reddit_session"] == "spare":
            return _Resp({}, 403, "<html>blocked")
        pid = url.split("/comments/")[1].split(".")[0]
        d = next(p for p in posts if p["id"] == pid)
        return _Resp([{"data": {"children": [{"data": dict(d, ups=500)}]}}, {}], text="[")

    monkeypatch.setattr(reddit, "_get", post)
    items = reddit.fetch(NOW)
    assert used == ["spare", "two", "two"] and len(items) == 2
    assert len(reddit.notices) == 1 and "1 account(s) left" in reddit.notices[0]


def test_reddit_raises_only_when_every_sub_fails_on_arctic_shift(tmp_path, monkeypatch):
    _jar(tmp_path, monkeypatch)
    monkeypatch.setattr(S, "REDDIT_SUBS", ["a1", "b2"])

    def flaky(url, params, **kw):
        if params["subreddit"] == "a1":
            raise ConnectionError("down")
        return _Resp({"data": []})

    monkeypatch.setattr(reddit.requests, "get", flaky)
    assert reddit.fetch(NOW) == []
    monkeypatch.setattr(reddit.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))
    with pytest.raises(RuntimeError, match="Arctic Shift"):
        reddit.fetch(NOW)


# --- 2026-11-23 alert-quality rules ---------------------------------------------
def test_gate_wants_a_real_event_rated_high_enough():
    assert not SC.passes([_label(95, incident=False)], us=True)       # a talking head
    assert not SC.passes([_label(70, us=False)], us=False)            # routine, abroad
    assert SC.passes([_label(75, us=False)], us=False)
    assert SC.passes([_label(62)], us=True)                           # US bar is lower
    assert not SC.passes([], us=True)                                 # no AI verdict
    assert SC.passes([_label(20, incident=False), _label(80)], us=False)


def test_talking_head_never_alerts_however_many_channels_post_it(env):
    pipe, note, clock, _ = env
    pipe.labeler = lambda *a: _label(90, incident=False, category="politics",
                                     place="White House", title="Trump speaks")
    for n, ch in enumerate(["ClashReport", "osintlive", "wfwitness", "remarks"]):
        run(pipe.ingest(_tg(str(n), "Trump: we are doing great", size=n + 1, chan=ch)))
    assert note.alerts == []


def test_no_ai_verdict_means_no_alert(env, monkeypatch):
    pipe, note, clock, labels = env
    monkeypatch.setattr(S, "AI_MAX_PER_DAY", 0)
    run(pipe.ingest(_tg("1", "Huge warehouse fire in Brooklyn")))
    run(pipe.ingest(_tg("2", "Huge warehouse fire Brooklyn now", size=9, chan="osintlive")))
    assert labels == [] and note.alerts == []


def test_one_story_in_many_words_and_categories_is_one_alert(env):
    pipe, note, clock, _ = env
    posts = [("war", "Benghazi, Libya", "Russian warship destroyed by drones"),
             ("explosion", "Benghazi port", "Corvette explodes, huge fireball"),
             ("fire", "Benghazi", "Ship burning off the coast tonight")]
    for n, (cat, place, text) in enumerate(posts):
        pipe.labeler = lambda *a, c=cat, p=place, t=text: _label(
            85, us=False, category=c, place=p, country="Libya", title=t)
        clock.t = NOW + n * 3 * 3600          # hours apart, still within the window
        run(pipe.ingest(_tg(str(n), text, size=n + 1, chan=f"c{n}", t=clock.t - 60)))
    assert len(note.alerts) == 1 and len(note.updates) == 2


def test_place_match_needs_a_real_place_and_agreeing_countries():
    inc = {"kind": "post", "place": "southern Lebanon", "country": "Lebanon", "tokens": ""}
    assert not cluster.matches(set(), {"place": "northern Lebanon", "country": "Lebanon"}, inc)
    inc = {"kind": "post", "place": "Kyiv", "country": "Ukraine", "tokens": ""}
    assert cluster.matches(set(), {"place": "Kyiv Oblast", "country": "Ukraine"}, inc)
    inc = {"kind": "post", "place": "Paris", "country": "France", "tokens": ""}
    assert not cluster.matches(set(), {"place": "Paris", "country": "United States"}, inc)


def test_small_us_quakes_never_alert_however_many_felt_them(monkeypatch):
    monkeypatch.setattr(S, "QUAKE_FLOOR_US", 4.0)
    assert not hazards.quake_alert({"mag": 2.5, "place": "8 km NW of Pinnacles, CA", "felt": 400})
    assert hazards.quake_alert({"mag": 4.4, "place": "1 km NNW of Commerce, CA", "felt": 150})


def test_one_quake_filed_twice_alerts_once(env):
    pipe, note, clock, _ = env
    for n, place in enumerate(["8 km NW of Pinnacles, CA", "10 km NNW of Pinnacles, CA"]):
        run(pipe.ingest(Item(source="usgs", kind="hazard", ext_id=f"q{n}", time=NOW,
                             text=f"M5.{n} earthquake — {place}", place=place,
                             metrics={"alert": True, "category": "earthquake"})))
    assert len(note.hazards) == 1


def test_snow_squalls_are_not_watched():
    assert "Snow Squall Warning" not in hazards.NWS_EVENTS
    assert not hazards.nws_alert({"event": "Snow Squall Warning"})


def test_a_place_groups_a_story_for_a_day_at_most():
    young = {"kind": "post", "place": "Kyiv", "country": "Ukraine", "tokens": "",
             "created": NOW, "updated": NOW + 3600}
    old = dict(young, updated=NOW + cluster.PLACE_STORY_MAX_S + 1)
    lab = {"place": "Kyiv", "country": "Ukraine"}
    assert cluster.matches(set(), lab, young) and not cluster.matches(set(), lab, old)
