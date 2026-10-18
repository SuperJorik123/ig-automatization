"""Offline: bundle mode in the news bot — starting a bundle from a plan card,
videos joining with no tap, the AI headline defaulting, Render (assignment,
carry-over, per-video captions), Publish (one scheduled post per account,
spread out), a restart, and Retry. Telegram, the renderer and the caption
model are stand-ins; the database is a throwaway one."""

import asyncio
import os
import types

import pytest

from shared import config

_STUBS = {
    "NEWS_BOT_TOKEN": "test:token",
    "TELEGRAM_CHAT_ID": "-100123",
    "TG_DESTINATIONS": [{"chat_id": "@somewhere", "lang": ""}],
}
_saved = {k: getattr(config, k) for k in _STUBS}
for _k, _v in _STUBS.items():
    if not getattr(config, _k):
        setattr(config, _k, _v)
from modules.telegram import bundle, news_bot  # noqa: E402
for _k, _v in _saved.items():
    setattr(config, _k, _v)


class _Msg:
    _next = 1000

    def __init__(self, message_id=None, chat=None, text=""):
        _Msg._next += 1
        self.message_id = message_id or _Msg._next
        self.chat = chat
        self.text = text
        self.edits = []

    async def edit_text(self, text, reply_markup=None):
        self.text = text
        self.edits.append((text, reply_markup))

    async def reply_text(self, text, reply_markup=None):
        return _Msg(chat=self.chat, text=text)


class _Chat:
    id = -100123

    def __init__(self):
        self.sent = []

    async def send_message(self, text, **kw):
        m = _Msg(chat=self, text=text)
        self.sent.append(m)
        return m


class _Q:
    def __init__(self, message, data=""):
        self.message, self.data = message, data
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)

    async def edit_message_text(self, text, reply_markup=None):
        await self.message.edit_text(text, reply_markup)


class _JobQueue:
    def __init__(self):
        self.jobs = []

    def run_once(self, cb, when, data=None, name=None):
        self.jobs.append(types.SimpleNamespace(cb=cb, when=when, data=data,
                                               name=name))

    def get_jobs_by_name(self, name):
        return [j for j in self.jobs if j.name == name]


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((text, kw))
        return _Msg(text=text)

    async def edit_message_text(self, text, **kw):
        pass


NAMES = [f"brand{i}" for i in range(10)]


@pytest.fixture
def env(store, tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    monkeypatch.setattr(news_bot, "_BUNDLE_DIR", str(media / "bundles"))
    monkeypatch.setattr(news_bot, "_SCHED_DIR", str(media / "scheduled"))
    monkeypatch.setattr(news_bot, "_BUSY_PATH", str(tmp_path / "busy"))
    monkeypatch.setattr(news_bot, "_track", lambda m: m)
    monkeypatch.setattr(news_bot, "CHAT_ID", -100123)
    monkeypatch.setattr(config, "IG_VISION_ENABLED", False)
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"png")
    brands = [{"name": n, "lang": "en", "group": "JNN", "tg": "", "yt": "",
               "tw": "", "fb": "", "ig": f"{n}_ig", "logo": str(logo)}
              for n in NAMES]
    monkeypatch.setattr(config, "BRANDS", brands)
    monkeypatch.setattr(config, "BUNDLE_ACCOUNTS", list(NAMES))
    monkeypatch.setattr(config, "BUNDLE_AUTO_PUBLISH", False)
    monkeypatch.setattr(news_bot.ig_graph, "publishing_quota", lambda a: (0, 100))
    monkeypatch.setattr(news_bot.ig_graph, "followers", lambda a: 100)

    rendered, captioned = [], []

    async def fake_render(bot, chat, state, brands, tag, label=""):
        out = []
        for b in brands:
            path = media / f"brand_{tag}_{b['name']}.mp4"
            path.write_bytes(b"r")
            state["files"].append(str(path))
            out.append({"brand": b, "path": str(path),
                        "headline": state["text"]})
        rendered.append((state["text"], [b["name"] for b in brands]))
        return out, [], [], 30.0

    async def fake_captions(text, pairs, footage=None, info="", cache=None,
                            source=None):
        names = {p["render"]["brand"]["name"] for p in pairs}
        captioned.append((text, info, sorted(names)))
        return {n: f"caption for {text} / {info}" for n in names}

    monkeypatch.setattr(news_bot, "_render_video", fake_render)
    monkeypatch.setattr(news_bot, "_ig_captions", fake_captions)
    chat = _Chat()
    jq = _JobQueue()
    ctx = types.SimpleNamespace(bot=_Bot(), job_queue=jq,
                                application=types.SimpleNamespace(
                                    create_task=asyncio.ensure_future))
    yield types.SimpleNamespace(store=store, media=media, chat=chat, ctx=ctx,
                                jq=jq, rendered=rendered, captioned=captioned)
    news_bot._pending.clear()


