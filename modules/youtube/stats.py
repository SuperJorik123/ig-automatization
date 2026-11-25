"""
modules/youtube/stats.py — a channel's latest Short and its public numbers.

Read with yt-dlp, the same library shared/reel_downloader.py downloads with:
no API key, no quota, no OAuth. The uploader's OAuth tokens couldn't do it
anyway — they carry only the youtube.upload scope, which can't read a video.

The channel is addressed by its PUBLIC handle (`@name`), a channel URL or a
`UC…` id — NOT the credentials/youtube/<folder> name, which is only a folder.
Set it per brand as `"youtube": {"handle": "@name"}` in
credentials/brands/<name>.json (→ BRAND_<NAME>_YT_HANDLE).

Blocking; the news bot calls it via asyncio.to_thread.
CLI: py modules/youtube/stats.py @handle
"""
import datetime as dt
import logging
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

log = logging.getLogger(__name__)

_OPTS = {"quiet": True, "no_warnings": True, "skip_download": True,
         "socket_timeout": 30}


def channel_url(handle: str) -> str:
    """'@name' / 'name' / 'UC…' / a full URL -> the channel's Shorts tab."""
    h = (handle or "").strip().rstrip("/")
    if h.startswith("http"):
        base = h
        for tab in ("/shorts", "/videos", "/featured"):
            if base.endswith(tab):
                base = base[: -len(tab)]
    elif h.startswith("UC") and len(h) == 24:
        base = f"https://www.youtube.com/channel/{h}"
    else:
        base = f"https://www.youtube.com/@{h.lstrip('@')}"
    return base + "/shorts"


def _posted_at(info: dict) -> str:
    ts = info.get("timestamp")
    if ts:
        return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()
    day = info.get("upload_date")  # YYYYMMDD
    if day and len(day) == 8:
        return f"{day[:4]}-{day[4:6]}-{day[6:]}T00:00:00+00:00"
    return ""


def latest_short(handle: str) -> tuple[dict | None, str]:
    """({url, posted_at, caption, views, likes, comments}, "") for the
    channel's newest Short — (None, "no shorts yet") or (None, why). Never
    raises."""
    try:
        import yt_dlp
    except ImportError:
        return None, "yt-dlp not installed"
    try:
        with yt_dlp.YoutubeDL({**_OPTS, "extract_flat": True,
                               "playlistend": 1}) as ydl:
            tab = ydl.extract_info(channel_url(handle), download=False)
        entries = [e for e in (tab or {}).get("entries") or [] if e]
        if not entries:
            return None, "no shorts yet"
        first = entries[0]
        url = first.get("url") or f"https://www.youtube.com/shorts/{first['id']}"
        with yt_dlp.YoutubeDL(_OPTS) as ydl:
            info = ydl.extract_info(url, download=False) or {}
    except Exception as exc:
        msg = str(exc).replace("ERROR: ", "")
        return None, msg[:300]
    return {
        "url": f"https://www.youtube.com/shorts/{info.get('id') or first['id']}",
        "posted_at": _posted_at(info),
        "caption": info.get("title") or "",
        "views": info.get("view_count"),
        "likes": info.get("like_count"),
        "comments": info.get("comment_count"),
    }, ""


RECENT_MAX = 15   # Shorts read per channel for the 24 h report, at most


def recent_shorts(handle: str, since: dt.datetime) -> tuple[list | None, str]:
    """Every Short the channel posted at or after `since` (newest first) with
    `latest_short`'s numbers — ([], "") when none, or (None, why). The Shorts
    tab lists newest first without dates, so each one is opened until the
    first older than `since` (at most RECENT_MAX). Never raises."""
    try:
        import yt_dlp
    except ImportError:
        return None, "yt-dlp not installed"
    try:
        with yt_dlp.YoutubeDL({**_OPTS, "extract_flat": True,
                               "playlistend": RECENT_MAX}) as ydl:
            tab = ydl.extract_info(channel_url(handle), download=False)
    except Exception as exc:
        msg = str(exc).replace("ERROR: ", "")
        if "does not have a shorts tab" in msg.lower():
            return [], ""
        return None, msg[:300]
    out = []
    for e in [e for e in (tab or {}).get("entries") or [] if e]:
        url = e.get("url") or f"https://www.youtube.com/shorts/{e['id']}"
        try:
            with yt_dlp.YoutubeDL(_OPTS) as ydl:
                info = ydl.extract_info(url, download=False) or {}
        except Exception as exc:
            log.info("[YT:%s] %s unreadable: %s", handle, url, exc)
            continue
        posted = _posted_at(info)
        if posted:
            t = dt.datetime.fromisoformat(posted)
            # A date with no time (no `timestamp`) is midnight UTC: compare
            # days then, or a Short from this afternoon would read as too old.
            old = (t.date() < since.astimezone(dt.timezone.utc).date()
                   if not info.get("timestamp") else t < since)
            if old:
                break
        out.append({
            "url": f"https://www.youtube.com/shorts/{info.get('id') or e['id']}",
            "posted_at": posted,
            "caption": info.get("title") or "",
            "views": info.get("view_count"),
            "likes": info.get("like_count"),
            "comments": info.get("comment_count"),
        })
    return out, ""


def subscribers(handle: str) -> tuple[int | None, str]:
    """(the channel's public subscriber count, "") or (None, why), for the
    weekly follower report. YouTube ROUNDS it publicly (12,345 shows as
    12.3K), so a small week can read as +0. Never raises."""
    try:
        import yt_dlp
    except ImportError:
        return None, "yt-dlp not installed"
    why = ""
    try:
        with yt_dlp.YoutubeDL({**_OPTS, "extract_flat": True,
                               "playlistend": 1}) as ydl:
            tab = ydl.extract_info(channel_url(handle), download=False) or {}
        if tab.get("channel_follower_count") is not None:
            return int(tab["channel_follower_count"]), ""
    except Exception as exc:
        why = str(exc).replace("ERROR: ", "")[:300]
    # yt-dlp misses the singular "1 subscriber" and fails outright on a
    # channel with no Shorts tab (both seen 2026-11-17), so read the count
    # off the channel page's header instead.
    count, page_why = _subscribers_from_page(handle)
    if count is not None:
        return count, ""
    return None, page_why or why or "subscriber count hidden"


_SUBS_RE = re.compile(r'"content":"(No|[0-9][0-9.,]*)\s*([KMB]?) subscribers?"')


def parse_subscribers(page: str) -> int | None:
    """The header's "1 subscriber" / "12.3K subscribers" / "No subscribers"
    -> an int, or None when the page shows no count (hidden, or zero on some
    channels)."""
    m = _SUBS_RE.search(page or "")
    if not m:
        return None
    num, unit = m.groups()
    if num == "No":
        return 0
    val = float(num.replace(",", ""))
    return int(round(val * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[unit]))


def _subscribers_from_page(handle: str) -> tuple[int | None, str]:
    try:
        import requests
        base = channel_url(handle)[: -len("/shorts")]
        resp = requests.get(base, timeout=30, headers={
            "User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.8"})
        resp.raise_for_status()
    except Exception as exc:
        return None, str(exc)[:300]
    count = parse_subscribers(resp.text)
    return (count, "") if count is not None else (
        None, "count not shown on the channel (hidden, or 0 subscribers)")


if __name__ == "__main__":
    import json
    print(json.dumps(latest_short(sys.argv[1]), indent=2, ensure_ascii=False))
