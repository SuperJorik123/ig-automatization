"""Which incident does a new item belong to? Pure — the store does the I/O.

Five channels posting the same robbery must become ONE alert, not five. An
item joins an open incident (updated within CLUSTER_WINDOW_H) when any of:

  1. it is the same video (same file size — a repost; checked by the caller
     through `Store.same_video`, it's a DB lookup);
  2. the AI put it in the same PLACE — a shared place word ("Benghazi port"
     and "Benghazi, Libya"; "Dymer, Kyiv Oblast" and "Dymer"), the country
     not disagreeing. Category is NOT required: the same Libyan port attack
     came labelled war / explosion / fire, and the same Sinaloa battle war /
     police / shooting / fire — 13 and 16 separate alerts in one weekend
     (2026-11-23). Generic words ("southern", "region", "port") and country
     names don't count as a place;
  3. their words overlap enough (`MIN_OVERLAP` of the shorter one's content
     words, with at least `MIN_SHARED` words in common so two 3-word posts
     sharing "fire" don't merge).
"""
from modules.radar import text as T

MIN_OVERLAP = 0.5
MIN_SHARED = 3


def _norm(s) -> str:
    return (s or "").strip().lower()


# Words in an AI "place" that don't name one.
_PLACE_FILLER = set("""
north south east west northern southern eastern western central near off coast coastal
city town village region oblast province governorate district county state area
port island bay gulf strait sea river border capital outskirts suburb downtown
the and of in on at de del la el al unknown none
""".split())


def place_words(place: str, *countries: str) -> set:
    drop = set()
    for c in countries:
        drop |= set(T.tokens(c))
    return {w for w in T.tokens(place) if w not in _PLACE_FILLER and w not in drop}


def _field(row, key: str) -> str:
    return (row[key] if key in row.keys() else "") or ""


def same_place(label: dict | None, incident) -> bool:
    if not label:
        return False
    inc_country = _field(incident, "country")
    a_c, b_c = _norm(label.get("country")), _norm(inc_country)
    if a_c and b_c and a_c not in ("unknown", "''") and b_c not in ("unknown", "''")             and a_c != b_c and not ({a_c, b_c} <= {"us", "usa", "united states"}):
        return False
    a = place_words(label.get("place"), label.get("country") or "", inc_country)
    b = place_words(_field(incident, "place"), label.get("country") or "", inc_country)
    return bool(a & b)


# A place alone groups a story for at most this long: Kyiv is hit most nights,
# and a new strike a day later is a new alert, not an edit of an old one.
PLACE_STORY_MAX_S = 24 * 3600


def _young(incident) -> bool:
    keys = incident.keys()
    if "created" not in keys or "updated" not in keys:
        return True
    return (incident["updated"] or 0) - (incident["created"] or 0) <= PLACE_STORY_MAX_S


def matches(item_tokens: set, label: dict | None, incident) -> bool:
    if incident["kind"] != "post":
        return False
    if same_place(label, incident) and _young(incident):
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
