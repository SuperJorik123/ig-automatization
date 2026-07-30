"""
modules/telegram/scorer.py — importance + region scoring for the smart filter.

One OpenRouter call per news item returns both the importance score (0-100)
and the regions the story matters to. The tier thresholds defined here are
what the dispatcher will read to decide which platforms an item goes to and
whether a human must approve it first.

Unlike the translator (which must never block a post), a scoring failure
returns None and the item simply stays status='new' — it gets retried on the
next pass rather than being published unscored.

CLI test (uses your real OPENROUTER_API_KEY):
    py modules/telegram/scorer.py Germany declares war on Poland
    py modules/telegram/scorer.py --hint ru Kremlin announces new tax policy
"""

import json
import logging
import os
import re
import sys

# Repo-root bootstrap for direct runs (`py modules/telegram/scorer.py ...`).
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from openai import OpenAI, APIError  # noqa: E402

from shared import config  # noqa: E402

log = logging.getLogger(__name__)

# The routing vocabulary. "global" means everyone cares (or the model is
# unsure) — dispatchers treat it as matching every channel.
REGIONS = ("us", "eu", "ru", "global")

# score → tier, checked top-down. The dispatcher maps tiers to platforms:
#   breaking → every platform, but a human approves first
#   high     → every platform, automatic
#   medium   → telegram + twitter
#   low      → telegram only
TIERS = (
    (85, "breaking"),
    (70, "high"),
    (40, "medium"),
    (0, "low"),
)


def tier(score: int) -> str:
    for floor, name in TIERS:
        if score >= floor:
            return name
    return "low"


_client = (
    OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=config.OPENROUTER_API_KEY,
    )
    if config.OPENROUTER_API_KEY
    else None
)

_SYSTEM = """You are the news desk editor of an international news network with audiences in the United States ("us"), Europe ("eu"), and Russia ("ru").

For the news item the user sends, reply with STRICT JSON only — no prose, no code fences:
{"score": <0-100>, "regions": ["..."]}

score — editorial importance:
  85-100  historic / breaking: wars declared, heads of state attacked, market crashes, major disasters, events that reshape world affairs
  70-84   major: national elections decided, big policy shifts, serious conflict escalations, economy-moving decisions
  40-69   solid everyday news: notable politics, business, world events clearly worth a post
  0-39    minor: local trivia, celebrity/influencer milestones, routine sports results, gossip, promotional content

regions — which audiences genuinely care: any of "us", "eu", "ru", or "global" when everyone would (or when unsure). Multiple allowed. Judge by the CONTENT, not the language it is written in.

The source hint below says where the item was collected from — it suggests a region but may be wrong; refine it from the content.
Source hint: """  # hint appended at call time (no .format — the JSON braces above would trip it)


def score(text: str, source_hint: str | None = None) -> dict | None:
    """Score one news item.

    Returns {"score": int 0-100, "regions": [...]} or None when scoring
    isn't possible (no key, API error, unparseable reply) — the caller
    leaves the item as 'new' and retries later.
    """
    if not (text or "").strip():
        return None
    if _client is None:
        log.warning("OPENROUTER_API_KEY not set — cannot score")
        return None

    try:
        resp = _client.chat.completions.create(
            model=config.SCORER_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM + (source_hint or "unknown")},
                {"role": "user", "content": text},
            ],
        )
    except APIError as exc:
        log.error("scoring failed: %s", exc)
        return None

    raw = (resp.choices[0].message.content or "").strip()
    # Some models wrap JSON in ```fences``` no matter what — strip them.
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    try:
        data = json.loads(raw)
        s = max(0, min(100, int(data["score"])))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        log.error("scorer returned unparseable output: %r", raw[:200])
        return None

    regions = [r for r in data.get("regions", []) if r in REGIONS]
    return {"score": s, "regions": regions or ["global"]}


_COMPARE_SYSTEM = """You are the news desk editor choosing the ONE story to publish right now on an international news channel. The numbered stories below are all already judged newsworthy; your job is to pick the single strongest one for this slot.

Prefer, in this order:
  1. the biggest real-world consequence — what most affects most people
  2. urgency: something the audience needs to know now rather than later
  3. broad interest over niche appeal
  4. a concrete, specific story over a vague or promotional one

Reply with STRICT JSON only — no prose, no code fences:
{"best": <the number of the story you pick>, "why": "<one short sentence>"}"""


def compare(stories: list) -> int | None:
    """Pick the strongest of several already-scored stories.

    `stories` is a list of texts, in the caller's order. Returns the index of
    the winner, or None when the comparison isn't possible (no key, API error,
    unparseable reply) — the caller then falls back to its own ordering, so a
    failure here degrades to score-ranking rather than blocking a post.

    Numeric scores tie constantly (a whole queue lands on 70-80), which is why
    the choice is made again, head-to-head, at the moment of publishing."""
    if not stories:
        return None
    if len(stories) == 1:
        return 0
    if _client is None:
        log.warning("OPENROUTER_API_KEY not set — cannot compare")
        return None

    listing = "\n\n".join(
        f"[{i}] {(t or '').strip()[:1200]}" for i, t in enumerate(stories)
    )
    try:
        resp = _client.chat.completions.create(
            model=config.SCORER_MODEL,
            messages=[
                {"role": "system", "content": _COMPARE_SYSTEM},
                {"role": "user", "content": listing},
            ],
        )
    except APIError as exc:
        log.error("comparison failed: %s", exc)
        return None

    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", (resp.choices[0].message.content or "").strip())
    try:
        data = json.loads(raw)
        best = int(data["best"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        log.error("comparison returned unparseable output: %r", raw[:200])
        return None
    if not 0 <= best < len(stories):
        log.error("comparison picked out-of-range story %d of %d", best, len(stories))
        return None
    log.info("comparison picked [%d]: %s", best, str(data.get("why", ""))[:200])
    return best


if __name__ == "__main__":
    args = sys.argv[1:]
    hint = None
    if args[:1] == ["--hint"] and len(args) >= 2:
        hint, args = args[1], args[2:]
    if not args:
        raise SystemExit('usage: py modules/telegram/scorer.py [--hint REGION] <news text>')
    result = score(" ".join(args), source_hint=hint)
    if result is None:
        raise SystemExit("scoring failed — check OPENROUTER_API_KEY / logs")
    print(json.dumps(result | {"tier": tier(result["score"])}, indent=2))
