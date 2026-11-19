"""Offline tests for the publish picker's idle auto-publish.

The picker ships what it has ticked once it has sat `_AUTO_PUBLISH_S` with
nobody touching it; every tap or reply restarts the clock, and Cancel or a
manual Publish stop it. Nothing here touches Telegram — `_do_publish` is
stubbed and the delay shrunk to milliseconds.
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
    monkeypatch.setattr(news_bot, "_AUTO_PUBLISH_S", 0.05)
    monkeypatch.setattr(news_bot, "_PUBLISH_GRACE_S", 0.04)
    monkeypatch.setattr(news_bot, "_COUNTDOWN_TICK_S", 0.02)
    yield calls
    news_bot._pending.pop(_Msg.message_id, None)


def _state(sel=frozenset({0})):
    state = {"mode": "publish", "sel_platforms": set(sel), "files": [],
             "platforms": [{"label": "TG"}, {"label": "X"}]}
    news_bot._pending[_Msg.message_id] = state
    return state


def _open_picker(sel=frozenset({0})):
    state = _state(sel)
    news_bot._arm_auto_publish(None, state, _Msg())
    return state


def test_an_untouched_picker_publishes_itself(published):
    async def go():
        state = _open_picker()
        await asyncio.sleep(0.25)
        return state
    state = asyncio.run(go())
    assert published == [state] and state["auto_published"]


def test_a_tap_restarts_the_clock(published):
    async def go():
        state = _open_picker()
        await asyncio.sleep(0.03)
        news_bot._arm_auto_publish(None, state, _Msg())  # what a tap does
        await asyncio.sleep(0.03)
        assert state["mode"] == "publish"  # 0.06 s since open, 0.03 since the tap
        await asyncio.sleep(0.25)
    asyncio.run(go())
    assert len(published) == 1


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


def test_cancel_stops_the_clock(published):
    async def go():
        state = _open_picker()
        news_bot._pending.pop(_Msg.message_id)
        news_bot._cleanup(state)  # the ✕ Cancel path
        await asyncio.sleep(0.15)
    asyncio.run(go())
    assert published == []


def test_nothing_ticked_publishes_nothing(published):
    async def go():
        _open_picker(sel=frozenset())
        await asyncio.sleep(0.15)
    asyncio.run(go())
    assert published == []


def test_a_picker_that_moved_on_is_left_alone(published):
    async def go():
        state = _open_picker()
        state["mode"] = "rendering"
        await asyncio.sleep(0.15)
    asyncio.run(go())
    assert published == []


def test_the_picker_says_it_will_publish_by_itself(monkeypatch):
    monkeypatch.setattr(news_bot, "_AUTO_PUBLISH_S", 300)
    assert "5 min" in news_bot._publish_prompt_text({"platforms": []})
    monkeypatch.setattr(news_bot, "_AUTO_PUBLISH_S", 0)
    assert "⏱" not in news_bot._publish_prompt_text({"platforms": []})
