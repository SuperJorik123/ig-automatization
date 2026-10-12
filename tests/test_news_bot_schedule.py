"""Offline: scheduling a publish picker, firing it once, cancelling it, and
what a restart does with pending / interrupted rows. Telegram, the JobQueue
and _do_publish are stand-ins."""

import asyncio
import datetime as dt
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
from modules.telegram import news_bot, schedule  # noqa: E402
for _k, _v in _saved.items():
    setattr(config, _k, _v)


class _Msg:
    def __init__(self, message_id=77):
        self.message_id = message_id
        self.edits = []
        self.replies = []

    async def edit_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))

    async def reply_text(self, text):
        self.replies.append(text)
        return _Msg(999)


class _JobQueue:
    def __init__(self):
        self.jobs = []

    def run_once(self, cb, when, data=None, name=None):
        job = types.SimpleNamespace(cb=cb, when=when, data=data, name=name,
                                    removed=False)
        job.schedule_removal = lambda: setattr(job, "removed", True)
        self.jobs.append(job)

    def get_jobs_by_name(self, name):
        return [j for j in self.jobs if j.name == name]


class _Bot:
    def __init__(self):
        self.sent, self.card_edits = [], []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)
        return _Msg(500 + len(self.sent))

    async def edit_message_text(self, text, **kw):
        self.card_edits.append(text)


@pytest.fixture
def env(store, tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    monkeypatch.setattr(news_bot, "_SCHED_DIR", str(media / "scheduled"))
    monkeypatch.setattr(news_bot, "_BUSY_PATH", str(tmp_path / "busy"))
    monkeypatch.setattr(news_bot, "_track", lambda m: m)
    brand = {"name": "alpha", "tw": "acc_alpha", "logo": "x"}
    monkeypatch.setattr(config, "BRANDS", [brand])
    published = []

    async def fake_publish(message, bot, state):
        published.append(state)
        for p in state["files"]:
            os.remove(p)

    monkeypatch.setattr(news_bot, "_do_publish", fake_publish)
    render = media / "brand_77_alpha.mp4"
    source = media / "brand_src_abc.mp4"
    render.write_bytes(b"r")
    source.write_bytes(b"s")
    r = {"brand": brand, "path": str(render), "headline": "Hi", "kind": "video"}
    state = {"mode": "publish", "text": "Hi", "info": "src", "renders": [r],
             "platforms": [{"platform": "tw", "label": "X", "renders": [r]}],
             "sel_platforms": {0}, "files": [str(source), str(render)],
             "media": []}
    news_bot._pending[77] = state
    jq = _JobQueue()
    yield types.SimpleNamespace(state=state, jq=jq, bot=_Bot(), store=store,
                                published=published, render=render,
                                source=source,
                                ctx=types.SimpleNamespace(job_queue=jq, bot=None))
    news_bot._pending.pop(77, None)


def _later(hours=2):
    return schedule.now() + dt.timedelta(hours=hours)


def test_schedule_then_fire_exactly_once(env):
    msg = _Msg()
    asyncio.run(news_bot._schedule_post(env.ctx, env.state, msg, _later()))

    assert 77 not in news_bot._pending
    assert not env.source.exists()                     # source cleaned up
    assert not env.render.exists()                     # render moved …
    moved = os.path.join(news_bot._SCHED_DIR, env.render.name)
    assert os.path.exists(moved)                       # … out of the sweep
    (job,) = env.jq.jobs
    assert 7100 < job.when < 7300                      # ~2 h, in seconds
    assert "scheduled #" in msg.edits[-1][0]
    row = env.store.get_scheduled(job.data)
    assert row["status"] == "pending"
    assert row["payload"]["renders"][0]["path"] == moved

    asyncio.run(news_bot._run_scheduled(env.bot, job.data))
    asyncio.run(news_bot._run_scheduled(env.bot, job.data))  # a second fire
    assert len(env.published) == 1
    st = env.published[0]
    assert st["platforms"][0]["renders"][0]["brand"]["tw"] == "acc_alpha"
    assert st["info"] == "src"
    assert env.store.get_scheduled(job.data)["status"] == "done"


def test_a_time_reply_schedules_without_touching_the_headline(env):
    reply = _Msg(5)
    asyncio.run(news_bot._schedule_post(env.ctx, env.state, _Msg(),
                                        _later(0), reply_msg=reply))
    assert "can't schedule" in reply.replies[0]        # now = in the past
    assert env.jq.jobs == [] and 77 in news_bot._pending
    assert env.state["text"] == "Hi"


def test_cancel_stops_it_and_deletes_the_render(env):
    asyncio.run(news_bot._schedule_post(env.ctx, env.state, _Msg(), _later()))
    (job,) = env.jq.jobs
    answers, edits = [], []

    async def answer(text=None, show_alert=False):
        answers.append(text)

    async def edit(text, reply_markup=None):
        edits.append(text)

    q = types.SimpleNamespace(data=f"s:{job.data}:x", answer=answer,
                              edit_message_text=edit)
    asyncio.run(news_bot._on_schedule_cancel(q, env.ctx))
    assert job.removed
    assert env.store.get_scheduled(job.data)["status"] == "cancelled"
    assert not os.listdir(news_bot._SCHED_DIR)
    assert "cancelled" in edits[-1]

    asyncio.run(news_bot._run_scheduled(env.bot, job.data))
    assert env.published == []                        # never fires after cancel


def test_restart_rearms_pending_and_reports_interrupted(env):
    asyncio.run(news_bot._schedule_post(env.ctx, env.state, _Msg(), _later()))
    pending = env.jq.jobs[0].data
    interrupted = env.store.add_scheduled(_later(1), -100, 12, {"renders": []})
    env.store.claim_scheduled(interrupted, "pending", "running")

    tasks = []
    app = types.SimpleNamespace(job_queue=_JobQueue(), bot=env.bot,
                                create_task=lambda coro: tasks.append(coro))
    news_bot._restore_scheduled(app)
    for coro in tasks:
        asyncio.run(coro)

    assert [j.data for j in app.job_queue.jobs] == [pending]
    assert env.store.get_scheduled(interrupted)["status"] == "failed"
    assert "interrupted" in env.bot.sent[0]


def test_busy_marker_exists_only_while_busy(env):
    with news_bot._busy("publishing"):
        with open(news_bot._BUSY_PATH, encoding="utf-8") as fh:
            assert "publishing" in fh.read()
    assert not os.path.exists(news_bot._BUSY_PATH)
