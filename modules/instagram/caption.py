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

THE HEADLINE IS NOT THE MODEL'S TO WRITE. Every call here returns the BODY —
the paragraphs and the tag line — and `compose` puts the brand's own headline
(the one burned into its render, already translated) on top of it, as
"{headline}, blank line, {body}". A model asked to "reproduce the headline"
paraphrased it on the rewrite path, so an account could publish a first line
that disagreed with its own banner.

The operator can hand the search more to go on with an `info:` reply — the
source, the names, what is actually known. It goes into the expansion prompt
as trusted facts that outrank the web search, and its words steer the search
query (it sits in the user message, which is what `:online` builds the query
from). It is never published as such.

THE MEDIA GOES IN FIRST. `:online` is a search plugin that runs BEFORE the
model sees the prompt and builds its query out of that prompt — so a prompt
that is only a headline searches the headline's WORDS and comes back with the
most prominent story matching them. In 2026-09 that published a caption about
Floyd Mayweather over a clip of a completely different boxer: the search was
never wrong about Mayweather, it was answering a question nobody should have
asked. Attaching the video to that same call would not have helped, because
the search would still have fired off the headline first.

So `expand` takes a second argument: `footage`, shared/vision.describe's report
of what the media actually shows, audio included. With it in the prompt the
search query is built from the footage, the search drops to corroboration
(names, places, dates — anything contradicting the report is dropped), and the
caption is written from the media. Without it — analysis off, or failed —
the request is byte-for-byte the one this function has always sent.

