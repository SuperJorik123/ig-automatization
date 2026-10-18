"""
modules/instagram/caption.py — the Instagram caption: a hook, the context the
clip doesn't show, a question, hashtags.

REDESIGNED 2026-10-18. The caption used to retell the clip in neutral
wire-service prose, written from a vision model's narration of it, and the
story got lost: Gemini gets actions wrong (a woman bumping into a man came
back as a hug) and the prompt let that narration override the operator's
headline — on posts/005.mp4 "teenager saves friend from electrocution"
became "two players fell over". And neutral narration is the wrong product
for Reels anyway: the viewer has just watched the clip. What grows the page
is the context the clip doesn't show plus a reason to comment, send or follow.

FACTS COME FROM PEOPLE, NOT FROM THE MODEL. In order of trust:
  1. the operator's `info:` reply — a person who checked the story;
  2. the operator's headline — the story;
  3. the SOURCE POST the clip came from (the tweet, the Reddit title and body,
     the Instagram caption — shared/reel_downloader.source_post);
  4. MEDIA NOTES from shared/vision — on-screen text, speech, language,
     setting. Never what anybody does.
Higher sources win; lower ones only add facts that don't conflict. No web
search (owner's decision, 2026-10-12): `searchless` strips `:online` from
whatever model id is configured.

THE SHAPE (`_SYSTEM`): a hook line (≤90 chars, keywords first — Instagram
search ranks caption text), one or two sentences of context, one question
about this story, then a line of EIGHT hashtag candidates, most relevant first.
No calls to action at all — Instagram demotes engagement bait, and the question
is the only prompt for a reaction. `compose` puts the hook on line 1 (the
brand's headline only when the model skipped it) and aims at 150-400
characters before the hashtags.

N ACCOUNTS, ONE CAPTION'S FACTS. `expand` is one call; every other account
gets a cheap searchless `rephrase` in its own TONE — `writing_style` in
brands/<group>/<name>/style.json, which governs tone only, never the shape —
with a different hook, different context wording and a different question. A
non-English account's rewrite is written wholly in its language and brings
its own eight hashtag candidates, the tags its readers actually use, instead
of the English pool.

HASHTAGS ARE MEASURED ONCE. `choose_hashtags` takes every account's candidates
in ONE Apify pass (modules/instagram/hashtags.py) and keeps up to four per
account: the 5k-200k ones in the model's relevance order, topped up to three
from the most relevant remaining candidates when the band is thin; tags with
no posts or under ~500 never. Apify down → the top four, unmeasured. The
account's own tag (`brand_tag`) always opens the line on top of those.

Two contracts, both from modules/telegram/translator.py, because they are what
make a model safe to put in a publishing path:

  NEVER RAISES — a missing key, a dead gateway, an unknown model id, an empty
  completion all return an empty body, and the post goes out as the headline
  with the account's tag. The render is made and publish is tapped; a caption
  hiccup must cost the post its caption, not its existence.

  FAITHFUL — no name, place, date, number or outcome the four sources don't
  give. On a news account an invented detail costs more than a short caption.

`temperature` is not sent — several current OpenAI models on OpenRouter reject
a non-default one, which would surface as a post that silently lost its
caption. `max_tokens` IS sent, generously (MAX_TOKENS): OpenRouter reserves
credit for a request's worst case up front, and an uncapped call demanded a
balance covering the model's full 65,536-token ceiling — that 402'd a live
caption at a $1.42 balance on 2026-09-09.

CLI, for tuning the prompts against real headlines (`--brands` prints the post
as those brands would publish it, each in its own tone):
    py modules/instagram/caption.py "Teenager saves friend from electrocution"
    py modules/instagram/caption.py --media clip.mp4 --info "…" --brands wswire,vestra24 "…"
"""

import itertools
import logging
import math
import os
import re
import sys
import zlib
from concurrent.futures import ThreadPoolExecutor

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from openai import OpenAI  # noqa: E402

from shared import config, vision  # noqa: E402
from modules.instagram import hashtags  # noqa: E402
from modules.instagram.graph import CAPTION_MAX, trim_caption  # noqa: E402

log = logging.getLogger(__name__)

_client = (
    OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=config.OPENROUTER_API_KEY,
    )
    if config.OPENROUTER_API_KEY
    else None
)

# Reasoning tokens plus a caption of a few hundred characters. Wide enough that
# the model is never the one that stops (see the module docstring), narrow
# enough that OpenRouter's up-front credit reservation stays under a cent.
MAX_TOKENS = 8000

