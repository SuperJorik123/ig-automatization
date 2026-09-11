"""
modules/instagram/caption.py — a headline into a full Instagram caption.

Instagram is the one platform in this repo where the burned-in headline is not
enough on its own. A Telegram post carries the video and a line of text, a
tweet is 280 characters — but an IG caption is read as the story, and the
account's own posts are always the same shape: the headline, three or four
short wire-service paragraphs under it, then one line of hashtags.

So the IG leg of the brand-it flow no longer posts `headline`. It posts what
this module makes of it: one OpenRouter call on a web-search-enabled model
(`IG_CAPTION_MODEL`, an `:online` id — that suffix is what gives any model on
the gateway live search) that looks the story up as it stands today and writes
those paragraphs out of what it finds.

Two contracts, both taken from modules/telegram/translator.py, because they are
what make a model safe to put in a publishing path:

  NEVER RAISES — a missing key, a dead gateway, an unknown model id, an empty
  completion all return the headline unchanged. The render is already made and
  the operator has already tapped publish; a caption hiccup must cost the post
  its paragraphs, not its existence.

  FAITHFUL — the search is there to source the headline's own story, not to
  build a story around it. The prompt says so at length, and says to write one
  short paragraph rather than pad when the search comes back thin. On a news
  account an invented detail costs more than a short caption ever will.

The output is in the source headline's language. news_bot translates it per
brand afterwards through the same translator the headline goes through, so a
Russian account gets Russian prose and — per the translator's FORMAT rule —
the identical, untranslated hashtags.

ONE CAPTION IS NOT ENOUGH FOR N ACCOUNTS. That expansion used to be cached per
LANGUAGE, and twelve of the thirteen brands are "en" — so every English
Instagram account published the same caption, character for character, within
seconds of each other. That is what Instagram reads as duplicate content. The
fix is not n searches: the search is the whole bill (~$0.01 a post, more than
the tokens), while the wording is nearly free. So the facts are bought ONCE and
the wording varies per account:

  `expand`   one `:online` call, unchanged, except that it now asks for a POOL
             of hashtags (POOL_HASHTAGS) rather than the five one post carries.
  `plan`     pure: which accounts need a variant, with which angle and which
             hashtag offset. The FIRST account of each language keeps the
             shared caption — that call is already paid for.
  `rephrase` one cheap, SEARCHLESS call per remaining account
             (IG_CAPTION_VARIANT_MODEL): same facts, in THIS ACCOUNT'S VOICE —
             the `writing_style` out of brands/<name>/style.json, which is the
             same on every post it publishes. An account with no voice gets a
             rotating ANGLES directive instead, never both: two structural
             instructions fight and the caption comes back in neither. Given a
             `lang` it also translates, so a foreign-language account's variant
             IS its translation — one call, not two.
  `pick_hashtags`
             pure: each account keeps the pool's two most specific tags and
             rotates three more out of the tail. No tokens, and no two accounts
             carry the same tag line.

The angle rotates by POST as well as by account (`seed_for`), so an account
does not open every one of its posts the same way.

`temperature` is not sent — several current OpenAI models on OpenRouter reject
a non-default one, which would surface here as an APIError, that is, as a post
that silently lost its caption.

`max_tokens` IS sent, generously (MAX_TOKENS below), and that is a fix, not a
limit. OpenRouter reserves credit for a request's WORST CASE before it runs:
uncapped, that is the model's full 65,536-token ceiling, so the gateway demands
a balance covering 65,536 output tokens for a job that emits about six hundred.
On 2026-09-09 that rejected a live caption with a 402 at a balance of $1.42 and
the post went out as the bare headline. A cap large enough that a reasoning
model can think first and still write — the failure mode the uncapped call was
avoiding — drops the reserve by an order of magnitude. Length is still governed
by the prompt, not by this number; nothing should ever come near it.

CLI, for tuning the prompts against real headlines — `--accounts N` prints the
post as N accounts would publish it, which is the only way to see whether the
angles are actually pulling the rewrites apart:
    py modules/instagram/caption.py "Released footage shows plane crash at Miami International Airport"
    py modules/instagram/caption.py --accounts 4 "Released footage shows plane crash …"
"""

