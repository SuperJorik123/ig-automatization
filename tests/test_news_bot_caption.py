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
    "on_screen_text": "CAM 3 02/04/2025",
    "speech": "Someone shouts: turn around!",
    "language": "English",
    "setting": "a residential street, daytime",
    "people_count": "3",
}

INFO = "Filmed in Asheville, North Carolina. Source: WLOS."

SOURCE = {"platform": "x", "url": "https://x.com/wlos/status/1",
          "text": "Bear wanders behind a man on Kimberly Ave.",
          "author": "wlos"}

# The base caption `expand` returns when the model skipped the hook: a prose
# paragraph too long to be one, then eight candidates — so every account's
# caption still opens on its own render headline.
EXPANDED = (
    "A bear crossed a residential street a few feet behind a man who did not "
    "see it, while people nearby shouted warnings until he finally turned "
    "around, spotted the animal and walked quickly away from it.\n"
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
    seen = {"expand": [], "rephrase": [], "hashtags": [], "comment": []}

    def _expand(headline, footage=None, model=None, info="", source=None):
        seen["expand"].append((headline, footage, info, source))
        return EXPANDED

    def _rephrase(text, lang="", style="", model=None, avoid=()):
        seen["rephrase"].append((text, lang, style, tuple(avoid)))
        return text

    real_choose = news_bot.ig_caption.choose_hashtags

    def _choose(candidates):
        seen["hashtags"].append(dict(candidates))
        return real_choose(candidates)      # no APIFY_TOKEN in tests: top four

    monkeypatch.setattr(news_bot.ig_caption, "expand", _expand)
    monkeypatch.setattr(news_bot.ig_caption, "rephrase", _rephrase)
    monkeypatch.setattr(news_bot.ig_caption, "choose_hashtags", _choose)

    def _comment(text, source=None, model=None):
        seen["comment"].append((text, source))
        return "Would you have noticed the bear?"

    monkeypatch.setattr(news_bot.ig_caption, "pinned_comment", _comment)
    return seen


def _run(*args, **kw):
    return asyncio.run(news_bot._ig_captions(*args, **kw))


# --------------------------------------------------------------------------- #
# the operator's info and the footage reaching the expansion                  #
# --------------------------------------------------------------------------- #


def test_the_info_is_passed_to_the_expansion(calls):
    _run("Man walks past a bear", [_pair()], FOOTAGE, INFO)
    assert calls["expand"] == [("Man walks past a bear", FOOTAGE, INFO, None)]


def test_info_does_not_skip_the_search(calls):
    """Info informs the caption; it is not the caption. The AI still writes."""
    _run("Man walks past a bear", [_pair()], {}, INFO)
    assert len(calls["expand"]) == 1


def test_the_footage_is_passed_to_the_expansion(calls):
    _run("Man walks past a bear", [_pair()], FOOTAGE)
    assert calls["expand"] == [("Man walks past a bear", FOOTAGE, "", None)]


def test_no_footage_passes_an_empty_dict(calls):
    _run("Man walks past a bear", [_pair()])
    assert calls["expand"] == [("Man walks past a bear", {}, "", None)]


def test_the_expansion_is_bought_once_for_every_account(calls):
    _run("h", [_pair("a"), _pair("b"), _pair("c")], FOOTAGE, INFO)
    assert len(calls["expand"]) == 1


def test_the_source_post_reaches_the_expansion(calls):
    _run("Man walks past a bear", [_pair()], FOOTAGE, INFO, source=SOURCE)
    assert calls["expand"] == [("Man walks past a bear", FOOTAGE, INFO, SOURCE)]


def test_every_account_is_measured_in_one_hashtag_pass(calls):
    _run("h", [_pair("a"), _pair("b"), _pair("c", "ru")], FOOTAGE)
    assert len(calls["hashtags"]) == 1
    assert set(calls["hashtags"][0]) == {"a", "b", "c"}


def test_only_the_first_unstyled_english_brand_skips_the_rewrite(calls):
    _run("h", [_pair("a"), _pair("b"), _pair("c", "ru")], FOOTAGE)
    assert [r[1] for r in calls["rephrase"] if not r[3]] == ["en", "ru"]
    assert all("#" not in r[0] for r in calls["rephrase"])   # tags stripped


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


def test_the_hook_replaces_the_headline_as_line_one(monkeypatch, calls):
    """The new shape: hook, context, question, tags — no share line."""
    monkeypatch.setattr(
        news_bot.ig_caption, "expand",
        lambda *a, **k: "Bear walks behind man on Asheville street\n\n"
                        "He never saw it.\n\nWould you have noticed?\n\n"
                        "#bear #bearsighting")
    out = _run("Man walks past a bear", [_pair()], FOOTAGE)["mir"]
    assert out.split("\n\n") == [
        "Bear walks behind man on Asheville street", "He never saw it.",
        "Would you have noticed?", "#mir #bear #bearsighting"]


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


def test_every_account_gets_its_tag_and_four_unique_story_tags(calls):
    out = _run("h", [_pair("mir"), _pair("wswire")], FOOTAGE)
    for caption in out.values():
        tags = caption.splitlines()[-1].split()
        assert len(tags) == 5
        assert len({t.lower() for t in tags}) == 5


def test_a_sibling_brand_tag_is_never_dealt(monkeypatch, calls):
    monkeypatch.setattr(news_bot.ig_caption, "expand",
                        lambda *a, **kw: "Para.\n\n#wswire #bear #usa #news")
    # (no Apify in tests: each account keeps the top four candidates)
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


def test_the_plan_card_shows_the_media_notes():
    body = news_bot._plan_text(_card(watching=True, footage=FOOTAGE))
    assert "watching" not in body
    assert "👁 on-screen text: CAM 3 02/04/2025" in body


def test_the_plan_card_shows_the_source_post():
    body = news_bot._plan_text(_card(source=SOURCE))
    assert "📎 source post (via @wlos): Bear wanders behind a man" in body


def test_no_source_post_means_no_source_line():
    assert "📎" not in news_bot._plan_text(_card())
    assert "📎" not in news_bot._plan_text(_card(source={"platform": ""}))


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


def test_a_facebook_pair_buys_the_caption_too(calls):
    """Facebook posts the same full caption Instagram does, not the headline."""
    out = _run("Man walks past a bear", [dict(_pair(), platform="fb")], FOOTAGE)
    assert len(calls["expand"]) == 1
    assert out["mir"].startswith("Man walks past a bear\n\nA bear crossed")


def test_the_publish_picker_offers_the_reply_for_facebook():
    state = _state(platforms=[{"platform": "fb"}])
    assert "info:" in news_bot._publish_prompt_text(state)


def test_a_brand_on_ig_and_fb_is_planned_once(calls):
    out = _run("h", [_pair(), dict(_pair(), platform="fb")], FOOTAGE)
    assert list(out) == ["mir"] and len(calls["expand"]) == 1


def test_a_brand_on_ig_and_yt_is_planned_once(calls):
    out = _run("h", [_pair(), dict(_pair(), platform="yt")], FOOTAGE)
    assert list(out) == ["mir"]


# --------------------------------------------------------------------------- #
# hand-posted brands: the caption arrives with the render preview             #
# --------------------------------------------------------------------------- #


def test_only_non_gmn_brands_without_ig_are_hand_posted():
    assert news_bot._manual_ig({"group": "JNN", "ig": ""})
    assert news_bot._manual_ig({"group": "", "ig": ""})
    assert not news_bot._manual_ig({"group": "JNN", "ig": "eur24news"})
    assert not news_bot._manual_ig({"group": "GMN", "ig": ""})
    assert not news_bot._manual_ig({"group": "gmn", "ig": ""})


class _Chat:
    id = 0  # not the control group, so _track leaves the DB alone

    def __init__(self):
        self.sent = []

    async def send_message(self, text, **kw):
        self.sent.append((text, kw))
        return type("M", (), {"chat": self, "message_id": len(self.sent)})()


def _render(name, group="JNN", ig=""):
    p = _pair(name)
    p["render"]["brand"].update(group=group, ig=ig)
    p["render"]["preview_id"] = 77
    return p["render"]


def test_hand_posted_brands_get_their_caption_as_a_reply(calls):
    chat, state = _Chat(), {"text": "Man walks past a bear", "footage": FOOTAGE}
    renders = [_render("eur24news"), _render("wswire", ig="wswire"),
               _render("mirnews", group="GMN")]
    asyncio.run(news_bot._send_manual_captions(chat, state, renders))
    assert len(chat.sent) == 1
    text, kw = chat.sent[0]
    assert "eur24news" in text and "<pre>" in text
    assert "#eur24news" in text
    assert kw["parse_mode"] == "HTML" and kw["reply_to_message_id"] == 77


def test_the_publish_reuses_the_render_time_search(calls):
    """One expansion per post: the hand-posted captions at render time and
    the IG publish afterwards share the cached one."""
    chat, state = _Chat(), {"text": "Man walks past a bear", "footage": FOOTAGE}
    asyncio.run(news_bot._send_manual_captions(chat, state, [_render("eur24news")]))
    _run("Man walks past a bear", [_pair("wswire")], FOOTAGE,
         cache=state["caption_cache"])
    assert len(calls["expand"]) == 1


def test_a_new_info_reply_buys_a_fresh_expansion(calls):
    cache = {}
    _run("h", [_pair()], FOOTAGE, cache=cache)
    _run("h", [_pair()], FOOTAGE, "Source: WLOS.", cache=cache)
    assert len(calls["expand"]) == 2


def test_a_cached_expansion_is_reused_with_the_same_sources(calls):
    cache = {}
    _run("h", [_pair()], FOOTAGE, INFO, cache=cache, source=SOURCE)
    _run("h", [_pair("b")], FOOTAGE, INFO, cache=cache, source=SOURCE)
    assert len(calls["expand"]) == 1


def test_hand_posted_brands_get_a_comment_to_pin(calls):
    """Written from that account's own caption, sent in the same message."""
    chat, state = _Chat(), {"text": "Man walks past a bear", "footage": FOOTAGE}
    asyncio.run(news_bot._send_manual_captions(
        chat, state, [_render("eur24news"), _render("pioneerwire")]))
    assert len(chat.sent) == 2
    for text, _ in chat.sent:
        assert "📌 Comment to pin:\n<pre>Would you have noticed the bear?</pre>" in text
    assert all("#" in c for c, _ in calls["comment"])  # the finished captions


def test_the_comment_to_pin_gets_the_source_post(calls):
    chat = _Chat()
    state = {"text": "Man walks past a bear", "footage": FOOTAGE,
             "source": SOURCE}
    asyncio.run(news_bot._send_manual_captions(chat, state,
                                               [_render("eur24news")]))
    assert calls["comment"][0][1] == SOURCE


def test_no_comment_block_when_the_comment_fails(monkeypatch, calls):
    monkeypatch.setattr(news_bot.ig_caption, "pinned_comment", lambda t, source=None, model=None: "")
    chat, state = _Chat(), {"text": "Man walks past a bear", "footage": FOOTAGE}
    asyncio.run(news_bot._send_manual_captions(chat, state, [_render("eur24news")]))
    assert "📌" not in chat.sent[0][0] and "<pre>" in chat.sent[0][0]