# The main caption prompt — the operator's spec of 2026-10-18, word for word,
# plus the LANGUAGE line (the base caption is the English one every rewrite is
# made from).
_SYSTEM = (
    "You write Instagram Reels captions for a news page. The viewer has just "
    "watched the clip, or is watching it as they read. A caption that retells "
    "the clip is wasted. Your caption adds what the clip does not show and "
    "gives the viewer a reason to comment, send it to someone, or follow the "
    "page.\n\n"
    "SOURCES, IN ORDER OF TRUST\n"
    "  1. OPERATOR'S INFO: written by a person who checked the story. Source "
    "of truth.\n"
    "  2. HEADLINE: the story. Its facts (what happened, the cause, who did "
    "what, the outcome) go into the caption.\n"
    "  3. SOURCE POST: the original post the clip came from. Use it for extra "
    "facts (place, date, what happened before or after) that do not conflict "
    "with 1 or 2.\n"
    "  4. MEDIA NOTES: on-screen text, speech, setting. Use only to add "
    "concrete detail.\n"
    "You have no web access. Invent nothing: no names, places, dates, "
    "numbers, ages or outcomes that the sources do not give. If no place is "
    "given, do not state one. Ignore opinions, insults and propaganda framing "
    "in the source post; take only facts from it.\n\n"
    "SHAPE, exactly:\n\n"
    "LINE 1, THE HOOK. At most 90 characters. States the story's point with "
    "its main keywords near the start, because Instagram search ranks these "
    "words: what happened plus the key subject. It carries the surprise or "
    "the stakes. It tells the reader what happened; it never withholds it. "
    "No question, no \"Breaking\", no hashtag.\n\n"
    "(blank line)\n\n"
    "CONTEXT. One or two short sentences, at most 220 characters in total. "
    "Add what the clip does not make obvious: the cause, the outcome, what "
    "happened afterwards, why it matters. If the sources give nothing beyond "
    "the hook, write ONE sentence pointing the viewer to the key moment. "
    "Never a full retelling.\n\n"
    "(blank line)\n\n"
    "QUESTION. One short question about THIS story that most viewers can "
    "answer in a few words, and that people will disagree on or relate to: "
    "\"Would you have gone in?\", \"Who's at fault here?\", \"Hero or "
    "reckless?\". Never \"What do you think?\" or \"Thoughts?\". On stories "
    "with deaths or serious injury, replace it with a sober line, or leave it "
    "out. That means someone died or was left badly, lastingly hurt: a "
    "rescue, a near miss or a shock everyone survived still gets a question. "
    "The question asks for the viewer's OPINION or EXPERIENCE, never for a "
    "fact: no question the caption or the clip already answers (\"Who did he "
    "rescue?\", \"How large was the crowd?\", \"When did it happen?\").\n\n"
    "(blank line)\n\n"
    "LAST LINE, HASHTAG CANDIDATES. Eight candidates, ordered MOST RELEVANT "
    "FIRST, lowercase, plain ASCII \"#\", letters and digits only. Every tag "
    "names THIS story: the kind of event, the action, the subject, the place "
    "when one is given. At most one generic browse tag (#caughtoncamera). No "
    "misspellings, nothing unrelated, never a compound coined for this one "
    "event. Do not add the brand tag. Their post counts are measured "
    "afterwards and the published tags are chosen from them, so aim for tags "
    "with roughly 5,000 to 200,000 posts: a single common word (#rescue, "
    "#football) is usually far bigger; a precise two-word topic "
    "(#electricshock, #futsalnews, #scamalert) usually lands in range.\n\n"
    "NO CALLS TO ACTION. Never ask the reader to send, share, save, like, "
    "follow or comment. Instagram demotes explicit engagement bait; the "
    "question is the only prompt for a reaction.\n\n"
    "TONE. Plain and conversational, like a sharp news page talking to "
    "people, not a wire service. Short sentences; contractions are fine. "
    "Past tense. You may name the emotion the event obviously carries, but "
    "never exaggerate a fact. No first person, no \"follow for more\", no "
    "links.\n\n"
    "NEVER\n"
    "  - refer to people by clothing colour: use roles (the driver, his "
    "friend, a bystander);\n"
    "  - mention race, ethnicity or religion unless the operator's info makes "
    "it the news;\n"
    "  - write \"the video shows\", \"footage\", \"is seen\", \"the clip "
    "ends\";\n"
    "  - take a side on politics or on a group of people.\n\n"
    "SENSITIVE STORIES (death, violence, suicide or self-harm): neutral "
    "wording, no playful question, no jokes, no method details.\n\n"
    "LANGUAGE. Write the caption in English, whatever language the sources "
    "are in.\n\n"
    "EXAMPLES. Follow their form, never their content.\n\n"
    "Headline: Teenager saves friend from electrocution despite suffering a "
    "severe shock himself\n\n"
    "Teen saves friend from electrocution on football pitch\n\n"
    "His friend collapsed by the wall near the goal. He ran in, got shocked "
    "himself, and still dragged him clear.\n\n"
    "Would you have known what to do?\n\n"
    "#electrocution #rescue #futsal\n\n"
    "Headline: Police stop pensioner from sending life savings to fake bank "
    "caller\n\n"
    "Fake bank call nearly cost a pensioner her life savings\n\n"
    "She was on the phone with scammers posing as her bank when officers "
    "arrived and stopped the transfer.\n\n"
    "Has anyone in your family had a call like this?\n\n"
    "#scamalert #bankscam #fraudawareness\n\n"
    "Output ONLY the caption. No preamble, no markdown, no quotation marks "
    "around it."
)

# The source post's text can be a long Reddit thread excerpt; the caption
# needs its facts, not its length.
SOURCE_TEXT_MAX = 1500

# How a source platform is named in the prompt and in a pinned comment's
# credit.
PLATFORM_NAMES = {"x": "X", "reddit": "Reddit", "instagram": "Instagram",
                  "telegram": "Telegram"}


def user_message(headline: str, info: str = "", footage: dict | None = None,
                 source: dict | None = None) -> str:
    """The four sources, in trust order, in the operator's spec format:

        OPERATOR'S INFO: <info reply or "(none)">
        HEADLINE: <headline>
        SOURCE POST (<platform>): <source_text or "(none)">
        MEDIA NOTES: on-screen text: ...; speech: ...; ...
    """
    source = source or {}
    platform = PLATFORM_NAMES.get(source.get("platform") or "", "")
    text = (source.get("text") or "").strip()
    if len(text) > SOURCE_TEXT_MAX:
        text = text[:SOURCE_TEXT_MAX].rstrip() + "…"
    notes = vision.as_prompt(footage or {}) or "MEDIA NOTES: (none)"
    label = f"SOURCE POST ({platform})" if platform else "SOURCE POST"
    return "\n".join([
        f"OPERATOR'S INFO: {(info or '').strip() or '(none)'}",
        f"HEADLINE: {(headline or '').strip()}",
        f"{label}: {text or '(none)'}",
        notes,
    ])