import itertools
import logging
import math
import os
import re
import sys
import zlib

# Repo-root bootstrap for direct runs (`py modules/instagram/caption.py ...`).
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from openai import OpenAI  # noqa: E402

from shared import config  # noqa: E402
from modules.instagram.graph import trim_caption  # noqa: E402

log = logging.getLogger(__name__)

_client = (
    OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=config.OPENROUTER_API_KEY,
    )
    if config.OPENROUTER_API_KEY
    else None
)

# Reasoning tokens plus a caption of at most 2200 characters. Wide enough that
# the model is never the one that stops (see the module docstring), narrow
# enough that OpenRouter's up-front credit reservation stays under a cent.
MAX_TOKENS = 8000

_SYSTEM = (
    "You write the Instagram captions for a news account. You are given one "
    "headline. Search the web for that story as it stands today, then write "
    "the account's caption for it.\n\n"
    "SHAPE — exactly this, and nothing else:\n"
    "  line 1: the headline, reproduced as given.\n"
    "  a blank line.\n"
    "  two to four paragraphs, one to three sentences each, a blank line "
    "between them. The first is the news itself: who, what, where, when. The "
    "ones after it carry the supporting facts — the names, the numbers, the "
    "dates, the official response, what happens next. A last paragraph may "
    "place the story in its standing context (\"The controversy comes as …\", "
    "\"The NTSB and FAA are investigating the cause of the crash.\").\n"
    "  a blank line.\n"
    "  last line: the hashtags.\n\n"
    "FAITHFUL — every fact, name, number, date and quote in the caption must "
    "come from the headline or from what your search actually returns. Invent "
    "nothing: no background you did not read, no consequence, no speculation, "
    "no plausible-sounding detail, no quote you cannot source. If the search "
    "returns little, write ONE short paragraph and stop — a thin caption is "
    "correct, a padded one is a lie on a news account. Never give a death "
    "toll, a casualty count, a suspect's name or a cause as settled when your "
    "sources disagree: attribute it (\"police said\") or leave it out.\n"
    "REGISTER — neutral wire-service prose, the way Reuters or AP writes. No "
    "hype, no editorialising, no adjective doing an opinion's work, no "
    "exclamation marks, no emoji, no rhetorical questions, no first person, "
    "no \"Breaking:\", no call to action, no \"follow us for more\", no links, "
    "no sign-off.\n"
    "HASHTAGS — one line, EXACTLY EIGHT, lowercase, space-separated, letters "
    "and digits only inside each tag. Each tag is a plain ASCII \"#\" with the "
    "word immediately after it — \"#madrid\", never a keycap emoji, never a "
    "space after the hash, never a comma between tags. That line is a POOL: the accounts "
    "publishing this story each draw a few tags from it, so eight genuinely "
    "relevant ones are wanted here. If the story cannot carry eight, give "
    "fewer — a tag that is not about this story is worse than a short line.\n"
    "  Pick them RELEVANCE FIRST. Every tag must be something this particular "
    "story is actually about — the place it happened, the institution or "
    "public figure at its centre, its subject, its topic. A tag a reader "
    "could not connect to the caption above it does not go in, however big "
    "that tag is.\n"
    "  Among the tags that pass that test, prefer the ones people actually "
    "search and follow — the short, established, high-traffic form over the "
    "long specific one nobody types (#ohio, not #ohiocourtsystem; #crime, not "
    "#attemptedmurdercase). Never reach for size alone: a popular tag that is "
    "not about this story is worse than one tag fewer.\n"
    "  Order them most specific first — place, then the main actors or "
    "subject, then the topic, then the general ones (#news, #breakingnews) "
    "last; the first two are the tags every account keeps, so they must be "
    "the two this story is most about. Tag places, institutions, countries "
    "and public events — never a private individual's name.\n\n"
    "Output ONLY the caption. No preamble, no explanation, no markdown, no "
    "bold, no bullet points, no surrounding quotation marks, no numbered "
    "citation markers, no source list at the end."
)

