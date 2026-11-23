"""The radar's brain: item -> label -> incident -> score -> alert.

Transport-free: `notifier` is whatever puts messages in the Radar group (the
live one is in main.py, tests pass a fake). Alerts go out THE MOMENT an
incident crosses RADAR_ALERT_MIN_SCORE — any hour (owner, 2026-11-19).

Rules:
  * Only an incident WITH VIDEO alerts, and only when the AI calls it a real
    event rated RADAR_AI_MIN+ (RADAR_AI_MIN_US+ in the US) — score.passes. Text-only posts still count: they
    join incidents (more sources = higher score) and feed spikes.
  * One incident alerts once. When it grows (a new source or a new video)
    the alert is edited in place; if you tapped 👀 Watch it also pings.
  * Hazards (quake / NWS / GDACS / NHC) alert on their own, once, the first
    time they qualify. The first poll of each hazard feed only records what's
    already there (otherwise every quake of the past day alerts at startup).
  * Spikes: see spikes.py; one that's about an incident already alerted, or
    about the same story as a spike just sent, stays quiet.
"""
import asyncio
import datetime as dt
import json
import logging
import time

from modules.radar import cluster, score as SC, settings as S, spikes, text as T
from modules.radar import alert as A
from modules.radar.label import label as ai_label
from modules.radar.sources import hazards, reddit

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, store, notifier, labeler=ai_label, clock=time.time):
        self.store = store
        self.notifier = notifier
        self.labeler = labeler
        self.clock = clock
        self._lock = asyncio.Lock()

    # --- intake -------------------------------------------------------------
    def seed(self, items):
        """Record without alerting (the first poll of a hazard feed)."""
        for it in items:
            row_id, new = self.store.add_item(it, self.clock())
            if new:
                self.store.mark_processed(row_id, None, None)

    async def ingest(self, it):
        async with self._lock:     # one item at a time: clustering reads then writes
            now = self.clock()
            row_id, new = self.store.add_item(it, now)
            if new and it.kind == "post":
                self.store.add_terms(T.spike_terms(it.text), int(now // 3600), it.source)
            if self.store.item(row_id)["processed"]:
                return
            if it.kind == "hazard":
                await self._hazard(row_id, it, now)
            else:
                await self._post(row_id, it, now)

    # --- posts ----------------------------------------------------------------
    async def _label(self, it, now: float):
        if S.AI_MAX_PER_DAY <= 0:
            return None
        day = dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y-%m-%d")
        if self.store.ai_calls(day) >= S.AI_MAX_PER_DAY:
            return None
        self.store.bump_ai(day)
        return await asyncio.to_thread(self.labeler, it.text, it.thumb, it.source)

    async def _post(self, row_id: int, it, now: float):
        if it.family == "reddit" and not reddit.worth_a_look(it):
            return                 # stays unprocessed; the next poll re-checks it
        lab = await self._label(it, now) if it.has_video else None
        toks = T.tokens(it.text + " " + (lab.get("title", "") if lab else ""))
        since = now - S.CLUSTER_WINDOW_H * 3600
        inc_id = self.store.same_video(it.media_size, since)
        if inc_id is None:
            best = cluster.pick(toks, lab, self.store.open_incidents(since))
            inc_id = best["id"] if best else None
        if inc_id is None:
            inc_id = self.store.new_incident(now, "post", toks, {})
        else:
            inc = self.store.incident(inc_id)
            self.store.update_incident(
                inc_id, tokens=cluster.merge_tokens(inc["tokens"], toks), updated=now)
        self.store.mark_processed(row_id, lab, inc_id)
        await self._evaluate(inc_id, now)

    def _summarise(self, items, now: float) -> dict:
        labels = [json.loads(i["label"]) for i in items if i["label"]]
        best = max(labels, key=lambda l: l.get("newsworthy", 0), default=None)
        velocity = 0.0
        for i in items:
            velocity = max(velocity, json.loads(i["metrics"] or "{}").get("velocity", 0))
        us = any(l.get("us") for l in labels) or any(
            i["country"] == "United States" for i in items)
        has_video = any(i["has_video"] for i in items)
        sources = len({i["source"] for i in items})
        newest = max(i["time"] for i in items)
        sc = SC.score(label=best, has_video=has_video, sources=sources,
                      newest_age_s=now - newest, us=us, velocity=velocity)
        title = (best or {}).get("title") or " ".join((items[0]["text"] or "").split())[:120]
        return {
            "score": sc, "has_video": int(has_video), "us": int(us), "title": title,
            "category": (best or {}).get("category", ""),
            "place": (best or {}).get("place", ""),
            "country": (best or {}).get("country", ""),
            "graphic": int(any(l.get("graphic") for l in labels)),
            "english": (best or {}).get("english", ""),
            "_sources": sources, "_videos": sum(1 for i in items if i["has_video"]),
            "_gate": SC.passes(labels, us),
        }

    def _room(self, now: float) -> bool:
        return S.MAX_PER_HOUR <= 0 or self.store.alerts_since(now - 3600) < S.MAX_PER_HOUR

    async def _evaluate(self, inc_id: int, now: float):
        items = self.store.incident_items(inc_id)
        if not items:
            return
        info = self._summarise(items, now)
        n_sources, n_videos = info.pop("_sources"), info.pop("_videos")
        gate = info.pop("_gate")
        self.store.update_incident(inc_id, updated=now, **info)
        inc = self.store.incident(inc_id)
        if inc["ignored"] or not inc["has_video"] or SC.blocked(inc["category"]):
            return
        if inc["alert_msg_id"] is None:
            if not gate or inc["score"] < S.ALERT_MIN_SCORE:
                return
            if not self._room(now):
                log.warning("alert cap reached (%d/h) — incident %s waits", S.MAX_PER_HOUR, inc_id)
                return
            video = next((i for i in reversed(items) if i["has_video"]), None)
            msg_id = await self.notifier.alert(inc, items, video)
            self.store.update_incident(inc_id, alert_msg_id=msg_id, alerted_at=now,
                                       alerted_sources=n_sources, alerted_videos=n_videos)
            self.store.note_alert(inc_id, now)
            return
        grew_sources = n_sources > inc["alerted_sources"]
        grew_videos = n_videos > inc["alerted_videos"]
        if grew_sources or grew_videos:
            new_video = items[-1] if grew_videos and items[-1]["has_video"] else None
            note = f"📈 update: {n_sources} sources, {n_videos} videos"
            await self.notifier.update(inc, items, note, new_video, bool(inc["watch"]))
            self.store.update_incident(inc_id, alerted_sources=n_sources,
                                       alerted_videos=n_videos)

    # --- hazards --------------------------------------------------------------
    def _same_quake(self, it, now: float) -> bool:
        """USGS often files one quake twice (a revised id, M5.0 vs M5.1, the
        Vanuatu M6.5 five minutes apart): one alert per area per QUAKE_DEDUPE_H."""
        if it.source != "usgs":
            return False
        key = hazards.area(it.place)
        since = now - S.QUAKE_DEDUPE_H * 3600
        return any(i["kind"] == "hazard" and i["category"] == "earthquake"
                   and i["alert_msg_id"] is not None and hazards.area(i["place"]) == key
                   for i in self.store.open_incidents(since))

    async def _hazard(self, row_id: int, it, now: float):
        if not it.metrics.get("alert"):
            return                 # may qualify on a later poll (quake "felt" reports)
        if self._same_quake(it, now):
            self.store.mark_processed(row_id, None, None)
            return
        inc_id = self.store.new_incident(now, "hazard", set(), {
            "category": it.metrics.get("category", "disaster"), "place": it.place,
            "country": it.country, "title": it.text.split("\n")[0][:200], "score": 90,
            "us": int(it.country == "United States")})
        self.store.mark_processed(row_id, None, inc_id)
        if not self._room(now):
            log.warning("alert cap reached — hazard %s not sent", it.ext_id)
            return
        msg_id = await self.notifier.hazard(it)
        self.store.update_incident(inc_id, alert_msg_id=msg_id, alerted_at=now)
        self.store.note_alert(inc_id, now)

    # --- spikes ---------------------------------------------------------------
    async def check_spikes(self):
        async with self._lock:
            now = self.clock()
            found = spikes.check(self.store, now)
            since = now - S.SPIKE_WINDOW_H * 3600
            covered = [set((i["tokens"] or "").split())
                       for i in self.store.open_incidents(since) if i["alert_msg_id"]]
            sent = []
            for sp in found:
                term = sp["term"]
                self.store.mark_spike(term, now)
                if any(term in toks for toks in covered):
                    continue       # an alert about this is already in the group
                samples = self.store.recent_texts(term, since)
                texts = [(s["text"] or "").lower() for s in samples]
                if any(sum(t in x for x in texts) * 2 >= len(texts) for t in sent):
                    continue       # same story as a spike just sent
                if not self._room(now):
                    break
                await self.notifier.text(A.spike_text(sp, samples))
                self.store.note_alert(0, now)
                sent.append(term)
            return sent

    # --- buttons --------------------------------------------------------------
    def on_button(self, verb: str, inc_id: int, reason):
        """Apply a tap; returns the incident row after it (or None)."""
        inc = self.store.incident(inc_id)
        if inc is None:
            return None
        if verb == "w":
            self.store.update_incident(inc_id, watch=0 if inc["watch"] else 1)
        elif verb == "xr":
            self.store.update_incident(inc_id, ignored=reason, watch=0)
        return self.store.incident(inc_id)

    # --- briefing -------------------------------------------------------------
    def briefing(self, today: dt.date, upcoming: list) -> str:
        since = self.clock() - 86400
        rows = self.store.db.execute(
            "SELECT title FROM incidents WHERE kind='hazard' AND created>=?"
            " ORDER BY created DESC LIMIT 8", (since,)).fetchall()
        return A.briefing_text(today, upcoming, rows, self.store.top_incidents(since))
