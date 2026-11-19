"""Alert messages and buttons — pure (Telegram HTML strings + button specs).

Callback namespace `r:`
  r:w:<incident>        toggle 👀 Watch (watched incidents ping again when they grow)
  r:x:<incident>        ✕ Not interesting -> the reasons row
  r:xr:<incident>:<k>   the reason (kept for learning) — closes the alert
  r:b:<incident>        back from the reasons row
"""
import datetime as dt
import html
import json

from modules.radar.sources.hazards import snap_map

EMOJI = {
    "robbery": "💰", "shooting": "🔫", "stabbing": "🔪", "fight": "👊", "police": "🚓",
    "chase": "🚨", "crash": "💥", "fire": "🔥", "explosion": "💥", "weather": "🌪",
    "flood": "🌊", "earthquake": "🌍", "disaster": "⚠️", "rescue": "🛟", "protest": "📢",
    "riot": "🔥", "politics": "🏛", "war": "🎖", "animal": "🐾", "crowd": "👥",
    "stunt": "🤸", "other": "📍",
}

REASONS = {
    "boring": "Boring", "old": "Old", "everywhere": "Already everywhere",
    "audience": "Not my audience", "graphic": "Too graphic",
}


def esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def ago(seconds: float) -> str:
    m = int(max(seconds, 0) // 60)
    if m < 1:
        return "just now"
    if m < 60:
        return f"{m} min ago"
    return f"{m // 60} h {m % 60} min ago"


def _source_name(src: str) -> str:
    fam, _, rest = src.partition(":")
    if fam == "tg":
        return rest
    if fam == "reddit":
        return f"r/{rest}"
    return fam.upper()


def _links(items, limit: int = 4) -> str:
    seen, parts = set(), []
    for it in items:
        if it["source"] in seen or not it["link"]:
            continue
        seen.add(it["source"])
        parts.append(f'<a href="{esc(it["link"])}">{esc(_source_name(it["source"]))}</a>')
        if len(parts) >= limit:
            break
    more = len({i["source"] for i in items}) - len(parts)
    return " · ".join(parts) + (f" +{more}" if more > 0 else "")


def incident_text(inc, items, now: float, note: str = "") -> str:
    cat = inc["category"] or "other"
    where = ", ".join(x for x in (inc["place"], inc["country"]) if x)
    first = min(i["time"] for i in items) if items else now
    sources = len({i["source"] for i in items})
    videos = sum(1 for i in items if i["has_video"])
    head = f'{EMOJI.get(cat, "📍")} <b>{esc(cat.upper())}</b>'
    if where:
        head += f" · {esc(where)}"
    head += f" · {ago(now - first)}"
    lines = [head, esc(inc["title"] or (items[0]["text"] if items else ""))[:300]]
    facts = [f"📡 {sources} source{'s' * (sources != 1)}", f"🎥 {videos}"]
    if inc["english"] in ("yes", "no"):
        facts.append("🗣 English" if inc["english"] == "yes" else "🗣 not English")
    for it in items:
        if it["source"].startswith("reddit:"):
            m = json.loads(it["metrics"] or "{}")
            facts.append(f"⬆ {m.get('ups', 0)} ({m.get('velocity', 0)}/min)")
            break
    if inc["graphic"]:
        facts.append("⚠️ graphic")
    facts.append(f"score {inc['score']}")
    lines.append(" · ".join(facts))
    lines.append("🔗 " + _links(items))
    reddit_only = all(i["source"].startswith("reddit:") for i in items if i["has_video"])
    lines.append("<i>→ send the link to the news bot</i>" if reddit_only
                 else "<i>→ forward the video above to the news bot</i>")
    if inc["watch"]:
        lines.append("👀 watching")
    if inc["ignored"]:
        lines.append(f"✕ not interesting — {esc(REASONS.get(inc['ignored'], inc['ignored']))}")
    if note:
        lines.append(note)
    return "\n".join(lines)


def keyboard(inc) -> list:
    """Rows of (label, callback_data); [] once the alert is closed."""
    if inc["ignored"]:
        return []
    watch = "👀 Watching ✓" if inc["watch"] else "👀 Watch"
    return [[(watch, f"r:w:{inc['id']}"), ("✕ Not interesting", f"r:x:{inc['id']}")]]


def reasons_keyboard(inc_id: int) -> list:
    keys = list(REASONS.items())
    rows = [[(label, f"r:xr:{inc_id}:{k}") for k, label in keys[:3]],
            [(label, f"r:xr:{inc_id}:{k}") for k, label in keys[3:]]]
    rows.append([("↩ Back", f"r:b:{inc_id}")])
    return rows


def parse_callback(data: str):
    """('w'|'x'|'xr'|'b', incident id, reason|None) or None."""
    parts = (data or "").split(":")
    if len(parts) < 3 or parts[0] != "r" or parts[1] not in ("w", "x", "xr", "b"):
        return None
    try:
        inc = int(parts[2])
    except ValueError:
        return None
    reason = parts[3] if parts[1] == "xr" and len(parts) > 3 else None
    if parts[1] == "xr" and reason not in REASONS:
        return None
    return parts[1], inc, reason


def hazard_text(item, now: float) -> str:
    cat = item.metrics.get("category", "disaster")
    lines = [f'{EMOJI.get(cat, "⚠️")} <b>{esc(cat.upper())}</b> · {esc(item.place or item.country)}'
             f" · {ago(now - item.time)}",
             esc(item.text)]
    links = []
    if item.link:
        links.append(f'<a href="{esc(item.link)}">{esc(item.source.upper())}</a>')
    sm = snap_map(item.lat, item.lon)
    if sm:
        links.append(f'<a href="{esc(sm)}">👻 Snap Map here</a>')
    if links:
        lines.append("🔗 " + " · ".join(links))
    lines.append("<i>→ no video yet: look on Snap Map / TikTok / X for this place</i>")
    return "\n".join(lines)


def spike_text(sp: dict, samples) -> str:
    normal = f"normal ≈{sp['expected']:g}" if sp["expected"] else "no history yet"
    lines = [f"📈 <b>Spike: “{esc(sp['term'])}”</b> — {sp['hits']} mentions in the last "
             f"window from {sp['sources']} sources ({normal})"]
    for s in samples:
        snippet = esc(" ".join((s["text"] or "").split())[:140])
        name = esc(_source_name(s["source"]))
        lines.append(f'• <a href="{esc(s["link"])}">{name}</a>: {snippet}' if s["link"]
                     else f"• {name}: {snippet}")
    lines.append("<i>→ something is happening; videos usually follow</i>")
    return "\n".join(lines)


def briefing_text(today: dt.date, upcoming: list, hazards: list, top: list) -> str:
    lines = [f"🌙 <b>Radar briefing — {today:%a %d %b}</b>", "", "<b>Coming up (7 days)</b>"]
    lines += [f"• {d:%a %d %b} — {esc(t)}" for d, t in upcoming] or ["• nothing on the calendar"]
    lines += ["", "<b>Hazards in the last 24 h</b>"]
    lines += [f"• {esc(h['title'])}" for h in hazards] or ["• none"]
    lines += ["", "<b>Top alerts in the last 24 h</b>"]
    lines += [f"• {EMOJI.get(i['category'] or 'other', '📍')} {esc(i['title'])} (score {i['score']})"
              for i in top] or ["• none"]
    return "\n".join(lines)
