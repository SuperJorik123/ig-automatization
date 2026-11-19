"""Incident score 0-100+ — pure rules on top of the AI's `newsworthy`.

Only incidents WITH a video (or a hazard) ever alert; a text-only story shows
up through spikes instead.
"""
from modules.radar import settings as S

FRESH_S = 30 * 60


def score(*, label: dict | None, has_video: bool, sources: int, newest_age_s: float,
          us: bool, velocity: float = 0.0) -> int:
    if label is None:
        base = 35            # no AI verdict (budget / outage): rules alone
    elif not label.get("incident"):
        base = min(label.get("newsworthy", 0), 30)
    else:
        base = label.get("newsworthy", 0)
    s = base
    if has_video:
        s += 10
    if newest_age_s <= FRESH_S:
        s += 10
    s += 5 * min(max(sources - 1, 0), 4)    # several sources at once
    if us:
        s += S.US_BONUS
    s += int(min(20, velocity * 10))         # Reddit: upvotes per minute
    return int(s)


def blocked(category: str) -> bool:
    return bool(category) and category in S.BLOCK_CATEGORIES
