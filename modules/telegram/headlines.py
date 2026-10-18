"""
modules/telegram/headlines.py — five headline options for a post, written
from the operator's `info:` reply.

The headline is the one line every platform carries: it is burned into the
render's lower-third banner and it is the post's first frame on a muted feed.
So when the operator replies `info: …` to a plan card, the bot asks a strong
model (`HEADLINE_MODEL`, default Claude Opus via OpenRouter) for five options
written for Instagram growth on a news account, and the card offers them as
buttons 1-5. The sixth option is the one that always existed: reply to the
card with your own headline.

Facts come from the info (and, for a video, the vision report of what the
clip shows) and nowhere else — no web search, same as the caption.

`propose(info, footage, current)` never raises: no key, a dead gateway, prose
instead of JSON all return None, and the card simply shows no options.
Blocking; the bot calls it through asyncio.to_thread.

CLI, for tuning the prompt:
    py modules/telegram/headlines.py "info text" [--headline "current"]
"""

import json
import logging
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from openai import OpenAI  # noqa: E402

from shared import config, vision  # noqa: E402

log = logging.getLogger(__name__)

_client = (
    OpenAI(base_url="https://openrouter.ai/api/v1",
           api_key=config.OPENROUTER_API_KEY)
    if config.OPENROUTER_API_KEY
    else None
)

N_OPTIONS = 5

# The banner wraps a headline onto at most three rows of a fixed-width box
# (shared/branding.py); past this it starts shrinking into unreadability.
MAX_CHARS = 90

# Room for a reasoning model to think and still answer; the answer is ~300
# tokens. Sent for the same reason caption.py sends one: OpenRouter reserves
# credit for the worst case up front.
MAX_TOKENS = 4000

_SYSTEM = (
    "You write the on-screen headlines for a fast-growing Instagram news "
    "account. The headline is burned into the video or photo as a banner and "
    "is the first thing a scrolling viewer reads, usually with the sound off. "
    "Its job is to stop the scroll and make people watch to the end, share "
    "and save — without ever misleading them, because a news account lives on "
    "being trusted.\n\n"
    f"Write {N_OPTIONS} DIFFERENT headline options for the story you are "
    "given. Make them genuinely different approaches, not rewordings: e.g. "
    "the stakes, the most striking detail, the human angle, the number or "
    "consequence, the open question the footage answers.\n\n"
    "WHAT MAKES A HEADLINE THAT GROWS A NEWS ACCOUNT:\n"
    "  Specific beats generic. Name the action, the place or the person's "
    "role, and the stakes — a concrete noun and a strong verb. Never \"wait "
    "for it\", \"you won't believe\", \"watch what happens\", \"shocking\", "
    "\"insane\".\n"
    "  Open a curiosity gap the footage closes: state what is at stake and "
    "leave the outcome unanswered when the clip has a payoff, so people "
    "watch to the end. Never spoil the payoff, and never promise anything "
    "the media does not deliver.\n"
    "  It must work muted: the text alone creates the question.\n"
    "  Front-load the hook: the first three words carry the story.\n"
    f"  Short: 5 to 12 words, at most {MAX_CHARS} characters. Short, common "
    "words. Correct English. No hashtags, no quotation marks around the "
    "whole line, no ALL CAPS words, no trailing full stop.\n"
    "  ABSOLUTELY NO EMOJI — not one, in any headline, for any story, "
    "however playful. No emoji, no emoticons, no pictographs, no symbols "
    "(★ ✓ → ❗), no keycap digits. Letters, digits and ordinary punctuation "
    "only. The banner's font cannot draw them, so an emoji prints as an "
    "empty box on the video. A headline containing one is a failed "
    "headline.\n\n"
    "TONE SWITCH:\n"
    "  Funny, wholesome or lucky-escape stories: playful and warm is fine — "
    "in the words, never with emoji.\n"
    "  Anyone seriously hurt or killed, violence, disaster, a crime: a "
    "straight news headline. No teasing hook, no jokes, no curiosity bait — "
    "state what happened, soberly.\n\n"
    "ACCURACY — every fact comes from the operator's info, the source "
    "post, the media notes or the current headline you are given; where they "
    "disagree, the operator's info wins, then the headline, then the source "
    "post. The media notes are on-screen text and speech only. You have NO web access "
    "and must not add anything from memory. If the info is an outlet's or "
    "someone else's claim of a death, an injury, a crime or another serious "
    "fact that is not confirmed, do not state it as fact: attribute it "
    "(\"Police say…\", \"Reports claim…\") and say so in the notes.\n"
    "SAFETY AND REACH — describe actions, never race or ethnicity. Censor "
    "profanity (f***). Never name a private individual the info does not "
    "name.\n\n"
    "Answer with ONE JSON object and nothing else:\n"
    "{\"headlines\": [five strings], \"pick\": <0-based index of your top "
    "pick>, \"why\": \"one short line on why it wins\", \"notes\": \"one "
    "short line for the operator — a claim to verify before posting, or a "
    "moment to cut or blur to avoid Instagram's sensitive-content screen; "
    "empty string when there is nothing\"}"
)


