"""Reddit "rising" on the viral-video subs — US street-level clips early.

One request covers every sub (`/r/a+b+c/rising`). With REDDIT_CLIENT_ID /
_SECRET (a free "script" app at reddit.com/prefs/apps) it uses the official
app-only OAuth; without them it tries the public JSON, which Reddit often
blocks (403) from servers — the radar then names the failure in the group.

Every poll re-reads the same rising posts; the pipeline evaluates a post once,
the first time it clears REDDIT_MIN_UPS or rises fast enough.
"""
import logging
import time

import requests

from modules.radar import settings as S
from modules.radar.items import Item

log = logging.getLogger(__name__)

_VIDEO_DOMAINS = ("v.redd.it", "streamable.com", "youtube.com", "youtu.be",
                  "x.com", "twitter.com", "tiktok.com", "instagram.com")

_token = {"value": "", "until": 0.0}


def _auth_headers() -> dict:
    h = {"User-Agent": S.USER_AGENT}
    if not (S.REDDIT_CLIENT_ID and S.REDDIT_CLIENT_SECRET):
        return h
    if time.time() >= _token["until"]:
        r = requests.post("https://www.reddit.com/api/v1/access_token",
                          auth=(S.REDDIT_CLIENT_ID, S.REDDIT_CLIENT_SECRET),
                          data={"grant_type": "client_credentials"},
                          headers=h, timeout=20)
        r.raise_for_status()
        body = r.json()
        _token["value"] = body["access_token"]
        _token["until"] = time.time() + int(body.get("expires_in", 3600)) - 120
    h["Authorization"] = f"bearer {_token['value']}"
    return h


def _url() -> str:
    subs = "+".join(S.REDDIT_SUBS)
    if S.REDDIT_CLIENT_ID and S.REDDIT_CLIENT_SECRET:
        return f"https://oauth.reddit.com/r/{subs}/rising?limit=100&raw_json=1"
    return f"https://www.reddit.com/r/{subs}/rising.json?limit=100&raw_json=1"


def is_video(d: dict) -> bool:
    return bool(d.get("is_video") or d.get("post_hint") in ("hosted:video", "rich:video")
                or any(dom in (d.get("domain") or "") for dom in _VIDEO_DOMAINS))


def _thumb(d: dict) -> str:
    try:
        return d["preview"]["images"][0]["source"]["url"]
    except (KeyError, IndexError, TypeError):
        t = d.get("thumbnail") or ""
        return t if t.startswith("https://") else ""


def parse(listing: dict, now: float) -> list:
    """Video posts younger than REDDIT_MAX_AGE_H as Items."""
    out = []
    for child in (listing.get("data") or {}).get("children", []):
        d = child.get("data") or {}
        created = float(d.get("created_utc") or 0)
        age_min = max((now - created) / 60, 1.0)
        if not created or age_min > S.REDDIT_MAX_AGE_H * 60 or not is_video(d):
            continue
        ups = int(d.get("ups") or d.get("score") or 0)
        out.append(Item(
            source=f"reddit:{d.get('subreddit', '?')}",
            ext_id=str(d.get("id")),
            time=created,
            text=d.get("title") or "",
            link="https://www.reddit.com" + (d.get("permalink") or ""),
            has_video=True,
            metrics={"ups": ups, "velocity": round(ups / age_min, 2),
                     "comments": int(d.get("num_comments") or 0),
                     "nsfw": bool(d.get("over_18"))},
            thumb=_thumb(d),
        ))
    return out


def worth_a_look(it: Item) -> bool:
    m = it.metrics
    return m.get("ups", 0) >= S.REDDIT_MIN_UPS or m.get("velocity", 0) >= 1.0


def fetch(now: float | None = None) -> list:
    """Blocking. Raises on HTTP trouble — the caller counts failures."""
    r = requests.get(_url(), headers=_auth_headers(), timeout=30)
    r.raise_for_status()
    return parse(r.json(), now or time.time())
