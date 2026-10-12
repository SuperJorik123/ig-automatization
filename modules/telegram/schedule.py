"""
modules/telegram/schedule.py — the pure half of scheduled publishing.

The publish picker (after 🎬 Render) has a 🕒 Schedule button next to
🚀 Publish: the renders are already made, so scheduling only decides WHEN the
ticked platforms get them. The time is a preset button (+1h, tomorrow 09:00,
…) or a reply to the picker ("18:30", "at 14.10 09:00", "+2h", "tomorrow
9:00"). Times are Moldova time (SCHEDULE_TZ, default Europe/Chisinau).

news_bot owns the I/O: it stores the post in queue_store's `scheduled`
table (so a restart doesn't lose it), moves the renders out of the nightly
sweep's reach, and fires it from the JobQueue. Everything here is pure and
offline-testable, like branded.py.

Callback namespace: "b:sched" opens the time picker, "b:at:<unix ts>" picks a
preset, "b:back" returns to the platform picker — all on the publish picker,
so under news_bot's "b:" router. "s:<id>:x" cancels a scheduled post; it is
routed on the DB row, not on in-memory state, so it works after a restart.
"""

import datetime as dt
import logging
import os
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.environ.get("SCHEDULE_TZ", "").strip() or "Europe/Chisinau")
except Exception as exc:  # no tz database on the box
    log.warning("schedule timezone unavailable (%s) — using UTC", exc)
    TZ = dt.timezone.utc

# Nothing closer than this (a "schedule" for 20 s from now is a publish), and
# nothing further out than MAX_AHEAD — the renders sit on disk until then.
MIN_AHEAD = dt.timedelta(minutes=1)
MAX_AHEAD = dt.timedelta(days=30)

_REL = re.compile(r"^(?:in\s+|\+)\s*(?:(\d+)\s*h(?:ours?|rs?)?)?\s*"
                  r"(?:(\d+)\s*m(?:in(?:utes?)?)?)?$")
_CLOCK = re.compile(r"^(?:at\s+)?(?:(today|tomorrow)\s+)?(?:at\s+)?"
                    r"(\d{1,2})[:.](\d{2})$")
_DATED = re.compile(r"^(?:at\s+|on\s+)?(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?"
                    r"\s+(?:at\s+)?(\d{1,2})[:.](\d{2})$")


def now() -> dt.datetime:
    return dt.datetime.now(TZ)


def parse_when(text: str, current: dt.datetime | None = None) -> dt.datetime | None:
    """A reply as a tz-aware moment in TZ, or None when it isn't a time.

    "18:30" / "at 18:30"      today, or tomorrow when that has passed
    "tomorrow 9:00"           tomorrow
    "14.10 18:30"             that date this year (next year when passed)
    "14.10.2026 18:30"        that date
    "+2h", "+90m", "in 1h30m" relative

    Range-checked against MIN_AHEAD/MAX_AHEAD by `check`, not here.
    """
    current = current or now()
    t = " ".join((text or "").lower().split())
    if not t:
        return None

    m = _REL.match(t)
    if m and (m.group(1) or m.group(2)):
        return current + dt.timedelta(hours=int(m.group(1) or 0),
                                      minutes=int(m.group(2) or 0))

    m = _CLOCK.match(t)
    if m:
        day, hh, mm = m.group(1), int(m.group(2)), int(m.group(3))
        if hh > 23 or mm > 59:
            return None
        when = current.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if day == "tomorrow":
            when += dt.timedelta(days=1)
        elif day is None and when <= current:
            when += dt.timedelta(days=1)
        return when

    m = _DATED.match(t)
    if m:
        d, mo, y, hh, mm = (m.group(1), m.group(2), m.group(3),
                            int(m.group(4)), int(m.group(5)))
        year = int(y) + (2000 if y and len(y) == 2 else 0) if y else current.year
        try:
            when = dt.datetime(year, int(mo), int(d), hh, mm, tzinfo=TZ)
        except ValueError:
            return None
        if not y and when <= current:
            when = when.replace(year=year + 1)
        return when
    return None


def check(when: dt.datetime, current: dt.datetime | None = None) -> str:
    """Why `when` can't be scheduled, or "" when it can."""
    current = current or now()
    if when < current + MIN_AHEAD:
        return "that time is in the past (or under a minute away)"
    if when > current + MAX_AHEAD:
        return f"more than {MAX_AHEAD.days} days ahead"
    return ""


def fmt(when: dt.datetime) -> str:
    """'Tue 14 Oct, 18:30' in TZ."""
    return when.astimezone(TZ).strftime("%a %d %b, %H:%M").replace(" 0", " ")


