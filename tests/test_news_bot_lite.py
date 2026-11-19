"""Lite mode (BOT_MODE=lite) — the render-only bot.

news_bot is imported in full mode (like the other news_bot tests) and lite is
switched on per test through config.LITE, which every gate reads at call time.
What is pinned here: the card offers only Render / Preview edit / Cancel, any
other callback is refused, non-video posts are refused, an `info:` reply is
not taken, and a brand account in the config is caught. The config half
(ENV_FILE, credentials skipped, own data dir) runs in a clean subprocess."""

import asyncio
import os
import subprocess
import sys
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
from modules.telegram import news_bot  # noqa: E402
for _k, _v in _saved.items():
    setattr(config, _k, _v)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def lite(monkeypatch):
    monkeypatch.setattr(config, "LITE", True)
    monkeypatch.setattr(news_bot, "_track", lambda m: m)
    return news_bot


def _state(**kw):
    s = {"text": "Bear walks behind a man", "media": [{"type": "video",
                                                       "file_id": "f"}],
         "files": [], "sel_tg": set(), "sel_yt": set(), "sel_tw": set(),
         "sel_em": set()}
    s.update(kw)
    news_bot._init_plan(s, "video")
    return s


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_card_offers_render_and_cancel_only(lite, monkeypatch):
    monkeypatch.setattr(config, "BRANDS", [{"name": "PulseNews", "logo": "x"}])
    s = _state()
    assert _callbacks(news_bot._plan_markup(s)) == ["b:go", "b:cancel"]
    s["edit"] = {"hook": [1, 3]}
    assert _callbacks(news_bot._plan_markup(s)) == ["b:go", "b:pv", "b:cancel"]
    text = news_bot._plan_text(s)
    assert "PulseNews" in text and "info:" not in text
    assert not s["watching"]  # no footage analysis in lite


def test_every_lite_button_is_whitelisted(lite):
    s = _state()
    s["edit"] = {"hook": [1, 3]}
    assert set(_callbacks(news_bot._plan_markup(s))) <= news_bot._LITE_CALLBACKS


class _Query:
    def __init__(self, data):
        self.data = data
        self.answers = []
        self.message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=news_bot.CHAT_ID), message_id=7)

    async def answer(self, text=None, **kw):
        self.answers.append(text)


@pytest.mark.parametrize("data", ["b:publish", "b:sched", "b:us", "b:asis", "b:dl",
                                  "b:bundle", "b:edit", "s:1:r", "u:go:1",
                                  "submit"])
def test_other_callbacks_are_refused(lite, monkeypatch, data):
    def boom(*a, **kw):
        raise AssertionError("reached a full-bot path")
    monkeypatch.setattr(news_bot, "_on_brand_callback", boom)
    monkeypatch.setattr(news_bot, "_on_schedule_retry", boom)
    monkeypatch.setattr(news_bot, "_on_bundle_callback", boom)
    news_bot._pending[7] = _state()
    try:
        q = _Query(data)
        update = types.SimpleNamespace(callback_query=q)
        asyncio.run(news_bot.on_callback(update, None))
        assert q.answers == ["Not available in this bot."]
    finally:
        news_bot._pending.pop(7, None)


class _Msg:
    def __init__(self, **kw):
        self.caption = kw.get("caption")
        self.text = kw.get("text")
        self.reply_to_message = kw.get("reply_to_message")
        self.media_group_id = kw.get("media_group_id")
        self.photo = kw.get("photo")
        self.video = kw.get("video")
        self.document = None
        self.message_id = 50
        self.replies = []
        self.edits = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        return _Msg()

    async def edit_text(self, text, reply_markup=None):
        self.edits.append(text)


def _on_message(msg):
    update = types.SimpleNamespace(effective_message=msg)
    asyncio.run(news_bot.on_message(update, None))


def test_a_photo_or_plain_text_is_refused(lite, monkeypatch):
    monkeypatch.setattr(news_bot, "_extract_media",
                        lambda m: {"type": "photo", "file_id": "p"} if m.photo else None)
    monkeypatch.setattr(news_bot, "_is_own_preview", lambda m: False)
    photo = _Msg(caption="headline", photo=True)
    _on_message(photo)
    assert photo.replies == [news_bot._LITE_VIDEO_ONLY]
    text = _Msg(text="just chatting")
    _on_message(text)
    assert text.replies == [news_bot._LITE_VIDEO_ONLY]


def test_info_reply_is_not_taken_and_a_headline_reply_is(lite, monkeypatch):
    monkeypatch.setattr(news_bot, "_is_own_preview", lambda m: False)
    card = _Msg()
    card.message_id = 77
    s = _state()
    news_bot._pending[77] = s
    try:
        info = _Msg(text="info: filmed in Ohio", reply_to_message=card)
        _on_message(info)
        assert "info" not in s and s["text"] == "Bear walks behind a man"
        assert "doesn't use info" in info.replies[0]
        _on_message(_Msg(text="Bear follows man home", reply_to_message=card))
        assert s["text"] == "Bear follows man home"
        assert card.edits and "Bear follows man home" in card.edits[-1]
    finally:
        news_bot._pending.pop(77, None)


def test_posting_accounts_are_caught(monkeypatch):
    monkeypatch.setattr(config, "TG_DESTINATIONS", [])
    monkeypatch.setattr(config, "YT_DESTINATIONS", [])
    monkeypatch.setattr(config, "TW_DESTINATIONS", [])
    monkeypatch.setattr(config, "IG_GRAPH_ACCOUNTS", [])
    monkeypatch.setattr(config, "BRANDS", [{"name": "PulseNews", "logo": "x"}])
    assert news_bot._lite_posting_config() == []
    monkeypatch.setattr(config, "BRANDS",
                        [{"name": "PulseNews", "logo": "x", "ig": "acct"}])
    assert news_bot._lite_posting_config() == ["BRAND_PULSENEWS_IG"]
    monkeypatch.setattr(config, "IG_GRAPH_ACCOUNTS", ["acct"])
    assert "IG_GRAPH_ACCOUNTS" in news_bot._lite_posting_config()


def test_lite_config_loads_only_its_env_file_and_no_credentials(tmp_path):
    """ENV_FILE is the only file read; credentials/brands/ is skipped; the
    data dir is its own. Clean environment: the test process has the
    developer's .env in os.environ, a systemd unit doesn't."""
    env_file = tmp_path / "lite.env"
    env_file.write_text("BOT_MODE=lite\nBRANDS=PulseNews\n"
                        "TG_DATA_DIR=modules/telegram/data_lite\n",
                        encoding="utf-8")
    keep = {k: v for k, v in os.environ.items()
            if k.upper() in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME",
                             "USERPROFILE", "PYTHONPATH", "APPDATA",
                             "LOCALAPPDATA")}
    keep["ENV_FILE"] = str(env_file)
    out = subprocess.run(
        [sys.executable, "-c",
         "import os; from shared import config as c;"
         "print(c.LITE, c.BRAND_ACCOUNTS, [b['name'] for b in c.BRANDS],"
         " os.path.relpath(c.TG_DATA_DIR, c.ROOT_DIR).replace(os.sep, '/'),"
         " bool(c.NEWS_BOT_TOKEN), c.BRANDS[0]['group'])"],
        cwd=_ROOT, env=keep, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["True", "[]", "['PulseNews']",
                                  "modules/telegram/data_lite", "False", "Lite"]
