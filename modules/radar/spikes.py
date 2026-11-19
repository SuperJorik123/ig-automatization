"""Spike detection — "this word suddenly shows up far more than usual".

World Monitor's recipe, on our own sources: every item's content words are
counted per hour and per source (`Store.add_terms`). A word SPIKES when, in the
last SPIKE_WINDOW_H hours,

  * it was used at least SPIKE_MIN_HITS times,
  * by at least SPIKE_MIN_SOURCES different sources (one spammy channel can't
    spike anything), and
  * at least SPIKE_RATIO x what the past 7 days would predict for that window.

With less than a day of history there is no baseline yet, so the bar is
doubled instead of every word looking new. A spiked word is quiet for
SPIKE_COOLDOWN_H.

"Lyon" twice a day, then 15 times in an hour from 5 channels -> alert, even
before anyone has posted a video.
"""
from modules.radar import settings as S

MAX_PER_CHECK = 3
MIN_HISTORY_H = 24


def check(store, now: float) -> list:
    hour_now = int(now // 3600)
    win_from = hour_now - S.SPIKE_WINDOW_H + 1
    base_from = hour_now - 7 * 24
    first = store.first_hour()
    if first is None:
        return []
    history_h = win_from - max(first, base_from)
    found = []
    for row in store.term_window(win_from):
        hits, sources, term = row["hits"], row["sources"], row["term"]
        if sources < S.SPIKE_MIN_SOURCES:
            continue
        if history_h < MIN_HISTORY_H:
            if hits < 2 * S.SPIKE_MIN_HITS:
                continue
            expected = 0.0
        else:
            if hits < S.SPIKE_MIN_HITS:
                continue
            base = store.term_baseline(term, max(first, base_from), win_from)
            expected = base / history_h * S.SPIKE_WINDOW_H
            if hits < S.SPIKE_RATIO * max(expected, 1.0):
                continue
        last = store.spike_at(term)
        if last and now - last < S.SPIKE_COOLDOWN_H * 3600:
            continue
        found.append({"term": term, "hits": hits, "sources": sources,
                      "expected": round(expected, 1),
                      "ratio": hits / max(expected, 1.0)})
    found.sort(key=lambda s: s["ratio"], reverse=True)
    return found[:MAX_PER_CHECK]
