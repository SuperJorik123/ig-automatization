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
        # Scheduled publishes from the news bot's publish picker. In the DB,
        # not in memory, so a restart (a deploy) doesn't lose them; the
        # payload is schedule.to_payload's JSON (renders by brand name).
        c.execute(
            """CREATE TABLE IF NOT EXISTS scheduled(
                   id         INTEGER PRIMARY KEY AUTOINCREMENT,
                   due_at     TEXT NOT NULL, -- UTC ISO
                   chat_id    TEXT NOT NULL, -- the control group
                   message_id INTEGER,       -- the picker turned schedule card
                   payload    TEXT NOT NULL,
                   status     TEXT NOT NULL, -- pending|running|done|cancelled|failed
                   created_at TEXT NOT NULL)"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_status ON scheduled(status)")
        # Bundle mode (modules/telegram/bundle.py). In the DB so an open
        # bundle — and a rendered one waiting for 🚀 Publish — survives a
        # restart. `plan` is the rendered bundle: assignment, renders,
        # captions, timetable (JSON).
        c.execute(
            """CREATE TABLE IF NOT EXISTS bundles(
                   id         INTEGER PRIMARY KEY AUTOINCREMENT,
                   chat_id    TEXT NOT NULL,
                   status     TEXT NOT NULL, -- open|rendering|ready|scheduled|cancelled
                   platforms  TEXT NOT NULL, -- JSON list of plan platform keys
                   message_id INTEGER,       -- the summary message, once rendered
                   plan       TEXT,
                   created_at TEXT NOT NULL)"""
        )
        # One row per video: the plan-card state that matters (headline,
        # info, source post, media notes, the file), JSON. `card_id` is the
        # video's bundle line — replies to it are how its info arrives.
        c.execute(
            """CREATE TABLE IF NOT EXISTS bundle_videos(
                   id         INTEGER PRIMARY KEY AUTOINCREMENT,
                   bundle_id  INTEGER NOT NULL,
                   card_id    INTEGER,
                   status     TEXT NOT NULL, -- active|removed|carried
                   state      TEXT NOT NULL,
                   created_at TEXT NOT NULL)"""
        )
        # Every Instagram reel the news bot published, bundle or not: the
        # history bundle assignment checks (video_key per account), and the
        # numbers the weekly report is made of.
        c.execute(
            """CREATE TABLE IF NOT EXISTS reel_log(
                   id                INTEGER PRIMARY KEY AUTOINCREMENT,
                   bundle_id         INTEGER,
                   video_key         TEXT NOT NULL,
                   brand             TEXT NOT NULL,
                   account           TEXT NOT NULL,
                   media_id          TEXT,
                   copy_count        INTEGER,
                   posted_at         TEXT NOT NULL,
                   followers_at_post INTEGER,
                   insights_24h      TEXT,
                   insights_72h      TEXT,
                   followers_24h     INTEGER,
                   followers_72h     INTEGER)"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_reel_log_brand ON reel_log(brand)")
        sched_cols = {r["name"] for r in c.execute("PRAGMA table_info(scheduled)")}
        if "bundle_id" not in sched_cols:
            c.execute("ALTER TABLE scheduled ADD COLUMN bundle_id INTEGER")
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


# --------------------------------------------------------------------------- #
# Scheduled publishes (news bot)                                              #
# --------------------------------------------------------------------------- #


def _scheduled(row) -> dict:
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    d["due_at"] = datetime.fromisoformat(d["due_at"])
    return d


def add_scheduled(due_at: datetime, chat_id, message_id: int,
                  payload: dict, bundle_id: int | None = None) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO scheduled(due_at, chat_id, message_id, payload, status,"
            " created_at, bundle_id) VALUES(?,?,?,?,'pending',?,?)",
            (due_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
             str(chat_id), message_id, json.dumps(payload), _now(), bundle_id))
        return cur.lastrowid


