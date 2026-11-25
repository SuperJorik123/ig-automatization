"""Offline: 🕒 Schedule on the plan card — the time picked before the render
decides what happens to the publish picker the render opens (`_after_render`).
Telegram and the scheduler are stand-ins."""

import asyncio
import time
import types

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


class _Prompt:
    message_id = 4242

    def __init__(self):
        self.edits = []

    async def edit_text(self, text, reply_markup=None):
        self.edits.append(text)


def _state(when, selected=frozenset({0})):
    return {"when": when, "sel_platforms": set(selected),
            "platforms": [{"label": "IG", "platform": "ig", "renders": []}]}


def _run(monkeypatch, state):
    calls = {"countdown": 0, "scheduled": []}
    monkeypatch.setattr(news_bot, "_start_countdown",
                        lambda bot, st, msg: calls.__setitem__("countdown", 1))

    async def fake_schedule(context, st, msg, when, reply_msg=None):
        calls["scheduled"].append(when)
    monkeypatch.setattr(news_bot, "_schedule_post", fake_schedule)
    monkeypatch.setattr(news_bot, "_taken_slots", lambda: [])
    prompt = _Prompt()
    ctx = types.SimpleNamespace(bot=object())
    asyncio.run(news_bot._after_render(ctx, state, prompt))
    return calls, prompt


def test_now_starts_the_countdown(monkeypatch):
    calls, prompt = _run(monkeypatch, _state("now"))
    assert calls["countdown"] == 1 and not calls["scheduled"]
    assert not prompt.edits


def test_a_delay_schedules_at_the_tapped_time(monkeypatch):
    ts = int(time.time()) + 3600
    calls, _ = _run(monkeypatch, _state(ts))
    assert [int(w.timestamp()) for w in calls["scheduled"]] == [ts]


def test_us_evening_takes_the_next_us_slot(monkeypatch):
    calls, _ = _run(monkeypatch, _state("us"))
    assert calls["scheduled"] == [schedule.next_us_slot([])]


def test_just_render_leaves_the_picker_alone(monkeypatch):
    calls, prompt = _run(monkeypatch, _state("go"))
    assert not calls["countdown"] and not calls["scheduled"] and not prompt.edits


def test_a_passed_time_keeps_the_picker_open(monkeypatch):
    calls, prompt = _run(monkeypatch, _state(int(time.time()) - 60))
    assert not calls["countdown"] and not calls["scheduled"]
    assert prompt.edits and "pick here instead" in prompt.edits[0]


def test_no_platform_keeps_the_picker_open(monkeypatch):
    calls, prompt = _run(monkeypatch, _state("now", selected=()))
    assert not calls["countdown"] and not calls["scheduled"]
    assert prompt.edits