# The per-account rewrite. Same shape, same facts, a different hook, context
# wording and question; the house style is TONE only — an old style that said
# "three paragraphs" or "lead on a figure" would rebuild the wire-service
# caption this redesign removed, which is why every style.json was rewritten
# as a tone line on 2026-10-18 and why the prompt says so outright.
_VARIANT_SYSTEM = (
    "You are the editor of a news page on Instagram. Another page in the same "
    "group is publishing the caption below for the same clip. Write YOUR "
    "page's caption for it, so a viewer who sees both does not see the same "
    "post twice.\n\n"
    "SAME FACTS, NOTHING ADDED. Every name, number, place, date and outcome "
    "comes from the caption you were given. You have no web access and know "
    "nothing the caption does not say. Leaving a detail out is fine; putting "
    "a new one in is not.\n\n"
    "SAME SHAPE, exactly:\n"
    "  LINE 1, THE HOOK: at most 90 characters, the same main keywords near "
    "the start, states what happened. No question, no \"Breaking\", no "
    "hashtag.\n"
    "  (blank line)\n"
    "  CONTEXT: one or two short sentences, at most 220 characters.\n"
    "  (blank line)\n"
    "  QUESTION: one short question about this story that most viewers can "
    "answer in a few words. Never \"What do you think?\" or \"Thoughts?\". "
    "Only a story with a death or a serious lasting injury gets a sober line "
    "instead, or nothing; a rescue or a near miss everyone survived gets a "
    "question. It asks for the viewer's OPINION or EXPERIENCE, never for a "
    "fact the caption or the clip already answers (\"Who did he rescue?\", "
    "\"How large was the crowd?\" are failures). Never a line that repeats "
    "the context.\n"
    "{tags_shape}\n"
    "DIFFERENT FROM THE ONE YOU WERE GIVEN: a different hook (same keywords, "
    "different wording and order), different wording in the context, and a "
    "different question. Synonym swaps are not enough.\n\n"
    "NO CALLS TO ACTION. Never ask the reader to send, share, save, like, "
    "follow or comment; the question is the only prompt for a reaction.\n\n"
    "NEVER refer to people by clothing colour, mention race, ethnicity or "
    "religion, write \"the video shows\", \"footage\", \"is seen\" or \"the "
    "clip ends\", or take a side on politics or on a group of people. On "
    "deaths, violence, suicide or self-harm: neutral wording, no playful "
    "question, no jokes.\n"
    "{style}{lang}\n\n"
    "Output ONLY the caption. No preamble, no markdown, no quotation marks "
    "around it, no note about what you changed."
)

# Appended on the second pass of `rewrite_all`, for a page whose first rewrite
# came back too close to another page's: the hooks and questions already used.
_VARIANT_AVOID = (
    "\nALREADY TAKEN by other pages in the group — your hook and your question "
    "must not resemble any of these (different opening words, different "
    "word order, a different question altogether):\n{taken}")

_VARIANT_NO_TAGS = (
    "  Nothing after the question: no hashtags, they are chosen elsewhere.\n")

_VARIANT_TAGS = (
    "  (blank line)\n"
    "  LAST LINE, HASHTAG CANDIDATES: eight tags that {lang_name} speakers "
    "actually use for this kind of story, ordered MOST RELEVANT FIRST, "
    "lowercase, \"#\" followed by letters and digits only. Every tag names "
    "THIS story: the kind of event, the action, the subject, the place when "
    "one is given. At most one English tag, and only for an international "
    "story. No misspellings, never a compound coined for this one event, no "
    "account's own tag. Aim for tags with roughly 5,000 to 200,000 posts.\n")

_VARIANT_STYLE = (
    "\nHOUSE TONE — how your page always sounds: {style}\n"
    "Tone only. It never changes the shape above, the lengths, or the facts; "
    "where it states a preference for a kind of question, follow it when it "
    "fits the story, and otherwise ask any question specific to the story.")

_VARIANT_LANG = (
    "\nLANGUAGE — write the whole caption in {lang_name}, the hook included, "
    "as a native {lang_name} news page would write it, not as a translation.")

# The languages the brands publish in, by the code in credentials/brands/*.json.
# An unknown code is passed through as it is — models know "ro" or "de" too.
LANG_NAMES = {"en": "English", "ru": "Russian", "ro": "Romanian",
              "uk": "Ukrainian", "de": "German", "fr": "French",
              "es": "Spanish", "it": "Italian", "pl": "Polish",
              "tr": "Turkish"}

# The language `expand` writes in. A brand in one of these rewrites in
# English and draws its tags from the base caption's candidates; any other
# language writes its own.
BASE_LANGS = ("", "en")


def lang_name(code: str) -> str:
    code = (code or "").strip()
    return LANG_NAMES.get(code.lower(), code)


def is_base_lang(code: str) -> bool:
    return (code or "").strip().lower() in BASE_LANGS

# A model will sometimes bracket citation markers inline despite the prompt
# ("… five people were killed [2].") — strip them rather than lose an
# otherwise good caption to them.
_CITATION = re.compile(r"[ \t]*\[\d{1,3}\](?=[\s.,;:!?)]|$)")

# What ONE account puts under ONE post: its own tag plus up to STORY_HASHTAGS
# story tags. A ceiling, not a target.
STORY_HASHTAGS = 4
MAX_HASHTAGS = STORY_HASHTAGS + 1

# When fewer than this many candidates are in the band, the most relevant
# remaining ones fill up to it (`hashtags.select`).
MIN_HASHTAGS = 3

# What `expand` (and a non-English rewrite) asks the model for: candidates,
# most relevant first, of which `choose_hashtags` keeps up to STORY_HASHTAGS.
CANDIDATE_HASHTAGS = 8