def fmt_in(when: dt.datetime, current: dt.datetime | None = None) -> str:
    """'in 3 h 20 min' — how far off it is."""
    mins = max(0, round(((when - (current or now())).total_seconds()) / 60))
    h, m = divmod(mins, 60)
    d, h = divmod(h, 24)
    parts = ([f"{d} d"] if d else []) + ([f"{h} h"] if h else []) + \
        ([f"{m} min"] if m or not (d or h) else [])
    return "in " + " ".join(parts)


def presets(current: dt.datetime | None = None) -> list:
    """(label, moment) pairs for the time-picker buttons. Absolute moments,
    so a tap a minute after the picker opened still means the time it shows."""
    current = (current or now()).replace(second=0, microsecond=0)
    out = [(f"+{h}h · {(current + dt.timedelta(hours=h)):%H:%M}",
            current + dt.timedelta(hours=h)) for h in (1, 3, 6)]
    tomorrow = current + dt.timedelta(days=1)
    for label, base, hh in (("Today 18:00", current, 18),
                            ("Today 21:00", current, 21),
                            ("Tomorrow 09:00", tomorrow, 9),
                            ("Tomorrow 18:00", tomorrow, 18)):
        when = base.replace(hour=hh, minute=0)
        if when >= current + dt.timedelta(minutes=30):
            out.append((label, when))
    return out


def picker_keyboard(current: dt.datetime | None = None) -> InlineKeyboardMarkup:
    """Preset times two per row, then back to the platform picker."""
    btns = [InlineKeyboardButton(label, callback_data=f"b:at:{int(when.timestamp())}")
            for label, when in presets(current)]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton("⬅ Back", callback_data="b:back")])
    return InlineKeyboardMarkup(rows)


def picker_text(ticked: list) -> str:
    return "\n\n".join([
        f"🕒 Schedule {' · '.join(ticked)} for when?",
        "Tap a time, or reply to this message with one — 18:30 · tomorrow "
        "9:00 · 14.10 18:30 · +2h",
        f"Times are {getattr(TZ, 'key', 'UTC')} time."])


def scheduled_text(sched_id: int, when: dt.datetime, ticked: list,
                   headline: str = "") -> str:
    lines = [f"🕒 scheduled #{sched_id} for {fmt(when)} ({fmt_in(when)})",
             "→ " + " · ".join(ticked)]
    if headline:
        lines.append(f"📰 {headline}")
    lines.append("Nothing has gone out yet — ✕ cancels it.")
    return "\n\n".join(lines)


def cancel_keyboard(sched_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "✕ Cancel schedule", callback_data=f"s:{sched_id}:x")]])


# --------------------------------------------------------------------------- #
# What gets stored                                                            #
# --------------------------------------------------------------------------- #
#
# A post is stored by BRAND NAME, not by brand dict: the dicts carry account
# handles that the credentials files may change before the post fires, and
# the fire should use the accounts as they are then.


def to_payload(state: dict) -> dict:
    """The JSON-able part of a publish picker's state that _do_publish needs."""
    renders = state["renders"]
    index = {id(r): i for i, r in enumerate(renders)}
    return {
        "text": state.get("text", ""),
        "info": state.get("info", ""),
        "footage": state.get("footage") or {},
        "renders": [{"brand": r["brand"]["name"], "path": r["path"],
                     "headline": r["headline"], "kind": r.get("kind", "video")}
                    for r in renders],
        "platforms": [{"platform": p["platform"], "label": p["label"],
                       "renders": [index[id(r)] for r in p["renders"]]}
                      for p in state["platforms"]],
        "selected": sorted(state["sel_platforms"]),
    }


def from_payload(payload: dict, brands: list) -> tuple:
    """(state, missing): a state _do_publish can run, rebuilt against the
    brands as configured now. A brand that no longer exists is left out and
    named in `missing`; a platform it leaves empty disappears."""
    by_name = {b["name"]: b for b in brands}
    renders, keep, missing = [], {}, []
    for i, r in enumerate(payload["renders"]):
        b = by_name.get(r["brand"])
        if b is None:
            missing.append(r["brand"])
            continue
        keep[i] = len(renders)
        renders.append({"brand": b, "path": r["path"],
                        "headline": r["headline"], "kind": r.get("kind", "video")})
    platforms, selected = [], set()
    for j, p in enumerate(payload["platforms"]):
        rs = [renders[keep[i]] for i in p["renders"]
              if i in keep and renders[keep[i]]["brand"].get(p["platform"])]
        if not rs:
            continue
        if j in payload["selected"]:
            selected.add(len(platforms))
        platforms.append({"platform": p["platform"], "label": p["label"],
                          "renders": rs})
    state = {"text": payload.get("text", ""), "info": payload.get("info", ""),
             "footage": payload.get("footage") or {}, "renders": renders,
             "platforms": platforms, "sel_platforms": selected,
             "files": [r["path"] for r in payload["renders"]], "media": []}
    return state, sorted(set(missing))


def ticked_labels(platforms: list, selected) -> list:
    return [p["label"] for i, p in enumerate(platforms) if i in selected]
