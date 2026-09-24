"""Offline tests for the news bot's Instagram-caption wiring.

news_bot exits at import without a configured control group, which is why its
pure pieces normally live in branded.py/groups.py. What is left inside it and
worth testing is the wiring itself: that the operator's info and the footage
reach the one expansion, that every account's caption opens on its own
headline, and what the pickers say. Nothing here touches Telegram, OpenRouter
or ffmpeg.
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
}

INFO = "Filmed in Asheville, North Carolina. Source: WLOS."

# The body `expand` returns: no headline, paragraphs, then the pool.
EXPANDED = (
    "A bear crossed a residential street a few feet behind a man who did not "
    "see it.\n"
    "\n"
    "#bear #usa #wildlife #caughtoncamera #viralvideo #news #animals #street"
)


def _pair(name="mir", lang="en", headline="Man walks past a bear"):
    brand = {"name": name, "lang": lang, "group": "", "tg": "", "yt": "",
             "tw": "", "ig": f"{name}gram",
             "logo": f"/nonexistent/{name}/logo.png"}
    return {"platform": "ig", "label": f"{name} → IG",
            "render": {"brand": brand, "path": f"/tmp/{name}.mp4",
                       "headline": headline}}


@pytest.fixture
def calls(monkeypatch):
    """Record every billed call the caption layer would make."""
    seen = {"expand": [], "rephrase": []}

    def _expand(headline, footage=None, model=None, info=""):
        seen["expand"].append((headline, footage, info))
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
# the operator's info and the footage reaching the expansion                  #
# --------------------------------------------------------------------------- #


def test_the_info_is_passed_to_the_expansion(calls):
    _run("Man walks past a bear", [_pair()], FOOTAGE, INFO)
    assert calls["expand"] == [("Man walks past a bear", FOOTAGE, INFO)]


def test_info_does_not_skip_the_search(calls):
    """Info informs the caption; it is not the caption. The AI still writes."""
    _run("Man walks past a bear", [_pair()], {}, INFO)
    assert len(calls["expand"]) == 1


def test_the_footage_is_passed_to_the_expansion(calls):
    _run("Man walks past a bear", [_pair()], FOOTAGE)
    assert calls["expand"] == [("Man walks past a bear", FOOTAGE, "")]


def test_no_footage_passes_an_empty_dict(calls):
    _run("Man walks past a bear", [_pair()])
    assert calls["expand"] == [("Man walks past a bear", {}, "")]


def test_the_search_is_bought_once_for_every_account(calls):
    _run("h", [_pair("a"), _pair("b"), _pair("c")], FOOTAGE, INFO)
    assert len(calls["expand"]) == 1


def test_a_post_with_no_instagram_pair_buys_nothing(calls):
    pair = dict(_pair(), platform="tg")
    assert _run("h", [pair], FOOTAGE) == {}
    assert calls["expand"] == []


def test_no_headline_buys_nothing(calls):
    assert _run("", [_pair()], {}, INFO) == {}
    assert calls["expand"] == []


def test_a_failure_leaves_every_pair_on_its_headline(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(news_bot.ig_caption, "expand", _boom)
    assert _run("h", [_pair()], FOOTAGE) == {}


def test_an_empty_expansion_leaves_every_pair_on_its_headline(monkeypatch):
    monkeypatch.setattr(news_bot.ig_caption, "expand", lambda *a, **kw: "")
    assert _run("h", [_pair()], FOOTAGE) == {}


# --------------------------------------------------------------------------- #
# the finished caption                                                        #
# --------------------------------------------------------------------------- #


def test_the_caption_opens_on_the_headline_then_a_blank_line(calls):
    out = _run("Man walks past a bear", [_pair()], FOOTAGE)
    assert out["mir"].startswith("Man walks past a bear\n\nA bear crossed")


def test_each_account_opens_on_its_own_render_headline(calls):
    """The translated headline on a brand's banner is the one its caption
    opens with — not the source text the operator typed."""
    out = _run("Man walks past a bear",
               [_pair("mir"), _pair("rusnews", "ru", headline="Мужчина и медведь")],
               FOOTAGE)
    assert out["mir"].splitlines()[0] == "Man walks past a bear"
    assert out["rusnews"].splitlines()[0] == "Мужчина и медведь"


def test_every_account_tag_line_opens_with_its_own_tag(calls):
    out = _run("h", [_pair("mir"), _pair("wswire")], FOOTAGE)
    assert out["mir"].splitlines()[-1].split()[0] == "#mir"
    assert out["wswire"].splitlines()[-1].split()[0] == "#wswire"


def test_every_account_gets_five_unique_tags(calls):
    out = _run("h", [_pair("mir"), _pair("wswire")], FOOTAGE)
    for caption in out.values():
        tags = caption.splitlines()[-1].split()
        assert len(tags) == 5
        assert len({t.lower() for t in tags}) == 5


def test_a_sibling_brand_tag_is_never_dealt(monkeypatch, calls):
    monkeypatch.setattr(news_bot.ig_caption, "expand",
                        lambda *a, **kw: "Para.\n\n#wswire #bear #usa #news")
    out = _run("h", [_pair("mir"), _pair("wswire")], FOOTAGE)
    assert "#wswire" not in out["mir"]
    assert out["wswire"].splitlines()[-1].split().count("#wswire") == 1


# --------------------------------------------------------------------------- #
# what the pickers say                                                        #
# --------------------------------------------------------------------------- #


def _state(**kw):
    state = {"text": "Man walks past a bear", "media": [], "files": [],
             "brands": [], "sel_brands": set(), "platforms": [],
             "sel_platforms": set()}
    state.update(kw)
    return state


def _card(**kw):
    """A plan-card state: _state plus what _init_plan adds."""
    state = _state(gate_kind="video", card=False, layout=None,
                   plan_platforms=set(), custom_brands=False, editing=False)
    state.update(kw)
    return state


def test_the_plan_card_says_it_is_watching_while_the_call_runs():
    body = news_bot._plan_text(_card(watching=True))
    assert "👁 watching the clip…" in body


def test_the_plan_card_shows_what_was_seen():
    body = news_bot._plan_text(_card(watching=True, footage=FOOTAGE))
    assert "watching" not in body
    assert FOOTAGE["summary"] in body


def test_the_plan_card_never_questions_the_headline():
    bad = dict(FOOTAGE, headline_ok=False, headline_note="different man",
               headline_suggestion="Elderly man doesn't notice a bear")
    body = news_bot._plan_text(_card(watching=True, footage=bad))
    assert "may not match" not in body
    assert "Elderly man doesn't notice a bear" not in body


def test_a_failed_analysis_leaves_the_card_without_an_eye_line():
    body = news_bot._plan_text(_card(watching=True, footage={}))
    assert "👁" not in body


def test_the_plan_card_advertises_the_info_reply():
    body = news_bot._plan_text(_card())
    assert "info:" in body and "caption:" not in body


def test_the_plan_card_opens_on_the_headline_and_shows_the_plan():
    body = news_bot._plan_text(_card())
    assert body.startswith("📰 Man walks past a bear")
    assert "Brands: none" in body and "Platforms: none" in body


def test_typed_info_is_echoed_back_on_every_picker():
    state = _card(info=INFO)
    for body in (news_bot._plan_text(state),
                 news_bot._publish_prompt_text(state)):
        assert "ℹ️" in body
        assert "Source: WLOS." in body


def test_the_publish_picker_only_offers_the_reply_when_instagram_is_there():
    with_ig = _state(platforms=[{"platform": "ig"}])
    without = _state(platforms=[{"platform": "tg"}])
    assert "info:" in news_bot._publish_prompt_text(with_ig)
    assert "info:" not in news_bot._publish_prompt_text(without)


def test_very_long_info_is_trimmed_in_the_echo():
    """Telegram caps a message at 4096 characters — the echo must not be what
    costs the picker its keyboard."""
    state = _state(info="word " * 2000)
    assert len(news_bot._info_lines(state)[0]) < 1000


def test_a_youtube_pair_buys_the_caption_too(calls):
    out = _run("h", [dict(_pair(), platform="yt")], FOOTAGE)
    assert len(calls["expand"]) == 1 and "mir" in out


def test_a_brand_on_ig_and_yt_is_planned_once(calls):
    out = _run("h", [_pair(), dict(_pair(), platform="yt")], FOOTAGE)
    assert list(out) == ["mir"]
