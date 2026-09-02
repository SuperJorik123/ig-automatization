"""
modules/newsroom/orders.py — the BulkFollows side of publishing.

Adapted from modules/telegram/reactions.py's order half. The catalogue and the
every-5th-post rule are unchanged; what changed is that every function takes a
`site` dict and reads its service ids from there.

That refactor is the whole point of this module. reactions.py reads
config.BULKFOLLOWS_SERVICE_ID from a module global, which is a single-tenant
assumption: correct for one operator with one balance, wrong for seven client
channels. Threading the site through was done while copying rather than after,
because the failure mode of getting it wrong is silent — the panel happily
accepts an order pointing at the wrong channel, the log line looks normal, and
the wrong client's balance pays for the wrong client's growth.

Three orders exist:

  views   every post, quantity random inside the site's views_phase1 range.
  bonus   every 5th post to a channel, 10 000, placed against the CHANNEL link.
          The panel's bonus service takes a channel URL and distributes across
          its recent posts itself — that is why the message id is dropped here.
  emoji   a random handful of reactions per post, placed after a delay.

Blocking HTTP — async callers hand these to asyncio.to_thread.
"""

import logging
import random

from shared import config
from modules.newsroom import smm, store

log = logging.getLogger(__name__)

# Reaction services on BulkFollows: one service id per reaction, ordered
# against the post link. The catalogue comes from NR_EMOJI_SERVICES in .env —
# service ids are panel-account data, not code, and the panel has renumbered
# them once already. A site picks which of these it uses via its emoji_pool
# (by name: the glyph itself, or a word like "positive" for a mixed set); the
# full catalogue is never the default, because the panel's list includes
# 💩/🖕/🤮 and those on a client's news post is not a bug the client will
# accept as a random draw.
EMOJI_SERVICES = config.NR_EMOJI_SERVICES

_BY_NAME = {e["name"]: e for e in EMOJI_SERVICES}

# Every Nth post to a channel triggers the bonus order. The count is a lifetime
# total in SQLite (store.bump_channel_posts), not in memory: at roughly one
# post a day an in-memory counter would need five days of unbroken uptime to
# ever reach the threshold, and any restart would send it back to zero.
CHANNEL_POST_THRESHOLD = 5

# Quantity of the bonus order.
THRESHOLD_QUANTITY = 10000


def face(emoji: dict) -> str:
    """What to show in a summary."""
    return emoji.get("label") or emoji["emoji"]


def _rand_range(pair, fallback) -> int:
    """A random int inside a [low, high] pair from site config, tolerating a
    malformed one. A typo in a site file must cost that site a sensible
    quantity, not the order."""
    try:
        low, high = int(pair[0]), int(pair[1])
        if low > high:
            low, high = high, low
        return random.randint(low, high)
    except (TypeError, ValueError, IndexError):
        log.warning("bad quantity range %r — falling back to %r", pair, fallback)
        return random.randint(*fallback)


def post_quantity(site: dict) -> int:
    """Quantity for the per-post views order. Randomised so the orders don't
    look metronomic."""
    return _rand_range(site.get("views_phase1"), (500, 5000))


def emoji_quantity(site: dict) -> int:
    """Quantity for a single reaction order."""
    return _rand_range(site.get("emoji_quantity"), (10, 40))


def random_emojis(site: dict) -> list:
    """The reactions to buy for one post.

    Sampled from the site's OWN pool, never from the full catalogue: an
    unconfigured pool means no reactions, which is the safe direction. A name
    that matches no service is dropped with a warning rather than skipping the
    rest of the pool."""
    pool = []
    for name in site.get("emoji_pool") or []:
        emoji = _BY_NAME.get(name)
        if emoji:
            pool.append(emoji)
        else:
            log.warning("[%s] unknown emoji %r in emoji_pool — ignored",
                        site.get("name"), name)
    if not pool:
        return []
    k = min(_rand_range(site.get("emoji_count"), (2, 4)), len(pool))
    return random.sample(pool, max(k, 0))


def channel_link(chat_id: str, post_link: str | None) -> str:
    """Public URL of the channel itself.

    A post link is the reliable source (t.me/<channel>/<id> → drop the message
    id) — it is the only thing that yields a usable URL for a numeric -100…
    chat id. Falls back to the @name."""
    if post_link:
        return post_link.rsplit("/", 1)[0]
    if chat_id.startswith("@"):
        return f"https://t.me/{chat_id[1:]}"
    return ""


