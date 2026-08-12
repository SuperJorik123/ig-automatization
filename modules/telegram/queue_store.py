"""
modules/telegram/queue_store.py — SQLite-backed NewsItem store for the smart
filter, plus the collector's per-source cursor, the per-platform publication
log, and the reaction asks the autopilot opens.

The collector enqueues raw posts (status='new'); the scorer fills in
score/regions and advances them to 'queued' or 'awaiting_approval'; each
platform's router publishes them and writes a row into `posts`. Plain stdlib
sqlite3, no extra dependency. DB lives under config.TG_DATA_DIR.

Item lifecycle:
    new → queued → posted | failed
        ↘ awaiting_approval → queued (approved) | rejected
Exact-text duplicates are dropped at enqueue time (see content_hash) so a
story forwarded across several source channels is stored — and scored — once.

`status` is a coarse overview only. **Eligibility is per platform**: an item
already on YouTube is still a candidate for Telegram, and that is decided by
the absence of a `posts` row, never by `status`. The legacy `items.posted`
JSON column is still stamped so `counts()`-style summaries keep working, but
no routing query reads it.
"""

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from shared import config

_DB = os.path.join(config.TG_DATA_DIR, "news.db")

# Same story forwarded across source channels usually keeps its exact text;
# one hash lookup inside this window kills those copies before they cost a
# scoring call. Semantic (reworded) dedup is a later, separate feature.
DEDUP_WINDOW_H = 24


def _conn():
    os.makedirs(config.TG_DATA_DIR, exist_ok=True)
    c = sqlite3.connect(_DB)
    c.row_factory = sqlite3.Row
    return c


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def content_hash(text: str) -> str | None:
    """Hash of whitespace/case-normalised text; None for media-only posts
    (no text to compare — never treated as duplicates)."""
    norm = re.sub(r"\s+", " ", (text or "").strip().lower())
    return hashlib.sha256(norm.encode()).hexdigest() if norm else None


def init() -> None:
    """Create tables if missing and migrate older databases in place. Safe to
    call on every startup."""
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS items(
                   id           INTEGER PRIMARY KEY AUTOINCREMENT,
                   source       TEXT,    -- "tg:@channel" (prefix = source type)
                   collected_at TEXT,    -- ISO UTC
                   text         TEXT,
                   media        TEXT,    -- JSON [{"path"|"file_id", "type"}]
                   content_hash TEXT,
                   score        INTEGER, -- 0-100, NULL until scored
                   regions      TEXT,    -- JSON ["us","global"], NULL until scored
                   status       TEXT DEFAULT 'new',
                   posted       TEXT DEFAULT '{}')"""  # JSON {platform: ISO ts}
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_items_status ON items(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_items_hash ON items(content_hash)")
        c.execute(
            """CREATE TABLE IF NOT EXISTS cursor(
                   source  TEXT PRIMARY KEY,
                   last_id INTEGER)"""
        )
        # One row per (item, platform, target) that actually went out. This —
        # not items.status — is what "has Telegram had this yet?" reads.
        c.execute(
            """CREATE TABLE IF NOT EXISTS posts(
                   id        INTEGER PRIMARY KEY AUTOINCREMENT,
                   item_id   INTEGER NOT NULL,
                   platform  TEXT NOT NULL,  -- 'telegram' | 'youtube'
                   target    TEXT NOT NULL,  -- chat_id, or YouTube account name
                   link      TEXT,           -- public t.me/… URL when there is one
                   posted_at TEXT NOT NULL)"""
        )
        # Republishing the same item to the same channel is always a bug (a
        # crash mid-tick, a double /next); the index makes it a no-op instead.
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_unique"
            " ON posts(item_id, platform, target)"
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_posts_item ON posts(item_id, platform)")
        # Reaction asks. State lives here rather than in memory so a bot
        # restart doesn't strand a published post without its reactions.
        c.execute(
            """CREATE TABLE IF NOT EXISTS asks(
                   id         INTEGER PRIMARY KEY AUTOINCREMENT,
                   item_id    INTEGER NOT NULL,
                   chat_id    TEXT,          -- where the ask message lives
                   message_id INTEGER,       -- NULL until the message is sent
                   state      TEXT NOT NULL, -- JSON: mode/cur/chans/sel
                   status     TEXT NOT NULL, -- 'open' | 'applied' | 'skipped'
                   created_at TEXT NOT NULL)"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_asks_status ON asks(status)")
        # Lifetime post count per channel, driving the every-Nth-post
        # BulkFollows channel order. It has to survive restarts: at roughly one
        # post a day, an in-memory counter would need five days of unbroken
        # uptime to ever reach the threshold.
        c.execute(
            """CREATE TABLE IF NOT EXISTS channel_counters(
                   target TEXT PRIMARY KEY,
                   posts  INTEGER NOT NULL DEFAULT 0)"""
        )
        # Every message the bot sees or sends in the control group, for the
        # weekly cleanup. The Bot API can't list chat history, so the bot can
        # only ever delete what it recorded here.
        c.execute(
            """CREATE TABLE IF NOT EXISTS group_messages(
                   chat_id     TEXT NOT NULL,
                   message_id  INTEGER NOT NULL,
                   recorded_at TEXT NOT NULL,
                   PRIMARY KEY(chat_id, message_id))"""
        )
        # Added after the first release — ALTER only when the column is absent
        # so existing news.db files migrate without losing rows.
        cols = {r["name"] for r in c.execute("PRAGMA table_info(items)")}
        if "attempts" not in cols:
            c.execute("ALTER TABLE items ADD COLUMN attempts INTEGER DEFAULT 0")


