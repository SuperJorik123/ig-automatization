"""The radar's own SQLite database (`RADAR_DATA_DIR/radar.db`).

items      — every post/hazard seen (deduped by source + ext_id)
incidents  — groups of items about the same event; what an alert is about
term_hits  — word counts per hour and source, the spike baseline
spikes     — when a word last spiked (cooldown)
ai_calls   — labelling calls per day (the budget)
alerts     — when alerts went out (the hourly runaway guard)

Everything older than KEEP_DAYS is pruned daily.
"""
import json
import os
import sqlite3
import time

KEEP_DAYS = 8

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    source TEXT, ext_id TEXT, time REAL, seen REAL, text TEXT, link TEXT,
    kind TEXT, has_video INTEGER, media_size INTEGER, duration REAL,
    place TEXT, country TEXT, metrics TEXT, ref TEXT, label TEXT,
    processed INTEGER DEFAULT 0, incident_id INTEGER,
    UNIQUE(source, ext_id)
);
CREATE INDEX IF NOT EXISTS items_seen ON items(seen);
CREATE INDEX IF NOT EXISTS items_incident ON items(incident_id);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY,
    created REAL, updated REAL, kind TEXT,
    category TEXT, place TEXT, country TEXT, us INTEGER DEFAULT 0,
    title TEXT, summary TEXT, tokens TEXT,
    score INTEGER DEFAULT 0, has_video INTEGER DEFAULT 0, graphic INTEGER DEFAULT 0,
    english TEXT DEFAULT '',
    alert_msg_id INTEGER, alerted_at REAL, alerted_sources INTEGER DEFAULT 0,
    alerted_videos INTEGER DEFAULT 0,
    watch INTEGER DEFAULT 0, ignored TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS incidents_updated ON incidents(updated);