_VARIANT_INTRO = (
    "You are the editor of a news account. Another account in the same group "
    "has already published the caption below, word for word, for the same "
    "story. Rewrite it as YOUR account's caption, so that a reader who sees "
    "both does not see the same post twice.\n\n"
)

# Only for an account with no voice of its own. An account that HAS one is
# differentiated by that, and adding an angle on top makes the two fight: a
# house style that says "two paragraphs, never more" and an angle that says
# "write it as four short paragraphs" cancel out, and what comes back is in
# neither voice. Live proof on 2026-09-11, which is why this is conditional.
_VARIANT_ANGLE = "HOW YOURS MUST DIFFER: {angle}\n\n"

_VARIANT_RULES = (
    "SAME FACTS, NOTHING ADDED — every name, number, date, place, quote and "
    "attribution in your version comes from the caption you were given, "
    "unchanged. You have not looked this story up and you know nothing the "
    "caption does not say: no background, no consequence, no speculation, no "
    "detail that merely sounds plausible. Leaving a secondary fact out to make "
    "the rewrite work is fine; putting a new one in is not.\n"
    "A REWRITE, NOT A PARAPHRASE — a different opening sentence, a different "
    "order of facts, different sentence lengths. Trading words for synonyms is "
    "not enough: the two captions must not line up sentence for sentence.\n"
    "SHAPE — line 1 is the headline, rewritten as a DIFFERENT phrasing of the "
    "same headline: same facts, same meaning, other words. Then a blank line, "
    "then the paragraphs with a blank line between them, then a blank line and "
    "the hashtag line. THE FIRST PARAGRAPH STILL CARRIES THE NEWS — who, what, "
    "where, when — however short or unconventional your house style is. A "
    "caption that opens on a detail and leaves the reader to infer the story "
    "from the headline is a failed caption, not a terse one.\n"
    "HASHTAGS — the last line is copied through character for character: the "
    "same tags in the same order, none added, none dropped, none translated. "
    "They are chosen elsewhere.\n"
    "REGISTER — neutral wire-service prose, the way Reuters or AP writes. No "
    "hype, no editorialising, no exclamation marks, no emoji, no rhetorical "
    "questions, no first person, no \"Breaking:\", no call to action, no "
    "links, no sign-off.\n\n"
    "Output ONLY the caption. No preamble, no explanation, no markdown, no "
    "surrounding quotation marks, and no note about what you changed."
)

# Appended when the account has a voice of its own — `writing_style` in
# brands/<name>/style.json (shared/branding.load_writing_style). The angle says
# how this post differs from the one next door; the STYLE says how this account
# always writes, and it is the same string on every post it publishes, which is
# what makes an account recognisable rather than merely different.
#
# It goes in AFTER the register rule and overrides it on matters of form only:
# FAITHFUL is above both, because a voice changes how a fact is told and never
# which facts there are.
_VARIANT_STYLE = (
    "\n\nHOUSE STYLE — this is how your account always writes, and it "
    "outranks the neutral register above wherever the two disagree on FORM. "
    "It never outranks SAME FACTS, NOTHING ADDED: a voice changes how a fact "
    "is told, never which facts exist, and an adjective the source does not "
    "support is an invention whatever the house style is.\n"
    "Any example inside the house style illustrates FORM ONLY. Never copy an "
    "example's words, names, places, times or closing lines into the caption "
    "— a style that shows you \"The investigation continues.\" is showing you "
    "the shape of a closing line, and putting that sentence on a story with no "
    "investigation in it is a fabrication. Every example you follow gets "
    "refilled with THIS story's facts.\n{style}"
)

