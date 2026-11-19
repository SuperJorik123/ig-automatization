"""
modules/telegram/reactions.py — the BulkFollows side of manual publishing:
which emoji can be ordered, and the orders themselves.

order_post / order_emoji / record_posts are thin, logged, never raising
wrappers over smm.place_order. Blocking HTTP: async callers hand them to
asyncio.to_thread.
"""

import logging
import random

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

    `emojis` are the reactions the operator ticked on the picker, ordered
    immediately for every posted channel."""
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
