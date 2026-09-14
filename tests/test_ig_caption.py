"""Offline tests for modules/instagram/caption.py — no OpenRouter calls.

The client is replaced by a scripted fake, so what is exercised here is the
part that has to hold when the model misbehaves: the never-raises contract
(every failure returns the bare headline, because the render is already made
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


def test_no_api_key_returns_the_bare_headline(on, monkeypatch):
    monkeypatch.setattr(caption, "_client", None)
    assert caption.expand("Pentagon criticized over benefits") == \
        "Pentagon criticized over benefits"


def test_disabled_returns_the_bare_headline_without_calling(on, monkeypatch):
    """The kill switch has to cut the billed call, not just the output."""
    fake = _client(monkeypatch, content=EXAMPLE)
    monkeypatch.setattr(config, "IG_CAPTION_ENABLED", False)
    assert caption.expand("Pentagon criticized") == "Pentagon criticized"
    assert fake.calls == []


def test_api_failure_returns_the_bare_headline(on, monkeypatch):
    _client(monkeypatch, exc=RuntimeError("502 from the gateway"))
    assert caption.expand("Pentagon criticized") == "Pentagon criticized"


def test_empty_completion_returns_the_bare_headline(on, monkeypatch):
    _client(monkeypatch, content="   \n  ")
    assert caption.expand("Pentagon criticized") == "Pentagon criticized"


def test_empty_headline_stays_empty(on, monkeypatch):
    fake = _client(monkeypatch, content=EXAMPLE)
    assert caption.expand("   ") == ""
    assert fake.calls == []


def test_the_headline_is_the_user_message(on, monkeypatch):
    fake = _client(monkeypatch, content=EXAMPLE)
    caption.expand("Pentagon criticized")
    assert fake.calls[0]["model"] == "openai/gpt-5.5:online"
    assert fake.calls[0]["messages"][-1] == {"role": "user",
                                             "content": "Pentagon criticized"}


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


def test_an_overlong_hashtag_line_is_cut_to_the_pool(on, monkeypatch):
    """expand() returns a POOL — each account draws its own five from it (see
    pick_hashtags), so what is capped here is the pool, not the post. A model
    that overshoots even that must not hand down a twenty-tag spam line;
    most-specific-first ordering means the HEAD is what survives."""
    _client(monkeypatch, content=EXAMPLE_TOO_MANY_TAGS)
    tags = caption.expand("h").splitlines()[-1].split()
    assert len(tags) == min(caption.POOL_HASHTAGS,
                            len(EXAMPLE_TOO_MANY_TAGS.splitlines()[-1].split()))
    assert tags[:5] == ["#miami", "#florida", "#planecrash", "#aviation",
                        "#amazon"]


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


def test_an_overlong_caption_is_trimmed_on_a_word_boundary(on, monkeypatch):
    long = " ".join(["word"] * 800)          # ~4000 chars
    _client(monkeypatch, content=long)
    out = caption.expand("h")
    assert len(out) <= 2200
    assert out.endswith("…")
    assert "wor…" not in out                 # never mid-word


# --------------------------------------------------------------------------- #
# per-account variants: the rotating angle                                    #
# --------------------------------------------------------------------------- #


def test_angle_for_is_deterministic_and_cycles():
    assert caption.angle_for(0) == caption.angle_for(len(caption.ANGLES))
    assert caption.angle_for(3) == caption.angle_for(3)
    # Enough angles that every IG account on a post gets its own one.
    assert len(caption.ANGLES) >= 8


def test_consecutive_offsets_give_different_angles():
    seen = [caption.angle_for(i) for i in range(len(caption.ANGLES))]
    assert len(set(seen)) == len(caption.ANGLES)


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


def test_the_account_tag_is_last_and_inside_the_five():
    tags = caption.pick_hashtags(POOL, 0, brand="frontiva24").splitlines()[-1].split()
    assert len(tags) == caption.MAX_HASHTAGS
    assert tags[-1] == "#frontiva24"
    # The story still keeps its two most specific tags; the brand tag takes a
    # slot off the rotating tail, never the head.
    assert tags[:2] == ["#miami", "#florida"]


def test_every_account_signs_its_own_line():
    lines = [caption.pick_hashtags(POOL, i, brand=b).splitlines()[-1]
             for i, b in enumerate(("altenews", "atlasnews", "frontiva24",
                                    "europamonitor", "wswire", "vestra24"))]
    assert len(set(lines)) == len(lines)
    for b, line in zip(("altenews", "atlasnews", "frontiva24",
                        "europamonitor", "wswire", "vestra24"), lines):
        assert line.endswith(f"#{b}")


def test_a_short_line_still_gets_the_account_tag():
    """Nothing to deal is not a reason to publish an unsigned caption."""
    short = "Headline\n\nA paragraph.\n\n#ohio #crime"
    out = caption.pick_hashtags(short, 4, brand="altenews")
    assert out.splitlines()[-1] == "#ohio #crime #altenews"


def test_a_caption_with_no_hashtag_line_gets_one():
    """The bare headline a failed expansion falls back to — the account tag is
    mandatory, so a tag line is made for it."""
    out = caption.with_brand_tag("Five killed in Miami crash", "atlasnews")
    assert out == "Five killed in Miami crash\n\n#atlasnews"


def test_the_account_tag_is_never_doubled():
    line = "Headline\n\nA paragraph.\n\n#miami #frontiva24 #usa"
    out = caption.with_brand_tag(line, "frontiva24").splitlines()[-1]
    assert out == "#miami #usa #frontiva24"


def test_a_full_line_loses_its_least_specific_tag_not_its_head():
    line = ("Headline\n\nA paragraph.\n\n"
            "#miami #florida #planecrash #aviation #news")
    out = caption.with_brand_tag(line, "wswire").splitlines()[-1].split()
    assert out == ["#miami", "#florida", "#planecrash", "#aviation", "#wswire"]


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


def test_the_first_brand_of_a_language_is_not_rephrased():
    """It posts the shared caption — that call is already paid for, and one
    account per language may as well carry the canonical wording."""
    p = caption.plan(BRANDS, seed=0)
    assert p[0]["angle"] is None            # first en
    assert p[2]["angle"] is None            # only ru


def test_every_later_brand_of_a_language_gets_its_own_angle():
    p = caption.plan(BRANDS, seed=0)
    angles = [p[1]["angle"], p[3]["angle"]]
    assert all(angles)
    assert len(set(angles)) == 2


def test_plan_offsets_are_distinct_so_no_two_accounts_share_hashtags():
    p = caption.plan(BRANDS, seed=99)
    assert len({e["offset"] for e in p}) == len(BRANDS)


def test_the_same_post_plans_the_same_way_twice():
    assert caption.plan(BRANDS, seed=5) == caption.plan(BRANDS, seed=5)


def test_a_different_post_rotates_the_angles():
    """"Rotating angle per post" — the same account must not always open the
    same way, or its posts become a template."""
    a = [e["angle"] for e in caption.plan(BRANDS, seed=0)]
    b = [e["angle"] for e in caption.plan(BRANDS, seed=1)]
    assert a != b


# --------------------------------------------------------------------------- #
# per-account variants: the rephrase call                                     #
# --------------------------------------------------------------------------- #


VARIANT = ("Five killed as cargo plane overruns Miami runway\n"
           "\nA paragraph.\n\n#miami #florida #ntsb")


def test_rephrase_returns_the_variant(on, monkeypatch):
    _client(monkeypatch, content=VARIANT)
    assert caption.rephrase(EXAMPLE, caption.ANGLES[0]) == VARIANT


def test_rephrase_uses_the_offline_variant_model(on, monkeypatch):
    """No search: the facts are already in the caption being rewritten, and a
    second search would double the only bill this feature has."""
    fake = _client(monkeypatch, content=VARIANT)
    monkeypatch.setattr(config, "IG_CAPTION_VARIANT_MODEL", "openai/cheap")
    caption.rephrase(EXAMPLE, caption.ANGLES[0])
    assert fake.calls[0]["model"] == "openai/cheap"
    assert ":online" not in fake.calls[0]["model"]


def test_rephrase_sends_the_angle_and_no_temperature(on, monkeypatch):
    fake = _client(monkeypatch, content=VARIANT)
    caption.rephrase(EXAMPLE, "Open with the number at the centre.")
    sent = fake.calls[0]["messages"][0]["content"]
    assert "Open with the number at the centre." in sent
    assert "temperature" not in fake.calls[0]
    assert fake.calls[0]["max_tokens"] == caption.MAX_TOKENS


def test_rephrase_into_another_language_is_one_call_not_two(on, monkeypatch):
    """A foreign-language account's variant IS its translation — asking for
    both in one call keeps the bill at the translate it replaces."""
    fake = _client(monkeypatch, content=VARIANT)
    caption.rephrase(EXAMPLE, caption.ANGLES[0], lang="ru")
    assert len(fake.calls) == 1
    assert "ru" in fake.calls[0]["messages"][0]["content"]


def test_rephrase_failure_returns_the_caption_unchanged(on, monkeypatch):
    _client(monkeypatch, exc=RuntimeError("502 from the gateway"))
    assert caption.rephrase(EXAMPLE, caption.ANGLES[0]) == EXAMPLE


def test_rephrase_empty_completion_returns_the_caption_unchanged(on, monkeypatch):
    _client(monkeypatch, content="  \n ")
    assert caption.rephrase(EXAMPLE, caption.ANGLES[0]) == EXAMPLE


def test_rephrase_without_a_key_returns_the_caption_unchanged(on, monkeypatch):
    monkeypatch.setattr(caption, "_client", None)
    assert caption.rephrase(EXAMPLE, caption.ANGLES[0]) == EXAMPLE


def test_rephrase_disabled_returns_the_caption_without_calling(on, monkeypatch):
    fake = _client(monkeypatch, content=VARIANT)
    monkeypatch.setattr(config, "IG_CAPTION_ENABLED", False)
    assert caption.rephrase(EXAMPLE, caption.ANGLES[0]) == EXAMPLE
    assert fake.calls == []


def test_rephrase_without_an_angle_or_a_style_does_not_call(on, monkeypatch):
    fake = _client(monkeypatch, content=VARIANT)
    assert caption.rephrase(EXAMPLE, "") == EXAMPLE
    assert fake.calls == []


def test_rephrase_cleans_up_after_the_model(on, monkeypatch):
    _client(monkeypatch, content="```\n" + VARIANT + "\n```")
    assert caption.rephrase(EXAMPLE, caption.ANGLES[0]) == VARIANT


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


def _rewrites(entry):
    """What news_bot asks: does this account write its own caption at all?"""
    return bool(entry["angle"] or entry["style"])


def test_a_styled_brand_always_gets_its_own_call():
    """A style is the whole point of having one, and the shared caption is
    written in no account's voice — so the free ride passes to a brand that
    has none."""
    p = caption.plan(STYLED, seed=0)
    assert _rewrites(p[0]) and _rewrites(p[1])
    assert not _rewrites(p[2])


def test_a_styled_brand_gets_no_angle_on_top_of_its_voice():
    """Two structural instructions fight: a voice that says "two paragraphs,
    never more" against an angle that says "write it as four short
    paragraphs" comes back in neither."""
    p = caption.plan(STYLED, seed=0)
    assert p[0]["angle"] is None and p[1]["angle"] is None