# The head of a tag line every account keeps when `pick_hashtags` has more
# tags than room. Since 2026-10-18 the line it gets is already the account's
# own selection (≤ STORY_HASHTAGS), so this only matters for a caller that
# hands it a longer one.
KEEP_HASHTAGS = 2

# Stride through the tag combinations (see `pick_hashtags`). Any number
# coprime with the number of combinations walks all of them; a prime is the
# cheapest way to be coprime with most list lengths.
_COMBO_STEP = 7

# Everything Instagram does not index inside a tag. A hashtag is letters and
# digits only — the dot in the handle "vestra.24" ends the tag at "#vestra",
# which is why the account's tag is built from the BRAND NAME (the directory
# under brands/, which is already the flat form: vestra24, frontiva24,
# dailynews) and not from `instagram.account` in its credentials file.
_TAG_CHARS = re.compile(r"[^a-z0-9]")

# The longest first paragraph still read as the model's HOOK. The prompt asks
# for at most 90 characters; the slack is for a model that runs a little over.
# A first paragraph past this, or on more than one line, is prose — the model
# skipped the hook — and the brand's headline goes on top instead.
HOOK_MAX = 120

# What a caption should weigh before its hashtags. Outside it is logged, not
# fixed: a short caption on a thin story is correct, and the hard cap is
# CAPTION_MAX.
TARGET_MIN, TARGET_MAX = 150, 400

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


def _unique_tags(tags, drop=()) -> list:
    """`tags` without repeats (case-insensitive, first spelling kept) and
    without anything in `drop` — which is lower-case tags."""
    seen, out = set(drop), []
    for t in tags:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out


def _cap_hashtags(text: str, limit: int = CANDIDATE_HASHTAGS) -> str:
    """De-duplicate the caption's trailing hashtag line and cut it to `limit`.

    The ceiling is the CANDIDATES, not the post: `choose_hashtags` picks each
    account's tags out of what survives. A model that ignores the count
    entirely must still not hand a twenty-tag line down the chain — and a
    repeated tag must go BEFORE the cut, or it spends a slot twice."""
    lines = text.split("\n")
    i = _tag_line_index(lines)
    if i < 0:
        return text
    lines[i] = " ".join(_unique_tags(lines[i].strip().split())[:limit])
    return "\n".join(lines)


def brand_tag(brand: str) -> str:
    """The account's OWN hashtag — "frontiva24" -> "#frontiva24".

    Built from the brand name, which is the directory under brands/ and the
    filename in credentials/brands/, not from the Instagram handle: a handle
    may carry a dot ("vestra.24", "daily.news.co") and Instagram ends a tag at
    the first character that is not a letter or a digit. Returns "" for a brand
    whose name has nothing taggable in it at all.
    """
    slug = _TAG_CHARS.sub("", (brand or "").strip().lower())
    return f"#{slug}" if slug else ""


def with_brand_tag(text: str, brand: str, limit: int = MAX_HASHTAGS) -> str:
    """`text` with this account's own tag FIRST on its hashtag line.

    MANDATORY, which is why it is a function of its own and not a branch inside
    `pick_hashtags`: every caption an account publishes carries the account's
    tag, including the ones that never went near the pool — the bare headline
    a failed expansion falls back to gets a tag line made for it.

    The tag always opens the line, it is never duplicated if the model already
    produced it (anywhere on the line), no other tag repeats either, and it is
    inside `limit`: a line already at the ceiling loses its LAST story tag,
    which is its least specific one, never its head.
    """
    tag = brand_tag(brand)
    if not tag or not (text or "").strip():
        return text
    lines = text.split("\n")
    i = _tag_line_index(lines)
    if i < 0:
        return text.rstrip() + "\n\n" + tag
    tags = _unique_tags(lines[i].split(), drop={tag})
    lines[i] = " ".join([tag] + tags[:max(limit - 1, 0)])
    return "\n".join(lines)


def pick_hashtags(text: str, offset: int, limit: int = MAX_HASHTAGS,
                  keep: int = KEEP_HASHTAGS, brand: str = "",
                  others=()) -> str:
    """One account's share of the caption's hashtag pool, plus its own tag.

    The first `keep` tags are what the story is about, and every account keeps
    them (the prompt orders the pool most-specific-first); the rest of the line
    is a combination drawn out of the tail by `offset`, so consecutive offsets
    give different tag lines. Pure, deterministic and free — the cheap half of
    not looking like the same post on six accounts.

    `brand` takes the FIRST of the `limit` slots for the account's own tag
    (`with_brand_tag`), so the story is dealt one tag fewer rather than the
    post carrying a sixth. Without a brand the line is exactly what it was.

    `others` is every brand name in the group: a sibling account's tag in the
    pool (the rewrite can pick one up from a style example) is dropped before
    dealing, so frontiva24 never signs a post #mirnews.

    Returns `text` with only the brand tag added when there is no hashtag line,
    or when the line already fits: dealing four out of four could only drop a
    relevant tag, which costs more reach than the duplicate line does.
    """
    tag = brand_tag(brand)
    room = limit - 1 if tag else limit
    lines = text.split("\n")
    i = _tag_line_index(lines)
    if i < 0:
        return with_brand_tag(text, brand, limit)
    drop = {tag} | {brand_tag(o) for o in others}
    drop.discard("")
    tags = _unique_tags(lines[i].strip().split(), drop=drop)
    if not tags:
        lines[i] = ""
        return with_brand_tag("\n".join(lines).rstrip(), brand, limit)
    lines[i] = " ".join(tags)
    if len(tags) <= room:
        return with_brand_tag("\n".join(lines), brand, limit)
    if room <= keep:
        lines[i] = " ".join(tags[:max(room, 0)])
        return with_brand_tag("\n".join(lines), brand, limit)
    head, tail = tags[:keep], tags[keep:]
    # Every COMBINATION of the tail, not a rotating window: a window of three
    # over a tail of six wraps after six accounts, and there are thirteen
    # brands. C(6,2) is fifteen distinct lines, none of them repeating a tag.
    combos = list(itertools.combinations(tail, room - len(head)))
    # Neighbouring offsets are neighbouring accounts on the same post, and
    # lexicographic neighbours share two tags out of three — step through the
    # list by a coprime stride instead, which visits every combination exactly
    # once but puts consecutive accounts far apart in it.
    step = _COMBO_STEP if math.gcd(_COMBO_STEP, len(combos)) == 1 else 1
    lines[i] = " ".join(head + list(combos[int(offset) * step % len(combos)]))
    return with_brand_tag("\n".join(lines), brand, limit)


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


