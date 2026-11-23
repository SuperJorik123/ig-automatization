"""Reddit video posts under REDDIT_MAX_AGE_H old, with LIVE upvotes.

Reddit closed its public API to new apps (Oct 2026) and blocks subreddit
listings (new / rising / hot) to anything that isn't its own app — even a
logged-in session gets "403 Blocked". Two steps get around neither block,
they just don't need the listings:

  1. FIND — Arctic Shift (arctic-shift.photon-reddit.com), a free public
     Reddit archive that picks new posts up within minutes. One request per
     sub, posts since `now - REDDIT_MAX_AGE_H`. Its upvotes are the ones it
     saw at intake (a fresh post reads 1), so they are never used.
  2. LIVE — the VIDEO posts only (Arctic Shift knows each post's link), one
     Reddit /comments/<id>.json request each — the only lookup Reddit still
     answers (/api/info.json and /by_id/ are 403 too, tested 2026-11-23) —
     sent with the cookies of SPARE Reddit accounts — one Netscape
     cookies.txt each: RADAR_REDDIT_COOKIES_FILE (default
     modules/radar/data/reddit_cookies.txt) plus every reddit_cookies*.txt
     beside it; requests take turns across them, and one that Reddit refuses
     is dropped (and named in the group) while the others carry on
     over curl_cffi with a Chrome TLS fingerprint — Reddit answers "403
     Blocked" to every Python TLS client. Never the news bot's prod session:
     if Reddit flags this account, only the radar loses Reddit.

One request per post, so the accounts are spared: a post is re-read at most
REDDIT_CHECKS_MAX times, REDDIT_RECHECK_M apart, never again once it has
cleared the bar (`worth_a_look` — the pipeline takes it from there), and
requests go LIVE_PAUSE_S apart. Most posts never take off; the cap is what
stops a dud being re-read every half hour until it is 4 h old. No cookie
file = Reddit off.
"""
import glob
import http.cookiejar
import logging
import os
import time

import requests

from modules.radar import settings as S
from modules.radar.items import Item

log = logging.getLogger(__name__)

ARCTIC = "https://arctic-shift.photon-reddit.com/api/posts/search"
POST = "https://www.reddit.com/comments/{}.json"
ARCTIC_PAUSE_S = 1.0   # a free volunteer service — it answers "slow down a bit" otherwise
LIVE_PAUSE_S = 2.0

# id -> {"checks": n, "last": ts, "done": bool, "created": ts}. In memory: a
# restart only costs one extra read per young post.
_track: dict = {}

_VIDEO_DOMAINS = ("v.redd.it", "streamable.com", "youtube.com", "youtu.be",
                  "x.com", "twitter.com", "tiktok.com", "instagram.com")


def accounts() -> list:
    """Every spare account's cookie file, in a stable order."""
    first = S.REDDIT_COOKIES_FILE
    pattern = os.path.join(os.path.dirname(first), "reddit_cookies*.txt")
    found = [first] if os.path.isfile(first) else []
    found += sorted(p for p in glob.glob(pattern) if os.path.abspath(p) != os.path.abspath(first))
    return found


def enabled() -> bool:
    return bool(accounts())


def _cookies(path: str | None = None) -> dict:
    jar = http.cookiejar.MozillaCookieJar()
    jar.load(path or S.REDDIT_COOKIES_FILE, ignore_discard=True, ignore_expires=True)
    return {c.name: c.value for c in jar if "reddit" in (c.domain or "")}


# Accounts Reddit refused, by file -> its mtime then: a re-exported file
# (newer mtime) is tried again without a restart.
_dead: dict = {}
_turn = {"n": 0}
notices: list = []      # one-off messages for the group (main.poll_reddit)


def _alive() -> list:
    out = []
    for p in accounts():
        try:
            m = os.path.getmtime(p)
        except OSError:
            continue
        if p in _dead and _dead[p] == m:
            continue
        _dead.pop(p, None)
        out.append(p)
    return out


def _get(url: str, **kw):
    """curl_cffi as Chrome (it is in requirements.txt), plain requests otherwise."""
    try:
        from curl_cffi import requests as cffi
    except ImportError:
        return requests.get(url, **kw)
    return cffi.get(url, impersonate="chrome", **kw)


def is_video(d: dict) -> bool:
    return bool(d.get("is_video") or d.get("post_hint") in ("hosted:video", "rich:video")
                or any(dom in (d.get("domain") or "") for dom in _VIDEO_DOMAINS))


