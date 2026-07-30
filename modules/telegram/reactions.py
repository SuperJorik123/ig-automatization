"""
modules/telegram/reactions.py — the BulkFollows side of publishing: which
emoji can be ordered, the "which reactions?" ask, and the orders themselves.

Split out of news_bot.py so the manual picker and the autopilot share one
catalogue and one set of ordering rules instead of drifting apart.

Two halves:

  ORDERS      order_post / order_emoji / record_posts — thin, logged, never
              raising wrappers over smm.place_order. Blocking HTTP: async
              callers hand them to asyncio.to_thread.

  THE ASK     a pure state machine (new_state / reduce / orders_from /
              render) driving the message the operator taps. The state is a
              plain JSON-able dict that lives in SQLite, so a bot restart
              doesn't strand a published post without its reactions, and the
              whole flow is testable without a Telegram connection.

Ask state:
    {"item_id": 41,
     "mode": "all" | "per",     # one set for every channel, or per channel
     "cur": 0,                  # channel being edited in "per" mode
     "chans": [{"chat_id": "@news_eu", "link": "https://t.me/news_eu/412"}, ...],
     "sel": {"0": [0, 1], "1": [0]}}   # channel index (str) -> emoji indices

In "all" mode a toggle is written to every channel at once, so switching to
"per" mode carries the current selection over as each channel's starting
point — pick the common set first, then adjust the odd one out.
"""

import logging
import random

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from shared import config
from modules.telegram import queue_store, smm

log = logging.getLogger(__name__)

# Reaction services on BulkFollows: one service id per emoji, ordered against
# the post link. `name` (not the glyph) is what goes into the logs — the
# Windows console can't encode emoji and would mangle the line.
EMOJI_SERVICES = [
    {"emoji": "❤️", "name": "heart",     "service": "5108"},
    {"emoji": "👍", "name": "like",      "service": "5110"},
    {"emoji": "👎", "name": "dislike",   "service": "5691"},
    {"emoji": "💩", "name": "shit",      "service": "9279"},
    {"emoji": "🤡", "name": "clown",     "service": "9291"},
    {"emoji": "🤮", "name": "throw up",  "service": "5702"},
    # A mixed set, not one reaction — no single glyph represents it, so the
    # button carries `label` text instead.
    {"emoji": "🙂", "name": "positive", "label": "Positive emojis", "service": "9271"},
    {"emoji": "😃", "name": "grinning",  "service": "5699"},
]

# Every Nth post to a channel triggers the per-channel bonus order. The count
# is a lifetime total kept in SQLite (queue_store.bump_channel_posts), not in
# memory: at roughly one post a day an in-memory counter would need five days
# of unbroken uptime to ever reach the threshold, and any restart would send
# it back to zero.
CHANNEL_POST_THRESHOLD = 5

# Quantity of the per-channel bonus order.
THRESHOLD_QUANTITY = 10000


def face(emoji: dict) -> str:
    """What to show on a button / in a summary."""
    return emoji.get("label") or emoji["emoji"]


# --------------------------------------------------------------------------- #
# Orders                                                                      #
# --------------------------------------------------------------------------- #


def post_quantity() -> int:
    """Quantity for the per-post order. Randomised so the orders don't look
    metronomic."""
    return random.randint(500, 5000)


def emoji_quantity() -> int:
    """Quantity for a single reaction order."""
    return random.randint(10, 40)


def channel_link(chat_id: str, post_link: str | None) -> str:
    """Public URL of the channel itself. A post link is the reliable source
    (t.me/<channel>/<id> → drop the message id) — it is the only thing that
    yields a usable URL for a numeric -100… chat id. Falls back to the @name."""
    if post_link:
        return post_link.rsplit("/", 1)[0]
    if chat_id.startswith("@"):
        return f"https://t.me/{chat_id[1:]}"
    return ""


def order_post(chat_id: str, link: str) -> None:
    """Called for every Telegram post that went out: one order for that post."""
    log.info("[%s] post order", chat_id)
    smm.place_order(link, post_quantity(), config.BULKFOLLOWS_SERVICE_ID)