# Appended when the account publishes in another language: for it, the variant
# IS the translation, so the rewrite and the translate are one call rather than
# two. The hashtag rule is the translator's own, and for the same reason —
# tags are only worth anything if they are the same tags everywhere.
_VARIANT_LANG = (
    "\n\nLANGUAGE — write your caption in {lang}, and in {lang} only, as a "
    "native {lang} newsroom would write it rather than as a translation of the "
    "English. The hashtag line is the exception: it stays exactly as it is, "
    "character for character, untranslated."
)

# A model that searched will sometimes bracket its sources inline despite the
# prompt ("… five people were killed [2].") — strip the markers rather than
# lose an otherwise good caption to them.
_CITATION = re.compile(r"[ \t]*\[\d{1,3}\](?=[\s.,;:!?)]|$)")

# What ONE account puts under ONE post. Five is a ceiling, not a target.
MAX_HASHTAGS = 5

# What `expand` asks the model for, and what `pick_hashtags` deals five out of.
# A pool is the cheapest de-duplication there is: eight tags, two kept by every
# account and three rotated out of the remaining six, gives six accounts six
# different tag lines for no tokens at all. Bigger buys little — past eight the
# model is reaching for tags the story is not about, which is the one thing
# RELEVANCE FIRST in the prompt is there to stop.
POOL_HASHTAGS = 8

# The head of the pool every account keeps. The prompt orders tags
# most-specific-first, so these two are what the story is actually about —
# rotating them away to manufacture a difference would cost more reach than the
# duplicate text ever did.
KEEP_HASHTAGS = 2

# Stride through the tag combinations (see `pick_hashtags`). Any number
# coprime with the number of combinations walks all of them; a prime is the
# cheapest way to be coprime with most list lengths.
_COMBO_STEP = 7

# The rewrite directives `plan` deals out, one per account after the first of
# its language. Each forces a DIFFERENT STRUCTURE rather than a synonym pass:
# two captions that open on the same sentence and run the same facts in the
# same order still read as the same post, however the adjectives differ. And a
# structural instruction is one a model follows without a temperature — which
# cannot be sent here anyway (several current OpenAI models on OpenRouter
# reject a non-default one; see the module docstring).
ANGLES = (
    "Open on WHERE it happened, then who and what.",
    "Open on the official or institutional response, then the event itself.",
    "Open on the number at the centre of the story — the toll, the sum, the "
    "count — then how it came about.",
    "Open on what happens next (the investigation, the vote, the hearing), "
    "then work back to what caused it.",
    "Write it TIGHT: two paragraphs, no more, the hardest facts only.",
    "Write it as FOUR short paragraphs, roughly one fact each.",
    "Open on the person or the body at the centre of the story, then the "
    "event.",
    "Open on WHEN it happened and how it unfolded, then the consequences.",
)

# A line that is nothing but hashtags — the caption's last line, by the shape
# the prompt asks for. Anything else (a paragraph that happens to mention a
# tag) is left alone.
_TAG_LINE = re.compile(r"^#[^\s#]+(?:\s+#[^\s#]+)*$")

# ``` / ```text fences around the whole answer.
_FENCE = re.compile(r"^```[a-zA-Z]*\n(.*)\n```$", re.DOTALL)

# A "Caption:"-style label on its own first line.
_LABEL = re.compile(r"^(caption|instagram caption|post)\s*:\s*\n+", re.IGNORECASE)


def _tag_line_index(lines: list) -> int:
    """Index of the caption's hashtag line, or -1 when it has none.

    The LAST non-empty line is the only candidate, and only when it is nothing
    but hashtags — a closing paragraph that happens to name one must not be
    chopped."""
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if not stripped:
            continue
        return i if _TAG_LINE.match(stripped) else -1
    return -1


def _cap_hashtags(text: str, limit: int = POOL_HASHTAGS) -> str:
    """Cut the caption's trailing hashtag line down to `limit` tags.

    The ceiling here is the POOL, not the post: `pick_hashtags` deals each
    account its five out of what survives. A model that ignores the count
    entirely must still not hand a twenty-tag line down the chain."""
    lines = text.split("\n")
    i = _tag_line_index(lines)
    if i < 0:
        return text
    tags = lines[i].strip().split()
    if len(tags) > limit:
        lines[i] = " ".join(tags[:limit])
    return "\n".join(lines)


