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


def test_rephrase_without_an_angle_does_not_call(on, monkeypatch):
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