def order_emoji(chat_id: str, link: str, emoji: dict) -> None:
    """One reaction order: the emoji's own service id against the post link,
    quantity rolled fresh so no two channels get identical counts."""
    log.info("[%s] %s reaction order", chat_id, emoji["name"])
    smm.place_order(link, emoji_quantity(), emoji["service"])


def order_channel_threshold(chat_id: str, post_link: str | None) -> None:
    """Called once a channel reaches CHANNEL_POST_THRESHOLD posts: one order
    for the channel itself."""
    log.info("[%s] hit %d posts — channel order", chat_id, CHANNEL_POST_THRESHOLD)
    smm.place_order(channel_link(chat_id, post_link), THRESHOLD_QUANTITY,
                    config.BULKFOLLOWS_SERVICE_ID_BONUS)


def record_posts(posted: list, links: list, emojis: list | None = None) -> None:
    """Order against every successfully posted channel, bump its counter, and
    fire order_channel_threshold (resetting the counter) on every 5th post.

    `posted` is publisher.publish's list of chat_ids, `links` its list of
    (chat_id, url) pairs — a channel can succeed without yielding a link, and
    that post is counted but cannot be ordered against.

    `emojis` are reactions to order immediately for every posted channel, as
    the manual picker does (it asks before posting). The autopilot passes None
    and opens an ask instead, applying the reactions later."""
    by_chat = dict(links)
    log.info("BulkFollows: %d channel(s) posted — %s | %d reaction(s): %s",
             len(posted), ", ".join(posted), len(emojis or []),
             ", ".join(e["name"] for e in emojis or []) or "—")
    for chat_id in posted:
        link = by_chat.get(chat_id)
        if link:
            order_post(chat_id, link)
            for emoji in emojis or []:
                order_emoji(chat_id, link, emoji)
        else:
            log.warning("no public link for %s — BulkFollows post order skipped", chat_id)

        n = queue_store.bump_channel_posts(chat_id)
        log.info("[%s] post %d (channel order every %d)", chat_id, n, CHANNEL_POST_THRESHOLD)
        if n % CHANNEL_POST_THRESHOLD == 0:
            order_channel_threshold(chat_id, link)


def apply_orders(state: dict) -> int:
    """Place every (channel, emoji) order the ask selected. Blocking — call it
    through asyncio.to_thread. Returns the number of orders attempted."""
    orders = orders_from(state)
    for chat_id, link, emoji in orders:
        order_emoji(chat_id, link, emoji)
    return len(orders)


# --------------------------------------------------------------------------- #
# The ask: pure state machine                                                 #
# --------------------------------------------------------------------------- #


def new_state(item_id: int, channels: list) -> dict:
    """Initial ask state. `channels` is [{"chat_id", "link"}] — normally the
    links publisher.publish handed back."""
    return {
        "item_id": item_id,
        "mode": "all",
        "cur": 0,
        "chans": [{"chat_id": c["chat_id"], "link": c.get("link")} for c in channels],
        "sel": {str(i): [] for i in range(len(channels))},
    }


def _selected(state: dict, idx: int) -> list:
    return state["sel"].get(str(idx), [])


def reduce(state: dict, verb: str) -> tuple[dict, str | None]:
    """Apply one button press. Returns (new_state, action) where action is
    None (redraw), "apply" or "skip" (the caller finishes the ask).

    Pure: no I/O, no globals. The verb is the part of callback_data after the
    ask id — t<i> toggle, pc per-channel mode, nx/pv navigate, ap apply,
    sk skip."""
    state = {**state, "sel": dict(state["sel"])}
    n = len(state["chans"])

    if verb.startswith("t") and verb[1:].isdigit():
        i = int(verb[1:])
        # "all" mode keeps every channel in sync, so switching to per-channel
        # later starts from what you already picked.
        targets = range(n) if state["mode"] == "all" else [state["cur"]]
        for t in targets:
            sel = list(_selected(state, t))
            state["sel"][str(t)] = [x for x in sel if x != i] if i in sel else sorted(sel + [i])
        return state, None

    if verb == "pc":
        state["mode"] = "per"
        return state, None

    if verb in ("nx", "pv") and n:
        step = 1 if verb == "nx" else -1
        state["cur"] = (state["cur"] + step) % n  # wraps, so you can't get stuck
        return state, None

    if verb == "ap":
        return state, "apply"
    if verb == "sk":
        return state, "skip"
    return state, None


