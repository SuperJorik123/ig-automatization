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
    seen = {"expand": [], "rephrase": [], "hashtags": []}

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
    # The rewrites run in parallel threads, so they are recorded in whatever
    # order they finish — compare as a set of languages, not a sequence.
    assert sorted(r[1] for r in calls["rephrase"] if not r[3]) == ["en", "ru"]
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
# render-time captions: shared with the publish                              #
# --------------------------------------------------------------------------- #


class _Chat:
    id = 0  # not the control group, so _track leaves the DB alone

    def __init__(self):
        self.sent = []

    async def send_message(self, text, **kw):
        self.sent.append((text, kw))
        return type("M", (), {"chat": self, "message_id": len(self.sent)})()

    def get_bot(self):
        return self

    async def edit_message_caption(self, chat_id, message_id, caption):
        self.edited = getattr(self, "edited", []) + [(message_id, caption)]


def _render(name, group="JNN", ig=""):
    p = _pair(name)
    p["render"]["brand"].update(group=group, ig=ig)
    p["render"]["preview_id"] = 77
    return p["render"]


def test_the_publish_reuses_the_render_time_search(calls):
    """One expansion per post: the hand-posted captions at render time and
    the IG publish afterwards share the cached one."""
    chat, state = _Chat(), {"text": "Man walks past a bear", "footage": FOOTAGE}
    asyncio.run(news_bot._send_captions(chat, state, [_render("eur24news")]))
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


# --------------------------------------------------------------------------- #
# every preview carries the caption its account will publish                  #
# --------------------------------------------------------------------------- #


def test_every_preview_gets_its_brand_and_caption(calls):
    chat, state = _Chat(), {"text": "Man walks past a bear", "footage": FOOTAGE}
    renders = [_render("wswire", ig="wswire"), _render("mirnews", group="GMN")]
    asyncio.run(news_bot._send_captions(chat, state, renders))
    assert chat.sent == []   # no hand-posted brand, so no extra message
    assert [mid for mid, _ in chat.edited] == [77, 77]
    for (_, cap), name in zip(chat.edited, ("wswire", "mirnews")):
        assert cap.startswith(f"{name}\n\n") and "<pre>" not in cap
        assert f"#{name}" in cap


def test_the_publish_ships_the_captions_the_previews_show(calls):
    chat, state = _Chat(), {"text": "Man walks past a bear", "footage": FOOTAGE}
    asyncio.run(news_bot._send_captions(chat, state, [_render("wswire", ig="wswire")]))
    assert news_bot._fresh_captions(state) == state["captions"] != {}
    state["info"] = "Source: WLOS."     # an info: reply makes them stale
    assert news_bot._fresh_captions(state) == {}


def test_captions_without_a_key_are_trusted():
    assert news_bot._fresh_captions({"text": "h", "captions": {"a": "x"}}) == {"a": "x"}


def test_a_caption_too_long_for_telegram_goes_as_a_reply():
    chat = _Chat()
    r = _render("wswire", ig="wswire")
    asyncio.run(news_bot._caption_previews(chat, [r], {"wswire": "x" * 1100}))
    assert not getattr(chat, "edited", [])
    text, kw = chat.sent[0]
    assert text.startswith("wswire\n\n") and kw["reply_to_message_id"] == 77


def test_a_bundle_preview_keeps_its_video_label():
    chat = _Chat()
    asyncio.run(news_bot._caption_previews(
        chat, [_render("wswire", ig="wswire")], {"wswire": "cap"}, "video 2"))
    assert chat.edited == [(77, "wswire · video 2\n\ncap")]


# --------------------------------------------------------------------------- #
# previews: one message, the light copy with its caption on it                #
# --------------------------------------------------------------------------- #


class _VideoChat(_Chat):
    def __init__(self):
        super().__init__()
        self.videos = []

    async def send_video(self, video, caption="", **kw):
        self.videos.append((getattr(video, "name", ""), caption))
        return type("M", (), {"chat": self, "message_id": 500 + len(self.videos)})()


@pytest.fixture
def renders(tmp_path, monkeypatch):
    copies = []

    def fake_copy(src, out, duration=None):
        copies.append(src)
        with open(out, "wb") as fh:
            fh.write(b"light")
        return out

    monkeypatch.setattr(news_bot.branding, "preview_copy", fake_copy)
    out = []
    for name in ("wswire", "mirnews"):
        p = tmp_path / f"brand_1_{name}.mp4"
        p.write_bytes(b"full render")
        out.append({"brand": {"name": name}, "path": str(p), "headline": "H"})
    return out, copies