def test_a_voiceless_brand_gets_an_angle_instead():
    brands = STYLED + [{"name": "otherplain", "lang": "en"}]
    p = caption.plan(brands, seed=0)
    assert p[3]["angle"] and not p[3]["style"]


def test_the_free_ride_is_taken_once_per_language():
    brands = STYLED + [{"name": "otherplain", "lang": "en"}]
    p = caption.plan(brands, seed=0)
    assert [_rewrites(e) for e in p] == [True, True, False, True]


def test_with_every_brand_styled_nobody_takes_the_shared_caption():
    p = caption.plan(STYLED[:2], seed=0)
    assert all(_rewrites(e) for e in p)


def test_a_style_alone_is_enough_to_rewrite(on, monkeypatch):
    """news_bot calls rephrase for a styled account with no angle at all."""
    fake = _client(monkeypatch, content=VARIANT)
    assert caption.rephrase(EXAMPLE, "", style="Two paragraphs.") == VARIANT
    sent = fake.calls[0]["messages"][0]["content"]
    assert "Two paragraphs." in sent
    assert "HOW YOURS MUST DIFFER" not in sent


def test_neither_angle_nor_style_does_not_call(on, monkeypatch):
    fake = _client(monkeypatch, content=VARIANT)
    assert caption.rephrase(EXAMPLE, "", style="") == EXAMPLE
    assert fake.calls == []