Two contracts, both taken from modules/telegram/translator.py, because they are
what make a model safe to put in a publishing path:

  NEVER RAISES — a missing key, a dead gateway, an unknown model id, an empty
  completion all return an empty body, and the post goes out as the headline
  with the account's tag. The render is already made and
  the operator has already tapped publish; a caption hiccup must cost the post
  its paragraphs, not its existence.

  FAITHFUL — the search sources the story the MEDIA shows, and never builds a
  story around the headline. On a news account an invented detail costs more
  than a short caption ever will.

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
             the `writing_style` out of brands/<group>/<name>/style.json, which is the
             same on every post it publishes. An account with no voice gets a
             rotating ANGLES directive instead, never both: two structural
             instructions fight and the caption comes back in neither. Given a
             `lang` it also translates, so a foreign-language account's variant
             IS its translation — one call, not two.
  `pick_hashtags`
             pure: each account opens the line with its OWN tag, keeps the
             pool's two most specific tags and draws two more out of the tail
             (#frontiva24 on frontiva24, #europamonitor on europamonitor). No tokens, and no two accounts carry the same tag
             line.

THE ACCOUNT'S OWN TAG IS MANDATORY (`with_brand_tag`), on every caption it
publishes and not only on the ones that came back with a pool — a failed
expansion falls back to the bare headline, and that gets a tag line made for
it. It is built from the BRAND NAME rather than the Instagram handle, because
a handle may carry a dot ("vestra.24") and Instagram ends a tag at the first
character that is not a letter or a digit. It OPENS the tag line and takes
the first of the five slots rather than adding a sixth; no tag repeats on a
line, and a sibling brand's tag is never dealt to another account.

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
angles are actually pulling the rewrites apart, and `--media` runs the whole
inverted pipeline against a real clip:
    py modules/instagram/caption.py "Released footage shows plane crash at Miami International Airport"
    py modules/instagram/caption.py --accounts 4 "Released footage shows plane crash …"
    py modules/instagram/caption.py --media clip.mp4 "Man walks past a bear"
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

# Reasoning tokens plus a caption of at most 2200 characters. Wide enough that
# the model is never the one that stops (see the module docstring), narrow
# enough that OpenRouter's up-front credit reservation stays under a cent.
MAX_TOKENS = 8000

# How to estimate a hashtag's size, shared by the caption prompts and the
# feedback round (`suggest_hashtags`). Every number in it was MEASURED on
# Instagram through Apify on 2026-10-07 — a model told only "aim for 5k-200k"
# put 0 of 10 picks in the band, so it is given the scale itself: the rules
# are the patterns those measurements show, the table is the calibration.
# Re-measure with `py modules/instagram/hashtags.py tag1 tag2 …` before
# changing a figure.
_HASHTAG_SIZE_GUIDE = (
    "HOW BIG A HASHTAG IS — estimate before you pick. The band that works is "
    "5,000 to 200,000 posts: bigger buries the post within seconds, smaller "
    "is a tag nobody browses. Measured sizes follow these rules:\n"
    "  1. ONE common word — a city of a million people or more, a country, a "
    "general subject (rain, flooding, wildfire, firefighters, nato) — is "
    "almost always 1 million or more: TOO BIG. A small town (under ~100k "
    "inhabitants) is often inside the band.\n"
    "  2. Every specific word you add divides the size by roughly 5 to 20: "
    "#flooding 1.3M -> #flashflood 117k; #weather (huge) -> #extremeweather "
    "159k -> #weatherwarning 41k. Two generic words together still tend to "
    "be too big (#heavyrain 725k, #planecrash 396k); a precise two-word "
    "phrase lands in the band.\n"
    "  3. City + news: a world city lands in the band (#londonnews 33k, "
    "#miaminews 62k); a smaller city falls under it (#kyivnews 4.9k, "
    "#ashevillenews 1.1k).\n"
    "  4. Place + event is in the band ONLY when people already use that "
    "exact phrase: an English-speaking place with an English word works "
    "(#texasflood 37k, #ukweather 175k, #londonfire 6k, #miamiairport 86k); "
    "a non-English country with an English word is usually EMPTY "
    "(#spainweather 0, #spainfloods 0), because locals tag in their own "
    "language. Never coin a compound for this one event (#valenciarain 0, "
    "#hatayearthquake 0, #valenciafloods 0).\n"
    "  5. Institutions: the FULL name of a large body sits high in the band "
    "(#europeanparliament 178k, #europeancommission 103k, #euparliament 23k); "
    "a famous abbreviation is too big (#nato 1.4M, #faa 406k); a niche "
    "agency's is small but usable (#ntsb 8.9k).\n"
    "  6. A niche topic people follow is the sweet spot: #aviationsafety 62k, "
    "#dashcamvideos 75k, #policechase 139k, #courtcase 80k, #bearsighting "
    "24k, #wildlifeencounter 16.5k, #floodwarning 37k.\n"
    "  Too big for certain: #news #breakingnews #viral #viralvideo #usa "
    "#world #trending #newsupdate (6.6M) #caughtoncamera (517k) #dashcam "
    "(1M) #wildfire (969k) #roadsafety (1.4M).\n"
)

# The hashtag rules, shared by both system prompts below. The line the model
# writes is a list of CANDIDATES: `verify_hashtags` measures them and keeps the
# in-band ones as the pool `pick_hashtags` deals out per account — so the two
# prompts have to ask for the same thing.
_HASHTAG_RULES = (
    "HASHTAGS — one line of up to TWENTY candidates, lowercase, "
    "space-separated, letters and digits only inside each tag. Each tag is a "
    "plain ASCII \"#\" with the word immediately after it — \"#madrid\", "
    "never a keycap emoji, never a space after the hash, never a comma "
    "between tags. Every candidate's real post count is then MEASURED and "
    "only those between 5,000 and 200,000 posts are published, so give "
    "twenty genuinely relevant candidates spread around that band rather "
    "than eight safe guesses. If the story cannot carry twenty, give fewer — "
    "a tag that is not about this story is worse than a short line.\n"
    "  Pick them RELEVANCE FIRST. Every tag must be something this particular "
    "story is actually about — the place it happened, the institution or "
    "public figure at its centre, its subject, its topic. A tag a reader "
    "could not connect to the caption above it does not go in, whatever its "
    "size.\n"
    + _HASHTAG_SIZE_GUIDE +
    "  Order them MOST RELEVANT FIRST — the place and the event, then the "
    "main actors or subject, then the topic; the first two that pass the "
    "count are the tags every account keeps. Tag places, institutions, "
    "countries and public events — never a private individual's name.\n"
)

# The growth half of the caption, shared by both system prompts: the search
# line it opens on and the share line it closes on. Instagram (2026) ranks a
# caption's TEXT in search, shows only ~125 characters before "more", and
# weighs sends and saves above likes — so the first line is written for search
# and the last prose line asks for a send or a save.
#
# These two lines are STRUCTURE, not voice: `_VARIANT_RULES` makes every
# per-account rewrite keep them, and `_VARIANT_STYLE` says a house style
# never removes them — otherwise a "two paragraphs, never more" brand would
# rewrite the growth work straight back out.
_GROWTH_RULES = (
    "LINE 1 — THE SEARCH LINE. Instagram ranks a caption's words in search "
    "and shows only about 125 characters before \"more\", so the first line "
    "is written to be FOUND. One line, at most 125 characters, stating the "
    "story plainly with its main keyword phrase near the start — the words a "
    "person would type into Instagram search to find it: the place and the "
    "kind of event (\"Miami airport plane crash\"), or the named institution "
    "and what it did. No wordplay, no pun, no question, no teaser that "
    "withholds the point, no hashtag, no emoji, no \"Breaking\". It is not "
    "the operator's headline copied out: write it for search. It obeys the "
    "same FAITHFUL rule as everything else — a place or a name your sources "
    "do not give stays out of it.\n"
    "KEYWORDS IN THE PARAGRAPHS — work one or two further phrases people "
    "search for this story into the paragraphs naturally: the town and the "
    "region, the event's common name, the institution's full name. Normal "
    "words in normal sentences, each used once; never a list of keywords, "
    "never the same phrase repeated to rank.\n"
    "LAST PROSE LINE — THE SHARE LINE. Sends and saves are what Instagram "
    "rewards most, so the caption's last line before the hashtags is ONE "
    "short sentence asking the reader to send the post to someone or to save "
    "it, tied to who would genuinely want it: \"Send this to someone who "
    "drives the I-95 every day.\", \"Save this if you are flying through "
    "Miami this week.\" Never \"like\", \"follow\" or \"comment below\", never "
    "a link, no emoji, and nothing it implies may go beyond the facts. On a "
    "story with deaths, violence or disaster keep it sober and useful "
    "(\"Share this so people in the area know the road is closed.\") — never "
    "playful.\n"
)

# What the operator's `info:` reply is to the model. Shared by both system
# prompts: it is the same promise whether or not the media was analysed.
_INFO_RULES = (
    "OPERATOR'S INFO — when the message carries a block headed OPERATOR'S "
    "INFO, it was written by the newsroom and is the SOURCE OF TRUTH: the "
    "outlet or account the story came from, who the people are, where and "
    "when it happened, what is confirmed. Trust it over the web search and "
    "over the headline wherever they disagree, and use its names, sources and "
    "places to find the right story when you search. It may name a person or "
    "a place nothing else names. Take the facts out of it and write them into "
    "the caption; never copy it in as a quotation, and never mention that you "
    "were given it.\n"
)

_SYSTEM = (
    "You write the Instagram captions for a news account. You are given one "
    "headline. Search the web for that story as it stands today, then write "
    "the account's caption for it.\n\n"
    "SHAPE — exactly this, and nothing else:\n"
    "  first line: the search line (below).\n"
    "  a blank line.\n"
    "  two to four paragraphs, one to three sentences each, a blank line "
    "between them. The first paragraph is the news itself: who, what, "
    "where, when. The "
    "ones after it carry the supporting facts — the names, the numbers, the "
    "dates, the official response, what happens next. A last paragraph may "
    "place the story in its standing context (\"The controversy comes as …\", "
    "\"The NTSB and FAA are investigating the cause of the crash.\").\n"
    "  a blank line.\n"
    "  the share line (below), on its own.\n"
    "  a blank line.\n"
    "  last line: the hashtags.\n\n"
    + _GROWTH_RULES +
    "FAITHFUL — every fact, name, number, date and quote in the caption must "
    "come from the headline, from the operator's info when there is any, or "
    "from what your search actually returns. Invent "
    "nothing: no background you did not read, no consequence, no speculation, "
    "no plausible-sounding detail, no quote you cannot source. If the search "
    "returns little, write ONE short paragraph — a thin caption is "
    "correct, a padded one is a lie on a news account. Never give a death "
    "toll, a casualty count, a suspect's name or a cause as settled when your "
    "sources disagree: attribute it (\"police said\") or leave it out.\n"
    "REGISTER — neutral wire-service prose, the way Reuters or AP writes. No "
    "hype, no editorialising, no adjective doing an opinion's work, no "
    "exclamation marks, no emoji, no rhetorical questions, no first person, "
    "no \"Breaking:\", no call to action other than the one share line, no "
    "\"follow us for more\", no links, no sign-off.\n"
    + _INFO_RULES + _HASHTAG_RULES +
    "\nOutput ONLY the caption. No preamble, no explanation, no markdown, no "
    "bold, no bullet points, no surrounding quotation marks, no numbered "
    "citation markers, no source list at the end."
)

# The footage-first system prompt. What it says about faithfulness, register
# and hashtags is the same; what changes is WHERE THE FACTS COME FROM, and that
# is the whole point of the inversion:
#
#   The `:online` suffix is a search plugin that runs BEFORE the model sees the
#   prompt, and it builds its query out of that prompt. Given a headline alone
#   it searches those words and returns the most prominent story matching them
#   — which is how a clip of one boxer published a caption about Floyd
#   Mayweather, who is not the man in the video (2026-09). With shared/vision's
#   report in the prompt, the query is built from what the media actually
#   shows, and the search drops from being the source of the story to being a
#   way of putting names and places on it.
#
# The second half of that bug was the SHAPE. "If the search returns little,
# write ONE short paragraph and stop" is a sound rule when the headline is all
# you have — it is what produced a headline followed by one paragraph restating
# the headline. With the footage in hand there is always material, so the
# escape hatch is gone and restating is banned outright.
_SYSTEM_FOOTAGE = (
    "You write the Instagram captions for a news account. You are given a "
    "report of what one video or photo ACTUALLY SHOWS — written by someone who "
    "watched it — and the headline its operator typed. Write the account's "
    "caption for that media.\n\n"
    "THE FOOTAGE IS GROUND TRUTH. The caption describes the media in front of "
    "you and nothing else. Search the web to find out WHICH event this is and "
    "to put names, places, dates and official responses on it — but the search "
    "serves the footage, never the other way round. Anything it returns that "
    "CONTRADICTS the report, or that belongs to a different event which merely "
    "matches the headline's words, is the wrong story: drop it and write from "
    "the footage alone. A search that finds nothing costs this caption "
    "nothing — you have a full account of what happens, which is all a caption "
    "needs. The headline does not outrank the report either; it is the "
    "operator's guess at what the media shows, and where the two disagree the "
    "report wins.\n\n"
    "PICK THE REGISTER FROM WHAT YOU HAVE:\n"
    "  A REPORTED EVENT — your search confirms a real news story behind this "
    "media: a crash, a court ruling, a strike, an attack, an official "
    "announcement. Write it as the news. Neutral wire-service prose, the way "
    "Reuters or AP writes: who and what and where and when first, then the "
    "supporting facts, the official response, what happens next.\n"
    "  THE CLIP IS THE STORY — the media is the whole of it, the kind of thing "
    "that circulates because of what it shows: an animal in a street, a near "
    "miss, a rescue, a stunt, a crowd reacting. Then NARRATE IT, in the order "
    "it happens — the situation, what happens, what the people around it do "
    "and say, how it ends. Plain, calm, readable sentences, the way you would "
    "tell somebody what they are about to watch. This is the commoner case and "
    "it is not the lesser caption.\n\n"
    "SHAPE — exactly this, and nothing else:\n"
    "  first line: the search line (below).\n"
    "  a blank line.\n"
    "  two to four paragraphs, one to three sentences each, a blank line "
    "between them.\n"
    "  a blank line.\n"
    "  the share line (below), on its own.\n"
    "  a blank line.\n"
    "  last line: the hashtags.\n\n"
    + _GROWTH_RULES +
    "  When the clip is the story and no search confirms where it happened, "
    "the search line's keyword is what happens in it (\"bear walks behind "
    "elderly man on residential street\") — never a place you were not "
    "given.\n\n"
    "NEVER RESTATE THE HEADLINE. The first paragraph ADVANCES the story: it "
    "sets the scene and begins what happens. A first paragraph that says the "
    "headline or the search line again in longer words is a failed caption, "
    "and so is one that "
    "hedges behind \"footage shows\" or \"apparently\" instead of telling the "
    "reader what happened. You have watched it, through the report — write "
    "what happened.\n\n"
    "TELL THE STORY, DON'T DESCRIBE THE VIDEO. The report is a witness "
    "account, not a text to paraphrase — take what HAPPENED out of it and "
    "write that:\n"
    "  Past tense, as a thing that happened, not a thing playing on a screen. "
    "\"A man was walking down the street when a bear appeared behind him\", "
    "never \"a man is seen walking\" and never \"the video shows\".\n"
    "  Name people by WHO THEY ARE — a player, his teammate, a passer-by, the "
    "driver, an elderly man. Not by what they are wearing. \"The player in the "
    "purple shirt\" is how you tell two figures apart on a screen, not how you "
    "tell somebody what happened; use a shirt, a car or a colour only when it "
    "is genuinely how the person is identified.\n"
    "  The AUDIBLE line tells you WHAT HAPPENED — that people shouted a "
    "warning, that a crowd reacted, that somebody was hurt. Write that. Never "
    "transcribe it as a sound effect: \"a high-pitched noise is heard\" is a "
    "note about a soundtrack and belongs nowhere in a caption.\n"
    "  Leave out what a reader would not ask. Camera angles, cuts, "
    "watermarks, the pitch markings, how long the clip runs — none of it is "
    "the story.\n"
    "  END ON THE LAST THING THAT HAPPENED, not on the recording stopping. "
    "\"as the video ended\", \"before the clip cuts off\", \"the footage ends "
    "there\" all put the camera back in a caption that had got rid of it. "
    "Where the report stops, simply stop — a reader does not need to be told "
    "that what you cannot see, you did not see.\n\n"
    "FAITHFUL — every fact, name, number, date and quote comes from the "
    "report, from the headline, from the operator's info, or from a search "
    "result that genuinely matches the report. Invent nothing. Do not name a "
    "person neither the report nor the operator's info names, however "
    "familiar somebody looks in your search results — that "
    "mistake is the reason you are given the report at all. Do not state a "
    "city, a country or a date the report leaves open: \"a residential "
    "street\" stays a residential street. Never give a death toll, a casualty "
    "count, a suspect's name or a cause as settled when your sources "
    "disagree — attribute it (\"police said\") or leave it out.\n"
    "REGISTER — no hype, no editorialising, no adjective doing an opinion's "
    "work, no exclamation marks, no emoji, no rhetorical questions, no first "
    "person, no \"Breaking:\", no call to action other than the one share "
    "line, no \"follow us for more\", no links, no sign-off. Narrating a clip "
    "is still neutral prose; it is "
    "simply prose about what happens rather than about what was reported.\n"
    + _INFO_RULES +
    "  The info tells you WHO and WHICH EVENT; the report tells you WHAT "
    "HAPPENS on screen. Where the info disagrees with what the report plainly "
    "shows happening, write what the report shows.\n"
    + _HASHTAG_RULES +
    "  A clip that is the story carries the tags people browse it under — "
    "#caughtoncamera, #bearencounter, #wildlifeencounter — beside the ones "
    "naming what is in it. Those are legitimate picks here, and they are not "
    "picks for a reported news event.\n\n"
    "Here is the shape a clip-is-the-story caption has. Follow its FORM — how "
    "it opens, how it moves, how plainly it is written — and never its "
    "content. The operator's headline was \"Elderly man doesn't notice a "
    "bear walking right beside him\":\n\n"
    "Bear walks right behind elderly man on a residential street\n\n"
    "An elderly man was walking down the street when a bear appeared just a "
    "few feet away from him.\n\n"
    "People nearby began shouting and warning him to turn around, but he "
    "initially didn't hear them. Eventually, he realized what was happening, "
    "turned around and spotted the bear.\n\n"
    "He then quickly moved away from the animal as people continued warning "
    "him.\n\n"
    "Send this to someone who lives in bear country.\n\n"
    "#bearencounter #bearsighting #caughtoncamera #wildlifeencounter\n\n"
    "Output ONLY the caption. No preamble, no explanation, no markdown, no "
    "bold, no bullet points, no surrounding quotation marks, no numbered "
    "citation markers, no source list at the end."
)

# The footage report and the headline, clearly separated — the report first,
# because it is what the caption is written from and what the search is run
# against; the headline after it, labelled as the operator's, so the model is
# never invited to treat it as the brief.
_USER_FOOTAGE = (
    "{footage}\n\n"
    "THE OPERATOR'S HEADLINE: {headline}\n\n"
    "{info}"
    "Write the caption."
)

# The operator's `info:` reply, labelled so the prompts' OPERATOR'S INFO rule
# can point at it. Sits in the USER message on purpose: `:online` builds its
# search query from the prompt, and the names and sources in it are exactly
# what that query should be built from.
_USER_INFO = "OPERATOR'S INFO:\n{info}\n\n"

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
_VARIANT_ANGLE = (
    "HOW YOURS MUST DIFFER: {angle} (Where to open and how many paragraphs "
    "refer to the paragraphs under the search line; the search line and the "
    "share line stay where they are.)\n\n"
)

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
    "SHAPE — the search line, a blank line, the paragraphs with a blank line "
    "between them, a blank line, the share line, a blank line and the "
    "hashtag line. THE FIRST PARAGRAPH STILL CARRIES THE NEWS — who, what, "
    "where, when — however short or unconventional your house style is. A "
    "caption that opens on a detail and leaves the reader to infer the story "
    "is a failed caption, not a terse one.\n"
    "THE SEARCH LINE — the caption's first line is written for Instagram "
    "search. Keep its main keyword phrase (the place, the event, the named "
    "institution) — those words are what the post is found by — but the "
    "line itself must NOT be the one you were given: change everything "
    "around the keywords (the verb, the order, the detail it adds), so the "
    "two accounts' first lines differ at a glance. One line, at most 125 "
    "characters, no hashtag, no emoji, no question, no wordplay.\n"
    "THE SHARE LINE — the last line before the hashtags asks the reader to "
    "send the post to someone or to save it. Keep exactly one such line, in "
    "that place, but write a DIFFERENT sentence from the one you were given "
    "— another reader to send it to, or save instead of send — and never "
    "turn it into \"like\", \"follow\" or \"comment\".\n"
    "KEYWORDS — the search phrases in the paragraphs (the town, the region, "
    "the event's name, the institution's full name) stay in, as normal words "
    "in your sentences.\n"
    "HASHTAGS — the last line is copied through character for character: the "
    "same tags in the same order, none added, none dropped, none translated. "
    "They are chosen elsewhere.\n"
    "REGISTER — neutral wire-service prose, the way Reuters or AP writes. No "
    "hype, no editorialising, no exclamation marks, no emoji, no rhetorical "
    "questions, no first person, no \"Breaking:\", no call to action other "
    "than the share line, no links, no sign-off.\n\n"
    "Output ONLY the caption. No preamble, no explanation, no markdown, no "
    "surrounding quotation marks, and no note about what you changed."
)

