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
