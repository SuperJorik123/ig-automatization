"""
modules/telegram/branded.py — the Brand-it flow's pure pieces: the plan card
(defaults, summary lines, the card and its editor keyboards), which platforms
a set of renders can publish to, and the platform picker. Lifted out of
news_bot.py the same way reactions.py was: news_bot exits at import without
env, so anything that wants an offline test has to live here. No I/O beyond
one os.path check.

A branded post opens as ONE plan card — brands, design and platforms already
filled in from what the operator chose last time — instead of a wizard that
asks each question on its own screen (see "The plan card" below).

The publish step picks PLATFORMS, not brand→platform pairs. Brands are already
chosen (and rendered) one step earlier, so re-listing every combination made
the keyboard grow brands×platforms rows for a choice the operator makes per
platform anyway: "this one goes to X and IG". `expand` turns the ticked
platforms back into the flat pair list `_do_publish` consumes.

Brand selection OPENS COLLAPSED: one row per account group (GMN / JNN, from
modules/telegram/groups.py) with the thirteen-brand list behind a "Custom…"
button, because in practice a post goes to one whole family. The selection
underneath is the same set either way, so a group tick and a hand edit
compose — expand Custom after ticking GMN and you see exactly its five. With
no group configured anywhere there is nothing to collapse and the rows are
the flat list.

Callback namespace "b:" (the manual picker owns t:/y:/e:, asks own r:):
    b:go  b:edit  b:asis  b:cancel      the plan card
    b:g:<i> b:custom b:groups           editor brands, collapsed (i indexes the groups)
    b:t:<i>                             editor brands, expanded (i indexes the brands)
    b:lay:<layout>  b:pk:<key>  b:done  editor design / platforms / back to the card
    b:p:<i> b:publish                   platform picker (i indexes the platforms list)
    b:noop                              disabled row (brand without a logo.png)
"""

import os
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from modules.telegram import groups
from modules.youtube.shorts_format import MAX_SHORT_S

# (brand-dict key, picker label) — the order platforms appear in the picker.
PLATFORMS = (("tg", "TG"), ("yt", "YT"), ("tw", "X"), ("ig", "IG"),
             ("fb", "FB"))

# A reply to an open picker used to mean exactly one thing: replace the
# headline. This prefix is how the operator hands the Instagram caption what
# they already know — the source, who the people are, what is confirmed. It is
# not published; it goes into the caption prompt as the source of truth and
# steers the web search. Optional space before the colon, any case; what
# follows keeps its own line breaks, only the prefix is stripped.
_INFO_PREFIX = re.compile(r"^\s*info\s*:[ \t]*\r?\n?", re.IGNORECASE)


def parse_reply(text: str) -> tuple:
    """A picker reply as (field, value): "info" or "text".

    "info:" with nothing after it is not a mistake — it is how the operator
    takes their info back off, so it returns empty info rather than falling
    through to the headline.
    """
    if _INFO_PREFIX.match(text or ""):
        return "info", _INFO_PREFIX.sub("", text, count=1).strip()
    return "text", (text or "").strip()


# The picker already carries the headline and the operator is reading it on a
# phone — what the analysis owes them here is one orientation line, not the
# report. The full thing goes into the caption prompt either way.
_SUMMARY_CHARS = 300


def footage_lines(footage: dict) -> list:
    """The 👁 line a picker shows for a footage analysis.

    Empty for an analysis that didn't happen or didn't come back — vision is
    best-effort, and a picker that says nothing about it looks exactly like the
    picker that shipped before it existed.
    """
    if not footage:
        return []
    summary = footage.get("summary", "").strip()
    if len(summary) > _SUMMARY_CHARS:
        summary = summary[:_SUMMARY_CHARS].rstrip() + "…"
    return [f"👁 {summary}"]


def available_brands(brands: list) -> list:
    """Copies with has_logo added — checked once when the picker opens, so a
    logo dropped in mid-flow doesn't confuse an open keyboard."""
    return [dict(b, has_logo=os.path.isfile(b["logo"])) for b in brands]


def _publishable(render: dict, key: str, duration_s: float) -> bool:
    """Can this one render go out on this platform? A brand with no account
    configured for it can't; YouTube additionally can't take a clip past the
    Shorts cap (an upload that can't be a Short shouldn't be offered) or a
    photo card at all. Instagram and Facebook take both — a video render
    goes out as a Reel, a photo card as a feed post."""
    if not render["brand"].get(key):
        return False
    if key == "yt" and (duration_s > MAX_SHORT_S
                        or render.get("kind") == "photo"):
        return False
    return True


def platforms_for(renders: list, duration_s: float) -> list:
    """The platforms the publish picker offers: one entry per platform at
    least one rendered brand can actually publish to, carrying that platform's
    own subset of the renders. A platform no rendered brand has configured
    never appears, so every ticked row is guaranteed to publish something."""
    out = []
    for key, label in PLATFORMS:
        usable = [r for r in renders if _publishable(r, key, duration_s)]
        if usable:
            out.append({"platform": key, "label": label, "renders": usable})
    return out


