"""
modules/newsroom/store.py — SQLite state for the WordPress -> Telegram bot.

Its own database (config.NR_DATA_DIR/newsroom.db), never the news aggregator's
news.db: the two bots share a machine, not a queue, and a client's channel must
not be reachable from the operator's own tooling by accident.

Four tables, each earning its place:

  articles   one row per WordPress post the bot has seen. UNIQUE(site, wp_id)
             is the duplicate guard, keyed on WordPress's OWN post id — not the
             URL (slugs get edited) and not the title (republished posts reuse
             them). Re-inserting a seen article is a silent no-op, which is
             what makes a poll that overlaps the previous poll free.

  posts      one row per published Telegram message. `message_id` and `link`
             are the whole reason this table exists: the BulkFollows orders are
             placed against t.me/<channel>/<id>, so a post whose link was never
             captured can be counted but never ordered against.

  orders     one row per panel call, written BEFORE the call and updated after.
             smm.place_order never raises by design, so a failed order is
             otherwise invisible; this is both the crash-recovery record and
             the replay list for when the panel was down.

  counters   lifetime posts per chat_id, driving the every-5th-post bonus. It
             is a durable counter for the same reason the news bot's is: at one
             post a day, an in-memory count would need five unbroken days of
             uptime to ever reach the threshold.

Plain stdlib sqlite3. Blocking — async callers use asyncio.to_thread.
"""

import os
import sqlite3
from datetime import datetime, timezone

from shared import config

_DB = os.path.join(config.NR_DATA_DIR, "newsroom.db")

# Article statuses.
NEW = "new"          # fetched, not yet posted
POSTED = "posted"    # published to its channel
FAILED = "failed"    # rewrite or send raised; not retried automatically
SKIPPED = "skipped"  # recorded as seen without posting (the backfill guard)


def _conn():
    os.makedirs(config.NR_DATA_DIR, exist_ok=True)
    c = sqlite3.connect(_DB)
    c.row_factory = sqlite3.Row
    return c


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init() -> None:
    """Create tables if missing. Safe to call on every startup; additive
    migrations go here, guarded by a column check, the same way
    modules/telegram/queue_store.init() handles them."""
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS articles(
                   id           INTEGER PRIMARY KEY AUTOINCREMENT,
                   site         TEXT NOT NULL,
                   wp_id        INTEGER NOT NULL,
                   url          TEXT,
                   title        TEXT,
                   body         TEXT,
                   media_url    TEXT,
                   media_type   TEXT,      -- "photo" | "video" | NULL
                   published_at TEXT,      -- ISO UTC, from WordPress date_gmt
                   seen_at      TEXT,      -- ISO UTC, when this bot first saw it
                   status       TEXT DEFAULT 'new',
                   UNIQUE(site, wp_id))"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_articles_site_status "
                  "ON articles(site, status)")
        c.execute(
            """CREATE TABLE IF NOT EXISTS posts(
                   id         INTEGER PRIMARY KEY AUTOINCREMENT,
                   article_id INTEGER NOT NULL,
                   site       TEXT,
                   chat_id    TEXT,
                   message_id INTEGER,
                   link       TEXT,        -- t.me/<channel>/<id>, NULL if none
                   posted_at  TEXT)"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_posts_chat ON posts(chat_id, id)")
        c.execute(
            """CREATE TABLE IF NOT EXISTS orders(
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   post_id     INTEGER,
                   kind        TEXT,       -- "views" | "bonus" | "emoji:<name>"
                   service     TEXT,
                   quantity    INTEGER,
                   panel_order TEXT,       -- the panel's order id, NULL if failed
                   error       TEXT,       -- set when the call did not succeed
                   placed_at   TEXT)"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_orders_post ON orders(post_id)")
        c.execute(
            """CREATE TABLE IF NOT EXISTS counters(
                   chat_id TEXT PRIMARY KEY,
                   n       INTEGER DEFAULT 0)"""
        )


# --------------------------------------------------------------------------- #
# Articles                                                                    #
# --------------------------------------------------------------------------- #


def seen_ids(site: str) -> set:
    """Every WordPress post id already recorded for `site`, whatever its
    status. The poller filters its fetch against this instead of using a date
    watermark — see wp.fetch_recent for why a watermark loses posts."""
    with _conn() as c:
        rows = c.execute("SELECT wp_id FROM articles WHERE site=?", (site,)).fetchall()
    return {r["wp_id"] for r in rows}


def has_articles(site: str) -> bool:
    """True once `site` has any recorded article. False means the next tick is
    that site's first, which is what the backfill guard keys on."""
    with _conn() as c:
        row = c.execute("SELECT 1 FROM articles WHERE site=? LIMIT 1", (site,)).fetchone()
    return row is not None