def _item(row) -> dict:
    """Row → dict with the JSON columns decoded."""
    d = dict(row)
    d["media"] = json.loads(d["media"] or "[]")
    d["regions"] = json.loads(d["regions"]) if d["regions"] else []
    d["posted"] = json.loads(d["posted"] or "{}")
    return d


def enqueue(source: str, text: str, media: list) -> int | None:
    """Insert a fresh item (status='new'). Returns its id, or None when an
    item with identical text already arrived inside DEDUP_WINDOW_H."""
    h = content_hash(text)
    with _conn() as c:
        if h:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=DEDUP_WINDOW_H)
            ).isoformat(timespec="seconds")
            dup = c.execute(
                "SELECT id FROM items WHERE content_hash=? AND collected_at>=? LIMIT 1",
                (h, cutoff),
            ).fetchone()
            if dup:
                return None
        cur = c.execute(
            "INSERT INTO items(source, collected_at, text, media, content_hash)"
            " VALUES(?,?,?,?,?)",
            (source, _now(), text or "", json.dumps(media or []), h),
        )
        return cur.lastrowid


def next_by_status(status: str) -> dict | None:
    """Oldest item in `status` (FIFO), or None."""
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM items WHERE status=? ORDER BY id LIMIT 1", (status,)
        ).fetchone()
        return _item(row) if row else None