def _thumb(d: dict) -> str:
    try:
        return d["preview"]["images"][0]["source"]["url"]
    except (KeyError, IndexError, TypeError):
        t = d.get("thumbnail") or ""
        return t if t.startswith("https://") else ""


# --- 1. find ------------------------------------------------------------------
def recent(now: float) -> list:
    """Arctic Shift's records of every VIDEO post in REDDIT_SUBS younger than
    REDDIT_MAX_AGE_H. A sub that fails is skipped (logged); all failing raises."""
    after = int(now - S.REDDIT_MAX_AGE_H * 3600)
    posts, errors = {}, []
    for n, sub in enumerate(S.REDDIT_SUBS):
        if n:
            time.sleep(ARCTIC_PAUSE_S)
        try:
            r = requests.get(ARCTIC, params={"subreddit": sub, "after": after, "limit": 100},
                             headers={"User-Agent": S.USER_AGENT}, timeout=30)
            r.raise_for_status()
            for p in r.json().get("data") or []:
                if p.get("id") and is_video(p):
                    posts[str(p["id"])] = p
        except Exception as exc:
            errors.append(f"{sub}: {exc}")
    if errors and len(errors) == len(S.REDDIT_SUBS):
        raise RuntimeError("Arctic Shift unreachable — " + errors[0])
    for e in errors:
        log.warning("Arctic Shift %s", e)
    return list(posts.values())


def due(posts: list, now: float) -> list:
    """The ids worth a Reddit request this poll."""
    cutoff = now - S.REDDIT_MAX_AGE_H * 3600
    for k in [k for k, t in _track.items() if t["created"] < cutoff]:
        del _track[k]
    out = []
    for p in posts:
        t = _track.setdefault(str(p["id"]), {"checks": 0, "last": 0.0, "done": False,
                                             "created": float(p.get("created_utc") or now)})
        if (t["done"] or t["checks"] >= S.REDDIT_CHECKS_MAX
                or now - t["last"] < S.REDDIT_RECHECK_M * 60):
            continue
        out.append(str(p["id"]))
    return out


# --- 2. live --------------------------------------------------------------------
def live(ids: list, now: float) -> dict:
    """Reddit's current data for `ids` as one Listing ({"data": {"children"}}),
    the requests taking turns across the spare accounts."""
    children, jars = [], {}
    for n, pid in enumerate(ids):
        alive = _alive()
        if not alive:
            raise RuntimeError(
                "Reddit refused every spare account — cookies expired or accounts "
                "flagged? Re-export " + ", ".join(os.path.basename(p) for p in accounts()))
        if n:
            time.sleep(LIVE_PAUSE_S)
        path = alive[_turn["n"] % len(alive)]
        _turn["n"] += 1
        if path not in jars:
            jars[path] = _cookies(path)
        r = _get(POST.format(pid), params={"raw_json": 1, "limit": 1}, cookies=jars[path],
                 timeout=30)
        if r.status_code in (401, 403) or (r.status_code == 200
                                           and not r.text.lstrip().startswith("[")):
            _dead[path] = os.path.getmtime(path)
            msg = (f"Reddit answered {r.status_code} to the spare account in "
                   f"{os.path.basename(path)} — cookies expired or account flagged? "
                   f"Re-export that file; {len(_alive())} account(s) left.")
            log.error(msg)
            notices.append(msg)
            continue          # this post waits for the next poll
        t = _track.get(pid)
        if t is not None:
            t["checks"] += 1
            t["last"] = now
        if r.status_code == 404:
            if t is not None:
                t["done"] = True
            continue
        r.raise_for_status()
        try:
            children.append(r.json()[0]["data"]["children"][0])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return {"data": {"children": children}}


def parse(listing: dict, now: float) -> list:
    """Video posts younger than REDDIT_MAX_AGE_H as Items."""
    out = []
    for child in (listing.get("data") or {}).get("children", []):
        d = child.get("data") or {}
        created = float(d.get("created_utc") or 0)
        age_min = max((now - created) / 60, 1.0)
        if not created or age_min > S.REDDIT_MAX_AGE_H * 60 or not is_video(d):
            continue
        if d.get("removed_by_category"):
            continue                       # removed by mods / Reddit
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
    """Blocking. Raises on trouble — the caller counts failures."""
    now = now or time.time()
    ids = due(recent(now), now)
    if not ids:
        return []
    items = parse(live(ids, now), now)
    for it in items:
        if worth_a_look(it):
            _track[it.ext_id]["done"] = True   # the pipeline has it now
    return items
