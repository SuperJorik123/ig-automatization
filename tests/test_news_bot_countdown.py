"""Offline tests for the publish countdown.

The 🚀 tap turns the picker into a `_PUBLISH_GRACE_S` countdown whose only
button is ✕ Cancel; nothing publishes until it runs out. Nothing here touches
Telegram — `_do_publish` is stubbed and the delays shrunk to milliseconds.
"""

import asyncio

import pytest

from shared import config


# Same import dance as test_news_bot_caption.py: news_bot exits at import
# without these, and no other test may see the stand-ins.
_STUBS = {
    "NEWS_BOT_TOKEN": "test:token",
    "TELEGRAM_CHAT_ID": "-100123",
    "TG_DESTINATIONS": [{"chat_id": "@somewhere", "lang": ""}],
}
_saved = {k: getattr(config, k) for k in _STUBS}
for _k, _v in _STUBS.items():
    if not getattr(config, _k):
        setattr(config, _k, _v)
from modules.telegram import news_bot  # noqa: E402
for _k, _v in _saved.items():
    setattr(config, _k, _v)


class _Msg:
    message_id = 4242

    def __init__(self):
        self.edits = []

    async def edit_text(self, text, reply_markup=None):
        self.edits.append(text)


@pytest.fixture
def published(monkeypatch):
    calls = []

    async def fake_publish(message, bot, state):
        calls.append(state)
        news_bot._pending.pop(message.message_id, None)

    monkeypatch.setattr(news_bot, "_do_publish", fake_publish)
    monkeypatch.setattr(news_bot, "_PUBLISH_GRACE_S", 0.04)
    monkeypatch.setattr(news_bot, "_COUNTDOWN_TICK_S", 0.02)
    yield calls
    news_bot._pending.pop(_Msg.message_id, None)


def _state(sel=frozenset({0})):
    state = {"mode": "publish", "sel_platforms": set(sel), "files": [],
             "platforms": [{"label": "TG"}, {"label": "X"}]}
    news_bot._pending[_Msg.message_id] = state
    return state


def test_publish_waits_out_the_countdown_then_ships(published):
    msg = _Msg()

    async def go():
        state = _state(sel={0, 1})
        news_bot._start_countdown(None, state, msg)  # the 🚀 tap
        await asyncio.sleep(0.01)
        assert published == [] and state["mode"] == "countdown"
        await asyncio.sleep(0.1)
    asyncio.run(go())
    assert len(published) == 1
    assert "TG · X" in msg.edits[0] and "Cancel" in msg.edits[0]
    assert len(msg.edits) >= 2  # the timer was redrawn


def test_cancel_during_the_countdown_publishes_nothing(published):
    async def go():
        state = _state()
        news_bot._start_countdown(None, state, _Msg())
        await asyncio.sleep(0.01)
        news_bot._pending.pop(_Msg.message_id)
        news_bot._cleanup(state)  # the ✕ Cancel path
        await asyncio.sleep(0.1)
    asyncio.run(go())
    assert published == []


def test_the_countdown_has_only_a_cancel_button():
    from modules.telegram import branded
    rows = branded.countdown_keyboard().inline_keyboard
    assert [[b.callback_data for b in r] for r in rows] == [["b:cancel"]]


def test_an_untouched_picker_never_publishes_itself(published):
    async def go():
        _state()
        await asyncio.sleep(0.15)
    asyncio.run(go())
    assert published == []
    assert "⏱" not in news_bot._publish_prompt_text({"platforms": []})
