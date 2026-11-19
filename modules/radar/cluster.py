"""Which incident does a new item belong to? Pure — the store does the I/O.

Five channels posting the same robbery must become ONE alert, not five. An
item joins an open incident (updated within CLUSTER_WINDOW_H) when any of:

  1. it is the same video (same file size — a repost; checked by the caller
     through `Store.same_video`, it's a DB lookup);
  2. the AI put it in the same place AND category ("fire" + "Brooklyn");
  3. their words overlap enough (`MIN_OVERLAP` of the shorter one's content
     words, with at least `MIN_SHARED` words in common so two 3-word posts
     sharing "fire" don't merge).
"""
from modules.radar import text as T

MIN_OVERLAP = 0.5
MIN_SHARED = 3


def _norm(s) -> str:
    return (s or "").strip().lower()


def matches(item_tokens: set, label: dict | None, incident) -> bool:
    if incident["kind"] != "post":
        return False
    if label and _norm(label.get("place")) and _norm(label.get("category")):
        if (_norm(label.get("place")) == _norm(incident["place"])
                and _norm(label.get("category")) == _norm(incident["category"])):
            return True
    inc_tokens = set((incident["tokens"] or "").split())
    shared = item_tokens & inc_tokens
    return len(shared) >= MIN_SHARED and T.overlap(item_tokens, inc_tokens) >= MIN_OVERLAP


def pick(item_tokens: set, label: dict | None, incidents: list):
    """The best matching open incident (most shared words), or None."""
    best, best_n = None, -1
    for inc in incidents:
        if matches(item_tokens, label, inc):
            n = len(item_tokens & set((inc["tokens"] or "").split()))
            if n > best_n:
                best, best_n = inc, n
    return best


# An incident's word set grows with every post; cap it so one huge incident
# doesn't end up matching everything.
MAX_TOKENS = 60


def merge_tokens(old: str, new: set) -> set:
    """The first posts' words stay; later ones only fill up to the cap."""
    merged = set((old or "").split())
    for w in sorted(new - merged):
        if len(merged) >= MAX_TOKENS:
            break
        merged.add(w)
    return merged
