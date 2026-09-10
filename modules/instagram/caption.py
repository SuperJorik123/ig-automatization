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

CLI, for tuning the prompt against real headlines:
    py modules/instagram/caption.py "Released footage shows plane crash at Miami International Airport"
"""

import logging
import os
import re
import sys

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
    "HASHTAGS — one line, five to nine of them, lowercase, space-separated, "
    "letters and digits only inside each tag. Order them place, then the main "
    "actors or subject, then the topic, then the general ones (#news, "
    "#breakingnews). Tag places, institutions, countries and public events — "
    "never a private individual's name.\n\n"
    "Output ONLY the caption. No preamble, no explanation, no markdown, no "
    "bold, no bullet points, no surrounding quotation marks, no numbered "
    "citation markers, no source list at the end."
)

# A model that searched will sometimes bracket its sources inline despite the
# prompt ("… five people were killed [2].") — strip the markers rather than
# lose an otherwise good caption to them.
_CITATION = re.compile(r"[ \t]*\[\d{1,3}\](?=[\s.,;:!?)]|$)")

# ``` / ```text fences around the whole answer.
_FENCE = re.compile(r"^```[a-zA-Z]*\n(.*)\n```$", re.DOTALL)

# A "Caption:"-style label on its own first line.
_LABEL = re.compile(r"^(caption|instagram caption|post)\s*:\s*\n+", re.IGNORECASE)


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
    out = re.sub(r"\n{3,}", "\n\n", out)
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


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description="Expand a headline into an Instagram caption.")
    ap.add_argument("headline", nargs="+", help="the headline to expand")
    ap.add_argument("--model", default=None,
                    help=f"override IG_CAPTION_MODEL ({config.IG_CAPTION_MODEL})")
    args = ap.parse_args()

    print(expand(" ".join(args.headline), model=args.model))