def _user_prompt(info: str, footage: dict | None, current: str,
                 source: str = "") -> str:
    parts = [f"OPERATOR'S INFO (the source of truth):\n{info or '(none)'}"]
    if source:
        parts.append("SOURCE POST (the post the clip came from — facts only, "
                     f"never its opinions):\n{source}")
    if footage and vision.as_prompt(footage):
        parts.append(vision.as_prompt(footage))
    if current:
        parts.append(f"CURRENT HEADLINE (the operator's draft or the source "
                     f"caption — improve on it, don't copy it):\n{current}")
    parts.append(f"Write the {N_OPTIONS} headline options.")
    return "\n\n".join(parts)


_FENCE = re.compile(r"^```[a-zA-Z]*\s*\n?(.*?)\n?```$", re.DOTALL)


# Emoji and pictographic symbols, plus what glues them together (ZWJ,
# variation selectors, skin tones, keycap mark, tag characters). The prompt
# forbids them; this is the guarantee, because the banner font has no glyph
# for any of them. Letters and ordinary punctuation are outside every range.
_EMOJI = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # mahjong … symbols & pictographs extended-A
    "\U0001FC00-\U0001FFFF"
    "←-⇿"           # arrows
    "⌀-⏿"           # misc technical (⌚ ⏰ ⏩)
    "①-⓿"           # enclosed alphanumerics (①)
    "■-➿"           # shapes, misc symbols, dingbats (★ ✓ ❗)
    "⤀-⥿"           # supplemental arrows
    "⬀-⯿"           # misc symbols and arrows (⭐ ⬆)
    "〰〽㊗㊙"
    "︀-️"           # variation selectors
    "‍⃣"            # zero-width joiner, keycap
    "\U000E0020-\U000E007F"   # tag characters (flag sequences)
    "]+")


def strip_emoji(text: str) -> str:
    """`text` without any emoji or pictographic symbol, spaces re-collapsed."""
    return " ".join(_EMOJI.sub(" ", text or "").split())


def _tidy(line: str) -> str:
    """One headline as the banner should get it: one line, no emoji, no
    wrapping quotes, no trailing full stop."""
    line = strip_emoji(str(line or ""))
    if len(line) >= 2 and line[0] in "\"'“‘" and line[-1] in "\"'”’":
        line = line[1:-1].strip()
    if line.endswith(".") and not line.endswith("..."):
        line = line[:-1].rstrip()
    return line


def parse(text: str) -> dict | None:
    """The model's answer as {headlines, pick, why, notes}, or None when it
    holds no usable headline. Tolerates a code fence and prose around the
    JSON; drops empty and duplicate options; a pick that doesn't point at a
    kept option falls back to the first."""
    text = (text or "").strip()
    m = _FENCE.match(text)
    if m:
        text = m.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("headlines"), list):
        return None

    raw = data["headlines"]
    try:
        pick_src = int(data.get("pick", 0))
    except (TypeError, ValueError):
        pick_src = 0
    heads, pick, seen = [], 0, set()
    for i, h in enumerate(raw):
        h = _tidy(h)
        if not h or h.lower() in seen:
            continue
        if i == pick_src:
            pick = len(heads)
        seen.add(h.lower())
        heads.append(h)
        if len(heads) == N_OPTIONS:
            break
    if not heads:
        return None
    return {"headlines": heads, "pick": pick,
            "why": _tidy(data.get("why", "")),
            "notes": " ".join(str(data.get("notes") or "").split())}


def propose(info: str, footage: dict | None = None, current: str = "",
            source: str = "", model: str | None = None) -> dict | None:
    """Five headline options for the story in `info` — or, with no info, in
    the `source` post the clip came from — or None. Never raises."""
    info = (info or "").strip()
    source = (source or "").strip()[:1500]
    if not info and not source:
        return None
    if _client is None:
        log.warning("OPENROUTER_API_KEY not set — no headline options")
        return None
    try:
        resp = _client.chat.completions.create(
            model=model or config.HEADLINE_MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "system", "content": _SYSTEM},
                      {"role": "user", "content": _user_prompt(
                          info, footage, (current or "").strip(), source)}],
        )
        out = parse(resp.choices[0].message.content or "")
    except Exception as exc:
        log.error("headline options failed: %s", exc)
        return None
    if out is None:
        log.warning("headline options came back unusable")
    return out


def card_lines(options: dict | None, chosen: int | None = None) -> list:
    """The block the plan card shows: numbered options, the model's pick
    starred, the chosen one marked, then its reason and any warning."""
    if not options:
        return []
    lines = [f"✨ Headlines — tap 1-{len(options['headlines'])}, "
             "or reply with your own:"]
    for i, h in enumerate(options["headlines"]):
        mark = "● " if i == chosen else ""
        star = " ⭐" if i == options["pick"] else ""
        lines.append(f"{i + 1}. {mark}{h}{star}")
    if options.get("why"):
        lines.append(f"⭐ {options['why']}")
    if options.get("notes"):
        lines.append(f"⚠️ {options['notes']}")
    return ["\n".join(lines)]


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("info")
    ap.add_argument("--headline", default="")
    ap.add_argument("--model", default=None,
                    help=f"override HEADLINE_MODEL ({config.HEADLINE_MODEL})")
    a = ap.parse_args()
    res = propose(a.info, current=a.headline, model=a.model)
    print("\n".join(card_lines(res)) if res else "(no options)")