# Appended when the account has a voice of its own — `writing_style` in
# brands/<group>/<name>/style.json (shared/branding.load_writing_style). The angle says
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
    "refilled with THIS story's facts.\n"
    "The house style governs the PARAGRAPHS. It never removes, merges or "
    "moves the search line, the share line or the search keywords — those "
    "are how the post is found and passed on, not a matter of voice. Where "
    "the style says how to open, that is the first paragraph, under the "
    "search line; where it says how to close or how many paragraphs to "
    "write, that is the last paragraph and the paragraph count, and the "
    "share line still follows them.\n{style}"
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

# What `pick_hashtags` deals five out of: the MEASURED in-band tags
# (`verify_hashtags`, HASHTAG_TARGET of them at most). A pool is the cheapest
# de-duplication there is: two tags kept by every account and the rest rotated,
# so thirteen accounts get different tag lines for no tokens at all. Without
# an Apify token the model's own first ten stand in, unmeasured.
POOL_HASHTAGS = 10

# What `expand` asks the model for: candidates, most relevant first, of which
# only the ones measured inside the band survive into the pool. Twenty, because
# a first live test put 0 of 10 blind picks in the band.
CANDIDATE_HASHTAGS = 20

# The head of the pool every account keeps. The prompt orders tags
# most-specific-first, so these two are what the story is actually about —
# rotating them away to manufacture a difference would cost more reach than the
# duplicate text ever did.
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


