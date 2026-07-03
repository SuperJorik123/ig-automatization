"""
modules/telegram/queue_store.py — SQLite-backed post queue + per-source dedup
cursor for the news aggregator.

The collector (collector.py) enqueues posts it reads from source channels; the
bot's daily job (news_bot.py) pops the oldest pending one. Plain stdlib sqlite3,
no extra dependency. DB lives under config.TG_DATA_DIR.
"""

import json
import os
import sqlite3
import time

from shared import config

_DB = os.path.join(config.TG_DATA_DIR, "news.db")


def _conn():
    os.makedirs(config.TG_DATA_DIR, exist_ok=True)
    c = sqlite3.connect(_DB)
    c.row_factory = sqlite3.Row
    return c


def init():
    """Create tables if missing. Safe to call on every startup."""
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS queue(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   source     TEXT,
                   text       TEXT,
                   media      TEXT,     -- JSON: [{"path": ..., "type": "photo"|"video"}]
                   status     TEXT DEFAULT 'pending',  -- pending | posted | failed
                   created_at REAL,
                   posted_at  REAL)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS cursor(
                   source  TEXT PRIMARY KEY,
                   last_id INTEGER)"""
        )


def enqueue(source: str, text: str, media: list) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO queue(source, text, media, created_at) VALUES(?,?,?,?)",
            (source, text or "", json.dumps(media or []), time.time()),
        )


def next_pending() -> dict | None:
    """Oldest pending post (FIFO), or None when the queue is empty."""
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM queue WHERE status='pending' ORDER BY id LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def mark_posted(item_id: int) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE queue SET status='posted', posted_at=? WHERE id=?",
            (time.time(), item_id),
        )


def mark_failed(item_id: int) -> None:
    with _conn() as c:
        c.execute("UPDATE queue SET status='failed' WHERE id=?", (item_id,))


def pending_count() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM queue WHERE status='pending'").fetchone()[0]


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
