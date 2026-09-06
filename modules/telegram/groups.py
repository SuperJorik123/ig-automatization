"""
modules/telegram/groups.py — account GROUPS (GMN / JNN), the one thing both
of the news bot's pickers collapse behind.

Thirteen brands means thirteen rows in the brand picker and one row per
destination in the as-is picker; in practice a post goes to one whole family
of accounts. So both pickers open showing a row per GROUP and hide the full
list behind a "Custom…" button, which expands to exactly the keyboard that
was there before.

The membership lives in ONE place — `"group": "GMN"` in
`credentials/brands/<name>.json`, expanded to `BRAND_<NAME>_GROUP` and read
back into `brand["group"]` by shared/config.py. The as-is picker doesn't list
brands at all (it lists TG_/YT_/TW_DESTINATIONS), so `dest_groups` maps a
destination back to the brand that OWNS that handle — `@WsWire` in
TG_DESTINATIONS is the `wswire` brand's `tg` account, hence its group. A
destination no brand claims stays reachable through Custom (and "All").

Pure — no I/O, offline-testable, the same reason branded.py and reactions.py
exist. A group that would select nothing is never returned: an empty row is a
trap, and both keyboards fall back to the full list when nothing is left.
"""

# Group row prefixes: fully selected / partly selected / not selected. The
# middle one is why a group tick can't lie about what Custom would show.
MARK_ALL, MARK_SOME, MARK_NONE = "☑", "◪", "☐"

# Platform keys of a destination group, in picker order. Matches the
# sel_tg/sel_yt/sel_tw split in the as-is picker's state.
DEST_KEYS = ("tg", "yt", "tw")


def group_of(brand: dict) -> str:
    return (brand.get("group") or "").strip()


def _handle(value) -> str:
    """Account handles compare case-insensitively and without the @ — a brand
    stores `@WsWire`, YT_DESTINATIONS stores `wswiremedia`, and an operator
    typing either into .env must still match."""
    return (value or "").strip().lstrip("@").lower()


def brand_groups(brands: list) -> list:
    """[{"name": "GMN", "members": {index, …}}, …], alphabetical by group
    name. Indexes point into `brands`. Brands with no group, and brands
    without a logo.png (they can't be rendered — the picker shows them as
    disabled rows under Custom), are left out."""
    by_name: dict[str, set] = {}
    for i, b in enumerate(brands):
        name = group_of(b)
        if not name or not b.get("has_logo", True):
            continue
        by_name.setdefault(name, set()).add(i)
    return [{"name": n, "members": by_name[n]} for n in sorted(by_name)]


def dest_groups(brands: list, tg=(), yt=(), tw=()) -> list:
    """[{"name": "GMN", "tg": {index}, "yt": {…}, "tw": {…}}, …], alphabetical.
    Indexes point into the three destination lists as the as-is picker's
    sel_tg/sel_yt/sel_tw do. A destination is grouped when its chat_id matches
    the corresponding account handle of a grouped brand; the first brand
    claiming a handle wins. Groups that end up with nothing are dropped —
    unlike brand_groups this doesn't care about the logo, since posting as-is
    renders nothing."""
    owner: dict[tuple, str] = {}
    for b in brands:
        name = group_of(b)
        if not name:
            continue
        for key in DEST_KEYS:
            handle = _handle(b.get(key))
            if handle:
                owner.setdefault((key, handle), name)

    out: dict[str, dict] = {}
    for key, dests in zip(DEST_KEYS, (tg, yt, tw)):
        for i, d in enumerate(dests):
            handle = _handle(d.get("chat_id"))
            name = owner.get((key, handle)) if handle else None
            if not name:
                continue
            g = out.setdefault(name, {"name": name,
                                      **{k: set() for k in DEST_KEYS}})
            g[key].add(i)
    return [out[n] for n in sorted(out)]


def dest_count(group: dict) -> int:
    """How many destinations one group row covers, across all platforms — the
    number on the button, so the row never hides how much it selects."""
    return sum(len(group[k]) for k in DEST_KEYS)


def mark(selected: set, members: set) -> str:
    """Checkbox for a group row: every member selected, some, or none.
    Selection outside the group is irrelevant to it."""
    if members and members <= selected:
        return MARK_ALL
    if members & selected:
        return MARK_SOME
    return MARK_NONE


def _pairs(by_key) -> set:
    """{platform: indexes} -> flat {(platform, index)} so a group spanning TG,
    YT and X compares as one thing."""
    return {(k, i) for k in DEST_KEYS for i in (by_key.get(k) or ())}


def dest_mark(group: dict, selected: dict) -> str:
    """Checkbox for a destination group row. `selected` maps platform key ->
    the picker's selected index set (sel_tg / sel_yt / sel_tw)."""
    return mark(_pairs(selected), _pairs(group))


def toggle_dest_group(group: dict, selected: dict) -> dict:
    """Tap on a destination group row -> a NEW {platform: index set}. Same
    rule as the brand rows: a partly selected group fills up first, a full one
    clears, and destinations outside it are never touched."""
    members = _pairs(group)
    full = bool(members) and members <= _pairs(selected)
    return {k: ((set(selected.get(k) or ()) - group[k]) if full
                else (set(selected.get(k) or ()) | group[k]))
            for k in DEST_KEYS}