def _cap_hashtags(text: str, limit: int = POOL_HASHTAGS) -> str:
    """De-duplicate the caption's trailing hashtag line and cut it to `limit`.

    The ceiling here is the POOL, not the post: `pick_hashtags` deals each
    account its five out of what survives. A model that ignores the count
    entirely must still not hand a twenty-tag line down the chain — and a
    repeated tag must go BEFORE the cut, or it spends a pool slot twice."""
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


def _clean(text: str, tag_limit: int = POOL_HASHTAGS) -> str:
    """Undo the wrappers a chat model reaches for when told not to.

    `tag_limit` caps the hashtag line: the pool for a finished caption, the
    full CANDIDATE_HASHTAGS for `expand`, whose line is measured next."""
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

    The prompts tell the model the headline is printed above its caption, and
    `compose` puts it there — so a model that reproduces it anyway would give
    the post the same line twice. Only an exact repeat (case and punctuation
    aside) is dropped: a first paragraph that merely starts like the headline
    is prose, and prose is never cut here."""
    parts = body.split("\n\n", 1)
    if len(parts) == 2 and _same_line(parts[0], headline):
        return parts[1].strip()
    return body


def expand(headline: str, footage: dict | None = None,
           model: str | None = None, info: str = "") -> str:
    """The caption BODY for `headline`: paragraphs, blank line, hashtag pool.

    Never the headline itself — `compose` puts each brand's own headline on
    top, so every account's first line is exactly the one on its banner.

    `footage` is shared/vision.describe's report of what the media actually
    shows. Given one, the caption is written FROM THE MEDIA and the search is
    demoted to corroboration — which is the whole point, since `:online` builds
    its query from this prompt and a headline-only prompt searches the
    headline's words rather than the story in front of it.

    `info` is the operator's `info:` reply — the source and the facts they
    already have. It goes into the user message as OPERATOR'S INFO, which the
    system prompts treat as the source of truth and which steers the search.

    Returns "" when expansion isn't wanted or possible — empty headline,
    IG_CAPTION_ENABLED off, no API key, a failed or empty call — and the post
    then goes out as the bare headline with its account tag. Never raises —
    see the module docstring. Blocking; async callers go through
    asyncio.to_thread.
    """
    headline = (headline or "").strip()
    info = (info or "").strip()
    if not headline:
        return ""
    if not config.IG_CAPTION_ENABLED:
        return ""
    if _client is None:
        log.warning("OPENROUTER_API_KEY not set — posting the bare headline "
                    "as the Instagram caption")
        return ""

    info_block = _USER_INFO.format(info=info) if info else ""
    if footage:
        system = _SYSTEM_FOOTAGE
        user = _USER_FOOTAGE.format(footage=vision.as_prompt(footage),
                                    headline=headline, info=info_block)
    else:
        system = _SYSTEM
        # No info: exactly the headline, as this request has always been.
        user = f"{headline}\n\n{info_block}".strip() if info else headline

    try:
        resp = _client.chat.completions.create(
            model=model or config.IG_CAPTION_MODEL,
            max_tokens=MAX_TOKENS,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
    # Broader than the translator's `except APIError`: a gateway fails outside
    # it too (DNS, TLS, a 502 HTML body), and this function's whole job is that
    # none of that reaches the publish loop.
    except Exception as exc:
        log.error("instagram caption for %r failed: %s — posting the bare "
                  "headline", headline[:80], exc)
        return ""

    out = _drop_headline(_clean(resp.choices[0].message.content or "",
                                CANDIDATE_HASHTAGS), headline)
    if not out:
        log.warning("instagram caption for %r came back empty — posting the "
                    "bare headline", headline[:80])
        return ""
    return out


# The feedback round of the hashtag search (hashtags.refine): the model sees
# its earlier candidates WITH their measured counts and proposes new ones.
# Searchless and cheap (IG_CAPTION_VARIANT_MODEL) — the facts are in the
# caption, and the numbers are what it was missing.
_SUGGEST_SYSTEM = (
    "You choose Instagram hashtags for a news post; its caption is below. A "
    "tag is only used when it has between 5,000 and 200,000 posts. Earlier "
    "candidates were MEASURED — their real post counts are listed under "
    "MEASURED. Propose {need} to {more} NEW candidates (none of the measured "
    "ones), each genuinely about this story, aimed inside the band:\n"
    "  for a tag that came back TOO BIG, go more specific — add the place, "
    "the kind of event, or a qualifier;\n"
    "  for one that came back TOO SMALL or with NO POSTS, go broader, or use "
    "the form people already type instead of a compound you made up;\n"
    "  tags under IN-BAND RELATED were measured inside the band already — "
    "use one when, and only when, it is about this story (most are not: a "
    "tag for a different city of the same name, a hobby, a brand).\n\n"
    + _HASHTAG_SIZE_GUIDE +
    "\nOutput ONLY one line of space-separated hashtags, most relevant first, "
    "lowercase, letters and digits only. No explanation."
)


def suggest_hashtags(text: str, measured: dict, related: dict, need: int,
                     model: str | None = None) -> list:
    """The next round's candidates, from the measured numbers. [] on failure."""
    if _client is None or not config.IG_CAPTION_ENABLED:
        return []
    lines = ["MEASURED:"] + [
        f"#{t} — {n:,} posts ({hashtags.verdict(n)})"
        for t, n in sorted(measured.items(), key=lambda kv: kv[1])]
    if related:
        lines += ["", "IN-BAND RELATED:"] + [
            f"#{t} — {n:,} posts" for t, n in sorted(related.items(),
                                                      key=lambda kv: kv[1])]
    need = max(int(need), 1)
    try:
        resp = _client.chat.completions.create(
            model=model or config.IG_CAPTION_VARIANT_MODEL,
            max_tokens=MAX_TOKENS,
            messages=[
                {"role": "system", "content": _SUGGEST_SYSTEM.format(
                    need=need + 5, more=need + 12)},
                {"role": "user",
                 "content": f"CAPTION:\n{text}\n\n" + "\n".join(lines)},
            ],
        )
    except Exception as exc:
        log.error("hashtag suggestions failed: %s", exc)
        return []
    raw = resp.choices[0].message.content or ""
    return [t for t in (hashtags.norm(w) for w in
                        re.findall(r"#?[^\s#,;]+", raw)) if t]


