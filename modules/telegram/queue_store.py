"""
modules/telegram/queue_store.py — SQLite-backed NewsItem store for the smart
filter, plus the collector's per-source cursor.

The collector enqueues raw posts (status='new'); the scorer fills in
score/regions and advances them to 'queued' or 'awaiting_approval'; the
dispatcher posts them and records where they went in `posted`. Plain stdlib
sqlite3, no extra dependency. DB lives under config.TG_DATA_DIR.

Item lifecycle:
    new → queued → posted | failed
        ↘ awaiting_approval → queued (approved) | rejected
Exact-text duplicates are dropped at enqueue time (see content_hash) so a
story forwarded across several source channels is stored — and scored — once.
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
    """Create tables if missing. Safe to call on every startup."""
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


def mark_posted(item_id: int, platform: str) -> None:
    """Stamp `platform` into the posted JSON and set status='posted'.
    Called once per platform — later platforms just add their key."""
    with _conn() as c:
        row = c.execute("SELECT posted FROM items WHERE id=?", (item_id,)).fetchone()
        posted = json.loads(row["posted"] or "{}") if row else {}
        posted[platform] = _now()
        c.execute(
            "UPDATE items SET posted=?, status='posted' WHERE id=?",
            (json.dumps(posted), item_id),
        )


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
