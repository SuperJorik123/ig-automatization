"""What is LIKELY to make video in the coming days — for the evening briefing.

Two parts: US dates that reliably produce viral clips every year (computed,
so they never need updating), and `calendar.json` — your own one-off entries
(a big game, a verdict, an announced protest). Name has a trailing underscore
so it doesn't shadow the standard library's `calendar`.
"""
import datetime as dt
import json
import os

from modules.radar import settings as S


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """n-th (1-based) weekday (Mon=0) of a month; n=-1 = the last one."""
    if n > 0:
        d = dt.date(year, month, 1)
        d += dt.timedelta(days=(weekday - d.weekday()) % 7)
        return d + dt.timedelta(weeks=n - 1)
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - dt.timedelta(days=1)
    return d - dt.timedelta(days=(d.weekday() - weekday) % 7)


def recurring(year: int) -> list:
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    return [
        (dt.date(year, 1, 1), "New Year's Day — fireworks accidents, celebrations worldwide"),
        (_nth_weekday(year, 2, 6, 2), "Super Bowl Sunday — fan celebrations / street chaos"),
        (dt.date(year, 3, 17), "St. Patrick's Day — street crowds"),
        (dt.date(year, 7, 4), "July 4th — fireworks mishaps, street takeovers"),
        (dt.date(year, 10, 31), "Halloween — crowds, pranks"),
        (thanksgiving, "Thanksgiving — travel chaos, frying-turkey fires"),
        (thanksgiving + dt.timedelta(days=1), "Black Friday — store fights / stampedes"),
        (dt.date(year, 12, 25), "Christmas"),
        (dt.date(year, 12, 31), "New Year's Eve — countdowns, fireworks, crowds"),
    ]


def custom(path: str | None = None) -> list:
    path = path or S.CALENDAR_FILE
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    out = []
    for e in data.get("events", []):
        try:
            out.append((dt.date.fromisoformat(e["date"]), e["title"]))
        except (KeyError, ValueError):
            continue
    return out


def upcoming(today: dt.date, days: int = 7, path: str | None = None) -> list:
    """(date, title) within [today, today+days], sorted."""
    end = today + dt.timedelta(days=days)
    allx = recurring(today.year) + recurring(today.year + 1) + custom(path)
    return sorted((d, t) for d, t in allx if today <= d <= end)
