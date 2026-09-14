"""Offline tests for the news bot's Instagram-caption wiring.

news_bot exits at import without a configured control group, which is why its
pure pieces normally live in branded.py/groups.py. What is left inside it and
worth testing is the wiring itself: that a caption the OPERATOR typed skips
both billed calls, that the footage reaches the expansion, and what the pickers
say about either. Nothing here touches Telegram, OpenRouter or ffmpeg.
"""

import asyncio

import pytest

from shared import config


# news_bot reads these at import and raises SystemExit on a blank one. Give it
# the minimum, on the config module rather than the environment, then put the
# real values straight back — no other test may see the stand-ins, and nothing
# here may depend on the developer's own .env.
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


FOOTAGE = {
    "summary": "An elderly man walks along a street as a bear crosses behind him.",
    "beats": ["He walks past parked cars.", "A bear steps out behind him."],
    "audible": "Bystanders shouting.",
    "setting": "A residential street.",
    "headline_ok": True,
    "headline_note": "",
    "headline_suggestion": "",
}

MANUAL = (
    "Elderly man doesn't notice a bear walking right beside him\n"
    "\n"
    "An elderly man was walking down the street when a bear appeared just a "
    "few feet away from him.\n"
    "\n"
    "#bear #usa #wildlife #caughtoncamera #viralvideo #news"
)

EXPANDED = (
    "Man walks past a bear\n"
    "\n"
    "A bear crossed a residential street a few feet behind a man who did not "
    "see it.\n"
    "\n"
    "#bear #usa #wildlife #caughtoncamera #viralvideo #news #animals #street"
)


def _pair(name="mir", lang="en"):
    brand = {"name": name, "lang": lang, "group": "", "tg": "", "yt": "",
             "tw": "", "ig": f"{name}gram",
             "logo": f"/nonexistent/{name}/logo.png"}
    return {"platform": "ig", "label": f"{name} → IG",
            "render": {"brand": brand, "path": f"/tmp/{name}.mp4",
                       "headline": "Man walks past a bear"}}


@pytest.fixture
def calls(monkeypatch):
    """Record every billed call the caption layer would make."""
    seen = {"expand": [], "rephrase": []}

    def _expand(headline, footage=None, model=None):
        seen["expand"].append((headline, footage))
        return EXPANDED

    def _rephrase(text, angle, lang="", style="", model=None):
        seen["rephrase"].append((text, angle, lang, style))
        return text

    monkeypatch.setattr(news_bot.ig_caption, "expand", _expand)
    monkeypatch.setattr(news_bot.ig_caption, "rephrase", _rephrase)
    monkeypatch.setattr(news_bot.translator, "translate",
                        lambda text, lang, src=None: text)
    return seen


def _run(*args, **kw):
    return asyncio.run(news_bot._ig_captions(*args, **kw))


# --------------------------------------------------------------------------- #
# the operator's own caption                                                  #
# --------------------------------------------------------------------------- #


def test_a_manual_caption_buys_nothing(calls):
    """No analysis, no search — the operator has already done the writing."""
    out = _run("Man walks past a bear", [_pair()], FOOTAGE, MANUAL)
    assert calls["expand"] == []
    assert out


def test_a_manual_caption_is_the_source_every_account_rewrites(calls):
    """Verbatim on thirteen accounts is the duplicate content the per-account
    layer exists to prevent — so their text goes in as the source, not as the
    output."""
    out = _run("h", [_pair("mir"), _pair("wswire")], {}, MANUAL)
    assert set(out) == {"mir", "wswire"}
    rewritten = [c[0] for c in calls["rephrase"]]
    assert rewritten and all(c == MANUAL for c in rewritten)


def test_a_manual_caption_still_signs_each_account(calls):
    out = _run("h", [_pair("mir"), _pair("wswire")], {}, MANUAL)
    assert out["mir"].splitlines()[-1].split()[-1] == "#mir"
    assert out["wswire"].splitlines()[-1].split()[-1] == "#wswire"