def _video(env, n, info=True):
    """A downloaded single-video post's state, as _handle_url builds it."""
    path = env.media / f"manual_url_{n}.mp4"
    path.write_bytes(b"v")
    state = {"text": f"Headline {n}", "media": [{"path": str(path),
                                                 "type": "video"}],
             "files": [str(path)], "sel_tg": set(), "sel_yt": set(),
             "sel_tw": set(), "sel_em": set(),
             "source": {"platform": "x", "url": f"https://x.com/a/status/{n}",
                        "text": "", "author": "a"}}
    if info:
        state["info"] = f"info {n}"
    return state


def _start(env, n_videos, info=True):
    """📦 Bundle on the first video's card, the others joining by themselves."""
    first = _video(env, 1, info)
    news_bot._init_plan(first, "video")
    first["plan_platforms"] = {"ig"}
    card = _Msg(chat=env.chat)
    news_bot._pending[card.message_id] = first
    q = _Q(card, "b:bundle")
    asyncio.run(news_bot._on_brand_callback(q, env.ctx, first, "bundle"))
    lines = [card]
    for n in range(2, n_videos + 1):
        b = env.store.current_bundle(news_bot.CHAT_ID)
        line = _Msg(chat=env.chat)
        asyncio.run(news_bot._join_bundle(env.ctx.bot, b, _video(env, n, info),
                                          line))
        lines.append(line)
    return env.store.current_bundle(news_bot.CHAT_ID), lines


# --------------------------------------------------------------------------- #
# starting and joining                                                        #
# --------------------------------------------------------------------------- #


def test_one_tap_starts_a_bundle_and_videos_join_with_none(env):
    b, lines = _start(env, 3)
    assert b["platforms"] == ["ig"]
    rows = env.store.bundle_videos(b["id"])
    assert len(rows) == 3
    assert lines[-1].edits[-1][0].startswith(f"📦 Bundle #{b['id']}: 3 videos")
    buttons = [x.callback_data for row in lines[-1].edits[-1][1].inline_keyboard
               for x in row]
    assert buttons == ["b:bgo", "b:bx"]
    # the downloads moved out of the nightly sweep's reach
    for row in rows:
        assert os.path.dirname(row["state"]["files"][0]).endswith(
            os.path.join("bundles", str(b["id"])))


def test_the_ai_pick_becomes_the_headline_without_a_tap(env, monkeypatch):
    b, lines = _start(env, 1)
    state = news_bot._pending[lines[0].message_id]
    monkeypatch.setattr(news_bot.headlines, "propose", lambda *a, **k: {
        "headlines": ["One", "Two", "Three"], "pick": 2, "why": "", "notes": ""})
    asyncio.run(news_bot._write_headlines(state, lines[0]))
    assert state["text"] == "Three" and state["headline_pick"] == 2
    saved = env.store.bundle_videos(b["id"])[0]["state"]
    assert saved["text"] == "Three"


def test_an_operators_own_headline_is_not_overwritten(env, monkeypatch):
    _, lines = _start(env, 1)
    state = news_bot._pending[lines[0].message_id]
    state.update(text="Mine", cap_src="edited")
    monkeypatch.setattr(news_bot.headlines, "propose", lambda *a, **k: {
        "headlines": ["One"], "pick": 0, "why": "", "notes": ""})
    asyncio.run(news_bot._write_headlines(state, lines[0]))
    assert state["text"] == "Mine"


def test_remove_this_one(env):
    b, lines = _start(env, 2)
    state = news_bot._pending[lines[1].message_id]
    asyncio.run(news_bot._on_brand_callback(_Q(lines[1]), env.ctx, state, "bx"))
    assert len(env.store.bundle_videos(b["id"])) == 1
    assert lines[1].message_id not in news_bot._pending


# --------------------------------------------------------------------------- #
# render                                                                      #
# --------------------------------------------------------------------------- #


def _render(env, b, lines):
    state = news_bot._pending[lines[0].message_id]
    q = _Q(lines[0], "b:bgo")
    asyncio.run(news_bot._on_brand_callback(q, env.ctx, state, "bgo"))
    return q


def test_render_waits_for_info_on_every_video(env):
    b, lines = _start(env, 2, info=False)
    q = _render(env, b, lines)
    assert "Waiting for an info: reply on video 1, 2" in q.answers[0]
    assert env.store.get_bundle(b["id"])["status"] == "open"
    assert env.rendered == []