def expand(platforms: list, selected: set) -> list:
    """Ticked platform indexes -> the flat (render, platform) pairs the
    publisher consumes — every selected platform crossed with the brands that
    platform is configured for. Grouped by platform, brands in render order."""
    pairs = []
    for i in sorted(selected):
        p = platforms[i]
        for r in p["renders"]:
            pairs.append({"render": r, "platform": p["platform"],
                          "label": f"{r['brand']['name']} → {p['label']}"})
    return pairs


# The card designs, in picker order: (photo_card layout, button label). Only
# offered when the post HAS the photos a layout needs — "split" and "insets"
# are the same picture as "solo" with one photo, so a single-photo post is
# given one card button rather than three that render identically.
CARD_LAYOUTS = (("insets", "⚪ Circles"), ("split", "◧ Split"),
                ("solo", "▭ Photo only"))


def card_layouts(n_photos: int) -> list[tuple[str, str]]:
    """The designs worth offering for a post of `n_photos` photos."""
    if n_photos <= 1:
        return [("solo", "🖼 Create post")]
    return list(CARD_LAYOUTS)


def toggle_brand_group(brands: list, selected: set, index: int) -> set:
    """Tap on a group row -> the new selection. A group that is only PARTLY
    selected fills up first (one tap gets you the whole family, which is what
    the row claims); tapping a full one clears just its members, never a brand
    picked by hand outside it."""
    members = groups.brand_groups(brands)[index]["members"]
    return (selected - members) if members <= selected else (selected | members)


def _brand_rows(brands: list, selected: set, custom: bool) -> list:
    """The plan editor's brand rows: group rows + "Custom…", or the full
    per-brand list + "⬅ Groups" (the full list is also what you get when no
    brand declares a group, since then there is nothing to collapse)."""
    rows = []
    gs = groups.brand_groups(brands)
    if gs and not custom:
        for i, g in enumerate(gs):
            n = len(g["members"])
            rows.append([InlineKeyboardButton(
                f"{groups.mark(selected, g['members'])} {g['name']} · "
                f"{n} brand{'' if n == 1 else 's'}",
                callback_data=f"b:g:{i}")])
        rows.append([InlineKeyboardButton("⚙ Custom…", callback_data="b:custom")])
    else:
        for i, b in enumerate(brands):
            if b["has_logo"]:
                mark = "☑" if i in selected else "☐"
                rows.append([InlineKeyboardButton(
                    f"{mark} {b['name']} · {b['lang'] or 'raw'}",
                    callback_data=f"b:t:{i}")])
            else:
                rows.append([InlineKeyboardButton(
                    f"🚫 {b['name']} (no logo.png)", callback_data="b:noop")])
        if gs:
            rows.append([InlineKeyboardButton("⬅ Groups",
                                              callback_data="b:groups")])
    return rows


def platform_keyboard(platforms: list, selected: set) -> InlineKeyboardMarkup:
    """One row per platform. The brand count is on the button because it's the
    only thing the row hides — YT may cover fewer brands than TG. The plan card
    pre-ticks the rows it promised, so the publish button names them: with the
    usual plan the whole step is that one tap."""
    rows = []
    for i, p in enumerate(platforms):
        mark = "☑" if i in selected else "☐"
        n = len(p["renders"])
        rows.append([InlineKeyboardButton(
            f"{mark} {p['label']} · {n} brand{'' if n == 1 else 's'}",
            callback_data=f"b:p:{i}")])
    ticked = [p["label"] for i, p in enumerate(platforms) if i in selected]
    rows.append([
        InlineKeyboardButton(("🚀 Publish → " + " · ".join(ticked)) if ticked
                             else "▶ Publish", callback_data="b:publish"),
        InlineKeyboardButton("✕ Cancel", callback_data="b:cancel"),
    ])
    return InlineKeyboardMarkup(rows)


# --------------------------------------------------------------------------- #
# The plan card                                                               #
# --------------------------------------------------------------------------- #
#
# A branded post used to be a wizard — gate, brand picker, render, platform
# picker — and every feature added a step. The plan card guesses every answer
# up front (what the operator picked last time for this kind of post) and asks
# for one approval: 🚀 Go renders, and the publish picker then opens with the
# plan's platforms already ticked. ✏️ Change opens one editor holding all the
# choices at once. A new option becomes a line on the card with a default, not
# another screen.
#
# Callback verbs (all under "b:"):
#     b:go  b:edit  b:asis  b:cancel      the card
#     b:g:<i> b:custom b:groups b:t:<i>   editor — brands (as in the picker)
#     b:lay:<layout>  b:pk:<key>  b:done  editor — design, platforms, back

# Platform names on the card, where there is room to spell them out.
PLATFORM_NAMES = {"tg": "Telegram", "yt": "YouTube", "tw": "X",
                  "ig": "Instagram", "fb": "Facebook"}