def verify_hashtags(body: str) -> str:
    """`body` with its hashtag line replaced by the MEASURED in-band pool.

    The line `expand` writes is up to CANDIDATE_HASHTAGS guesses; this keeps
    the ones Apify measures between HASHTAG_MIN_POSTS and HASHTAG_MAX_POSTS,
    after up to HASHTAG_ROUNDS rounds of measured feedback (hashtags.refine),
    in the model's relevance order. None in the band after the last round:
    the measured tags nearest the band's TOP stand in (hashtags.nearest);
    only when every candidate had no posts at all does the line go, and the
    post carries just the account's own tag (`pick_hashtags`/`with_brand_tag`).

    No APIFY_TOKEN, or Apify unreachable on the first round: the model's own
    tags are kept unmeasured (capped to the pool), exactly the behaviour
    before the lookup existed — a post is never blocked on it. Never raises.
    """
    body = (body or "").strip()
    lines = body.split("\n")
    i = _tag_line_index(lines)
    if i < 0:
        return body
    if not config.APIFY_TOKEN:
        return _cap_hashtags(body)
    prose = "\n".join(lines[:i]).strip()
    try:
        found = hashtags.refine(prose, lines[i].split(), suggest_hashtags)
    except Exception:
        log.exception("hashtag check failed — keeping the unchecked tags")
        found = None
    if found is None:
        log.error("hashtag counts unavailable — publishing unchecked tags")
        return _cap_hashtags(body)
    if not found:
        log.warning("no candidate hashtag had any posts — account tag only")
        return prose
    return f"{prose}\n\n" + " ".join(f"#{t}" for t in found[:POOL_HASHTAGS])


