"""
modules/telegram/smart_filter.py — the news filter over the collector's queue.

  is_news()           the cheapest gate: a story must carry media.
  evaluate()          the BRAIN — how good is this story, and who cares about it.
                      Today it is the demo implementation: one OpenRouter call
                      via scorer.py. The real filter (semantic dedup, source
                      trust, freshness decay, duplicate-story clustering)
                      replaces this function and every caller inherits it.
  best_of()           head-to-head comparison of the strongest scored stories.

Nothing here talks to Telegram, YouTube or the database, and nothing here
publishes — it is pure decision logic over dicts, which is also why it is the
easiest part of the system to test. dispatcher.py runs is_news + evaluate
over every collected story.
"""

import logging

from shared import config
from modules.telegram import scorer

log = logging.getLogger(__name__)

# Re-exported so callers don't reach past this module into the scorer — the
# whole point is that the brain is replaceable behind this seam.
REGIONS = scorer.REGIONS
tier = scorer.tier


def evaluate(text: str, source_hint: str | None = None) -> dict | None:
    """Score one news item: {"score": 0-100, "regions": [...], "tier": str}.

    Returns None when the item can't be judged (no API key, API error,
    unparseable reply). The caller must leave such an item unscored and retry
    later — never publish it on the assumption that it's fine."""
    result = scorer.score(text, source_hint=source_hint)
    if result is None:
        return None
    return {**result, "tier": tier(result["score"])}


def best_of(pairs: list, top: int | None = None) -> tuple:
    """Choose the strongest of several scored stories.

    `pairs` is [(item, targets)] already ordered best-score-first. The top
    `TG_COMPARE_TOP` of them are compared head-to-head by the model and the
    winner is returned; everything below that cut is never considered.

    Scoring happens once per story, in isolation, the moment it arrives — so
    a whole day's queue tends to sit on 70-80 with no way to tell those apart.
    Comparing the finalists against each other is what turns "good enough"
    into "the best thing we have right now".

    Falls back to the top-scoring story whenever the comparison can't run
    (comparison disabled, no API key, API error)."""
    if not pairs:
        return None, None
    if top is None:
        top = config.TG_COMPARE_TOP
    shortlist = pairs[: max(1, top)]
    if len(shortlist) == 1:
        return shortlist[0]

    winner = scorer.compare([item.get("text") or "" for item, _ in shortlist])
    if winner is None:
        log.info("comparison unavailable — falling back to the top score (item %s)",
                 shortlist[0][0].get("id"))
        return shortlist[0]
    log.info("chose item %s over %d other candidate(s)",
             shortlist[winner][0].get("id"), len(shortlist) - 1)
    return shortlist[winner]


def is_news(item: dict) -> bool:
    """Does this collected post qualify as news at all?

    A story must carry media — photo(s), video(s), or both. Text-only posts
    from a source channel are commentary, link dumps or announcements, not the
    kind of thing these channels publish, so they are dropped before they cost
    a scoring call. `NEWS_REQUIRE_MEDIA=0` in .env turns the rule off."""
    if not config.NEWS_REQUIRE_MEDIA:
        return True
    return bool(item.get("media"))