def pick_hashtags(text: str, offset: int, limit: int = MAX_HASHTAGS,
                  keep: int = KEEP_HASHTAGS) -> str:
    """One account's share of the caption's hashtag pool.

    The first `keep` tags are what the story is about, and every account keeps
    them (the prompt orders the pool most-specific-first); the rest of the line
    is a window of `limit - keep` tags rotated `offset` places into the tail, so
    consecutive offsets give different tag lines. Pure, deterministic and free —
    the cheap half of not looking like the same post on six accounts.

    Returns `text` untouched when there is no hashtag line, or when the line is
    already within `limit`: dealing five out of five could only drop a relevant
    tag, which costs more reach than the duplicate line does.
    """
    lines = text.split("\n")
    i = _tag_line_index(lines)
    if i < 0:
        return text
    tags = lines[i].strip().split()
    if len(tags) <= limit:
        return text
    head, tail = tags[:keep], tags[keep:]
    # Every COMBINATION of the tail, not a rotating window: a window of three
    # over a tail of six wraps after six accounts, and there are thirteen
    # brands. C(6,3) is twenty distinct lines, none of them repeating a tag.
    combos = list(itertools.combinations(tail, limit - len(head)))
    # Neighbouring offsets are neighbouring accounts on the same post, and
    # lexicographic neighbours share two tags out of three — step through the
    # list by a coprime stride instead, which visits every combination exactly
    # once but puts consecutive accounts far apart in it.
    step = _COMBO_STEP if math.gcd(_COMBO_STEP, len(combos)) == 1 else 1
    lines[i] = " ".join(head + list(combos[int(offset) * step % len(combos)]))
    return "\n".join(lines)


# What a model returns when it means "#madrid" and gets it wrong. Seen live on
# 2026-09-11: "#\uFE0F\u20E3 madrid", the KEYCAP HASH emoji plus a space, which
# is not a hashtag on Instagram and — worse — does not match _TAG_LINE, so the
# tag line silently escaped both the pool cap and the per-account pick and every
# account published the same eight dead tags. A malformed tag line has to be
# repaired here, not passed down.
_TAG_NOISE = str.maketrans({"\uFE0F": "", "\u20E3": "", ",": " ", ";": " "})
_HASH_GAP = re.compile(r"#[ \t]+(?=[^\s#])")


def _normalise_tag_line(text: str) -> str:
    """Repair the caption's last line when a model mangles the hashtags.

    Only the last non-empty line is touched, and only when the repair turns it
    into a clean tag line — prose that merely contains a "#" is left exactly as
    it was, which is the same rule every other hashtag helper here follows.
    """
    lines = text.split("\n")
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if not stripped:
            continue
        if "#" not in stripped:
            return text
        fixed = " ".join(stripped.translate(_TAG_NOISE).split())
        fixed = _HASH_GAP.sub("#", fixed)
        if fixed != stripped and _TAG_LINE.match(fixed):
            lines[i] = fixed
            return "\n".join(lines)
        return text
    return text


def _clean(text: str) -> str:
    """Undo the wrappers a chat model reaches for when told not to."""
    out = (text or "").strip()
    fenced = _FENCE.match(out)
    if fenced:
        out = fenced.group(1).strip()
    out = _LABEL.sub("", out).strip()
    out = _CITATION.sub("", out)
    if len(out) > 1 and out[0] == '"' and out[-1] == '"':
        out = out[1:-1].strip()
    # Collapse the runs of blank lines a model leaves between paragraphs.
    # Trailing spaces first: a "blank" line the model left a space on is not
    # blank to the regex, and it survived as an empty first paragraph on a
    # live post (2026-09-11).
    out = "\n".join(line.rstrip() for line in out.split("\n"))
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = _normalise_tag_line(out)
    out = _cap_hashtags(out)
    return out.strip()