def set_scheduled_payload(sched_id: int, payload: dict) -> None:
    with _conn() as c:
        c.execute("UPDATE scheduled SET payload=? WHERE id=?",
                  (json.dumps(payload), sched_id))


def scheduled_for_bundle(bundle_id: int, *statuses: str) -> list[dict]:
    marks = ",".join("?" * len(statuses))
    with _conn() as c:
        rows = c.execute(f"SELECT * FROM scheduled WHERE bundle_id=? AND "
                         f"status IN ({marks}) ORDER BY due_at",
                         (bundle_id, *statuses))
        return [_scheduled(r) for r in rows]


def get_scheduled(sched_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM scheduled WHERE id=?", (sched_id,)).fetchone()
        return _scheduled(row) if row else None


def scheduled_with_status(*statuses: str) -> list[dict]:
    """Rows in any of `statuses`, soonest first."""
    marks = ",".join("?" * len(statuses))
    with _conn() as c:
        rows = c.execute(f"SELECT * FROM scheduled WHERE status IN ({marks})"
                         " ORDER BY due_at", statuses)
        return [_scheduled(r) for r in rows]


def claim_scheduled(sched_id: int, from_status: str, to_status: str) -> bool:
    """Move a row from one status to another, only if it is still in the
    first — the one guard against a post firing twice, or firing after a
    cancel that raced it."""
    with _conn() as c:
        cur = c.execute("UPDATE scheduled SET status=? WHERE id=? AND status=?",
                        (to_status, sched_id, from_status))
        return cur.rowcount == 1


# --------------------------------------------------------------------------- #
# bundles                                                                     #
# --------------------------------------------------------------------------- #


def _bundle(row) -> dict:
    d = dict(row)
    d["platforms"] = json.loads(d["platforms"] or "[]")
    d["plan"] = json.loads(d["plan"]) if d["plan"] else None
    return d


def open_bundle(chat_id, platforms: list) -> int:
    with _conn() as c:
        return c.execute(
            "INSERT INTO bundles(chat_id, status, platforms, created_at)"
            " VALUES(?, 'open', ?, ?)",
            (str(chat_id), json.dumps(list(platforms)), _now())).lastrowid


def get_bundle(bundle_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM bundles WHERE id=?", (bundle_id,)).fetchone()
        return _bundle(row) if row else None


def current_bundle(chat_id) -> dict | None:
    """The chat's open bundle — at most one (`open_bundle` is only called
    when this returns None)."""
    with _conn() as c:
        row = c.execute("SELECT * FROM bundles WHERE chat_id=? AND status='open'"
                        " ORDER BY id DESC LIMIT 1", (str(chat_id),)).fetchone()
        return _bundle(row) if row else None


def bundles_with_status(*statuses: str) -> list[dict]:
    marks = ",".join("?" * len(statuses))
    with _conn() as c:
        rows = c.execute(f"SELECT * FROM bundles WHERE status IN ({marks})"
                         " ORDER BY id", statuses)
        return [_bundle(r) for r in rows]


def claim_bundle(bundle_id: int, from_status: str, to_status: str) -> bool:
    """Same fire-once guard as claim_scheduled: two taps on Render (or Render
    racing Cancel) can't both win."""
    with _conn() as c:
        return c.execute("UPDATE bundles SET status=? WHERE id=? AND status=?",
                         (to_status, bundle_id, from_status)).rowcount == 1


def update_bundle(bundle_id: int, **fields) -> None:
    """Set `plan` (dict), `message_id`, `platforms` or `status`."""
    if "plan" in fields and fields["plan"] is not None:
        fields["plan"] = json.dumps(fields["plan"])
    if "platforms" in fields:
        fields["platforms"] = json.dumps(list(fields["platforms"]))
    cols = ", ".join(f"{k}=?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE bundles SET {cols} WHERE id=?",
                  (*fields.values(), bundle_id))


def _bundle_video(row) -> dict:
    d = dict(row)
    d["state"] = json.loads(d["state"])
    return d


def add_bundle_video(bundle_id: int, state: dict, card_id: int | None) -> int:
    with _conn() as c:
        return c.execute(
            "INSERT INTO bundle_videos(bundle_id, card_id, status, state,"
            " created_at) VALUES(?,?,'active',?,?)",
            (bundle_id, card_id, json.dumps(state), _now())).lastrowid


def save_bundle_video(video_id: int, state: dict | None = None,
                      card_id: int | None = None, status: str | None = None,
                      bundle_id: int | None = None) -> None:
    fields = {}
    if state is not None:
        fields["state"] = json.dumps(state)
    if card_id is not None:
        fields["card_id"] = card_id
    if status is not None:
        fields["status"] = status
    if bundle_id is not None:
        fields["bundle_id"] = bundle_id
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE bundle_videos SET {cols} WHERE id=?",
                  (*fields.values(), video_id))


def bundle_videos(bundle_id: int, status: str = "active") -> list[dict]:
    """A bundle's videos in the order they joined."""
    with _conn() as c:
        rows = c.execute("SELECT * FROM bundle_videos WHERE bundle_id=? AND "
                         "status=? ORDER BY id", (bundle_id, status))
        return [_bundle_video(r) for r in rows]


def get_bundle_video(video_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM bundle_videos WHERE id=?",
                        (video_id,)).fetchone()
        return _bundle_video(row) if row else None


# --------------------------------------------------------------------------- #
# reel log                                                                    #
# --------------------------------------------------------------------------- #


def _reel(row) -> dict:
    d = dict(row)
    for key in ("insights_24h", "insights_72h"):
        d[key] = json.loads(d[key]) if d[key] else None
    return d


def log_reel(video_key: str, brand: str, account: str, media_id: str | None,
             bundle_id: int | None = None, copy_count: int | None = None,
             followers: int | None = None) -> int:
    with _conn() as c:
        return c.execute(
            "INSERT INTO reel_log(bundle_id, video_key, brand, account, media_id,"
            " copy_count, posted_at, followers_at_post) VALUES(?,?,?,?,?,?,?,?)",
            (bundle_id, video_key, brand, account, media_id, copy_count, _now(),
             followers)).lastrowid


def published_videos(brands) -> dict:
    """{brand: set of video keys it has published} — bundle history."""
    brands = list(brands)
    if not brands:
        return {}
    marks = ",".join("?" * len(brands))
    out = {b: set() for b in brands}
    with _conn() as c:
        for row in c.execute(f"SELECT brand, video_key FROM reel_log WHERE "
                             f"brand IN ({marks})", brands):
            out[row["brand"]].add(row["video_key"])
    return out


def reels_due(field: str, older_than_h: int) -> list[dict]:
    """Logged reels with a media id, older than `older_than_h`, whose
    `field` (insights_24h / insights_72h) is still empty."""
    assert field in ("insights_24h", "insights_72h")
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=older_than_h)).isoformat(timespec="seconds")
    with _conn() as c:
        rows = c.execute(f"SELECT * FROM reel_log WHERE media_id IS NOT NULL "
                         f"AND {field} IS NULL AND posted_at<=? ORDER BY id",
                         (cutoff,))
        return [_reel(r) for r in rows]


def save_insights(reel_id: int, window: str, insights: dict,
                  followers: int | None) -> None:
    assert window in ("24h", "72h")
    with _conn() as c:
        c.execute(f"UPDATE reel_log SET insights_{window}=?, followers_{window}=?"
                  " WHERE id=?", (json.dumps(insights), followers, reel_id))


def reels_since(since: datetime) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM reel_log WHERE posted_at>=? ORDER BY id",
                         (since.astimezone(timezone.utc).isoformat(timespec="seconds"),))
        return [_reel(r) for r in rows]