def _clean(text: str, tag_limit: int = CANDIDATE_HASHTAGS) -> str:
    """Undo the wrappers a chat model reaches for when told not to.

    `tag_limit` caps the hashtag line at the candidates `choose_hashtags`
    measures next."""
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
    out = _cap_hashtags(out, tag_limit)
    return out.strip()


def _same_line(a: str, b: str) -> bool:
    """True when two lines say the same words, ignoring case and punctuation."""
    norm = lambda t: " ".join(re.sub(r"[^\w\s]", " ", t.lower()).split())
    return bool(norm(a)) and norm(a) == norm(b)


def _drop_headline(body: str, headline: str) -> str:
    """`body` without a first paragraph that is only the headline again.

    Used by `youtube_description`, where the headline is the video's title and
    must not open the description too. Only an exact repeat (case and
    punctuation aside) is dropped: a first paragraph that merely starts like the headline
    is prose, and prose is never cut here."""
    parts = body.split("\n\n", 1)
    if len(parts) == 2 and _same_line(parts[0], headline):
        return parts[1].strip()
    return body


def searchless(model: str) -> str:
    """`model` with OpenRouter's `:online` search suffix stripped. The caption
    is written from the operator's info alone (2026-10-12), and a VPS `.env`
    that still names an `:online` id must not quietly bring the search back."""
    model = (model or "").strip()
    return model[:-len(":online")] if model.endswith(":online") else model


def expand(headline: str, footage: dict | None = None,
           model: str | None = None, info: str = "",
           source: dict | None = None) -> str:
    """The base caption: hook, context, question, and a line of up to
    CANDIDATE_HASHTAGS hashtag candidates (measured later, `choose_hashtags`).

    `info` is the operator's `info:` reply, `source` the post the clip came
    from (shared/reel_downloader.source_post: platform, url, text, author),
    `footage` shared/vision's media notes — the four sources `user_message`
    lays out in trust order. Every one of them may be empty but the headline.

    Returns "" when expansion isn't wanted or possible — empty headline,
    IG_CAPTION_ENABLED off, no API key, a failed or empty call — and the post
    then goes out as the bare headline with its account tag. Never raises —
    see the module docstring. Blocking; async callers go through
    asyncio.to_thread.
    """
    headline = (headline or "").strip()
    if not headline:
        return ""
    if not config.IG_CAPTION_ENABLED:
        return ""
    if _client is None:
        log.warning("OPENROUTER_API_KEY not set — posting the bare headline "
                    "as the Instagram caption")
        return ""

    try:
        resp = _client.chat.completions.create(
            model=searchless(model or config.IG_CAPTION_MODEL),
            max_tokens=MAX_TOKENS,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user",
                 "content": user_message(headline, info, footage, source)},
            ],
        )
    # Broader than the translator's `except APIError`: a gateway fails outside
    # it too (DNS, TLS, a 502 HTML body), and this function's whole job is that
    # none of that reaches the publish loop.
    except Exception as exc:
        log.error("instagram caption for %r failed: %s — posting the bare "
                  "headline", headline[:80], exc)
        return ""

    out = _clean(resp.choices[0].message.content or "", CANDIDATE_HASHTAGS)
    if not out:
        log.warning("instagram caption for %r came back empty — posting the "
                    "bare headline", headline[:80])
        return ""
    return out


def split_tags(text: str) -> tuple[str, list]:
    """(the caption without its hashtag line, that line's tags normalised and
    de-duplicated, in order). No tag line → (text, [])."""
    lines = (text or "").strip().split("\n")
    i = _tag_line_index(lines)
    if i < 0:
        return "\n".join(lines).strip(), []
    tags = [t for t in dict.fromkeys(hashtags.norm(w)
                                     for w in lines[i].split()) if t]
    return "\n".join(lines[:i]).strip(), tags


def choose_hashtags(candidates: dict) -> dict:
    """Up to STORY_HASHTAGS tags per key, out of ONE measurement pass.

    `candidates` is {key: [tag, ...]}, each list most relevant first — the
    base caption's line for the English accounts, a non-English account's
    own line for it. Every tag across every key goes into a single
    `hashtags.lookup` (cached tags cost nothing), then `hashtags.select`
    picks per key. Apify unavailable (no token, outage, timeout) → each key
    keeps its top STORY_HASHTAGS unmeasured: a post is never blocked on it.
    Never raises. Blocking."""
    cands = {k: [t for t in dict.fromkeys(hashtags.norm(c) for c in v or ())
                 if t] for k, v in candidates.items()}
    union = list(dict.fromkeys(t for v in cands.values() for t in v))
    counts = None
    if union and config.APIFY_TOKEN:
        try:
            res = hashtags.lookup(union)
        except Exception:
            log.exception("hashtag counts failed")
            res = None
        if res is None:
            log.error("hashtag counts unavailable — publishing the top %d "
                      "candidates unmeasured", STORY_HASHTAGS)
        else:
            counts = res[0]
            log.info("hashtags: one measurement pass for %d candidate tags "
                     "across %d caption(s)", len(union), len(cands))
    out = {}
    for key, tags in cands.items():
        if counts is None:
            out[key] = tags[:STORY_HASHTAGS]
        else:
            out[key] = hashtags.select(tags, counts, STORY_HASHTAGS,
                                       MIN_HASHTAGS, label=str(key))
    return out