def expand(headline: str, model: str | None = None) -> str:
    """Expand `headline` into a full Instagram caption with hashtags.

    Returns `headline` unchanged when expansion isn't wanted or possible:
      - empty headline,
      - IG_CAPTION_ENABLED is off,
      - the API key is missing, the call fails, or it comes back empty.
    Never raises — see the module docstring. Blocking; async callers go
    through asyncio.to_thread.
    """
    headline = (headline or "").strip()
    if not headline:
        return headline
    if not config.IG_CAPTION_ENABLED:
        return headline
    if _client is None:
        log.warning("OPENROUTER_API_KEY not set — posting the bare headline "
                    "as the Instagram caption")
        return headline

    try:
        resp = _client.chat.completions.create(
            model=model or config.IG_CAPTION_MODEL,
            max_tokens=MAX_TOKENS,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": headline},
            ],
        )
    # Broader than the translator's `except APIError`: a gateway fails outside
    # it too (DNS, TLS, a 502 HTML body), and this function's whole job is that
    # none of that reaches the publish loop.
    except Exception as exc:
        log.error("instagram caption for %r failed: %s — posting the bare "
                  "headline", headline[:80], exc)
        return headline

    out = _clean(resp.choices[0].message.content or "")
    if not out:
        log.warning("instagram caption for %r came back empty — posting the "
                    "bare headline", headline[:80])
        return headline
    return trim_caption(out)


def seed_for(text: str) -> int:
    """A stable number to rotate this post's angles and hashtags by.

    Not `hash()`: Python salts string hashing per process, so a restart between
    the render and the publish would re-plan the same post differently. crc32
    is the same number on every machine and every run.
    """
    return zlib.crc32((text or "").encode("utf-8"))


def angle_for(index: int) -> str:
    """The rewrite directive at `index`, wrapping round ANGLES."""
    return ANGLES[int(index) % len(ANGLES)]


def plan(brands: list, seed: int = 0) -> list:
    """Who rewrites, how, and with which slice of the hashtag pool.

    `brands` is the Instagram brands of one publish, in a stable order (config
    loads them alphabetically). Returns one entry per brand — `name`, `lang`,
    `angle` and `offset` — in the same order.

    ONE VOICELESS BRAND PER LANGUAGE GETS NO ANGLE: it publishes the shared
    caption, whose call is already paid for, and nothing it could be rewritten
    away from has been published yet. Every other brand in that language gets
    its own angle, so no two accounts run the same words. `offset` is always
    distinct, so their hashtag lines differ even when the prose does not (a
    brand whose rewrite call fails falls back to the shared caption).

    A BRAND WITH A `style` IS NEVER THAT ONE, AND GETS NO ANGLE. Its voice is
    the whole point of having one, and the shared caption is written in no
    account's voice — so a styled brand always gets its own call, and the free
    ride passes to the first brand of that language with no style at all. With
    every brand styled (which is the case for the JNN accounts on Instagram)
    nobody takes it and the shared caption is only ever a source text.

    The angle is NOT stacked on top of a style: the two are both structural
    instructions and they fight — a voice that says "two paragraphs, never
    more" against an angle that says "write it as four short paragraphs" comes
    back in neither. So an entry carries an angle OR a style, and `rephrase`
    is called for either.

    `seed` rotates the whole deal per post (`seed_for`), so an account does not
    open every post it publishes the same way. Pure and offline-testable.
    """
    out, free = [], set()
    for i, brand in enumerate(brands):
        lang = (brand.get("lang") or "").strip()
        style = (brand.get("style") or "").strip()
        # The free ride is per language, and only a voiceless brand can take it.
        shared = not style and lang not in free
        if shared:
            free.add(lang)
        out.append({
            "name": brand.get("name", ""),
            "lang": lang,
            "style": style,
            # A voice differentiates on its own; an angle is what a
            # voiceless account is given instead.
            "angle": None if (shared or style) else angle_for(seed + i),
            "offset": seed + i,
        })
    return out


