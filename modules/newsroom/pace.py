"""
modules/newsroom/pace.py — when one channel may post.

One article per channel per calendar day (UTC), carrying THAT day's latest
article. The sites publish in a single burst of 2-5 articles lasting 5-25
minutes in the early morning; the channel behind each one gets exactly one of
them, and the client's subscribers see a current story every day.

Three rules. The first is the one that was asked for; the other two are what
make it actually behave that way:

  ONE PER DAY. `store.posted_on` is the whole gate — a post row stamped today
  stops the tick before the rewrite, which is the only billed call in the flow.

  ONLY TODAY'S ARTICLES ARE ELIGIBLE. Yesterday's leftovers (the ones that lost
  to the article that shipped, or an afternoon straggler that arrived after the
  day's post) are marked skipped, never posted. Without this the channel locks
  one day behind: the first tick after midnight finds yesterday's straggler,
  posts it, and today's burst then waits for tomorrow — forever.

  THE BURST MUST SETTLE. Posting the instant an article appears posts the FIRST
  of the morning's 2-5, not the latest. Nothing ships until the newest article
  has been quiet for the settle delay, so "the latest" means the day's last and
  not whichever one happened to be first through the door.

The settle delay is DERIVED, not drawn. A fresh random on every poll would let
the channel post on whichever draw came in lowest — at NR_POLL_S=300 that is
288 draws a day, which pins every post to the minimum and throws the variation
away. Hashing (chat_id, day) gives ONE stable answer per channel per day: the
same across polls, the same across restarts, and different per channel, so
seven channels do not post in lockstep.

Pure — no I/O, no config reads, and no clock of its own (the caller passes
`now`), which is what makes the whole rule testable offline.
"""

import hashlib
from datetime import datetime, timezone


def _aware(value) -> datetime | None:
    """An ISO timestamp (or a datetime) as an aware UTC datetime, else None."""
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def utc_day(value) -> str:
    """The UTC calendar day of a timestamp as "YYYY-MM-DD", "" if unreadable.

    UTC and not a site-local zone on purpose: every timestamp in this bot is
    already UTC, the bursts land mid-morning UTC, and the day boundary is
    therefore hours away from anything that actually happens. A per-site zone
    would buy nothing and would have to be right in seven places."""
    dt = _aware(value)
    return dt.date().isoformat() if dt else ""


def settle_seconds(chat_id: str, day: str, min_m: float, max_m: float) -> float:
    """How long the day's newest article must be quiet before it ships.

    A stable value in [min_m, max_m) minutes per (channel, day) — see the
    module docstring for why it is hashed rather than drawn."""
    lo, hi = sorted((max(0.0, min_m), max(0.0, max_m)))
    if hi <= lo:
        return lo * 60.0
    seed = f"{chat_id}|{day}".encode("utf-8")
    frac = int.from_bytes(hashlib.sha256(seed).digest()[:8], "big") / float(1 << 64)
    return (lo + frac * (hi - lo)) * 60.0


def hold_remaining(chat_id: str, newest_published, now: datetime,
                   min_m: float, max_m: float) -> float:
    """Seconds left before the day's newest article may ship. 0.0 = go.

    Measured from the NEWEST article, so a burst that is still arriving keeps
    resetting the wait and the last one through the door is the one posted."""
    published = _aware(newest_published)
    if published is None:
        return 0.0  # no usable date — post it rather than hold it forever
    now = _aware(now) or datetime.now(timezone.utc)
    settle = settle_seconds(chat_id, utc_day(published), min_m, max_m)
    return max(0.0, settle - (now - published).total_seconds())


def format_wait(seconds: float) -> str:
    """A wait as "1h05m" / "42m", for the one line each held tick logs."""
    minutes = int(max(0.0, seconds)) // 60
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"