def compose(headline: str, body: str) -> str:
    """The caption Instagram gets: the hook, the context, the question and the
    tag line, blank lines between.

    `body` opens on the model's HOOK, which replaces the headline as line 1 —
    the headline is burned into the render already, and the first line's job
    on Instagram is to be found. When the body opens on prose instead (the
    model skipped the hook), `headline` — the brand's own, the text on its
    render — goes on top.

    Aims at TARGET_MIN..TARGET_MAX characters before the hashtags and only
    logs outside it; over CAPTION_MAX the prose is cut on a word boundary and
    the tag line (which opens with the account's own tag) is kept whole."""
    headline = (headline or "").strip()
    body = (body or "").strip()
    if not body:
        return trim_caption(headline)
    lines = body.split("\n")
    i = _tag_line_index(lines)
    tag_line = lines[i].strip() if i >= 0 else ""
    paras = [p.strip() for p in
             "\n".join(lines[:i] if i >= 0 else lines).strip().split("\n\n")
             if p.strip()]
    if not (paras and _single_line(paras[0], HOOK_MAX)) and headline:
        paras = [headline] + paras
    prose = "\n\n".join(paras)
    if prose and not TARGET_MIN <= len(prose) <= TARGET_MAX:
        log.info("instagram caption is %d chars before hashtags (target "
                 "%d-%d)", len(prose), TARGET_MIN, TARGET_MAX)
    if not tag_line:
        return trim_caption(prose)
    room = CAPTION_MAX - len(tag_line) - 2
    return f"{trim_caption(prose, room)}\n\n{tag_line}" if prose else tag_line


def _single_line(par: str, limit: int) -> bool:
    return bool(par) and "\n" not in par and len(par) <= limit \
        and not _TAG_LINE.match(par)


def finish(headline: str, prose: str, tags: list, brand: str = "",
           others=()) -> str:
    """One account's finished caption: its prose, its selected story `tags`
    under its own tag (`pick_hashtags`), composed (`compose`)."""
    body = (prose or "").strip()
    if tags:
        body += "\n\n" + " ".join(f"#{t}" for t in tags)
    body = pick_hashtags(body, 0, brand=brand, others=others)
    return compose(headline, body)


# The YouTube description's prose budget. A Short's description sits behind a
# "more" tap and YouTube shows only its first line or two, so the Instagram
# caption's 2-4 paragraphs are trimmed to the lead: whole paragraphs while they
# fit, at least one. The hard cap (5000) is nowhere near.
YT_DESC_PROSE = 450

# YouTube rejects the WHOLE upload (invalidDescription) over an angle bracket.
_YT_BANNED = str.maketrans({"<": "", ">": ""})


def youtube_description(caption: str, headline: str = "",
                        limit: int = YT_DESC_PROSE) -> str:
    """The YouTube description made from one account's finished IG caption.

    The headline goes (it is the video's title), the prose is cut to the lead
    — whole paragraphs up to `limit`, the first one trimmed on a word boundary
    if it alone is over — and the account's hashtag line is kept, with
    `#shorts` added at the end. YouTube puts the first three tags of the
    description above the title, which is why the account's own tag, opening
    the line, stays first. Pure; "" in, "" out."""
    caption = (caption or "").strip()
    if not caption:
        return ""
    body = _drop_headline(caption, headline) if headline else caption
    lines = body.split("\n")
    i = _tag_line_index(lines)
    tags = lines[i].split() if i >= 0 else []
    prose = "\n".join(lines[:i] if i >= 0 else lines).strip()
    if headline and _same_line(prose, headline):
        prose = ""
    kept = []
    for para in (p.strip() for p in prose.split("\n\n")):
        if not para:
            continue
        if kept and len("\n\n".join(kept + [para])) > limit:
            break
        kept.append(para)
    prose = trim_caption("\n\n".join(kept), limit) if kept else ""
    if "#shorts" not in {t.lower() for t in tags}:
        tags.append("#shorts")
    out = f"{prose}\n\n{' '.join(tags)}" if prose else " ".join(tags)
    return out.translate(_YT_BANNED).strip()


def seed_for(text: str) -> int:
    """A stable number to rotate this post's angles and hashtags by.

    Not `hash()`: Python salts string hashing per process, so a restart between
    the render and the publish would re-plan the same post differently. crc32
    is the same number on every machine and every run.
    """
    return zlib.crc32((text or "").encode("utf-8"))


def plan(brands: list, seed: int = 0) -> list:
    """Who rewrites, in which tone and language.

    `brands` is the Instagram brands of one publish, in a stable order (config
    loads them alphabetically), each with its `lang` and `style` (the tone line
    from style.json). Returns one entry per brand — `name`, `lang`, `style`,
    `rewrite` and `offset` — in the same order.

    ONE ENGLISH BRAND WITH NO STYLE KEEPS THE BASE CAPTION (`rewrite` False):
    its call is already paid for and nothing it could differ from has been
    published yet. Every other brand rewrites — a styled one in its own tone, a
    non-English one in its own language (with its own hashtag candidates). Pure
    and offline-testable.
    """
    out, free = [], False
    for i, brand in enumerate(brands):
        lang = (brand.get("lang") or "").strip()
        style = (brand.get("style") or "").strip()
        shared = not free and not style and is_base_lang(lang)
        free = free or shared
        out.append({"name": brand.get("name", ""), "lang": lang,
                    "style": style, "rewrite": not shared,
                    "offset": seed + i})
    return out


