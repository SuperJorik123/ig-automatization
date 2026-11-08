"""
modules/telegram/spellfix.py — fix typos in the headline before it is burned in.

The headline the operator types goes straight into the banner of every render
(and from there into each brand's translation and the IG caption's hook), so a
"goverment" typed in a hurry ships thirteen times. One cheap OpenRouter call
(`SPELLFIX_MODEL`, default openai/gpt-5.6-luna) runs right before the render
and returns the same phrase with spelling and grammar mistakes fixed — nothing
reworded, nothing added.

The reply is NOT trusted blindly: a model that answers instead of correcting
("Sure! Here is…"), rephrases, or translates would put its own words on the
video. `accept` keeps the correction only when it is still recognisably the
same phrase (similarity ≥ MIN_SIMILARITY, length within MAX_LEN_DRIFT); anything
else keeps the original.

Never raises: no key, an outage, an empty or unusable reply all return the
headline untouched. `HEADLINE_SPELLFIX=0` turns it off.

CLI: py modules/telegram/spellfix.py "Goverment anounces new tax"
"""
import argparse
import difflib
import logging
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from openai import OpenAI  # noqa: E402

from shared import config  # noqa: E402

log = logging.getLogger(__name__)

_client = (
    OpenAI(base_url="https://openrouter.ai/api/v1",
           api_key=config.OPENROUTER_API_KEY)
    if config.OPENROUTER_API_KEY
    else None
)

ENABLED = os.environ.get("HEADLINE_SPELLFIX", "1").strip().lower() not in (
    "0", "false", "no", "off")
# gpt-5.6-luna, not the translator's gemini-2.5-flash: tested 2026-11-01,
# flash title-cased headlines and "fixed" Zelensky -> Zelenskyy despite the
# prompt; luna (and claude-haiku-5.5) changed only the typos.
MODEL = os.environ.get("SPELLFIX_MODEL", "").strip() or "openai/gpt-5.6-luna"

# A typo fix changes a few characters; a rewrite changes many. 0.75 lets
# several fixes through in a short headline ("Goverment anounces new tax" ->
# "Government announces new tax" is ~0.96) and stops a reworded one.
MIN_SIMILARITY = 0.75
MAX_LEN_DRIFT = 0.25
# Short answer; the headroom is for reasoning models. OpenRouter reserves
# credit for max_tokens up front, so it stays modest.
MAX_TOKENS = 1000

_SYSTEM = (
    "Correct any spelling and grammatical mistakes in the phrase the user "
    "sends. It is a news headline. Keep the same language, the same words, "
    "the same word order, capitalisation style and punctuation wherever they "
    "are already correct. Do not rephrase, shorten, translate, add or remove "
    "information. Never change the spelling of names of people, places, "
    "teams or brands — a name usually has several valid spellings, and the "
    "one used is deliberate. If "
    "the phrase has no mistakes, return it unchanged. Reply with ONLY the "
    "corrected phrase - no quotes, no explanation, nothing else."
)

_QUOTES = "\"'“”‘’«»`"


def _clean(reply: str) -> str:
    """First non-empty line of the reply, outer quotes / code fences off."""
    text = (reply or "").strip().strip("`").strip()
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if len(line) >= 2 and line[0] in _QUOTES and line[-1] in _QUOTES:
        line = line[1:-1].strip()
    return re.sub(r"\s+", " ", line)


def accept(original: str, corrected: str) -> str:
    """`corrected` when it is still the same phrase with small fixes, else
    `original`."""
    corrected = _clean(corrected)
    if not corrected or corrected == original:
        return original
    if abs(len(corrected) - len(original)) > MAX_LEN_DRIFT * max(len(original), 1):
        log.warning("spellfix reply rejected (length): %r -> %r", original, corrected)
        return original
    ratio = difflib.SequenceMatcher(None, original.lower(), corrected.lower()).ratio()
    if ratio < MIN_SIMILARITY:
        log.warning("spellfix reply rejected (similarity %.2f): %r -> %r",
                    ratio, original, corrected)
        return original
    return corrected


def correct(headline: str, model: str | None = None) -> str:
    """The headline with typos fixed, or the headline itself. Never raises."""
    text = (headline or "").strip()
    if not text or not ENABLED or not re.search(r"[^\W\d_]{2,}", text):
        return headline
    if _client is None:
        log.warning("OPENROUTER_API_KEY not set — headline not spell-checked")
        return headline
    # A multi-line headline is the operator's own banner layout: fix each line
    # on its own so the line breaks survive.
    lines = text.split("\n")
    try:
        out = []
        for line in lines:
            if not line.strip():
                out.append(line)
                continue
            resp = _client.chat.completions.create(
                model=model or MODEL,
                max_tokens=MAX_TOKENS,
                messages=[{"role": "system", "content": _SYSTEM},
                          {"role": "user", "content": line.strip()}],
            )
            out.append(accept(line.strip(), resp.choices[0].message.content or ""))
    except Exception as exc:
        log.warning("spellfix failed, keeping the headline as typed: %s", exc)
        return headline
    fixed = "\n".join(out)
    return fixed if fixed != text else headline


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("headline")
    ap.add_argument("--model")
    args = ap.parse_args()
    print(correct(args.headline, args.model))


if __name__ == "__main__":
    main()