CREATE TABLE IF NOT EXISTS term_hits (
    term TEXT, hour INTEGER, source TEXT, n INTEGER,
    PRIMARY KEY (term, hour, source)
);
CREATE INDEX IF NOT EXISTS term_hits_hour ON term_hits(hour);
CREATE TABLE IF NOT EXISTS spikes (term TEXT PRIMARY KEY, at REAL);
CREATE TABLE IF NOT EXISTS ai_calls (day TEXT PRIMARY KEY, n INTEGER);
CREATE TABLE IF NOT EXISTS alerts (at REAL, incident_id INTEGER);
"""


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        self.db.commit()

    # --- items --------------------------------------------------------------
    def add_item(self, it, now: float | None = None):
        """(row id, is_new). An existing item gets its metrics refreshed —
        a Reddit post is seen on every poll while it rises."""
        now = now or time.time()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO items (source, ext_id, time, seen, text, link, kind,"
            " has_video, media_size, duration, place, country, metrics, ref)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (it.source, it.ext_id, it.time, now, it.text, it.link, it.kind,
             int(it.has_video), it.media_size, it.duration, it.place, it.country,
             json.dumps(it.metrics), json.dumps(it.ref)))
        if cur.rowcount:
            self.db.commit()
            return cur.lastrowid, True
        row = self.db.execute("SELECT id FROM items WHERE source=? AND ext_id=?",
                              (it.source, it.ext_id)).fetchone()
        self.db.execute("UPDATE items SET metrics=? WHERE id=?",
                        (json.dumps(it.metrics), row["id"]))
        self.db.commit()
        return row["id"], False

    def item(self, item_id: int):
        return self.db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()

    def mark_processed(self, item_id: int, label: dict | None, incident_id: int):
        self.db.execute("UPDATE items SET processed=1, label=?, incident_id=? WHERE id=?",
                        (json.dumps(label) if label else None, incident_id, item_id))
        self.db.commit()

    def incident_items(self, incident_id: int) -> list:
        return self.db.execute("SELECT * FROM items WHERE incident_id=? ORDER BY time",
                               (incident_id,)).fetchall()

    def same_video(self, media_size: int, since: float):
        """An incident already holding a video of exactly this size (a repost)."""
        if not media_size:
            return None
        row = self.db.execute(
            "SELECT incident_id FROM items WHERE media_size=? AND seen>=?"
            " AND incident_id IS NOT NULL ORDER BY seen DESC LIMIT 1",
            (media_size, since)).fetchone()
        return row["incident_id"] if row else None

    def recent_texts(self, term: str, since: float, limit: int = 4) -> list:
        return self.db.execute(
            "SELECT source, text, link FROM items WHERE seen>=? AND lower(text) LIKE ?"
            " ORDER BY seen DESC LIMIT ?", (since, f"%{term}%", limit)).fetchall()

    # --- incidents ----------------------------------------------------------
    def new_incident(self, now: float, kind: str, tokens: set, fields: dict) -> int:
        cur = self.db.execute(
            "INSERT INTO incidents (created, updated, kind, tokens) VALUES (?,?,?,?)",
            (now, now, kind, " ".join(sorted(tokens))))
        self.db.commit()
        self.update_incident(cur.lastrowid, **fields)
        return cur.lastrowid

    def update_incident(self, incident_id: int, **fields):
        if not fields:
            return
        if "tokens" in fields and not isinstance(fields["tokens"], str):
            fields["tokens"] = " ".join(sorted(fields["tokens"]))
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE incidents SET {cols} WHERE id=?",
                        (*fields.values(), incident_id))
        self.db.commit()

    def incident(self, incident_id: int):
        return self.db.execute("SELECT * FROM incidents WHERE id=?",
                               (incident_id,)).fetchone()

    def open_incidents(self, since: float) -> list:
        return self.db.execute("SELECT * FROM incidents WHERE updated>=?",
                               (since,)).fetchall()

    def top_incidents(self, since: float, limit: int = 5) -> list:
        return self.db.execute(
            "SELECT * FROM incidents WHERE created>=? AND alert_msg_id IS NOT NULL"
            " AND ignored='' ORDER BY score DESC LIMIT ?", (since, limit)).fetchall()

    def incident_by_alert(self, msg_id: int):
        return self.db.execute("SELECT * FROM incidents WHERE alert_msg_id=?",
                               (msg_id,)).fetchone()

    # --- spikes -------------------------------------------------------------
    def add_terms(self, terms, hour: int, source: str):
        self.db.executemany(
            "INSERT INTO term_hits (term, hour, source, n) VALUES (?,?,?,1)"
            " ON CONFLICT(term, hour, source) DO UPDATE SET n=n+1",
            [(t, hour, source) for t in terms])
        self.db.commit()

    def term_window(self, from_hour: int) -> list:
        """Per term since `from_hour`: hits and distinct sources."""
        return self.db.execute(
            "SELECT term, SUM(n) AS hits, COUNT(DISTINCT source) AS sources"
            " FROM term_hits WHERE hour>=? GROUP BY term", (from_hour,)).fetchall()

    def term_baseline(self, term: str, from_hour: int, to_hour: int) -> int:
        row = self.db.execute(
            "SELECT COALESCE(SUM(n),0) AS n FROM term_hits WHERE term=? AND hour>=?"
            " AND hour<?", (term, from_hour, to_hour)).fetchone()
        return row["n"]

    def first_hour(self):
        row = self.db.execute("SELECT MIN(hour) AS h FROM term_hits").fetchone()
        return row["h"]

    def spike_at(self, term: str):
        row = self.db.execute("SELECT at FROM spikes WHERE term=?", (term,)).fetchone()
        return row["at"] if row else None

    def mark_spike(self, term: str, now: float):
        self.db.execute("INSERT OR REPLACE INTO spikes (term, at) VALUES (?,?)", (term, now))
        self.db.commit()

    # --- budget / alert guard ----------------------------------------------
    def ai_calls(self, day: str) -> int:
        row = self.db.execute("SELECT n FROM ai_calls WHERE day=?", (day,)).fetchone()
        return row["n"] if row else 0

    def bump_ai(self, day: str):
        self.db.execute("INSERT INTO ai_calls (day, n) VALUES (?,1)"
                        " ON CONFLICT(day) DO UPDATE SET n=n+1", (day,))
        self.db.commit()

    def note_alert(self, incident_id: int, now: float):
        self.db.execute("INSERT INTO alerts (at, incident_id) VALUES (?,?)", (now, incident_id))
        self.db.commit()

    def alerts_since(self, since: float) -> int:
        return self.db.execute("SELECT COUNT(*) AS n FROM alerts WHERE at>=?",
                               (since,)).fetchone()["n"]

    def counts_since(self, since: float) -> dict:
        rows = self.db.execute("SELECT source, COUNT(*) AS n FROM items WHERE seen>=?"
                               " GROUP BY source", (since,)).fetchall()
        return {r["source"]: r["n"] for r in rows}

    # --- housekeeping -------------------------------------------------------
    def prune(self, now: float):
        cut = now - KEEP_DAYS * 86400
        self.db.execute("DELETE FROM items WHERE seen<?", (cut,))
        self.db.execute("DELETE FROM incidents WHERE updated<?", (cut,))
        self.db.execute("DELETE FROM term_hits WHERE hour<?", (int(cut // 3600),))
        self.db.execute("DELETE FROM alerts WHERE at<?", (cut,))
        self.db.commit()