def add_article(site: str, article: dict, status: str = NEW) -> int | None:
    """Record one normalised article (wp.fetch_recent's shape). Returns its row
    id, or None when this (site, wp_id) was already stored — the duplicate case
    is normal and silent, not an error."""
    with _conn() as c:
        cur = c.execute(
            """INSERT OR IGNORE INTO articles
               (site, wp_id, url, title, body, media_url, media_type,
                published_at, seen_at, status)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (site, article["wp_id"], article.get("url"), article.get("title"),
             article.get("body"), article.get("media_url"), article.get("media_type"),
             article.get("published_at"), _now(), status),
        )
        return cur.lastrowid if cur.rowcount else None


def get_article(site: str, wp_id: int):
    """The stored row for one (site, wp_id), or None. What --force-latest uses
    to re-post an article the normal flow has already marked as handled."""
    with _conn() as c:
        return c.execute(
            "SELECT * FROM articles WHERE site=? AND wp_id=?", (site, wp_id)
        ).fetchone()


def pending(site: str) -> list:
    """Articles waiting to be posted for `site`, OLDEST FIRST — the channel
    should read in the order the site published, not newest-first as the API
    returns them."""
    with _conn() as c:
        return c.execute(
            "SELECT * FROM articles WHERE site=? AND status=? "
            "ORDER BY published_at ASC, wp_id ASC",
            (site, NEW),
        ).fetchall()


def mark(article_id: int, status: str) -> None:
    with _conn() as c:
        c.execute("UPDATE articles SET status=? WHERE id=?", (status, article_id))


# --------------------------------------------------------------------------- #
# Posts and counters                                                          #
# --------------------------------------------------------------------------- #


def record_post(article_id: int, site: str, chat_id: str,
                message_id: int | None, link: str | None) -> int:
    """One published Telegram message. Returns the post row id, which every
    order for that post is filed under."""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO posts(article_id, site, chat_id, message_id, link, posted_at) "
            "VALUES(?,?,?,?,?,?)",
            (article_id, site, chat_id, message_id, link, _now()),
        )
        return cur.lastrowid


def bump_channel_posts(chat_id: str) -> int:
    """Increment and return this channel's lifetime post count.

    Per chat_id, deliberately: the bonus order fires on every 5th post OF A
    CHANNEL. A shared counter would make site A's fifth post trigger a bonus
    order on site B's channel — accepted by the panel, invisible in the logs,
    and paid for out of the wrong client's balance."""
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO counters(chat_id, n) VALUES(?, 0)", (chat_id,))
        c.execute("UPDATE counters SET n = n + 1 WHERE chat_id=?", (chat_id,))
        return c.execute("SELECT n FROM counters WHERE chat_id=?", (chat_id,)).fetchone()["n"]


def recent_posts(chat_id: str, limit: int = 5) -> list:
    """The channel's most recent posts, newest first. Not used by the current
    order flow (the panel's bonus service takes a channel link and finds the
    last posts itself) — kept because it is the query any future per-post
    top-up would need."""
    with _conn() as c:
        return c.execute(
            "SELECT * FROM posts WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()


# --------------------------------------------------------------------------- #
# Orders                                                                      #
# --------------------------------------------------------------------------- #


def open_order(post_id: int, kind: str, service: str, quantity: int) -> int:
    """Record an order we are ABOUT to place. Written first so a crash between
    here and the panel call leaves evidence that the order may or may not have
    gone out — the alternative is a silent gap."""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO orders(post_id, kind, service, quantity, placed_at) "
            "VALUES(?,?,?,?,?)",
            (post_id, kind, service, quantity, _now()),
        )
        return cur.lastrowid


def close_order(order_id: int, result: dict | None) -> None:
    """Settle an open order with the panel's answer. `result` is
    smm.place_order's return: a dict on success, None on any failure."""
    panel = str(result.get("order")) if isinstance(result, dict) and result.get("order") else None
    # An order id is the ONLY evidence the panel accepted the order. A None
    # result (network, rejection) and a 200 whose body carries no "order" are
    # both failures, and both must say so — an unexplained NULL in this column
    # reads as "never attempted", which is a different problem entirely.
    if panel:
        error = None
    elif result is None:
        error = "order failed — see the BulkFollows log line"
    else:
        error = f"no order id in panel response: {str(result)[:200]}"
    with _conn() as c:
        c.execute("UPDATE orders SET panel_order=?, error=? WHERE id=?",
                  (panel, error, order_id))


def failed_orders(limit: int = 50) -> list:
    """Orders that never got a panel id — the replay list for a panel outage.
    Nothing calls this automatically; replaying costs money and is the
    operator's decision."""
    with _conn() as c:
        return c.execute(
            "SELECT * FROM orders WHERE panel_order IS NULL ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