def test_render_assigns_every_account_and_posts_one_summary(env):
    b, lines = _start(env, 3)
    _render(env, b, lines)
    row = env.store.get_bundle(b["id"])
    assert row["status"] == "ready"
    plan = row["plan"]
    assert set(plan["assignment"]) == set(NAMES)
    assert sorted(bundle.copies(plan["assignment"]).values()) == [3, 3, 4]
    assert len(plan["slots"]) == 10
    summary = env.chat.sent[-1]
    assert summary.text.startswith(f"📦 Bundle #{b['id']} ready: 3 videos → "
                                   "10 accounts")
    data = [x.callback_data for r in summary.edits[-1][1].inline_keyboard
            for x in r]
    assert data == [f"u:pub:{b['id']}", f"u:rs:{b['id']}", f"u:x:{b['id']}"]


def test_each_videos_captions_come_from_its_own_info_and_headline(env):
    b, lines = _start(env, 3)
    _render(env, b, lines)
    assert sorted(t for t, _, _ in env.captioned) == \
        ["Headline 1", "Headline 2", "Headline 3"]
    for text, info, names in env.captioned:
        n = text.split()[-1]
        assert info == f"info {n}"
        plan = env.store.get_bundle(b["id"])["plan"]
        vid = next(v for v, d in plan["videos"].items() if d["n"] == int(n))
        assert names == sorted(a for a, v in plan["assignment"].items()
                               if v == vid)


def test_twelve_videos_carry_two_into_a_new_open_bundle(env):
    b, lines = _start(env, 12)
    _render(env, b, lines)
    plan = env.store.get_bundle(b["id"])["plan"]
    assert len(plan["videos"]) == 10
    nxt = env.store.current_bundle(news_bot.CHAT_ID)
    assert nxt["id"] != b["id"] and len(env.store.bundle_videos(nxt["id"])) == 2
    assert any("2 videos carried over" in n for n in plan["notes"])


def test_a_paused_account_gets_nothing(env, monkeypatch):
    monkeypatch.setattr(config, "BUNDLE_ACCOUNTS", NAMES[:9])
    b, lines = _start(env, 3)
    _render(env, b, lines)
    plan = env.store.get_bundle(b["id"])["plan"]
    assert "brand9" not in plan["assignment"] and len(plan["assignment"]) == 9


def test_history_keeps_a_video_off_an_account_that_posted_it(env):
    env.store.log_reel("https://x.com/a/status/1", "brand0", "brand0_ig", "m1")
    b, lines = _start(env, 2)
    _render(env, b, lines)
    plan = env.store.get_bundle(b["id"])["plan"]
    first = next(v for v, d in plan["videos"].items() if d["n"] == 1)
    assert plan["assignment"]["brand0"] != first


# --------------------------------------------------------------------------- #
# publish                                                                     #
# --------------------------------------------------------------------------- #


def test_publish_schedules_one_post_per_account_spread_out(env):
    b, lines = _start(env, 3)
    _render(env, b, lines)
    summary = env.chat.sent[-1]
    asyncio.run(news_bot._publish_bundle(env.ctx, b["id"], summary))
    rows = env.store.scheduled_for_bundle(b["id"], "pending")
    assert len(rows) == 10 and len(env.jq.jobs) == 10
    accounts = [r["payload"]["renders"][0]["brand"] for r in rows]
    assert sorted(accounts) == sorted(NAMES)
    slots = [(int((r["due_at"] - rows[0]["due_at"]).total_seconds()),
              r["payload"]["renders"][0]["brand"],
              r["payload"]["bundle"]["video_key"]) for r in rows]
    assert bundle.check_timetable(slots)
    p = rows[0]["payload"]
    acc = p["renders"][0]["brand"]
    assert p["captions"][acc].startswith("caption for Headline")
    assert p["bundle"]["copy_count"] in (3, 4)
    assert env.store.get_bundle(b["id"])["status"] == "scheduled"
    assert "scheduled" in summary.edits[-1][0]


def test_publish_is_three_taps_in_total(env):
    """📦 Bundle, 🎬 Render bundle, 🚀 Publish — the joins and the headline
    pick are tapless."""
    b, lines = _start(env, 3)          # tap 1 (+ two tapless joins)
    _render(env, b, lines)             # tap 2
    summary = env.chat.sent[-1]
    q = _Q(summary, f"u:pub:{b['id']}")
    asyncio.run(news_bot._on_bundle_callback(q, env.ctx))     # tap 3
    assert len(env.store.scheduled_for_bundle(b["id"], "pending")) == 10


def test_auto_publish_skips_the_tap(env, monkeypatch):
    monkeypatch.setattr(config, "BUNDLE_AUTO_PUBLISH", True)
    b, lines = _start(env, 2)
    _render(env, b, lines)
    assert len(env.store.scheduled_for_bundle(b["id"], "pending")) == 10


def test_a_full_quota_rolls_to_tomorrow_and_says_so(env, monkeypatch):
    monkeypatch.setattr(news_bot.ig_graph, "publishing_quota",
                        lambda a: (100, 100) if a == "brand3_ig" else (0, 100))
    b, lines = _start(env, 1)
    _render(env, b, lines)
    asyncio.run(news_bot._publish_bundle(env.ctx, b["id"], env.chat.sent[-1]))
    plan = env.store.get_bundle(b["id"])["plan"]
    assert any("brand3: Instagram quota full" in n for n in plan["notes"])


