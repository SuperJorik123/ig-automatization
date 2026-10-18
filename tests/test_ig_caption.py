"""Offline tests for modules/instagram/caption.py — no OpenRouter calls.

The client is replaced by a scripted fake, so what is exercised here is the
part that has to hold when the model misbehaves: the never-raises contract
(every failure returns an empty body, because the render is already made
and the operator has already tapped publish) and the cleanup of the wrappers a
chat model reaches for after being told not to use them.
"""

import pytest

from modules.instagram import caption
from shared import config


EXAMPLE = (
    "Released footage shows plane crash at Miami International Airport\n"
    "\n"
    "Newly released footage shows the moment an Amazon cargo plane overran "
    "the runway while landing at Miami International Airport.\n"
    "\n"
    "The Boeing 767, operating for Amazon Prime Air, crashed into multiple "
    "vehicles and caught fire. Five people were killed and five others were "
    "injured.\n"
    "\n"
    "#miami #florida #planecrash #aviation #amazon"
)

# The same caption as a model that ignored the count would return it: the
# prompt's most-specific-first order, run past the pool it was asked for.
EXAMPLE_TOO_MANY_TAGS = EXAMPLE + (" #breakingnews #usa #boeing #ntsb "
                                   "#cargo #runway #news")


class _Fake:
    """Stands in for the OpenAI client: returns `content`, or raises `exc`."""

    def __init__(self, content="", exc=None):
        self.content = content
        self.exc = exc
        self.calls = []
        self.chat = self                      # client.chat.completions.create
        self.completions = self

    def create(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        msg = type("M", (), {"content": self.content})
        return type("R", (), {"choices": [type("C", (), {"message": msg})]})


@pytest.fixture
def on(monkeypatch):
    """Expansion enabled — the default, made explicit so a developer's .env
    can't turn these tests green for the wrong reason."""
    monkeypatch.setattr(config, "IG_CAPTION_ENABLED", True)
    monkeypatch.setattr(config, "IG_CAPTION_MODEL", "openai/gpt-5.5:online")


def _client(monkeypatch, **kw):
    fake = _Fake(**kw)
    monkeypatch.setattr(caption, "_client", fake)
    return fake


# --------------------------------------------------------------------------- #
# the never-raises contract                                                   #
# --------------------------------------------------------------------------- #


def test_expansion_returns_the_caption(on, monkeypatch):
    _client(monkeypatch, content=EXAMPLE)
    assert caption.expand("Released footage shows plane crash") == EXAMPLE


def test_no_api_key_returns_no_body(on, monkeypatch):
    """No body = the post goes out as the bare headline (compose/with_brand_tag
    put that together — expand never returns the headline itself)."""
    monkeypatch.setattr(caption, "_client", None)
    assert caption.expand("Pentagon criticized over benefits") == ""


def test_disabled_returns_no_body_without_calling(on, monkeypatch):
    """The kill switch has to cut the billed call, not just the output."""
    fake = _client(monkeypatch, content=EXAMPLE)
    monkeypatch.setattr(config, "IG_CAPTION_ENABLED", False)
    assert caption.expand("Pentagon criticized") == ""
    assert fake.calls == []


def test_api_failure_returns_no_body(on, monkeypatch):
    _client(monkeypatch, exc=RuntimeError("502 from the gateway"))
    assert caption.expand("Pentagon criticized") == ""


def test_empty_completion_returns_no_body(on, monkeypatch):
    _client(monkeypatch, content="   \n  ")
    assert caption.expand("Pentagon criticized") == ""


def test_a_first_paragraph_that_merely_starts_like_the_headline_stays(on, monkeypatch):
    body = "Pentagon criticized over benefits by veterans.\n\n#usa"
    _client(monkeypatch, content=body)
    assert caption.expand("Pentagon criticized") == body


# --------------------------------------------------------------------------- #
# the operator's info                                                         #
# --------------------------------------------------------------------------- #


INFO = "Filmed in Asheville, North Carolina. Source: WLOS."


def test_blank_info_changes_nothing(on, monkeypatch):
    a = _client(monkeypatch, content=EXAMPLE)
    caption.expand("Plane crash at Miami International")
    b = _client(monkeypatch, content=EXAMPLE)
    caption.expand("Plane crash at Miami International", info="   ")
    assert a.calls[0]["messages"] == b.calls[0]["messages"]


def test_empty_headline_stays_empty(on, monkeypatch):
    fake = _client(monkeypatch, content=EXAMPLE)
    assert caption.expand("   ") == ""
    assert fake.calls == []


def test_no_temperature_is_sent(on, monkeypatch):
    """Current OpenAI models on OpenRouter reject a non-default temperature,
    and that APIError costs the post its caption. See the module docstring."""
    fake = _client(monkeypatch, content=EXAMPLE)
    caption.expand("Pentagon criticized")
    assert "temperature" not in fake.calls[0]


def test_max_tokens_is_capped_so_openrouter_reserves_little(on, monkeypatch):
    """OpenRouter reserves credit for the WORST CASE up front, so an uncapped
    call demands a balance covering the model's full 65,536-token ceiling — on
    2026-09-09 that 402'd a live caption and the post went out as the bare
    headline. Roomy enough to never truncate, small enough to be affordable."""
    fake = _client(monkeypatch, content=EXAMPLE)
    caption.expand("Pentagon criticized")
    assert fake.calls[0]["max_tokens"] == caption.MAX_TOKENS
    # A 2200-character caption plus room for a reasoning model to think.
    assert 2000 <= caption.MAX_TOKENS <= 16000


# --------------------------------------------------------------------------- #
# cleaning up after the model                                                 #
# --------------------------------------------------------------------------- #


def test_code_fence_is_stripped(on, monkeypatch):
    _client(monkeypatch, content="```\n" + EXAMPLE + "\n```")
    assert caption.expand("h") == EXAMPLE


def test_caption_label_is_stripped(on, monkeypatch):
    _client(monkeypatch, content="Caption:\n\n" + EXAMPLE)
    assert caption.expand("h") == EXAMPLE


def test_surrounding_quotes_are_stripped(on, monkeypatch):
    _client(monkeypatch, content='"' + EXAMPLE + '"')
    assert caption.expand("h") == EXAMPLE


def test_citation_markers_are_stripped_without_touching_the_prose(on, monkeypatch):
    _client(monkeypatch, content="Five people were killed [2]. Police said "
                                 "the cause [10] is under investigation.\n"
                                 "\n#miami #usa")
    out = caption.expand("h")
    assert out == ("Five people were killed. Police said the cause is under "
                   "investigation.\n\n#miami #usa")


def test_blank_line_runs_are_collapsed_to_one(on, monkeypatch):
    _client(monkeypatch, content="Headline\n\n\n\nA paragraph.\n\n\n#news")
    assert caption.expand("h") == "Headline\n\nA paragraph.\n\n#news"


def test_hashtag_line_survives_cleaning(on, monkeypatch):
    """The hashtags are the one part every downstream step must not lose: the
    translator copies them verbatim and IG indexes them."""
    _client(monkeypatch, content=EXAMPLE)
    assert caption.expand("h").splitlines()[-1] == (
        "#miami #florida #planecrash #aviation #amazon")


def test_a_short_hashtag_line_is_left_alone(on, monkeypatch):
    _client(monkeypatch, content="Headline\n\nA paragraph.\n\n#ohio #crime")
    assert caption.expand("h").splitlines()[-1] == "#ohio #crime"


def test_prose_mentioning_a_tag_is_not_treated_as_the_tag_line(on, monkeypatch):
    """Only a line that is NOTHING but hashtags is a candidate — a closing
    paragraph that happens to name one must not be chopped."""
    body = ("Headline\n\nThe campaign used #ohio and six other tags across a "
            "dozen posts this week, according to the filing.")
    _client(monkeypatch, content=body)
    assert caption.expand("h") == body


# --------------------------------------------------------------------------- #
# Instagram's 2200-character ceiling                                          #
# --------------------------------------------------------------------------- #


def test_an_overlong_caption_is_trimmed_on_a_word_boundary():
    long = " ".join(["word"] * 800)          # ~4000 chars
    out = caption.compose("Headline", long)
    assert len(out) <= 2200
    assert out.endswith("…")
    assert "wor…" not in out                 # never mid-word


def test_an_overlong_caption_keeps_its_headline_and_its_tag_line():
    long = " ".join(["word"] * 800) + "\n\n#mir #miami #usa"
    out = caption.compose("Headline", long)
    assert len(out) <= 2200
    assert out.startswith("Headline\n\nword")
    assert out.endswith("…\n\n#mir #miami #usa")


# --------------------------------------------------------------------------- #
# compose: search line (or headline), blank line, body                         #
# --------------------------------------------------------------------------- #


PROSE = ("A bear crossed a residential street a few feet behind an elderly "
         "man who did not see it, while people nearby shouted warnings until "
         "he turned around and walked away.")


def test_compose_without_a_search_line_opens_on_the_headline():
    """A model that skipped the search line: the headline goes back on top."""
    assert caption.compose("Man walks past bear", PROSE + "\n\n#mir #usa") \
        == "Man walks past bear\n\n" + PROSE + "\n\n#mir #usa"


def test_compose_without_a_body_is_the_headline():
    assert caption.compose("Man walks past bear", "") == "Man walks past bear"


def test_compose_never_doubles_the_headline():
    assert caption.compose("Man walks past bear",
                           "Man walks past bear\n\n" + PROSE) == \
        "Man walks past bear\n\n" + PROSE


# --------------------------------------------------------------------------- #
# per-account variants: the seed                                              #
# --------------------------------------------------------------------------- #


def test_seed_for_is_stable_across_processes():
    """hash() is salted per process — the same post must plan the same way on
    a restart, so the seed comes from crc32, not hash()."""
    assert caption.seed_for("Pentagon criticized") == 1295926431
    assert caption.seed_for("") == 0
    assert caption.seed_for("a") >= 0


# --------------------------------------------------------------------------- #
# per-account variants: the hashtag pick                                      #
# --------------------------------------------------------------------------- #


POOL = ("Headline\n\nA paragraph.\n\n"
        "#miami #florida #planecrash #aviation #amazon #boeing #ntsb #usa")


def test_pick_hashtags_keeps_the_head_and_cuts_to_five():
    line = caption.pick_hashtags(POOL, 0).splitlines()[-1].split()
    assert len(line) == caption.MAX_HASHTAGS
    # The two most specific tags are what the story is about — every account
    # keeps them; only the tail rotates.
    assert line[:2] == ["#miami", "#florida"]


def test_pick_hashtags_gives_each_account_a_different_line():
    lines = {caption.pick_hashtags(POOL, i).splitlines()[-1] for i in range(6)}
    assert len(lines) == 6


def test_pick_hashtags_is_deterministic():
    assert caption.pick_hashtags(POOL, 7) == caption.pick_hashtags(POOL, 7)


def test_pick_hashtags_leaves_the_prose_alone():
    assert caption.pick_hashtags(POOL, 3).startswith("Headline\n\nA paragraph.")


def test_pick_hashtags_leaves_a_short_line_alone():
    """Five tags back means there is nothing to vary — dropping a relevant tag
    to manufacture a difference would cost more than it buys."""
    short = "Headline\n\nA paragraph.\n\n#ohio #crime"
    assert caption.pick_hashtags(short, 4) == short


def test_pick_hashtags_ignores_prose_that_merely_mentions_a_tag():
    body = ("Headline\n\nThe campaign used #ohio and six other tags across a "
            "dozen posts this week, according to the filing.")
    assert caption.pick_hashtags(body, 2) == body


# --------------------------------------------------------------------------- #
# per-account variants: the account's own tag                                 #
# --------------------------------------------------------------------------- #


def test_brand_tag_is_the_brand_name():
    assert caption.brand_tag("frontiva24") == "#frontiva24"
    assert caption.brand_tag("europamonitor") == "#europamonitor"


def test_brand_tag_drops_what_instagram_would_not_index():
    """A handle may carry a dot ("vestra.24") and a tag ends at the first
    character that is not a letter or a digit — so the tag is built flat."""
    assert caption.brand_tag("Vestra.24") == "#vestra24"
    assert caption.brand_tag("daily news co") == "#dailynewsco"
    assert caption.brand_tag("  ") == ""


def test_the_account_tag_is_first_and_inside_the_five():
    tags = caption.pick_hashtags(POOL, 0, brand="frontiva24").splitlines()[-1].split()
    assert len(tags) == caption.MAX_HASHTAGS
    assert tags[0] == "#frontiva24"
    # The story still keeps its two most specific tags right after it; the
    # brand tag takes a slot off the rotating tail, never the head.
    assert tags[1:3] == ["#miami", "#florida"]


def test_every_account_opens_its_own_line():
    brands = ("altenews", "atlasnews", "frontiva24", "europamonitor",
              "wswire", "vestra24")
    lines = [caption.pick_hashtags(POOL, i, brand=b).splitlines()[-1]
             for i, b in enumerate(brands)]
    assert len(set(lines)) == len(lines)
    for b, line in zip(brands, lines):
        assert line.startswith(f"#{b} ")


def test_a_short_line_still_gets_the_account_tag():
    """Nothing to deal is not a reason to publish an unsigned caption."""
    short = "Headline\n\nA paragraph.\n\n#ohio #crime"
    out = caption.pick_hashtags(short, 4, brand="altenews")
    assert out.splitlines()[-1] == "#altenews #ohio #crime"


def test_a_repeated_tag_is_dealt_once():
    """A model that writes #miami twice must not publish it twice — nor spend
    two of the account's five slots on it."""
    dup = "A paragraph.\n\n#miami #Miami #florida #miami #usa"
    tags = caption.pick_hashtags(dup, 0, brand="wswire").splitlines()[-1].split()
    assert tags == ["#wswire", "#miami", "#florida", "#usa"]


def test_a_sibling_brands_tag_is_never_dealt():
    pool = "A paragraph.\n\n#mirnews #miami #florida #usa #news #wswire"
    out = caption.pick_hashtags(pool, 0, brand="wswire",
                                others=["mirnews", "wswire", "frontiva24"])
    assert out.splitlines()[-1] == "#wswire #miami #florida #usa #news"


def test_a_pool_of_only_sibling_tags_still_signs_the_account():
    out = caption.pick_hashtags("A paragraph.\n\n#mirnews", 0, brand="wswire",
                                others=["mirnews"])
    assert out == "A paragraph.\n\n#wswire"


def test_a_caption_with_no_hashtag_line_gets_one():
    """The bare headline a failed expansion falls back to — the account tag is
    mandatory, so a tag line is made for it."""
    out = caption.with_brand_tag("Five killed in Miami crash", "atlasnews")
    assert out == "Five killed in Miami crash\n\n#atlasnews"


def test_the_account_tag_is_never_doubled():
    line = "Headline\n\nA paragraph.\n\n#miami #frontiva24 #usa"
    out = caption.with_brand_tag(line, "frontiva24").splitlines()[-1]
    assert out == "#frontiva24 #miami #usa"


def test_a_full_line_loses_its_least_specific_tag_not_its_head():
    line = ("Headline\n\nA paragraph.\n\n"
            "#miami #florida #planecrash #aviation #news")
    out = caption.with_brand_tag(line, "wswire").splitlines()[-1].split()
    assert out == ["#wswire", "#miami", "#florida", "#planecrash", "#aviation"]


def test_no_brand_leaves_the_line_exactly_as_it_was():
    assert caption.pick_hashtags(POOL, 3, brand="") == caption.pick_hashtags(POOL, 3)
    assert caption.with_brand_tag(POOL, "") == POOL


def test_the_account_tag_never_lands_in_prose():
    body = ("Headline\n\nThe campaign used #ohio and six other tags across a "
            "dozen posts this week, according to the filing.")
    assert caption.with_brand_tag(body, "altenews") == \
        body + "\n\n#altenews"


# --------------------------------------------------------------------------- #
# per-account variants: the plan                                              #
# --------------------------------------------------------------------------- #


BRANDS = [
    {"name": "altenews", "lang": "en"},
    {"name": "atlasnews", "lang": "en"},
    {"name": "mirnews", "lang": "ru"},
    {"name": "worldbrief", "lang": "en"},
]


def test_plan_covers_every_brand_in_order():
    p = caption.plan(BRANDS, seed=0)
    assert [e["name"] for e in p] == [b["name"] for b in BRANDS]


def test_plan_offsets_are_distinct_so_no_two_accounts_share_hashtags():
    p = caption.plan(BRANDS, seed=99)
    assert len({e["offset"] for e in p}) == len(BRANDS)


def test_the_same_post_plans_the_same_way_twice():
    assert caption.plan(BRANDS, seed=5) == caption.plan(BRANDS, seed=5)


# --------------------------------------------------------------------------- #
# per-account variants: the rephrase call                                     #
# --------------------------------------------------------------------------- #


VARIANT = ("Five killed as cargo plane overruns Miami runway\n"
           "\nA paragraph.\n\n#miami #florida #ntsb")


def test_pick_hashtags_has_a_line_for_every_brand_there_is():
    """Thirteen brands is thirteen accounts on one story; a rotating window
    over the pool's tail only had six lines in it."""
    lines = {caption.pick_hashtags(POOL, i).splitlines()[-1] for i in range(13)}
    assert len(lines) == 13


def test_pick_hashtags_never_repeats_a_tag_in_a_line():
    for i in range(20):
        tags = caption.pick_hashtags(POOL, i).splitlines()[-1].split()
        assert len(tags) == len(set(tags))


# --------------------------------------------------------------------------- #
# per-account variants: the house style                                       #
# --------------------------------------------------------------------------- #


STYLED = [
    {"name": "worldbrief", "lang": "en", "style": "Two paragraphs, never more."},
    {"name": "wswire", "lang": "en", "style": "One long lede sentence."},
    {"name": "plainbrand", "lang": "en"},
]


def test_plan_carries_the_style_through():
    p = caption.plan(STYLED, seed=0)
    assert p[0]["style"] == "Two paragraphs, never more."
    assert p[2]["style"] == ""


def test_a_blank_line_with_a_space_on_it_is_still_blank(on, monkeypatch):
    """A model leaves a space on an "empty" line often enough, and the
    blank-run regex does not see it as blank — it shipped as an empty first
    paragraph on a live post (2026-09-11)."""
    _client(monkeypatch, content="Headline\n \n\nA paragraph.  \n\n#news")
    assert caption.expand("h") == "Headline\n\nA paragraph.\n\n#news"


# --------------------------------------------------------------------------- #
# a mangled hashtag line                                                      #
# --------------------------------------------------------------------------- #


KEYCAP = "#\ufe0f\u20e3 "        # what the model sent instead of "#"


def test_a_keycap_hashtag_line_is_repaired(on, monkeypatch):
    """Live, 2026-09-11: the model returned the KEYCAP HASH emoji plus a
    space. Instagram indexes none of that, and _TAG_LINE did not match it, so
    the line escaped the pool cap AND the per-account pick, and four accounts
    published the same eight dead tags."""
    line = KEYCAP + "madrid " + KEYCAP + "spain " + KEYCAP + "housing"
    _client(monkeypatch, content="Headline\n\nA paragraph.\n\n" + line)
    assert caption.expand("h").splitlines()[-1] == "#madrid #spain #housing"


def test_comma_separated_tags_are_repaired(on, monkeypatch):
    _client(monkeypatch, content="Headline\n\nA paragraph.\n\n"
                                 "#madrid, #spain, #housing")
    assert caption.expand("h").splitlines()[-1] == "#madrid #spain #housing"


def test_prose_containing_a_hash_is_never_repaired(on, monkeypatch):
    body = ("Headline\n\nThe campaign used #ohio, and six other tags, "
            "across a dozen posts.")
    _client(monkeypatch, content=body)
    assert caption.expand("h") == body


def test_a_clean_tag_line_is_left_exactly_alone(on, monkeypatch):
    _client(monkeypatch, content=EXAMPLE)
    assert caption.expand("h") == EXAMPLE


# --------------------------------------------------------------------------- #
# footage-first expansion                                                     #
# --------------------------------------------------------------------------- #
#
# The caption used to be written from the headline alone, which meant the
# :online search — a plugin that runs BEFORE the model and builds its query
# from the prompt — went looking for the headline's words and published a
# caption about whatever most famous story matched them. The footage now goes
# in first and the search is told it may only corroborate it.

FOOTAGE = {
    "on_screen_text": "02/04/2025 18:41",
    "speech": "Someone shouts: turn around!",
    "language": "English",
    "setting": "a residential street, daytime",
    "people_count": "3",
}


def _system_of(fake) -> str:
    return fake.calls[0]["messages"][0]["content"]


def _user_of(fake) -> str:
    return fake.calls[0]["messages"][-1]["content"]


def test_no_footage_sends_exactly_what_it_always_sent(on, monkeypatch):
    """The old path has to stay byte-identical: vision is best-effort, and an
    analysis that fails must not change the caption that gets written."""
    a = _client(monkeypatch, content=EXAMPLE)
    caption.expand("Plane crash at Miami International")
    b = _client(monkeypatch, content=EXAMPLE)
    caption.expand("Plane crash at Miami International", footage={})
    assert a.calls[0]["messages"] == b.calls[0]["messages"]


def test_the_headline_prompt_has_no_web_access(on, monkeypatch):
    fake = _client(monkeypatch, content=EXAMPLE)
    caption.expand("Pentagon criticized", info="Source: Reuters")
    system = _system_of(fake).lower()
    assert "no web access" in system
    assert "search the web" not in system


def test_searchless_strips_only_the_online_suffix():
    assert caption.searchless("openai/gpt-5.6-luna:online") == "openai/gpt-5.6-luna"
    assert caption.searchless("openai/gpt-5.6-luna") == "openai/gpt-5.6-luna"


def test_footage_expansion_still_never_raises(on, monkeypatch):
    _client(monkeypatch, exc=RuntimeError("gateway down"))
    assert caption.expand("Man walks past bear", footage=FOOTAGE) == ""


def test_footage_expansion_is_cleaned_like_any_other(on, monkeypatch):
    _client(monkeypatch, content="```\n" + EXAMPLE + "\n```")
    assert caption.expand("h", footage=FOOTAGE) == EXAMPLE


def test_disabled_skips_the_call_even_with_footage(on, monkeypatch):
    fake = _client(monkeypatch, content=EXAMPLE)
    monkeypatch.setattr(config, "IG_CAPTION_ENABLED", False)
    assert caption.expand("Man walks past bear", footage=FOOTAGE) == ""
    assert fake.calls == []


# --------------------------------------------------------------------------- #
# youtube_description                                                         #
# --------------------------------------------------------------------------- #


YT_CAPTION = (
    "Man walks past a bear\n"
    "\n"
    "A bear crossed a residential street a few feet behind a man.\n"
    "\n"
    "Neighbours said it had been seen in the area for a week.\n"
    "\n"
    "Wildlife officials asked residents to secure their bins.\n"
    "\n"
    "#mir #bear #usa #wildlife #viralvideo"
)


def test_youtube_description_drops_the_headline_it_is_the_title():
    out = caption.youtube_description(YT_CAPTION, "Man walks past a bear")
    assert not out.startswith("Man walks past a bear")
    assert out.startswith("A bear crossed")


def test_youtube_description_keeps_the_tags_and_adds_shorts():
    out = caption.youtube_description(YT_CAPTION, "Man walks past a bear")
    assert out.splitlines()[-1] == "#mir #bear #usa #wildlife #viralvideo #shorts"


def test_youtube_description_is_cut_to_whole_lead_paragraphs():
    out = caption.youtube_description(YT_CAPTION, "Man walks past a bear",
                                      limit=130)
    assert "Neighbours said" in out
    assert "Wildlife officials" not in out


def test_youtube_description_trims_one_long_lead_paragraph():
    long = "Headline\n\n" + "word " * 300 + "\n\n#mir #news"
    out = caption.youtube_description(long, "Headline")
    prose = out.split("\n\n")[0]
    assert len(prose) <= caption.YT_DESC_PROSE and prose.endswith("…")


def test_youtube_description_of_a_bare_headline_is_just_the_tags():
    out = caption.youtube_description("Headline\n\n#mir", "Headline")
    assert out == "#mir #shorts"


def test_youtube_description_never_doubles_shorts_or_keeps_angle_brackets():
    out = caption.youtube_description("H\n\nA <b> c.\n\n#mir #Shorts", "H")
    assert out.count("#") == 2 and "<" not in out and ">" not in out


# --------------------------------------------------------------------------- #
# the comment to pin                                                          #
# --------------------------------------------------------------------------- #


def test_pinned_comment_drops_a_paragraph(on, monkeypatch):
    _client(monkeypatch, content="word " * 60)
    assert caption.pinned_comment("A caption.") == ""


def test_pinned_comment_never_raises(on, monkeypatch):
    class _Boom:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    raise RuntimeError("gateway down")
    monkeypatch.setattr(caption, "_client", _Boom)
    assert caption.pinned_comment("A caption.") == ""
    assert caption.pinned_comment("") == ""


# --------------------------------------------------------------------------- #
# the 2026-10-18 redesign: sources, shape, rewrites, hashtags                 #
# --------------------------------------------------------------------------- #


BASE = ("Teen saves friend from electrocution on football pitch\n\n"
        "His friend collapsed by the wall near the goal. He ran in, got "
        "shocked himself, and still dragged him clear.\n\n"
        "Would you have known what to do?\n\n"
        "#electrocution #rescue #futsal #electricshock #teenhero #futsall "
        "#football #hero")

SOURCE = {"platform": "x", "url": "https://x.com/a/status/1",
          "text": "Brazil: a 15-year-old pulled his friend off a live wall.",
          "author": "newsdesk"}


def test_the_user_message_lists_the_four_sources_in_trust_order(on, monkeypatch):
    fake = _client(monkeypatch, content=BASE)
    caption.expand("Teenager saves friend from electrocution", FOOTAGE,
                   info="Happened in Sao Paulo.", source=SOURCE)
    user = _user_of(fake).splitlines()
    assert user[0] == "OPERATOR'S INFO: Happened in Sao Paulo."
    assert user[1] == "HEADLINE: Teenager saves friend from electrocution"
    assert user[2] == ("SOURCE POST (X): Brazil: a 15-year-old pulled his "
                       "friend off a live wall.")
    assert user[3].startswith("MEDIA NOTES: on-screen text: 02/04/2025 18:41;")


def test_missing_sources_say_none(on, monkeypatch):
    fake = _client(monkeypatch, content=BASE)
    caption.expand("Teenager saves friend")
    assert _user_of(fake).splitlines() == [
        "OPERATOR'S INFO: (none)", "HEADLINE: Teenager saves friend",
        "SOURCE POST: (none)", "MEDIA NOTES: (none)"]


def test_an_overlong_source_post_is_cut(on, monkeypatch):
    fake = _client(monkeypatch, content=BASE)
    caption.expand("h", source=dict(SOURCE, text="word " * 1000))
    assert len(_user_of(fake)) < caption.SOURCE_TEXT_MAX + 200


def test_old_narration_never_reaches_the_prompt(on, monkeypatch):
    """A scheduled post stored before the redesign carries summary/beats —
    exactly the narration that turned an electrocution into a fall."""
    fake = _client(monkeypatch, content=BASE)
    caption.expand("h", {"summary": "two players fell over",
                         "beats": ["they fell"]})
    assert "fell" not in _user_of(fake)


def test_the_prompt_is_the_operators_spec(on, monkeypatch):
    fake = _client(monkeypatch, content=BASE)
    caption.expand("h")
    system = _system_of(fake)
    for must in ("SOURCES, IN ORDER OF TRUST", "LINE 1, THE HOOK. At most 90",
                 "CONTEXT. One or two short sentences", "QUESTION.",
                 "Eight candidates", "NO CALLS TO ACTION",
                 "clothing colour", "\"the video shows\""):
        assert must in system
    assert "share line" not in system.lower()


def test_the_expansion_keeps_eight_candidates(on, monkeypatch):
    _client(monkeypatch, content=BASE + " #extra1 #extra2")
    _, tags = caption.split_tags(caption.expand("h"))
    assert len(tags) == caption.CANDIDATE_HASHTAGS == 8


def test_split_tags_returns_the_prose_and_normalised_tags():
    prose, tags = caption.split_tags(BASE)
    assert prose.endswith("Would you have known what to do?")
    assert tags[:3] == ["electrocution", "rescue", "futsal"]
    assert caption.split_tags("Just prose.") == ("Just prose.", [])


def test_split_tags_keeps_cyrillic_tags():
    _, tags = caption.split_tags("Текст.\n\n#Новости #спасение")
    assert tags == ["новости", "спасение"]


# --- plan --------------------------------------------------------------------


def test_one_unstyled_english_brand_keeps_the_base_caption():
    p = caption.plan([{"name": "a", "lang": "en"}, {"name": "b", "lang": "en"},
                      {"name": "c", "lang": "en", "style": "Calm."}])
    assert [e["rewrite"] for e in p] == [False, True, True]


def test_every_styled_brand_rewrites():
    p = caption.plan([{"name": "a", "lang": "en", "style": "Punchy."},
                      {"name": "b", "lang": "en", "style": "Calm."}])
    assert all(e["rewrite"] for e in p)


def test_a_non_english_brand_always_rewrites():
    p = caption.plan([{"name": "v", "lang": "ru"}])
    assert p[0]["rewrite"] is True


# --- rephrase -----------------------------------------------------------------


PROSE_ONLY = caption.split_tags(BASE)[0]


def test_rephrase_returns_the_variant_without_tags_in_english(on, monkeypatch):
    _client(monkeypatch, content="Hook\n\nContext.\n\nQuestion?\n\n#stray")
    assert caption.rephrase(PROSE_ONLY, "en", "Punchy.") == \
        "Hook\n\nContext.\n\nQuestion?"


def test_rephrase_keeps_a_foreign_rewrites_own_candidates(on, monkeypatch):
    out = "Хук\n\nКонтекст.\n\nВопрос?\n\n#удартоком #спасение"
    _client(monkeypatch, content=out)
    assert caption.rephrase(PROSE_ONLY, "ru") == out


def test_rephrase_asks_a_foreign_brand_for_native_tags(on, monkeypatch):
    fake = _client(monkeypatch, content="x")
    caption.rephrase(PROSE_ONLY, "ru", "Calm.")
    system = _system_of(fake)
    assert "Russian" in system and "HASHTAG CANDIDATES" in system
    assert "the hook included" in system


def test_rephrase_in_english_asks_for_no_tags(on, monkeypatch):
    fake = _client(monkeypatch, content="x")
    caption.rephrase(PROSE_ONLY, "en", "Calm.")
    system = _system_of(fake)
    assert "no hashtags" in system and "HASHTAG CANDIDATES" not in system


def test_the_tone_is_tone_only_and_the_question_preference_is_soft(on, monkeypatch):
    fake = _client(monkeypatch, content="x")
    caption.rephrase(PROSE_ONLY, "en", "Prefers a blunt question.")
    system = _system_of(fake)
    assert "HOUSE TONE — how your page always sounds: Prefers a blunt question." in system
    assert "Tone only" in system
    assert "otherwise ask any question specific to the story" in system


def test_rephrase_demands_a_different_hook_and_question(on, monkeypatch):
    fake = _client(monkeypatch, content="x")
    caption.rephrase(PROSE_ONLY, "en", "Calm.")
    system = _system_of(fake)
    assert "a different hook" in system and "a different question" in system
    assert "SAME FACTS, NOTHING ADDED" in system
    assert "Never ask the reader to send, share, save" in system


def test_rephrase_is_searchless_and_sends_no_temperature(on, monkeypatch):
    monkeypatch.setattr(config, "IG_CAPTION_VARIANT_MODEL", "x/y:online")
    fake = _client(monkeypatch, content="x")
    caption.rephrase(PROSE_ONLY, "en", "Calm.")
    assert fake.calls[0]["model"] == "x/y"
    assert "temperature" not in fake.calls[0]


@pytest.mark.parametrize("kw", [dict(exc=RuntimeError("down")), dict(content="")])
def test_rephrase_failure_returns_the_base(on, monkeypatch, kw):
    _client(monkeypatch, **kw)
    assert caption.rephrase(PROSE_ONLY, "en", "Calm.") == PROSE_ONLY


def test_rephrase_disabled_or_keyless_does_not_call(on, monkeypatch):
    fake = _client(monkeypatch, content="x")
    monkeypatch.setattr(config, "IG_CAPTION_ENABLED", False)
    assert caption.rephrase(PROSE_ONLY, "en", "Calm.") == PROSE_ONLY
    assert fake.calls == []
    monkeypatch.setattr(caption, "_client", None)
    monkeypatch.setattr(config, "IG_CAPTION_ENABLED", True)
    assert caption.rephrase(PROSE_ONLY, "en", "Calm.") == PROSE_ONLY


# --- choose_hashtags ----------------------------------------------------------


@pytest.fixture
def counts(monkeypatch):
    seen = []
    table = {"electrocution": 45_000, "rescue": 9_000_000, "futsal": 4_000_000,
             "electricshock": 12_000, "teenhero": 0, "futsall": 300,
             "удартоком": 7_000, "спасение": 600_000}

    def lookup(tags):
        seen.append(list(tags))
        return {t: table.get(t, 0) for t in tags}, {"unrelated": 50_000}
    monkeypatch.setattr(config, "APIFY_TOKEN", "token")
    monkeypatch.setattr(caption.hashtags, "lookup", lookup)
    return seen


def test_every_caption_is_measured_in_one_pass(counts):
    caption.choose_hashtags({
        "wswire": ["electrocution", "rescue"],
        "vestra24": ["удартоком", "спасение"]})
    assert len(counts) == 1
    assert sorted(counts[0]) == sorted(
        ["electrocution", "rescue", "удартоком", "спасение"])


def test_in_band_first_then_relevance_fallback_up_to_three(counts):
    out = caption.choose_hashtags({"en": ["electrocution", "rescue", "futsal",
                                          "electricshock", "teenhero", "futsall"]})
    assert out["en"] == ["electrocution", "rescue", "electricshock"]


def test_zero_and_tiny_tags_are_never_picked(counts):
    out = caption.choose_hashtags({"en": ["teenhero", "futsall", "rescue"]})
    assert out["en"] == ["rescue"]


def test_related_suggestions_are_never_used(counts):
    out = caption.choose_hashtags({"en": ["teenhero"]})
    assert out["en"] == []


def test_apify_down_keeps_the_top_four_unmeasured(monkeypatch):
    monkeypatch.setattr(config, "APIFY_TOKEN", "token")
    monkeypatch.setattr(caption.hashtags, "lookup", lambda tags: None)
    out = caption.choose_hashtags({"en": list("abcdef")})
    assert out["en"] == ["a", "b", "c", "d"]


def test_no_token_never_calls_apify(monkeypatch):
    monkeypatch.setattr(config, "APIFY_TOKEN", "")
    monkeypatch.setattr(caption.hashtags, "lookup",
                        lambda tags: (_ for _ in ()).throw(AssertionError))
    assert caption.choose_hashtags({"en": ["a", "b"]}) == {"en": ["a", "b"]}


# --- compose / finish ---------------------------------------------------------


def test_compose_opens_on_the_hook():
    body = "Teen saves friend from electrocution\n\nContext.\n\nQ?\n\n#a #b"
    assert caption.compose("Headline", body) == body


def test_compose_puts_the_headline_on_top_when_the_hook_is_missing():
    long = "word " * 40
    assert caption.compose("Headline", long.strip()).startswith("Headline\n\n")


def test_an_overlong_caption_keeps_its_tag_line():
    body = "Hook\n\n" + "word " * 600 + "\n\n#mir #a"
    out = caption.compose("Headline", body)
    assert len(out) <= caption.CAPTION_MAX
    assert out.startswith("Hook\n\n") and out.endswith("\n\n#mir #a")


def test_finish_signs_the_line_with_the_brand_tag_first():
    out = caption.finish("Headline", "Hook\n\nContext.\n\nQ?",
                         ["electrocution", "rescue", "electricshock"],
                         brand="wswire", others={"mirnews"})
    assert out.splitlines()[-1] == "#wswire #electrocution #rescue #electricshock"


def test_finish_with_no_tags_still_carries_the_brand_tag():
    out = caption.finish("Headline", "Hook\n\nContext.", [], brand="wswire")
    assert out.endswith("\n\n#wswire")


def test_finish_drops_a_sibling_brands_tag():
    out = caption.finish("H", "Hook", ["mirnews", "rescue"], brand="wswire",
                         others={"mirnews", "wswire"})
    assert out.splitlines()[-1] == "#wswire #rescue"


# --- captions_for (the CLI / trace pipeline) ------------------------------------


def test_captions_for_runs_the_whole_pipeline(on, monkeypatch, counts):
    _client(monkeypatch, content=BASE)
    monkeypatch.setattr(caption, "rephrase",
                        lambda text, lang="", style="", model=None, avoid=():
                        "Хук\n\nКонтекст.\n\nВопрос?\n\n#удартоком #спасение"
                        if lang == "ru" else "Other hook\n\nCtx.\n\nQ2?")
    out = caption.captions_for(
        "Teenager saves friend", [
            {"name": "wswire", "lang": "en", "style": "Formal."},
            {"name": "vestra24", "lang": "ru", "style": "Calm."}],
        source=SOURCE)
    assert out["wswire"].startswith("Other hook")
    assert out["wswire"].splitlines()[-1].startswith("#wswire #electrocution")
    assert out["vestra24"].splitlines()[-1] == "#vestra24 #удартоком #спасение"
    assert len(counts) == 1


# --- pinned comment credit ------------------------------------------------------


@pytest.mark.parametrize("source,want", [
    ({"platform": "x", "author": "newsdesk"}, " via @newsdesk"),
    ({"platform": "instagram", "author": "clips.daily"}, " via @clips.daily"),
    ({"platform": "reddit", "author": "throwaway1"}, " via u/throwaway1"),
    ({"platform": "reddit", "author": ""}, " via Reddit"),
    ({"platform": "", "author": "someone"}, ""),
    (None, ""),
])
def test_credit_never_invents_a_handle(source, want):
    assert caption.credit(source) == want


def test_pinned_comment_is_credited_to_the_source(on, monkeypatch):
    fake = _client(monkeypatch, content='"Would you have gone in?"')
    out = caption.pinned_comment("A caption.", SOURCE)
    assert out == "Would you have gone in? via @newsdesk"
    assert "Brazil: a 15-year-old" in _user_of(fake)


def test_pinned_comment_without_a_source_has_no_credit(on, monkeypatch):
    _client(monkeypatch, content="Would you have gone in?")
    assert caption.pinned_comment("A caption.") == "Would you have gone in?"


# --- rewrite_all: no two pages with the same hook or question ---------------------


def test_similar_catches_a_one_word_swap_but_not_a_rewrite():
    assert caption.similar(
        "Teenager saves friend from electrocution after suffering a severe shock",
        "Teenager rescues friend from electrocution after suffering a severe shock")
    assert not caption.similar(
        "Crowded train passenger erupts after frustration builds",
        "Passenger hits a breaking point on a crowded train")


def test_a_page_too_close_to_another_is_rewritten_once_told_what_is_taken(monkeypatch):
    seen = []

    def fake(text, lang="", style="", model=None, avoid=()):
        seen.append((style, list(avoid)))
        if avoid:
            return "A brand new hook here\n\nCtx.\n\nWho is to blame here?"
        return "Same hook for everyone\n\nCtx.\n\nWould you have helped?"

    monkeypatch.setattr(caption, "rephrase", fake)
    entries = caption.plan([{"name": "a", "lang": "en", "style": "A."},
                            {"name": "b", "lang": "en", "style": "B."}])
    texts = caption.rewrite_all("Base hook\n\nCtx.\n\nQ?", entries)
    assert texts[0].startswith("Same hook") and texts[1].startswith("A brand new")
    retry = [a for _, a in seen if a]
    assert retry == [["Same hook for everyone", "Would you have helped?"]]


def test_distinct_rewrites_cost_no_second_call(monkeypatch):
    calls = []

    def fake(text, lang="", style="", model=None, avoid=()):
        calls.append(style)
        return {"A.": "First page hook\n\nCtx.\n\nHero or reckless?",
                "B.": "Totally other opening line\n\nCtx.\n\nWould you go in?"}[style]

    monkeypatch.setattr(caption, "rephrase", fake)
    entries = caption.plan([{"name": "a", "lang": "en", "style": "A."},
                            {"name": "b", "lang": "en", "style": "B."}])
    caption.rewrite_all("Base\n\nCtx.\n\nQ?", entries)
    assert sorted(calls) == ["A.", "B."]


def test_the_avoid_list_reaches_the_prompt(on, monkeypatch):
    fake = _client(monkeypatch, content="x")
    caption.rephrase("Hook\n\nCtx.", "en", "Calm.", avoid=["Taken hook", "Taken?"])
    system = _system_of(fake)
    assert "ALREADY TAKEN" in system and "- Taken hook" in system


def test_both_prompts_ban_quiz_questions(on, monkeypatch):
    fake = _client(monkeypatch, content=BASE)
    caption.expand("h")
    assert "OPINION or EXPERIENCE" in _system_of(fake)
    fake = _client(monkeypatch, content="x")
    caption.rephrase("Hook\n\nCtx.", "en", "Calm.")
    assert "OPINION or EXPERIENCE" in _system_of(fake)