def rephrase(text: str, lang: str = "", style: str = "",
             model: str | None = None, avoid=()) -> str:
    """One account's version of the base caption `text` (hook, context,
    question — pass it WITHOUT its tag line, `split_tags`).

    Same facts and shape, a different hook, context wording and question, in
    the account's `style` (tone only) and, for a non-English `lang`, wholly in
    that language with a line of CANDIDATE_HASHTAGS native hashtag candidates
    at the end. An English rewrite carries no tag line: those accounts draw
    from the base caption's candidates. No search — the facts are in `text`.

    `avoid`: hooks and questions other pages already use (`rewrite_all`'s
    second pass) — this one must not resemble them.

    Returns `text` unchanged when a rewrite isn't possible — no text, the kill
    switch off, no API key, a failed or empty call. Never raises, for the
    same reason `expand` doesn't. Blocking; async callers go through
    asyncio.to_thread.
    """
    text = (text or "").strip()
    if not text:
        return text
    if not config.IG_CAPTION_ENABLED:
        return text
    if _client is None:
        log.warning("OPENROUTER_API_KEY not set — accounts share the caption")
        return text

    foreign = not is_base_lang(lang)
    name = lang_name(lang)
    system = _VARIANT_SYSTEM.format(
        tags_shape=(_VARIANT_TAGS.format(lang_name=name) if foreign
                    else _VARIANT_NO_TAGS),
        style=_VARIANT_STYLE.format(style=style.strip()) if style.strip() else "",
        lang=_VARIANT_LANG.format(lang_name=name) if foreign else "")
    taken = [t for t in (avoid or ()) if t]
    if taken:
        system += _VARIANT_AVOID.format(
            taken="\n".join(f"- {t}" for t in taken))
    try:
        resp = _client.chat.completions.create(
            model=searchless(model or config.IG_CAPTION_VARIANT_MODEL),
            max_tokens=MAX_TOKENS,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
        )
    except Exception as exc:
        log.error("instagram caption variant (%s) failed: %s — posting the "
                  "base caption", lang or style[:40], exc)
        return text

    out = _clean(resp.choices[0].message.content or "", CANDIDATE_HASHTAGS)
    if not out:
        log.warning("instagram caption variant (%s) came back empty — posting "
                    "the base caption", lang or style[:40])
        return text
    if not foreign:
        out, _ = split_tags(out)       # told not to; English tags come from the base
    return out


# Two hooks (or questions) sharing this much of their vocabulary read as the
# same post. Hooks share their KEYWORDS by design (it is what search ranks), so
# the bar is high: "Teenager saves friend from electrocution after suffering a
# severe shock" vs the same with "rescues" is 0.82 — a copy.
SIMILAR = 0.75


def _words(text: str) -> set:
    return set(re.findall(r"\w+", (text or "").lower()))


def similar(a: str, b: str) -> bool:
    """Word-set (Jaccard) overlap of at least SIMILAR."""
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= SIMILAR


def hook_and_question(text: str) -> tuple[str, str]:
    """(the caption's first paragraph, its question paragraph or "")."""
    paras = [p.strip() for p in split_tags(text)[0].split("\n\n") if p.strip()]
    hook = paras[0] if paras else ""
    question = next((p for p in paras[1:] if p.rstrip().endswith("?")), "")
    return hook, question


def rewrite_all(prose: str, entries: list) -> list:
    """Every `plan` entry's text for the base caption `prose`, in order.

    The rewrites run in PARALLEL (8 at a time) — and so cannot see each other,
    which on a live trace (2026-10-18) gave three of eleven pages the very same
    hook. So a second pass follows: walking the pages in order, one whose hook
    or question is `similar` to a page before it (the base caption's included,
    when a page publishes it) is rewritten once more, ONE AT A TIME, told every
    hook and question already taken (`rephrase(avoid=…)`). Usually a few calls;
    whatever the second try returns is kept. Never raises; blocking."""
    def one(entry, avoid=()):
        if not entry["rewrite"]:
            return prose
        return rephrase(prose, entry["lang"], entry["style"], avoid=avoid)

    with ThreadPoolExecutor(max_workers=8) as pool:
        texts = list(pool.map(one, entries))
    taken, redo = [], []
    for i, (entry, text) in enumerate(zip(entries, texts)):
        hook, question = hook_and_question(text)
        if entry["rewrite"] and any(similar(x, y) for x in (hook, question)
                                    if x for y in taken):
            redo.append(i)
            continue
        taken += [x for x in (hook, question) if x]
    for i in redo:
        log.info("instagram caption for %s is too close to another page's — "
                 "rewriting it once more", entries[i]["name"])
        texts[i] = one(entries[i], avoid=list(taken))
        taken += [x for x in hook_and_question(texts[i]) if x]
    return texts


# The comment an account pins under its own post. Instagram weighs replies, and
# a line right under the caption is what gets the first ones. Written by the
# cheap rewrite model from the account's FINISHED caption plus the source post,
# so it can add one fact the caption left out — in that account's language.
COMMENT_MAX = 100

_COMMENT_SYSTEM = (
    "You run a news page on Instagram. Below is the caption of a post it is "
    "publishing, and the SOURCE POST the clip came from. Write the ONE "
    "comment the page will pin under its own post.\n\n"
    "It is EITHER one extra fact from the source post that the caption does "
    "not already give (only a fact the source post states plainly, never an "
    "opinion, and only one that does not conflict with the caption), OR — "
    "when the source post adds nothing — a sharper version of the caption's "
    "question: more specific, easier to answer in a few words. Under "
    f"{COMMENT_MAX} characters, one sentence, written in the same language as "
    "the caption.\n"
    "No new fact beyond the caption and the source post, no name or place "
    "they do not give. No hashtags, no links, no emoji, no \"like\", "
    "\"follow\", \"share\" or \"comment\", no credit line (it is added "
    "afterwards). On a story with deaths, violence or disaster keep it "
    "respectful — never a joke, never bait.\n\n"
    "Output ONLY the comment, without quotation marks."
)