# --------------------------------------------------------------------------- #
# restart and retry                                                           #
# --------------------------------------------------------------------------- #


def test_an_open_bundle_survives_a_restart(env):
    b, lines = _start(env, 2)
    news_bot._pending.clear()
    app = types.SimpleNamespace(bot=_Bot(), create_task=lambda c: c.close())
    news_bot._restore_bundles(app)
    for line in lines:
        st = news_bot._pending[line.message_id]
        assert st["mode"] == "bundle" and st["bundle_id"] == b["id"]
    _render(env, b, lines)
    assert env.store.get_bundle(b["id"])["status"] == "ready"


def test_one_failed_account_gets_a_retry_button(env, monkeypatch):
    b, lines = _start(env, 1)
    _render(env, b, lines)
    asyncio.run(news_bot._publish_bundle(env.ctx, b["id"], env.chat.sent[-1]))
    row = env.store.scheduled_for_bundle(b["id"], "pending")[0]

    async def failing(message, bot, state):
        state["failed_platforms"] = ["ig"]

    monkeypatch.setattr(news_bot, "_do_publish", failing)
    asyncio.run(news_bot._run_scheduled(env.ctx.bot, row["id"]))
    assert env.store.get_scheduled(row["id"])["status"] == "failed"
    text, kw = env.ctx.bot.sent[-1]
    assert "failed on ig" in text
    assert kw["reply_markup"].inline_keyboard[0][0].callback_data == \
        f"s:{row['id']}:r"

    published = []

    async def ok(message, bot, state):
        published.append(state)

    monkeypatch.setattr(news_bot, "_do_publish", ok)

    async def retry():
        q = _Q(_Msg(text="❌"), f"s:{row['id']}:r")
        await news_bot._on_schedule_retry(q, env.ctx)
        await asyncio.sleep(0.05)
    asyncio.run(retry())
    assert len(published) == 1
    assert env.store.get_scheduled(row["id"])["status"] == "done"


def test_the_other_accounts_still_go_when_one_fails(env, monkeypatch):
    b, lines = _start(env, 1)
    _render(env, b, lines)
    asyncio.run(news_bot._publish_bundle(env.ctx, b["id"], env.chat.sent[-1]))
    rows = env.store.scheduled_for_bundle(b["id"], "pending")

    async def first_fails(message, bot, state):
        if state["bundle"]["account"] == "brand0":
            state["failed_platforms"] = ["ig"]

    monkeypatch.setattr(news_bot, "_do_publish", first_fails)
    for r in rows:
        asyncio.run(news_bot._run_scheduled(env.ctx.bot, r["id"]))
    statuses = sorted(env.store.get_scheduled(r["id"])["status"] for r in rows)
    assert statuses == ["done"] * 9 + ["failed"]


# --------------------------------------------------------------------------- #
# the reel log                                                                #
# --------------------------------------------------------------------------- #


def test_a_published_reel_is_logged_with_its_bundle_and_copy_count(env):
    state = {"video_key": "https://x.com/a/status/1",
             "bundle": {"id": 7, "copy_count": 4}}
    brand = {"name": "brand0", "ig": "brand0_ig"}
    asyncio.run(news_bot._log_reel(state, brand, "m42"))
    (row,) = env.store.reels_since(__import__("datetime").datetime(2000, 1, 1,
                                   tzinfo=__import__("datetime").timezone.utc))
    assert (row["bundle_id"], row["copy_count"], row["media_id"],
            row["followers_at_post"]) == (7, 4, "m42", 100)
    assert env.store.published_videos(["brand0"]) == {
        "brand0": {"https://x.com/a/status/1"}}


def test_insights_are_stored_or_the_missing_permission_reported(env, monkeypatch):
    env.store.log_reel("k", "brand0", "brand0_ig", "m1")
    with env.store._conn() as c:
        c.execute("UPDATE reel_log SET posted_at='2000-01-01T00:00:00+00:00'")
    monkeypatch.setattr(news_bot.ig_graph, "reel_insights",
                        lambda m, a: (None, "missing permission "
                                      "instagram_business_manage_insights"))
    monkeypatch.setattr(news_bot, "_insights_warned", False)
    asyncio.run(news_bot._insights_job(env.ctx))
    assert "instagram_business_manage_insights" in env.ctx.bot.sent[0][0]
    rows = env.store.reels_since(__import__("datetime").datetime(1999, 1, 1,
                                 tzinfo=__import__("datetime").timezone.utc))
    assert rows[0]["insights_24h"]["error"].startswith("missing permission")
