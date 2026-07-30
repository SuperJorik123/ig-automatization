"""
modules/telegram/smart_filter.py — the one filter every platform routes through.

Two responsibilities, deliberately kept apart because they change on different
schedules:

  evaluate()          the BRAIN — how good is this story, and who cares about it.
                      Today it is the demo implementation: one OpenRouter call
                      via scorer.py. The real filter (semantic dedup, source
                      trust, freshness decay, duplicate-story clustering)
                      replaces this function and every platform inherits it.

  *_targets()         per-platform POLICY — given a scored item and a list of
                      configured destinations, which ones actually get it.
                      "Is this important" and "does @news_ru want it" are
                      different questions; keeping them separate is what lets
                      the brain be swapped without touching the routing.

Nothing here talks to Telegram, YouTube or the database — it is pure decision
logic over dicts, which is also why it is the easiest part of the system to
test. Callers (dispatcher.py, autopilot.py) do the publishing.
"""

import logging

from shared import config
from modules.telegram import scorer

log = logging.getLogger(__name__)

# Re-exported so callers don't reach past this module into the scorer — the
# whole point is that the brain is replaceable behind this seam.
REGIONS = scorer.REGIONS
tier = scorer.tier

# The scorer's wildcard: an item tagged "global" matters everywhere, so it
# matches every channel regardless of that channel's own region.
GLOBAL = "global"


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
    """Choose which of several publishable stories actually goes out now.

    `pairs` is [(item, targets)] already ordered best-score-first. The top
    `TG_COMPARE_TOP` of them are compared head-to-head by the model and the
    winner is returned; everything below that cut is never considered.

    This is the second half of the filter, and it runs at PUBLISH time rather
    than collection time. Scoring happens once per story, in isolation, the
    moment it arrives — so a whole day's queue tends to sit on 70-80 with no
    way to tell those apart. Comparing the finalists against each other, when
    the slot is actually being filled, is what turns "good enough to post"
    into "the best thing we have right now".

    Falls back to the top-scoring story whenever the comparison can't run
    (comparison disabled, no API key, API error) — a failure here must never
    cost you the post."""
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


def matches(item_regions, dest: dict) -> bool:
    """Does this destination serve this item's audience?

    A destination with no regions configured is a CATCH-ALL — it receives
    everything that clears the score threshold. That keeps two-field
    TG_DESTINATIONS entries (`@chan:en`) behaving exactly as they did before
    regions existed."""
    chan = set(dest.get("regions") or ())
    if not chan:
        return True
    item = set(item_regions or ())
    return GLOBAL in item or bool(chan & item)


def has_video(item: dict) -> bool:
    return any(m.get("type") == "video" for m in item.get("media") or ())


def is_news(item: dict) -> bool:
    """Does this collected post qualify as news at all?

    A story must carry media — photo(s), video(s), or both. Text-only posts
    from a source channel are commentary, link dumps or announcements, not the
    kind of thing these channels publish, so they are dropped before they cost
    a scoring call. `NEWS_REQUIRE_MEDIA=0` in .env turns the rule off."""
    if not config.NEWS_REQUIRE_MEDIA:
        return True
    return bool(item.get("media"))


def telegram_targets(
    item: dict, dests: list | None = None, min_score: int | None = None
) -> list:
    """Telegram channels that should receive `item`.

    Policy: qualify as news (media required), clear the score floor, then
    match the channel's region."""
    if dests is None:
        dests = config.TG_DESTINATIONS
    if min_score is None:
        min_score = config.TG_AUTO_MIN_SCORE
    if not is_news(item) or (item.get("score") or 0) < min_score:
        return []
    return [d for d in dests if matches(item.get("regions"), d)]


def youtube_targets(
    item: dict, dests: list | None = None, min_score: int | None = None
) -> list:
    """YouTube channels that should receive `item`.

    Policy: a higher floor than Telegram (a Shorts upload costs 1600 quota
    units against a 10k/day budget) and a video is mandatory. Region is not
    consulted today — YT_DESTINATIONS are language-targeted, and every
    configured channel gets the qualifying videos."""
    if dests is None:
        dests = config.YT_DESTINATIONS
    if min_score is None:
        min_score = config.YT_AUTO_MIN_SCORE
    if (item.get("score") or 0) < min_score or not has_video(item):
        return []
    return list(dests)
