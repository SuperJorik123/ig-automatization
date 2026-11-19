"""One cheap AI call per VIDEO item: what is it, where, is it worth a reel.

Text-only posts are never sent (they only feed grouping and spikes), so the
spend follows the number of videos, and `RADAR_AI_MAX_PER_DAY` caps it — past
the cap, items are scored by the rules alone. The thumbnail goes along when
there is one (a Telegram video's own preview, a Reddit preview URL): the
caption of a repost channel often says nothing ("😳😳"), the picture does.

Never raises: no key, no budget, a bad reply -> None.
"""
import base64
import json
import logging
import re

from modules.radar import settings as S

log = logging.getLogger(__name__)

CATEGORIES = [
    "robbery", "shooting", "stabbing", "fight", "police", "chase", "crash",
    "fire", "explosion", "weather", "flood", "earthquake", "disaster", "rescue",
    "protest", "riot", "politics", "war", "animal", "crowd", "stunt", "other",
]

PROMPT = """You screen social-media posts for a viral news video account aimed at a US audience.
For the post below (text, and its video thumbnail if attached) answer ONLY with JSON:
{"incident": true/false,          // a real event caught on camera (crime, accident, disaster, clash, rescue, notable moment) — not memes, ads, talking heads, maps, studio TV
 "category": one of %s,
 "place": "city or area, '' if unknown",
 "country": "country name, '' if unknown",
 "us": true/false,                // happened in the United States
 "english": "yes" | "no" | "unknown",   // is the speech/text in the video likely English
 "graphic": true/false,           // gore, a death on camera, a child hurt — not postable as is
 "title": "max 80 chars, plain English, what happened and where",
 "newsworthy": 0-100              // how likely a US viewer stops scrolling for this clip TODAY
}
Never invent a place or facts that the post doesn't give.""" % json.dumps(CATEGORIES)


def _client():
    from openai import OpenAI
    return OpenAI(base_url="https://openrouter.ai/api/v1", api_key=S.OPENROUTER_API_KEY,
                  timeout=60)


def _image_part(thumb):
    if not thumb:
        return None
    if isinstance(thumb, (bytes, bytearray)):
        url = "data:image/jpeg;base64," + base64.b64encode(thumb).decode()
    elif isinstance(thumb, str) and thumb.startswith("https://"):
        url = thumb
    else:
        return None
    return {"type": "image_url", "image_url": {"url": url}}


def parse(reply: str) -> dict | None:
    m = re.search(r"\{.*\}", reply or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    cat = str(data.get("category", "other")).lower()
    try:
        worth = max(0, min(100, int(data.get("newsworthy", 0))))
    except (TypeError, ValueError):
        worth = 0
    return {
        "incident": bool(data.get("incident")),
        "category": cat if cat in CATEGORIES else "other",
        "place": str(data.get("place") or "")[:80],
        "country": str(data.get("country") or "")[:60],
        "us": bool(data.get("us")),
        "english": str(data.get("english") or "unknown").lower(),
        "graphic": bool(data.get("graphic")),
        "title": str(data.get("title") or "")[:120],
        "newsworthy": worth,
    }


def label(text: str, thumb=None, source: str = "") -> dict | None:
    """Blocking — callers use asyncio.to_thread."""
    if not S.OPENROUTER_API_KEY:
        return None
    content = [{"type": "text", "text": f"SOURCE: {source}\nPOST TEXT:\n{(text or '')[:1500]}"}]
    img = _image_part(thumb)
    if img:
        content.append(img)
    try:
        resp = _client().chat.completions.create(
            model=S.LABEL_MODEL, max_tokens=600,
            messages=[{"role": "system", "content": PROMPT},
                      {"role": "user", "content": content}])
        return parse(resp.choices[0].message.content)
    except Exception as exc:
        log.warning("label call failed: %s", exc)
        return None