def get_item(item_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return _item(row) if row else None


def set_score(item_id: int, score: int, regions: list, status: str) -> None:
    """Record the scorer's verdict and advance the item ('queued' or
    'awaiting_approval' — the caller applies the tier rules)."""
    with _conn() as c:
        c.execute(
            "UPDATE items SET score=?, regions=?, status=? WHERE id=?",
            (score, json.dumps(regions), status, item_id),
        )


def set_status(item_id: int, status: str) -> None:
    with _conn() as c:
        c.execute("UPDATE items SET status=? WHERE id=?", (status, item_id))


def record_post(item_id: int, platform: str, target: str, link: str | None = None) -> None:
    """Record that `item_id` went out on `platform` to `target` (a chat id or a
    YouTube account), with its public link when there is one.

    Called once per destination. Re-recording the same destination is a no-op
    (unique index) except that a link discovered later replaces a missing one.
    Also stamps the legacy items.posted JSON and status so the coarse overview
    stays truthful — routing itself reads the posts table."""
    with _conn() as c:
        c.execute(
            "INSERT INTO posts(item_id, platform, target, link, posted_at)"
            " VALUES(?,?,?,?,?)"
            " ON CONFLICT(item_id, platform, target) DO UPDATE SET"
            " link=COALESCE(excluded.link, posts.link)",
            (item_id, platform, target, link, _now()),
        )
        row = c.execute("SELECT posted FROM items WHERE id=?", (item_id,)).fetchone()
        posted = json.loads(row["posted"] or "{}") if row else {}
        posted[platform] = _now()
        c.execute(
            "UPDATE items SET posted=?, status='posted', attempts=0 WHERE id=?",
            (json.dumps(posted), item_id),
        )


def bump_channel_posts(target: str) -> int:
    """Count one more post to `target` and return the new lifetime total.

    Counts BOTH autopilot and manual posts, since the SMM panel doesn't care
    which one filled the channel. Monotonic — callers test `n % threshold`
    rather than resetting, so nothing is lost to a restart mid-cycle."""
    with _conn() as c:
        c.execute(
            "INSERT INTO channel_counters(target, posts) VALUES(?, 1)"
            " ON CONFLICT(target) DO UPDATE SET posts = posts + 1",
            (target,),
        )
        row = c.execute(
            "SELECT posts FROM channel_counters WHERE target=?", (target,)
        ).fetchone()
        return row["posts"] if row else 0


def channel_post_counts() -> dict:
    """{channel: lifetime posts} — for /queue."""
    with _conn() as c:
        rows = c.execute("SELECT target, posts FROM channel_counters ORDER BY target")
        return {r["target"]: r["posts"] for r in rows}


def last_post_at(platform: str) -> str | None:
    """ISO timestamp of the most recent publication on `platform`, or None.

    The drip reads this on startup so a restart resumes the rhythm instead of
    posting again — with a daily cadence, "5 minutes after boot" would turn
    five restarts into five posts."""
    with _conn() as c:
        row = c.execute(
            "SELECT MAX(posted_at) AS t FROM posts WHERE platform=?", (platform,)
        ).fetchone()
        return row["t"] if row and row["t"] else None


def links_for(item_id: int, platform: str) -> list[tuple[str, str | None]]:
    """[(target, link)] already published for this item on this platform."""
    with _conn() as c:
        rows = c.execute(
            "SELECT target, link FROM posts WHERE item_id=? AND platform=? ORDER BY id",
            (item_id, platform),
        )
        return [(r["target"], r["link"]) for r in rows]


def candidates(
    platform: str, min_score: int, max_age_h: int, limit: int = 10
) -> list[dict]:
    """Best unpublished items for `platform`, most important first.

    Eligible = scored at or above `min_score`, collected within `max_age_h`,
    not dead ('failed'/'rejected'), and with no `posts` row for this platform.
    An item already on another platform stays eligible here — that is the
    whole point of the per-platform log.

    Several are returned because the top-scoring item may target regions you
    don't run a channel for; the caller walks the list until one matches
    instead of letting that item block the drip forever."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=max_age_h)
    ).isoformat(timespec="seconds")
    with _conn() as c:
        rows = c.execute(
            """SELECT * FROM items
                WHERE score IS NOT NULL
                  AND score >= ?
                  AND status NOT IN ('failed', 'rejected')
                  AND collected_at >= ?
                  AND NOT EXISTS (SELECT 1 FROM posts
                                   WHERE posts.item_id = items.id
                                     AND posts.platform = ?)
                ORDER BY score DESC, collected_at DESC
                LIMIT ?""",
            (min_score, cutoff, platform, limit),
        )
        return [_item(r) for r in rows]


def bump_attempts(item_id: int, max_attempts: int = 3) -> int:
    """Count one failed publishing attempt. At `max_attempts` the item is
    marked 'failed' so a permanently broken post (bad media, oversized
    caption) can't be re-picked on every tick forever. Returns the new count."""
    with _conn() as c:
        c.execute(
            "UPDATE items SET attempts=COALESCE(attempts,0)+1 WHERE id=?", (item_id,)
        )
        row = c.execute("SELECT attempts FROM items WHERE id=?", (item_id,)).fetchone()
        n = row["attempts"] if row else 0
        if n >= max_attempts:
            c.execute("UPDATE items SET status='failed' WHERE id=?", (item_id,))
        return n


def counts() -> dict:
    """{status: n} — for a quick /queue-style overview."""
    with _conn() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM items GROUP BY status")
        return {r["status"]: r["n"] for r in rows}


def get_cursor(source: str) -> int:
    with _conn() as c:
        row = c.execute("SELECT last_id FROM cursor WHERE source=?", (source,)).fetchone()
        return row["last_id"] if row else 0


def set_cursor(source: str, last_id: int) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO cursor(source, last_id) VALUES(?,?) "
            "ON CONFLICT(source) DO UPDATE SET last_id=excluded.last_id",
            (source, last_id),
        )


