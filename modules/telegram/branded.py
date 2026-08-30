"""
modules/telegram/branded.py — the Brand-it flow's pure pieces: which
brand→platform pairs a set of renders can publish to, and the three inline
keyboards (gate, brand picker, publish picker). Lifted out of news_bot.py the
same way reactions.py was: news_bot exits at import without env, so anything
that wants an offline test has to live here. No I/O beyond one os.path check.

Callback namespace "b:" (the manual picker owns t:/y:/e:, asks own r:):
    b:asis  b:brand              the as-is / brand-it gate (video)
    b:asis  b:card               the as-is / create-post gate (photos)
    b:t:<i> b:render b:cancel    brand picker (i indexes the brands list)
    b:p:<i> b:publish            publish picker (i indexes the pairs list)
    b:noop                       disabled row (brand without a logo.png)
"""

import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from modules.youtube.shorts_format import MAX_SHORT_S

# (brand-dict key, picker label) — the order pairs appear in the picker.
PLATFORMS = (("tg", "TG"), ("yt", "YT"), ("tw", "X"))


def available_brands(brands: list) -> list:
    """Copies with has_logo added — checked once when the picker opens, so a
    logo dropped in mid-flow doesn't confuse an open keyboard."""
    return [dict(b, has_logo=os.path.isfile(b["logo"])) for b in brands]


def pairs_for(renders: list, duration_s: float) -> list:
    """Publishable (render, platform) pairs: one per configured platform of
    each rendered brand. YouTube pairs disappear past the Shorts cap — an
    upload that can't be a Short shouldn't be offered — and for photo cards
    (render["kind"] == "photo"), which YouTube can't take at all."""
    pairs = []
    for r in renders:
        b = r["brand"]
        for key, label in PLATFORMS:
            if not b.get(key):
                continue
            if key == "yt" and (duration_s > MAX_SHORT_S
                                or r.get("kind") == "photo"):
                continue
            pairs.append({"render": r, "platform": key,
                          "label": f"{b['name']} → {label}"})
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


def brand_keyboard(brands: list, selected: set) -> InlineKeyboardMarkup:
    rows = []
    for i, b in enumerate(brands):
        if b["has_logo"]:
            mark = "☑" if i in selected else "☐"
            rows.append([InlineKeyboardButton(
                f"{mark} {b['name']} · {b['lang'] or 'raw'}",
                callback_data=f"b:t:{i}")])
        else:
            rows.append([InlineKeyboardButton(
                f"🚫 {b['name']} (no logo.png)", callback_data="b:noop")])
    rows.append([
        InlineKeyboardButton("🎬 Render", callback_data="b:render"),
        InlineKeyboardButton("✕ Cancel", callback_data="b:cancel"),
    ])
    return InlineKeyboardMarkup(rows)


def publish_keyboard(pairs: list, selected: set) -> InlineKeyboardMarkup:
    rows = []
    for i, p in enumerate(pairs):
        mark = "☑" if i in selected else "☐"
        rows.append([InlineKeyboardButton(f"{mark} {p['label']}",
                                          callback_data=f"b:p:{i}")])
    rows.append([
        InlineKeyboardButton("▶ Publish", callback_data="b:publish"),
        InlineKeyboardButton("✕ Cancel", callback_data="b:cancel"),
    ])
    return InlineKeyboardMarkup(rows)