def plan_platform_keys(brands: list, selected: set, kind: str,
                       yt_enabled: bool) -> list:
    """The platforms the selected brands could publish to, in picker order.
    Mirrors `_publishable` as far as it can before anything is rendered: a
    photo card never goes to YouTube, and YouTube is off entirely while the
    upload kill switch is. The Shorts length cap is only known after the
    probe — `platforms_for` still applies it after the render."""
    keys = []
    for key, _ in PLATFORMS:
        if key == "yt" and (kind == "photo" or not yt_enabled):
            continue
        if any(brands[i].get(key) for i in selected):
            keys.append(key)
    return keys


def default_plan(brands: list, kind: str, n_photos: int, saved: dict,
                 yt_enabled: bool) -> dict:
    """The plan a fresh card opens with: the operator's last choice for this
    kind of post (`saved`, as `plan_memory` wrote it), resolved against the
    brands and platforms available NOW. Anything that no longer resolves falls
    back to everything — a renamed brand must not leave the card with an empty
    plan that Go refuses.

    Returns {"sel_brands": {index}, "layout": str | None, "platforms": {key}}.
    """
    saved = saved or {}
    usable = {i for i, b in enumerate(brands) if b.get("has_logo", True)}
    names = set(saved.get("brands") or ())
    sel = {i for i in usable if brands[i]["name"] in names} or set(usable)

    layout = None
    if kind == "photo":
        options = [lay for lay, _ in card_layouts(n_photos)]
        layout = saved.get("layout") if saved.get("layout") in options \
            else options[0]

    avail = plan_platform_keys(brands, sel, kind, yt_enabled)
    keys = set(saved.get("platforms") or ()) & set(avail) or set(avail)
    return {"sel_brands": sel, "layout": layout, "platforms": keys}


def plan_memory(brands: list, selected: set, layout, platforms,
                n_photos: int, previous: dict) -> dict:
    """What to remember for next time, by NAME (indexes shift when a brand is
    added). The layout is only learnt from a post that had a real choice of
    designs: a single photo is always "solo", and remembering that would
    silently turn the next album's Circles into Photo only."""
    out = dict(previous or {})
    out["brands"] = sorted(brands[i]["name"] for i in selected)
    out["platforms"] = [k for k, _ in PLATFORMS if k in set(platforms)]
    if layout and n_photos > 1:
        out["layout"] = layout
    return out


def brands_summary(brands: list, selected: set) -> str:
    """The Brands line: whole groups by name ("GMN + JNN (13)"), a short hand
    pick by its brands, anything else by count — never a thirteen-name list."""
    if not selected:
        return "none"
    whole = [g for g in groups.brand_groups(brands) if g["members"] <= selected]
    covered = set().union(*(g["members"] for g in whole)) if whole else set()
    if whole and covered == selected:
        return " + ".join(g["name"] for g in whole) + f" ({len(selected)})"
    names = [brands[i]["name"] for i in sorted(selected)]
    if len(names) <= 3:
        return ", ".join(names)
    return f"{len(names)} brands"


def layout_label(layout: str) -> str:
    return dict(CARD_LAYOUTS).get(layout, layout)


def plan_lines(brands: list, selected: set, platforms: list,
               layout=None, n_photos: int = 0) -> list:
    """The plan block of the card. `platforms` is what will actually be
    offered (the plan's keys that the selected brands still have)."""
    lines = [f"Brands: {brands_summary(brands, selected)}"]
    if layout and n_photos > 1:  # one photo has one design — nothing to say
        lines.append(f"Design: {layout_label(layout)}")
    lines.append("Platforms: " + (" · ".join(PLATFORM_NAMES[k] for k in platforms)
                                  or "none"))
    return lines


def plan_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Go", callback_data="b:go"),
         InlineKeyboardButton("✏️ Change", callback_data="b:edit")],
        [InlineKeyboardButton("📤 Post as-is", callback_data="b:asis"),
         InlineKeyboardButton("✕ Cancel", callback_data="b:cancel")],
    ])


def plan_edit_keyboard(brands: list, selected: set, custom: bool,
                       layouts: list, layout, available: list,
                       platforms: set) -> InlineKeyboardMarkup:
    """Every choice on one screen: the brand rows, a design row (only when
    there is more than one design to choose), a platform row, then back to the
    card. `layouts` is card_layouts(n) for a photo post, [] for a video."""
    rows = _brand_rows(brands, selected, custom)
    if len(layouts) > 1:
        rows.append([InlineKeyboardButton(
            ("● " if lay == layout else "") + label,
            callback_data=f"b:lay:{lay}") for lay, label in layouts])
    if available:
        rows.append([InlineKeyboardButton(
            f"{'☑' if k in platforms else '☐'} {dict(PLATFORMS)[k]}",
            callback_data=f"b:pk:{k}") for k in available])
    rows.append([InlineKeyboardButton("✅ Done", callback_data="b:done")])
    return InlineKeyboardMarkup(rows)