def _place(post_id: int, kind: str, service: str, quantity: int, link: str) -> bool:
    """One panel call, bracketed by its store row.

    The row is written BEFORE the call: smm.place_order never raises and
    returns None on every kind of failure, so without this the only trace of a
    lost order is a log line nobody reads. Returns True when the panel gave
    back an order id."""
    order_id = store.open_order(post_id, kind, service, quantity)
    result = smm.place_order(link, quantity, service)
    store.close_order(order_id, result)
    return bool(isinstance(result, dict) and result.get("order"))


def order_post(site: dict, post_id: int, link: str) -> None:
    """Phase 1: the per-post views order, on every post."""
    qty = post_quantity(site)
    log.info("[%s] views order, qty %d", site["chat_id"], qty)
    _place(post_id, "views", site.get("service_views", ""), qty, link)


def order_channel_threshold(site: dict, post_id: int, post_link: str | None) -> None:
    """Phase 2: the bonus order, every CHANNEL_POST_THRESHOLD posts.

    Placed against the CHANNEL link, not the post link — the panel's bonus
    service takes a channel URL and spreads the quantity over its recent posts
    itself."""
    link = channel_link(site["chat_id"], post_link)
    if not link:
        log.warning("[%s] no channel URL (numeric chat id, no post link) — "
                    "bonus order skipped", site["chat_id"])
        return
    log.info("[%s] hit %d posts — bonus order, qty %d",
             site["chat_id"], CHANNEL_POST_THRESHOLD, THRESHOLD_QUANTITY)
    _place(post_id, "bonus", site.get("service_bonus", ""), THRESHOLD_QUANTITY, link)


def order_emoji(site: dict, post_id: int, link: str, emoji: dict) -> None:
    """One reaction order: the emoji's own service id against the post link,
    quantity rolled fresh so no two posts get identical counts."""
    qty = emoji_quantity(site)
    log.info("[%s] %s reaction order, qty %d", site["chat_id"], emoji["name"], qty)
    _place(post_id, f"emoji:{emoji['name']}", emoji["service"], qty, link)


def after_publish(site: dict, post_id: int, link: str | None) -> None:
    """Everything that happens the moment a post ships: the views order, the
    channel counter, and the bonus order when the counter comes round.

    The counter is bumped even when there is no link to order against — the
    post still happened, and skipping the bump would let a channel with one
    unlinkable post drift out of phase with the every-5th rule forever."""
    if link:
        order_post(site, post_id, link)
    else:
        log.warning("[%s] no public link — views order skipped", site["chat_id"])

    n = store.bump_channel_posts(site["chat_id"])
    log.info("[%s] post %d (bonus order every %d)",
             site["chat_id"], n, CHANNEL_POST_THRESHOLD)
    if n % CHANNEL_POST_THRESHOLD == 0:
        order_channel_threshold(site, post_id, link)


def order_reactions(site: dict, post_id: int, link: str | None) -> int:
    """The delayed half: a random handful of reactions for one post. Returns
    how many orders were placed."""
    if not link:
        log.warning("[%s] no public link — reaction orders skipped", site["chat_id"])
        return 0
    emojis = random_emojis(site)
    if not emojis:
        return 0
    log.info("[%s] %d reaction(s): %s", site["chat_id"], len(emojis),
             ", ".join(e["name"] for e in emojis))
    for emoji in emojis:
        order_emoji(site, post_id, link, emoji)
    return len(emojis)


# --------------------------------------------------------------------------- #
# DORMANT                                                                     #
# --------------------------------------------------------------------------- #
#
# DORMANT: the reactions ASK. modules/telegram/reactions.py carries a pure
# state machine (new_state / reduce / orders_from / render) that lets an
# operator tap which reactions to buy, per channel, from an inline keyboard,
# with the state persisted in SQLite so a restart never strands a published
# post. This bot picks at random instead: it is unattended, has no control
# group, and nobody is watching seven channels to answer a question per post.
#
# If the client ever wants manual control, do not rewrite it — lift the ask
# half of reactions.py wholesale, add an `asks` table to store.py mirroring
# queue_store's, and route the callback namespace through main.py. The pieces
# that already exist there and would otherwise be rebuilt badly: "all channels"
# vs per-channel mode with the selection carried over on the switch, and the
# rule that a channel with no public link is shown but never ordered against.
