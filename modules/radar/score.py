"""Incident score 0-100+ — pure rules on top of the AI's `newsworthy`.

Only incidents WITH a video (or a hazard) ever alert; a text-only story shows
up through spikes instead. Whether one alerts is `passes` — the AI's verdict;
`score` only ranks (briefing order, the number on the alert). Every Telegram
video arrives fresh and as a video, so those +20 used to be free points that
let talking heads through (2026-11-23).
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


def passes(labels: list, us: bool) -> bool:
    """The AI is the gate: some post of the incident is a real event rated
    AI_MIN+ (AI_MIN_US+ when it happened in the US). No label = no alert."""
    real = [l.get("newsworthy", 0) for l in labels if l and l.get("incident")]
    if not real:
        return False
    best = max(real)
    return best >= S.AI_MIN or (us and best >= S.AI_MIN_US)


def blocked(category: str) -> bool:
    return bool(category) and category in S.BLOCK_CATEGORIES
