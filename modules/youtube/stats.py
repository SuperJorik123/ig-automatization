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


if __name__ == "__main__":
    import json
    print(json.dumps(latest_short(sys.argv[1]), indent=2, ensure_ascii=False))