def orders_from(state: dict) -> list:
    """[(chat_id, link, emoji)] the current selection would order. Channels
    that never produced a public link are skipped — there is nothing to order
    against — and logged by the caller via summary()."""
    out = []
    for i, chan in enumerate(state["chans"]):
        if not chan.get("link"):
            continue
        for e in _selected(state, i):
            if 0 <= e < len(EMOJI_SERVICES):
                out.append((chan["chat_id"], chan["link"], EMOJI_SERVICES[e]))
    return out


# --------------------------------------------------------------------------- #
# The ask: rendering                                                          #
# --------------------------------------------------------------------------- #


def render(ask_id: int, state: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Message text + keyboard for an open ask."""
    chans = state["chans"]
    n = len(chans)
    if state["mode"] == "all":
        head = [f"🎛 Reactions · item {state['item_id']} · {n} channel(s)"]
        for c in chans:
            head.append(f"• {c['chat_id']} — {c.get('link') or 'no public link'}")
        shown = _selected(state, 0)
    else:
        cur = state["cur"]
        c = chans[cur] if chans else {"chat_id": "—", "link": None}
        head = [
            f"🎛 Reactions · item {state['item_id']}",
            f"{cur + 1}/{n} · {c['chat_id']}",
            c.get("link") or "no public link — reactions can't be ordered here",
        ]
        shown = _selected(state, cur)

    rows = []
    for i in range(0, len(EMOJI_SERVICES), 2):
        row = []
        for j, em in enumerate(EMOJI_SERVICES[i:i + 2], start=i):
            mark = "☑" if j in shown else "☐"
            row.append(InlineKeyboardButton(f"{mark} {face(em)}", callback_data=f"r:{ask_id}:t{j}"))
        rows.append(row)

    if state["mode"] == "all":
        rows.append([InlineKeyboardButton(f"✅ Apply to all {n} channel(s)",
                                          callback_data=f"r:{ask_id}:ap")])
        rows.append([InlineKeyboardButton("⚙ Per-channel…", callback_data=f"r:{ask_id}:pc"),
                     InlineKeyboardButton("✕ Skip", callback_data=f"r:{ask_id}:sk")])
    else:
        rows.append([InlineKeyboardButton("◂ prev", callback_data=f"r:{ask_id}:pv"),
                     InlineKeyboardButton("next ▸", callback_data=f"r:{ask_id}:nx")])
        rows.append([InlineKeyboardButton("✅ Apply all", callback_data=f"r:{ask_id}:ap"),
                     InlineKeyboardButton("✕ Skip", callback_data=f"r:{ask_id}:sk")])

    head.append("Pick the reactions to buy, then Apply.")
    return "\n".join(head), InlineKeyboardMarkup(rows)


def summary(state: dict, applied: bool) -> str:
    """What the ask message becomes once it's answered."""
    if not applied:
        return f"✕ reactions skipped · item {state['item_id']}"
    lines = [f"✅ reactions ordered · item {state['item_id']}"]
    for i, chan in enumerate(state["chans"]):
        picked = " ".join(face(EMOJI_SERVICES[e]) for e in _selected(state, i)
                          if 0 <= e < len(EMOJI_SERVICES))
        if not chan.get("link"):
            lines.append(f"• {chan['chat_id']}: — (no public link)")
        else:
            lines.append(f"• {chan['chat_id']}: {picked or '—'}")
    return "\n".join(lines)
