"""
modules/newsroom/pace.py — how often one channel may post.

The client's rule is one article per channel per 24 hours: a WordPress site
that publishes five stories in an afternoon must not empty itself into the
Telegram channel behind it. `main.tick()` asks this module whether a channel's
window is open BEFORE it rewrites anything, so a throttled tick costs no model
call.

Two things about the jitter are worth knowing, because both are easy to get
wrong in the obvious way:

  It is ADDED, never subtracted. NR_MIN_INTERVAL_H is the promise made to the
  client and a hard floor; the jitter only ever pushes the next post later, so
  "at most one in 24 h" cannot be broken by an unlucky draw.

  It is DERIVED, not drawn. A fresh random on every poll would let the channel
  post on whichever draw happened to come in lowest — at NR_POLL_S=300 that is
  288 draws a day against a 0-3 h spread, which collapses the wait back to the
  floor and throws the jitter away. Hashing (chat_id, last post) instead gives
  ONE stable answer per window: the same across polls, the same across
  restarts, and different per channel, so seven channels do not drift into
  posting in lockstep.

Pure — no I/O, no config reads, and no clock of its own (the caller passes
`now`), which is what makes the whole rule testable offline.
"""

import hashlib
from datetime import datetime, timezone

_HOUR_S = 3600.0


def _aware(value) -> datetime | None:
    """A stored ISO timestamp (or a datetime) as an aware UTC datetime.

    Returns None for anything unparseable. Callers treat that as "no last
    post", i.e. fail OPEN: a single malformed row must not wedge a client's
    channel forever, and the post that follows writes a good timestamp."""
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def jitter_seconds(chat_id: str, last_posted_iso: str, jitter_h: float) -> float:
    """A stable extra wait in [0, jitter_h) hours for this channel's window.

    Keyed on the last post, so it changes once per window and not once per
    poll. 0 (or a non-positive jitter_h) disables it."""
    if jitter_h <= 0:
        return 0.0
    seed = f"{chat_id}|{last_posted_iso}".encode("utf-8")
    frac = int.from_bytes(hashlib.sha256(seed).digest()[:8], "big") / float(1 << 64)
    return frac * jitter_h * _HOUR_S


def cooldown_remaining(chat_id: str, last_posted_iso, now: datetime,
                       min_interval_h: float, jitter_h: float) -> float:
    """Seconds this channel must still wait. 0.0 means it may post now."""
    if not last_posted_iso:
        return 0.0  # never posted — the window is open
    last = _aware(last_posted_iso)
    if last is None:
        return 0.0
    now = _aware(now) or datetime.now(timezone.utc)
    wait = max(0.0, min_interval_h) * _HOUR_S + jitter_seconds(
        chat_id, last_posted_iso, jitter_h)
    return max(0.0, wait - (now - last).total_seconds())


def format_wait(seconds: float) -> str:
    """A wait as "18h42m" / "42m", for the one line each throttled tick logs."""
    minutes = int(max(0.0, seconds)) // 60
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"