# The longest first paragraph still read as the model's SEARCH LINE. The
# prompt asks for at most 125 characters (what Instagram shows before "more");
# the slack is for a model that runs a little over. A first paragraph past
# this, or on more than one line, is prose — the model skipped the search
# line — and the brand's headline goes back on top as before.
SEARCH_LINE_MAX = 160

# The longest closing paragraph `compose` treats as the share line when it
# has to cut a caption down: kept whole, with the cut taken out of the
# paragraphs above it instead.
SHARE_LINE_MAX = 240


def _single_line(par: str, limit: int) -> bool:
    return bool(par) and "\n" not in par and len(par) <= limit \
        and not _TAG_LINE.match(par)


def compose(headline: str, body: str) -> str:
    """The caption Instagram gets: the search line, a blank line, the body.

    The body opens on the model's SEARCH LINE (see `_GROWTH_RULES`), which
    replaces the headline as the caption's first line — the headline is
    burned into the render already, and the first line's job on Instagram is
    to be found. When the body opens on prose instead (the model skipped the
    line), `headline` — the brand's own, the text on its render — goes on top
    exactly as before, and a body that merely repeats it is not doubled.

    When the whole thing is over Instagram's cap it is the MIDDLE that gives
    way: the first line, the share line and the hashtag line (which opens
    with the account's own tag) are kept whole, and the paragraphs between
    them are cut on a word boundary to make room."""
    headline = (headline or "").strip()
    body = (body or "").strip()
    if not body:
        return trim_caption(headline)
    if headline:
        body = _drop_headline(body, headline)
    lines = body.split("\n")
    i = _tag_line_index(lines)
    tag_line = lines[i].strip() if i >= 0 else ""
    paras = [p.strip() for p in
             "\n".join(lines[:i] if i >= 0 else lines).strip().split("\n\n")
             if p.strip()]
    if paras and _single_line(paras[0], SEARCH_LINE_MAX):
        lead, paras = paras[0], paras[1:]
    else:
        lead = headline
    head = [lead] if lead else []
    full = "\n\n".join(head + paras + ([tag_line] if tag_line else []))
    if len(full) <= CAPTION_MAX:
        return full
    share = []
    if len(paras) > 1 and _single_line(paras[-1], SHARE_LINE_MAX):
        share, paras = [paras[-1]], paras[:-1]
    keep = "\n\n".join(share + ([tag_line] if tag_line else []))
    room = CAPTION_MAX - (len(keep) + 2 if keep else 0)
    out = trim_caption("\n\n".join(head + paras), room)
    return f"{out}\n\n{keep}" if keep else out


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
    account's permanent voice (`writing_style` in brands/<group>/<name>/style.json),
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
    return out