# --------------------------------------------------------------------------- #
# Reaction asks                                                               #
# --------------------------------------------------------------------------- #


def _ask(row) -> dict:
    d = dict(row)
    d["state"] = json.loads(d["state"] or "{}")
    return d


def open_ask(item_id: int, state: dict) -> int:
    """Open a reaction ask for a published item. `state` is the reducer's
    initial state (reactions.new_state) and carries the channels + links this
    ask can order against. Returns the ask id, which is what rides in the
    callback data."""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO asks(item_id, state, status, created_at) VALUES(?,?,'open',?)",
            (item_id, json.dumps(state), _now()),
        )
        return cur.lastrowid


def bind_ask(ask_id: int, chat_id, message_id: int) -> None:
    """Remember where the ask's message ended up, so /asks can edit it rather
    than posting a duplicate."""
    with _conn() as c:
        c.execute(
            "UPDATE asks SET chat_id=?, message_id=? WHERE id=?",
            (str(chat_id), message_id, ask_id),
        )


def get_ask(ask_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM asks WHERE id=?", (ask_id,)).fetchone()
        return _ask(row) if row else None


def save_ask_state(ask_id: int, state: dict) -> None:
    with _conn() as c:
        c.execute("UPDATE asks SET state=? WHERE id=?", (json.dumps(state), ask_id))


def close_ask(ask_id: int, status: str) -> None:
    """Finish an ask: 'applied' (orders placed) or 'skipped'."""
    with _conn() as c:
        c.execute("UPDATE asks SET status=? WHERE id=?", (status, ask_id))


def open_asks() -> list[dict]:
    """Every unanswered ask, oldest first — what /asks re-renders."""
    with _conn() as c:
        rows = c.execute("SELECT * FROM asks WHERE status='open' ORDER BY id")
        return [_ask(r) for r in rows]


# --------------------------------------------------------------------------- #
# Control-group message tracking (weekly cleanup)                             #
# --------------------------------------------------------------------------- #


def track_group_message(chat_id, message_id: int) -> None:
    """Remember one control-group message id for the weekly wipe. Idempotent."""
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO group_messages(chat_id, message_id, recorded_at)"
            " VALUES(?,?,?)",
            (str(chat_id), message_id, _now()),
        )


def tracked_message_ids(chat_id) -> list[int]:
    with _conn() as c:
        rows = c.execute(
            "SELECT message_id FROM group_messages WHERE chat_id=?"
            " ORDER BY message_id",
            (str(chat_id),),
        )
        return [r["message_id"] for r in rows]


def clear_group_messages(chat_id, message_ids: list[int]) -> None:
    with _conn() as c:
        c.executemany(
            "DELETE FROM group_messages WHERE chat_id=? AND message_id=?",
            [(str(chat_id), m) for m in message_ids],
        )