def test_each_preview_is_the_light_copy_with_its_caption(renders):
    rs, copies = renders
    chat, state = _VideoChat(), {"files": []}
    asyncio.run(news_bot._send_previews(
        chat, state, rs, 30.0, "", {"wswire": "Cap W", "mirnews": "Cap M"}, []))
    assert copies == [r["path"] for r in rs]
    assert [(n.endswith("_preview.mp4"), c) for n, c in chat.videos] == [
        (True, "wswire\n\nCap W"), (True, "mirnews\n\nCap M")]
    assert chat.sent == []                       # no extra messages
    assert [r["preview_id"] for r in rs] == [501, 502]
    assert all(p.endswith("_preview.mp4") for p in state["files"])


def test_a_caption_too_long_for_the_video_follows_as_a_reply(renders):
    rs, _ = renders
    chat = _VideoChat()
    asyncio.run(news_bot._send_previews(
        chat, {"files": []}, rs[:1], 30.0, "", {"wswire": "x" * 1100}, []))
    assert chat.videos[0][1] == "🏷 wswire"
    text, kw = chat.sent[0]
    assert text == "wswire\n\n" + "x" * 1100 and kw["reply_to_message_id"] == 501


def test_no_captions_means_the_brand_name_only(renders):
    rs, _ = renders
    chat = _VideoChat()
    asyncio.run(news_bot._send_previews(chat, {"files": []}, rs[:1], 30.0,
                                        "video 2", {}, []))
    assert chat.videos[0][1] == "🏷 wswire · video 2" and chat.sent == []


def test_lite_mode_sends_the_full_render(renders, monkeypatch):
    rs, copies = renders
    monkeypatch.setattr(config, "LITE", True)
    chat = _VideoChat()
    asyncio.run(news_bot._send_previews(chat, {"files": []}, rs, 30.0, "", {}, []))
    assert copies == []
    assert [n for n, _ in chat.videos] == [r["path"] for r in rs]


def test_a_failed_copy_falls_back_to_the_full_render(renders, monkeypatch):
    rs, _ = renders

    def boom(src, out, duration=None):
        raise RuntimeError("ffmpeg died")

    monkeypatch.setattr(news_bot.branding, "preview_copy", boom)
    chat = _VideoChat()
    asyncio.run(news_bot._send_previews(chat, {"files": []}, rs[:1], 30.0, "",
                                        {"wswire": "Cap"}, []))
    assert chat.videos == [(rs[0]["path"], "wswire\n\nCap")]


def test_caption_texts_from_a_failed_or_missing_task():
    async def go():
        async def bad():
            raise RuntimeError("x")

        async def good():
            return {"a": "cap"}

        failed, ok = asyncio.create_task(bad()), asyncio.create_task(good())
        return (await news_bot._caption_texts(None),
                await news_bot._caption_texts(failed),
                await news_bot._caption_texts(ok))

    assert asyncio.run(go()) == ({}, {}, {"a": "cap"})


# --------------------------------------------------------------------------- #
# the banner's row limit: a headline over 3 rows is refused before ffmpeg     #
# --------------------------------------------------------------------------- #

import os  # noqa: E402

_FONT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "assets", "fonts", "DejaVuSans-Bold.ttf")
_STYLE = {"font": _FONT, "font_size": 49}


def test_headline_rows_follows_the_render_wrap():
    rows = news_bot.branding.headline_rows
    assert rows("Short headline", "x/logo.png", _STYLE) == 1
    assert rows("", "x/logo.png", _STYLE) == 0
    long = " ".join(["Officer says he let a double homicide suspect walk"] * 4)
    expected = news_bot.branding.wrap_to_px(long, _FONT, 49).count("\n") + 1
    assert rows(long, "x/logo.png", _STYLE) == expected > 3


def _job(name, headline):
    return {"brand": {"name": name}, "headline": headline,
            "logo_path": f"brands/X/{name}/logo.png"}


def test_a_headline_over_three_rows_stops_the_render(monkeypatch):
    counts = {"short": 2, "long": 4, "russian": 5}
    monkeypatch.setattr(news_bot.branding, "headline_rows",
                        lambda h, logo, style=None: counts[h])
    news_bot._check_headline_rows([_job("wswire", "short")])   # fine
    with pytest.raises(news_bot._HeadlineTooLong) as err:
        news_bot._check_headline_rows([_job("wswire", "short"),
                                       _job("dailynews", "long"),
                                       _job("vestra24", "russian")])
    assert err.value.over == [("dailynews", 4), ("vestra24", 5)]
    msg = str(err.value)
    assert msg.startswith("headline too long — 5 rows on vestra24; "
                          "4 rows on dailynews (max 3)")
    assert "shorter" in msg


def test_a_broken_brand_style_is_left_to_the_render(monkeypatch):
    def boom(h, logo, style=None):
        raise FileNotFoundError("style.json")

    monkeypatch.setattr(news_bot.branding, "headline_rows", boom)
    news_bot._check_headline_rows([_job("wswire", "anything")])   # no raise


def test_the_row_limit_is_a_render_setup_error():
    """So a bundle lists the video as failed instead of crashing the render."""
    assert issubclass(news_bot._HeadlineTooLong, news_bot._RenderSetupError)