def test_plan_carries_the_style_through():
    p = caption.plan(STYLED, seed=0)
    assert p[0]["style"] == "Two paragraphs, never more."
    assert p[2]["style"] == ""


def test_the_style_is_sent_with_the_angle(on, monkeypatch):
    fake = _client(monkeypatch, content=VARIANT)
    caption.rephrase(EXAMPLE, caption.ANGLES[0], style="Two paragraphs, never more.")
    sent = fake.calls[0]["messages"][0]["content"]
    assert "Two paragraphs, never more." in sent
    assert caption.ANGLES[0] in sent


def test_the_style_never_outranks_the_facts(on, monkeypatch):
    """A voice changes how a fact is told, never which facts exist — the
    prompt has to say so next to the style itself, not only above it."""
    fake = _client(monkeypatch, content=VARIANT)
    caption.rephrase(EXAMPLE, caption.ANGLES[0], style="Punchy and loud.")
    sent = fake.calls[0]["messages"][0]["content"]
    house = sent.index("HOUSE STYLE")
    assert "never which facts exist" in sent[house:]


def test_no_style_sends_no_house_style_section(on, monkeypatch):
    fake = _client(monkeypatch, content=VARIANT)
    caption.rephrase(EXAMPLE, caption.ANGLES[0])
    assert "HOUSE STYLE" not in fake.calls[0]["messages"][0]["content"]