def test_a_manual_caption_works_with_no_headline_at_all(calls):
    """The caption is the whole post on Instagram — an empty headline is no
    reason to refuse the text the operator typed."""
    assert _run("", [_pair()], {}, MANUAL)
    assert calls["expand"] == []


def test_a_blank_manual_caption_falls_back_to_expanding(calls):
    """`caption:` alone clears the override; it must not also silence IG."""
    out = _run("Man walks past a bear", [_pair()], FOOTAGE, "   ")
    assert len(calls["expand"]) == 1
    assert out


# --------------------------------------------------------------------------- #
# the footage reaching the expansion                                          #
# --------------------------------------------------------------------------- #


def test_the_footage_is_passed_to_the_expansion(calls):
    _run("Man walks past a bear", [_pair()], FOOTAGE)
    assert calls["expand"] == [("Man walks past a bear", FOOTAGE)]


def test_no_footage_passes_an_empty_dict(calls):
    _run("Man walks past a bear", [_pair()])
    assert calls["expand"] == [("Man walks past a bear", {})]


def test_the_search_is_bought_once_for_every_account(calls):
    _run("h", [_pair("a"), _pair("b"), _pair("c")], FOOTAGE)
    assert len(calls["expand"]) == 1


def test_a_post_with_no_instagram_pair_buys_nothing(calls):
    pair = dict(_pair(), platform="tg")
    assert _run("h", [pair], FOOTAGE) == {}
    assert calls["expand"] == []


def test_a_failure_leaves_every_pair_on_its_headline(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(news_bot.ig_caption, "expand", _boom)
    assert _run("h", [_pair()], FOOTAGE) == {}


# --------------------------------------------------------------------------- #
# what the pickers say                                                        #
# --------------------------------------------------------------------------- #


def _state(**kw):
    state = {"text": "Man walks past a bear", "media": [], "files": [],
             "brands": [], "sel_brands": set(), "platforms": [],
             "sel_platforms": set()}
    state.update(kw)
    return state


def test_the_brand_picker_says_it_is_watching_while_the_call_runs():
    body = news_bot._brand_prompt_text(_state(vision_task=object()))
    assert "👁 watching the clip…" in body


def test_the_brand_picker_shows_what_was_seen():
    body = news_bot._brand_prompt_text(
        _state(vision_task=object(), footage=FOOTAGE))
    assert "watching" not in body
    assert FOOTAGE["summary"] in body


def test_the_brand_picker_warns_on_a_mismatched_headline():
    bad = dict(FOOTAGE, headline_ok=False, headline_note="different man",
               headline_suggestion="Elderly man doesn't notice a bear")
    body = news_bot._brand_prompt_text(_state(vision_task=object(), footage=bad))
    assert "⚠️" in body and "Elderly man doesn't notice a bear" in body


def test_a_failed_analysis_leaves_the_picker_as_it_always_was():
    body = news_bot._brand_prompt_text(_state(vision_task=object(), footage={}))
    assert "👁" not in body and "⚠️ headline" not in body


def test_the_brand_picker_advertises_the_caption_reply():
    assert "caption:" in news_bot._brand_prompt_text(_state())


def test_a_typed_caption_is_echoed_back_on_every_picker():
    state = _state(caption=MANUAL, gate_kind="video")
    for body in (news_bot._gate_text(state),
                 news_bot._brand_prompt_text(state),
                 news_bot._publish_prompt_text(state)):
        assert "✍️" in body
        assert "An elderly man was walking down the street" in body


def test_the_publish_picker_only_offers_the_reply_when_instagram_is_there():
    with_ig = _state(platforms=[{"platform": "ig"}])
    without = _state(platforms=[{"platform": "tg"}])
    assert "caption:" in news_bot._publish_prompt_text(with_ig)
    assert "caption:" not in news_bot._publish_prompt_text(without)


def test_a_very_long_caption_is_trimmed_in_the_echo():
    """Telegram caps a message at 4096 characters and an IG caption runs to
    2200 — the echo must not be what costs the picker its keyboard."""
    state = _state(caption="word " * 2000)
    assert len(news_bot._caption_lines(state)[0]) < 1000
