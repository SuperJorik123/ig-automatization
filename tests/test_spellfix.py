"""Offline tests for the headline spell-fix (modules/telegram/spellfix.py) and
its news_bot wiring. No OpenRouter call is made: the client is a stand-in."""

import asyncio
from types import SimpleNamespace

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
from modules.telegram import news_bot  # noqa: E402
for _k, _v in _saved.items():
    setattr(config, _k, _v)

from modules.telegram import spellfix  # noqa: E402


class _Client:
    """Answers every call from `replies` (a str, a list, or an exception)."""

    def __init__(self, replies):
        self.replies = list(replies) if isinstance(replies, list) else [replies]
        self.sent = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.sent.append(kw["messages"][-1]["content"])
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=r))])


@pytest.fixture
def client(monkeypatch):
    def use(replies):
        c = _Client(replies)
        monkeypatch.setattr(spellfix, "_client", c)
        monkeypatch.setattr(spellfix, "ENABLED", True)
        return c
    return use


def test_typos_are_fixed(client):
    client("Government announces new tax")
    assert spellfix.correct("Goverment anounces new tax") == \
        "Government announces new tax"


def test_quotes_and_extra_lines_are_stripped(client):
    client('"Government announces new tax"\n\n(fixed 2 typos)')
    assert spellfix.correct("Goverment anounces new tax") == \
        "Government announces new tax"


def test_a_chatty_answer_is_rejected(client):
    client("Sure! Here's the corrected headline for you: Government announces "
           "a brand new tax on everything")
    assert spellfix.correct("Goverment anounces new tax") == \
        "Goverment anounces new tax"


def test_a_rewrite_is_rejected(client):
    client("Officials unveil fresh levy")
    assert spellfix.correct("Goverment anounces new tax") == \
        "Goverment anounces new tax"


def test_failure_keeps_the_headline(client):
    client(RuntimeError("402 payment required"))
    assert spellfix.correct("Goverment anounces") == "Goverment anounces"


def test_empty_reply_keeps_the_headline(client):
    client("")
    assert spellfix.correct("Goverment anounces") == "Goverment anounces"


def test_no_key_or_disabled_is_a_no_op(monkeypatch):
    monkeypatch.setattr(spellfix, "_client", None)
    assert spellfix.correct("Goverment") == "Goverment"
    c = _Client("X")
    monkeypatch.setattr(spellfix, "_client", c)
    monkeypatch.setattr(spellfix, "ENABLED", False)
    assert spellfix.correct("Goverment") == "Goverment"
    assert c.sent == []


def test_nothing_to_check_makes_no_call(client):
    c = client("x")
    assert spellfix.correct("   ") == "   "
    assert spellfix.correct("2026 · 3:1") == "2026 · 3:1"
    assert c.sent == []


def test_each_line_is_fixed_on_its_own(client):
    c = client(["Breaking news", "Government announces tax"])
    assert spellfix.correct("Braking news\nGoverment anounces tax") == \
        "Breaking news\nGovernment announces tax"
    assert c.sent == ["Braking news", "Goverment anounces tax"]


# --- news_bot wiring -------------------------------------------------------- #

class _Chat:
    def __init__(self):
        self.sent = []

    async def send_message(self, text, **kw):
        self.sent.append(text)
        return SimpleNamespace(message_id=1, chat_id=-100123)


def test_render_gets_the_fixed_headline_and_the_group_is_told(monkeypatch):
    calls = []

    def fake(text):
        calls.append(text)
        return "Government announces tax"
    monkeypatch.setattr(news_bot.spellfix, "correct", fake)
    monkeypatch.setattr(news_bot, "_track", lambda m: m)
    state, chat = {"text": "Goverment anounces tax"}, _Chat()
    asyncio.run(news_bot._spellfix_headline(state, chat))
    assert state["text"] == "Government announces tax"
    assert chat.sent and "Goverment anounces tax" in chat.sent[0]
    # A re-render of the same headline doesn't ask again.
    asyncio.run(news_bot._spellfix_headline(state, chat))
    assert calls == ["Goverment anounces tax"]


def test_correct_headline_is_left_alone_quietly(monkeypatch):
    monkeypatch.setattr(news_bot.spellfix, "correct", lambda t: t)
    state, chat = {"text": "All good here"}, _Chat()
    asyncio.run(news_bot._spellfix_headline(state, chat))
    assert state["text"] == "All good here" and chat.sent == []