def test_style_and_language_travel_together_in_one_call(on, monkeypatch):
    fake = _client(monkeypatch, content=VARIANT)
    caption.rephrase(EXAMPLE, caption.ANGLES[0], lang="ru", style="Numbers first.")
    assert len(fake.calls) == 1
    sent = fake.calls[0]["messages"][0]["content"]
    assert "Numbers first." in sent and "ru" in sent


def test_a_blank_line_with_a_space_on_it_is_still_blank(on, monkeypatch):
    """A model leaves a space on an "empty" line often enough, and the
    blank-run regex does not see it as blank — it shipped as an empty first
    paragraph on a live post (2026-09-11)."""
    _client(monkeypatch, content="Headline\n \n\nA paragraph.  \n\n#news")
    assert caption.expand("h") == "Headline\n\nA paragraph.\n\n#news"


def test_the_house_style_examples_are_marked_as_form_only(on, monkeypatch):
    """A style descriptor carries examples, and a model copied one verbatim
    onto a story it did not fit — "The investigation continues." on a protest
    (2026-09-11). The prompt has to say examples are shapes, not text."""
    fake = _client(monkeypatch, content=VARIANT)
    caption.rephrase(EXAMPLE, "", style="End on a status line.")
    sent = fake.calls[0]["messages"][0]["content"]
    house = sent.index("HOUSE STYLE")
    assert "FORM ONLY" in sent[house:]


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


def test_a_repaired_line_is_then_capped_and_picked(on, monkeypatch):
    tags = " ".join(KEYCAP + t for t in
                    ("a", "b", "c", "d", "e", "f", "g", "h", "i", "j"))
    _client(monkeypatch, content="Headline\n\nA paragraph.\n\n" + tags)
    out = caption.expand("h")
    assert len(out.splitlines()[-1].split()) == caption.POOL_HASHTAGS
    assert len(caption.pick_hashtags(out, 0).splitlines()[-1].split()) == \
        caption.MAX_HASHTAGS


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
    "summary": "An elderly man walks along a street as a bear crosses behind him.",
    "beats": [
        "A man walks along a pavement past parked cars.",
        "A bear steps out of the treeline a few feet behind him.",
        "He turns, sees the bear and backs away.",
    ],
    "audible": "Bystanders shouting, warning the man to turn around.",
    "setting": "A residential street, daytime.",
    "headline_ok": True,
    "headline_note": "",
    "headline_suggestion": "",
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


def test_the_footage_is_sent_with_the_headline(on, monkeypatch):
    fake = _client(monkeypatch, content=EXAMPLE)
    caption.expand("Man walks past bear", footage=FOOTAGE)
    user = _user_of(fake)
    assert "Man walks past bear" in user
    assert FOOTAGE["summary"] in user
    assert "Bystanders shouting" in user
    assert "A bear steps out of the treeline" in user


def test_the_footage_prompt_makes_the_media_outrank_the_search(on, monkeypatch):
    fake = _client(monkeypatch, content=EXAMPLE)
    caption.expand("Man walks past bear", footage=FOOTAGE)
    system = _system_of(fake).lower()
    assert "ground truth" in system
    assert "contradict" in system


def test_the_footage_prompt_forbids_restating_the_headline(on, monkeypatch):
    """The thin duplicate caption — headline, then one paragraph saying the
    headline again — is the second half of the bug this replaced."""
    fake = _client(monkeypatch, content=EXAMPLE)
    caption.expand("Man walks past bear", footage=FOOTAGE)
    assert "restate" in _system_of(fake).lower()


def test_the_footage_prompt_offers_both_registers(on, monkeypatch):
    fake = _client(monkeypatch, content=EXAMPLE)
    caption.expand("Man walks past bear", footage=FOOTAGE)
    system = _system_of(fake)
    assert "THE CLIP IS THE STORY" in system
    assert "A REPORTED EVENT" in system


def test_footage_expansion_still_never_raises(on, monkeypatch):
    _client(monkeypatch, exc=RuntimeError("gateway down"))
    assert caption.expand("Man walks past bear", footage=FOOTAGE) == \
        "Man walks past bear"


def test_footage_expansion_is_cleaned_like_any_other(on, monkeypatch):
    _client(monkeypatch, content="```\n" + EXAMPLE + "\n```")
    assert caption.expand("h", footage=FOOTAGE) == EXAMPLE


def test_disabled_skips_the_call_even_with_footage(on, monkeypatch):
    fake = _client(monkeypatch, content=EXAMPLE)
    monkeypatch.setattr(config, "IG_CAPTION_ENABLED", False)
    assert caption.expand("Man walks past bear", footage=FOOTAGE) == \
        "Man walks past bear"
    assert fake.calls == []