def rephrase(text: str, angle: str, lang: str = "", style: str = "",
             model: str | None = None) -> str:
    """One account's variant of a caption another account is publishing.

    Takes an `angle` OR a `style`, never both (see `plan`): `style` is the
    account's permanent voice (`writing_style` in brands/<name>/style.json),
    the same on every post it publishes and what makes it recognisable;
    `angle` is a per-post structural directive out of ANGLES, which is what a
    voiceless account gets instead so it at least differs from its neighbour.
    `lang`, when given, also translates, which is what keeps a
    foreign-language account at one call instead of two. No search: the facts
    are already in `text`, and a second search would double the only real bill
    this feature has.

    Returns `text` unchanged when a variant isn't wanted or possible — neither
    angle nor style, no text, the kill switch off, no API key, a failed or
    empty call.
    Never raises, for the same reason `expand` doesn't: the accounts sharing
    one caption is a smaller problem than an account posting none. Blocking;
    async callers go through asyncio.to_thread.
    """
    text = (text or "").strip()
    angle, style = (angle or "").strip(), (style or "").strip()
    if not text or not (angle or style):
        return text
    if not config.IG_CAPTION_ENABLED:
        return text
    if _client is None:
        log.warning("OPENROUTER_API_KEY not set — accounts share the caption")
        return text

    system = _VARIANT_INTRO
    if angle:
        system += _VARIANT_ANGLE.format(angle=angle)
    system += _VARIANT_RULES
    if style:
        system += _VARIANT_STYLE.format(style=style)
    if (lang or "").strip():
        system += _VARIANT_LANG.format(lang=lang.strip())

    try:
        resp = _client.chat.completions.create(
            model=model or config.IG_CAPTION_VARIANT_MODEL,
            max_tokens=MAX_TOKENS,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
        )
    except Exception as exc:
        log.error("instagram caption variant (%s) failed: %s — posting the "
                  "shared caption", (angle or style)[:40], exc)
        return text

    out = _clean(resp.choices[0].message.content or "")
    if not out:
        log.warning("instagram caption variant (%s) came back empty — posting "
                    "the shared caption", (angle or style)[:40])
        return text
    return trim_caption(out)


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description="Expand a headline into an Instagram caption.")
    ap.add_argument("headline", nargs="+", help="the headline to expand")
    ap.add_argument("--model", default=None,
                    help=f"override IG_CAPTION_MODEL ({config.IG_CAPTION_MODEL})")
    ap.add_argument("--accounts", type=int, default=1, metavar="N",
                    help="print the caption as N accounts would publish it — "
                         "one search, then N-1 rewrites (default 1)")
    ap.add_argument("--lang", default="", metavar="CODE",
                    help="language for the rewritten accounts (default: the "
                         "source language)")
    ap.add_argument("--brands", default="", metavar="A,B,C",
                    help="real brand names instead of --accounts, each writing "
                         "in its own brands/<name>/style.json voice")
    args = ap.parse_args()

    headline = " ".join(args.headline)
    shared = expand(headline, model=args.model)
    if args.brands:
        from shared import branding
        fake = [{"name": n, "lang": args.lang,
                 "style": branding.load_writing_style(os.path.join(
                     _ROOT, "brands", n))}
                for n in args.brands.split(",") if n.strip()]
    else:
        fake = [{"name": f"account{i + 1}", "lang": args.lang if i else ""}
                for i in range(max(1, args.accounts))]
    for entry in plan(fake, seed_for(headline)):
        text = (rephrase(shared, entry["angle"], entry["lang"], entry["style"])
                if (entry["angle"] or entry["style"]) else shared)
        if len(fake) > 1:
            print(f"--- {entry['name']}: "
                  f"{'its own voice' if entry['style'] else entry['angle'] or 'the shared caption'}"
                  f" ---")
        print(pick_hashtags(text, entry["offset"]))
        print()