# The comment an account pins under its own post. Instagram weighs replies, and
# a question right under the caption is what gets the first ones. Written from
# the account's FINISHED caption — no search, the facts are all in it — by the
# cheap rewrite model, so it costs a fraction of a cent and comes out in that
# account's language and wording.
COMMENT_MAX = 100

_COMMENT_SYSTEM = (
    "You run a news account on Instagram. Below is the caption of a post it "
    "is publishing. Write the ONE comment the account will pin under its own "
    "post, to get readers replying.\n\n"
    "It is a QUESTION that invites a reply about this specific story — what "
    "readers think should happen next, what they would have done, whether "
    "they have seen the same where they live — or a \"which one?\" choice "
    "between two concrete options from the story. Under "
    f"{COMMENT_MAX} characters, one sentence, written in the same language as "
    "the caption.\n"
    "Everything it mentions comes from the caption: no new fact, no name or "
    "place the caption does not give. No hashtags, no links, no emoji, no "
    "\"like\", \"follow\" or \"share\". On a story with deaths, violence or "
    "disaster ask something respectful and useful (what should be done, how "
    "people can stay safe) — never a joke, never bait, never a question that "
    "invites mocking the people in it.\n\n"
    "Output ONLY the comment, without quotation marks."
)


def pinned_comment(text: str, model: str | None = None) -> str:
    """A pinned-comment suggestion for one account's finished caption `text`.

    One searchless call (`IG_CAPTION_VARIANT_MODEL`). Returns "" when it isn't
    wanted or possible — no text, the kill switch off, no API key, a failed or
    empty call, or an answer far over the length a pinned comment should have.
    Never raises; blocking, async callers go through asyncio.to_thread.
    """
    text = (text or "").strip()
    if not text or not config.IG_CAPTION_ENABLED or _client is None:
        return ""
    try:
        resp = _client.chat.completions.create(
            model=model or config.IG_CAPTION_VARIANT_MODEL,
            max_tokens=MAX_TOKENS,
            messages=[
                {"role": "system", "content": _COMMENT_SYSTEM},
                {"role": "user", "content": text},
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
    return out


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
                         "in its own brands/<group>/<name>/style.json voice")
    ap.add_argument("--media", default="", metavar="PATH",
                    help="the clip or photo this caption is for — analysed "
                         "first, so the search is driven by what it shows "
                         "instead of by the headline's words")
    ap.add_argument("--info", default="", metavar="TEXT",
                    help="the operator's info: the source and the facts "
                         "already known, as an `info:` reply would give it")
    args = ap.parse_args()

    headline = " ".join(args.headline)
    footage = vision.describe(args.media, headline) if args.media else {}
    if args.media:
        print("--- what the media shows ---")
        print(vision.as_prompt(footage) or "(no analysis — see the log above)")
        print()
    shared = verify_hashtags(
        expand(headline, footage, model=args.model, info=args.info))
    if args.brands:
        from shared import branding
        fake = [{"name": n, "lang": args.lang,
                 "style": branding.load_writing_style(config.brand_dir(n))}
                for n in args.brands.split(",") if n.strip()]
    else:
        fake = [{"name": f"account{i + 1}", "lang": args.lang if i else ""}
                for i in range(max(1, args.accounts))]
    names = [b["name"] for b in fake]
    for entry in plan(fake, seed_for(headline)):
        text = (rephrase(shared, entry["angle"], entry["lang"], entry["style"])
                if (entry["angle"] or entry["style"]) else shared)
        if len(fake) > 1:
            print(f"--- {entry['name']}: "
                  f"{'its own voice' if entry['style'] else entry['angle'] or 'the shared caption'}"
                  f" ---")
        body = pick_hashtags(text, entry["offset"], brand=entry["name"],
                             others=names)
        print(compose(headline, body) if shared
              else with_brand_tag(headline, entry["name"]))
        print()
