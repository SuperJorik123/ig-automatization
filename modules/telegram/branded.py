"""
modules/telegram/branded.py — the Brand-it flow's pure pieces: which
platforms a set of renders can publish to, and the three inline keyboards
(gate, brand picker, platform picker). Lifted out of news_bot.py the same way
reactions.py was: news_bot exits at import without env, so anything that wants
an offline test has to live here. No I/O beyond one os.path check.

The publish step picks PLATFORMS, not brand→platform pairs. Brands are already
chosen (and rendered) one step earlier, so re-listing every combination made
the keyboard grow brands×platforms rows for a choice the operator makes per
platform anyway: "this one goes to X and IG". `expand` turns the ticked
platforms back into the flat pair list `_do_publish` consumes.

The brand picker itself OPENS COLLAPSED: one row per account group (GMN /
JNN, from modules/telegram/groups.py) with the thirteen-brand list behind a
"Custom…" button, because in practice a post goes to one whole family. The
selection underneath is the same set either way, so a group tick and a hand
edit compose — expand Custom after ticking GMN and you see exactly its five.
With no group configured anywhere there is nothing to collapse and the picker
is the flat list it always was.

Callback namespace "b:" (the manual picker owns t:/y:/e:, asks own r:):
    b:asis  b:brand              the as-is / brand-it gate (video)
    b:asis  b:card               the as-is / create-post gate (photos)
    b:g:<i> b:custom b:groups    brand picker, collapsed (i indexes the groups)
    b:t:<i> b:render b:cancel    brand picker, expanded (i indexes the brands)
    b:p:<i> b:publish            platform picker (i indexes the platforms list)
    b:noop                       disabled row (brand without a logo.png)
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


def gate_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📤 Post as-is", callback_data="b:asis"),
        InlineKeyboardButton("🎨 Brand it", callback_data="b:brand"),
    ]])


def card_gate_keyboard() -> InlineKeyboardMarkup:
    """Photo post gate: post the photos as they are, or compose a news card
    (hero + circular insets + logo + headline, shared/photo_card.py)."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📤 Post as-is", callback_data="b:asis"),
        InlineKeyboardButton("🖼 Create post", callback_data="b:card"),
    ]])


def toggle_brand_group(brands: list, selected: set, index: int) -> set:
    """Tap on a group row -> the new selection. A group that is only PARTLY
    selected fills up first (one tap gets you the whole family, which is what
    the row claims); tapping a full one clears just its members, never a brand
    picked by hand outside it."""
    members = groups.brand_groups(brands)[index]["members"]
    return (selected - members) if members <= selected else (selected | members)


def brand_keyboard(brands: list, selected: set,
                   custom: bool = False) -> InlineKeyboardMarkup:
    """Collapsed by default: one row per group plus "Custom…". `custom=True`
    is the full per-brand list (what this keyboard always was) plus a way back
    — and it is also what you get when no brand declares a group, since then
    there is nothing to collapse."""
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
    rows.append([
        InlineKeyboardButton("🎬 Render", callback_data="b:render"),
        InlineKeyboardButton("✕ Cancel", callback_data="b:cancel"),
    ])
    return InlineKeyboardMarkup(rows)


def platform_keyboard(platforms: list, selected: set) -> InlineKeyboardMarkup:
    """One row per platform. The brand count is on the button because it's the
    only thing the row hides — YT may cover fewer brands than TG."""
    rows = []
    for i, p in enumerate(platforms):
        mark = "☑" if i in selected else "☐"
        n = len(p["renders"])
        rows.append([InlineKeyboardButton(
            f"{mark} {p['label']} · {n} brand{'' if n == 1 else 's'}",
            callback_data=f"b:p:{i}")])
    rows.append([
        InlineKeyboardButton("▶ Publish", callback_data="b:publish"),
        InlineKeyboardButton("✕ Cancel", callback_data="b:cancel"),
    ])
    return InlineKeyboardMarkup(rows)