def credit(source: dict | None) -> str:
    """" via @handle" / " via u/handle" / " via Reddit" — or "" when the
    source is unknown. Built from what the downloader read, never by a model:
    a guessed handle is a false credit."""
    source = source or {}
    platform = source.get("platform") or ""
    author = (source.get("author") or "").strip()
    if author and platform in ("x", "instagram"):
        return f" via @{author}"
    if author and platform == "reddit":
        return f" via u/{author}"
    name = PLATFORM_NAMES.get(platform)
    return f" via {name}" if name else ""


def pinned_comment(text: str, source: dict | None = None,
                   model: str | None = None) -> str:
    """A pinned-comment suggestion for one account's finished caption `text`,
    credited to the source (`credit`) when it is known.

    One searchless call (`IG_CAPTION_VARIANT_MODEL`). Returns "" when it isn't
    wanted or possible — no text, the kill switch off, no API key, a failed or
    empty call, or an answer far over the length a pinned comment should have.
    Never raises; blocking, async callers go through asyncio.to_thread.
    """
    text = (text or "").strip()
    if not text or not config.IG_CAPTION_ENABLED or _client is None:
        return ""
    src = ((source or {}).get("text") or "").strip()[:SOURCE_TEXT_MAX]
    try:
        resp = _client.chat.completions.create(
            model=searchless(model or config.IG_CAPTION_VARIANT_MODEL),
            max_tokens=MAX_TOKENS,
            messages=[
                {"role": "system", "content": _COMMENT_SYSTEM},
                {"role": "user", "content":
                    f"CAPTION:\n{text}\n\nSOURCE POST: {src or '(none)'}"},
            ],
        )
    except Exception as exc:
        log.error("pinned comment failed: %s", exc)
        return ""
    out = " ".join(_clean(resp.choices[0].message.content or "").split())
    if len(out) > 1 and out[0] in "\"'“«" and out[-1] in "\"'”»":
        out = out[1:-1].strip()
    # A little over the target is still a usable comment; a paragraph is not.
    if len(out) > COMMENT_MAX * 1.5:
        log.warning("pinned comment came back %d chars — dropped", len(out))
        return ""
    return out + credit(source) if out else ""


def captions_for(headline: str, brands: list, footage: dict | None = None,
                 info: str = "", source: dict | None = None,
                 base: str | None = None, headlines: dict | None = None,
                 others=()) -> dict:
    """Every brand's finished caption for one post, {name: caption} — the
    whole pipeline, blocking, for the CLI and the trace script (news_bot runs
    the same steps itself, async, with its cache).

    `brands`: dicts with name, lang and style. `base`: an expansion already
    made (else one is). `headlines`: each brand's own render headline (default
    `headline`). {} when the expansion fails. Never raises."""
    base = expand(headline, footage, None, info, source) if base is None else base
    if not base:
        return {}
    prose, base_tags = split_tags(base)
    entries = plan(brands, seed_for(headline))
    texts = rewrite_all(prose, entries)
    bodies, cands = {}, {}
    for e, text in zip(entries, texts):
        body, own = split_tags(text)
        bodies[e["name"]] = body
        cands[e["name"]] = own if (own and not is_base_lang(e["lang"])) else base_tags
    chosen = choose_hashtags(cands)
    names = set(others) | set(bodies)
    return {n: finish((headlines or {}).get(n) or headline, bodies[n],
                      chosen.get(n, []), brand=n, others=names)
            for n in bodies}


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description="Write the Instagram captions for a headline.")
    ap.add_argument("headline", nargs="+", help="the operator's headline")
    ap.add_argument("--model", default=None,
                    help=f"override IG_CAPTION_MODEL ({config.IG_CAPTION_MODEL})")
    ap.add_argument("--brands", default="", metavar="A,B,C",
                    help="real brand names, each writing in its own "
                         "brands/<group>/<name>/style.json tone and language")
    ap.add_argument("--media", default="", metavar="PATH",
                    help="the clip or photo — its media notes go in the prompt")
    ap.add_argument("--info", default="", metavar="TEXT",
                    help="the operator's info, as an `info:` reply gives it")
    ap.add_argument("--source", default="", metavar="TEXT",
                    help="the source post's text (tweet, Reddit title, IG caption)")
    ap.add_argument("--platform", default="", choices=["", "x", "reddit",
                                                       "instagram"])
    ap.add_argument("--author", default="", help="the source post's handle")
    args = ap.parse_args()

    headline = " ".join(args.headline)
    footage = vision.describe(args.media) if args.media else {}
    source = {"platform": args.platform, "text": args.source,
              "author": args.author}
    print("--- prompt ---")
    print(user_message(headline, args.info, footage, source))
    print()
    base = expand(headline, footage, args.model, args.info, source)
    print("--- base caption ---")
    print(base or "(failed — see the log)")
    print()
    if args.brands:
        from shared import branding
        langs = {b["name"]: b["lang"] for b in config.BRANDS}
        brands = [{"name": n.strip(), "lang": langs.get(n.strip(), ""),
                   "style": branding.load_writing_style(config.brand_dir(n.strip()))}
                  for n in args.brands.split(",") if n.strip()]
        for name, text in captions_for(headline, brands, footage, args.info,
                                       source, base=base).items():
            print(f"--- {name} ---")
            print(text)
            print()
