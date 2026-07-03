"""
reel_downloader.py — fetch an Instagram reel/post video and save it
locally so the rest of the upload pipeline can treat it like a file
the user dropped into `posts/`.

We use yt-dlp under the hood: it talks to Instagram's GraphQL endpoint
directly, returns a CDN URL on fbcdn.net, and streams the MP4 from
there. No third-party scraper, no client token to harvest, no Selenium.
"""

import os
import re

import yt_dlp


# IG URLs look like https://www.instagram.com/{reel,reels,p,tv}/<shortcode>/...
INSTAGRAM_URL_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:reel|reels|p|tv)/[A-Za-z0-9_-]+",
    re.IGNORECASE,
)

# Twitter / X: twitter.com, x.com, mobile.twitter.com, www.* — all the
# same backend, yt-dlp accepts every variant.
TWITTER_URL_RE = re.compile(
    r"https?://(?:www\.|mobile\.)?(?:twitter\.com|x\.com)/[A-Za-z0-9_]+/status/\d+",
    re.IGNORECASE,
)

# Combined matcher used by the Telegram bot. Order matters only for the
# `match.group(0)` slice — both alternates capture the full URL.
SUPPORTED_URL_RE = re.compile(
    f"(?:{INSTAGRAM_URL_RE.pattern})|(?:{TWITTER_URL_RE.pattern})",
    re.IGNORECASE,
)


def is_instagram_url(text: str) -> bool:
    """True if `text` contains a recognisable Instagram content URL."""
    return bool(text) and bool(INSTAGRAM_URL_RE.search(text))


def is_twitter_url(text: str) -> bool:
    """True if `text` contains a recognisable Twitter / X status URL."""
    return bool(text) and bool(TWITTER_URL_RE.search(text))


def is_supported_url(text: str) -> bool:
    """True if `text` contains any URL the downloader can handle (IG or X)."""
    return bool(text) and bool(SUPPORTED_URL_RE.search(text))


def download_media(url: str, dest_path: str, kind: str = "reel") -> tuple[str, str]:
    """Download the media at `url` next to `dest_path`, replacing the
    extension with whatever yt-dlp picks. `dest_path` is a suggestion —
    we use its stem so callers can pre-decide the NNN naming.

    `kind` is "reel" (video) or "post" (photo). For reels we steer
    yt-dlp toward an mp4 format; for posts we let it pick "best", which
    is typically a single .jpg.

    Heads up: IG gates many photo posts behind a login. yt-dlp surfaces
    that as `Instagram sent an empty media response`; we re-raise as
    RuntimeError so the server can return a 502 with the message.

    Returns (absolute_path, caption). Caption is the IG post's
    description text as yt-dlp parsed it (may be empty string if the
    post had no caption or IG didn't return one). Raises RuntimeError
    on extraction or download failure."""
    dest_dir = os.path.dirname(dest_path) or "."
    os.makedirs(dest_dir, exist_ok=True)
    stem, _ = os.path.splitext(dest_path)
    fmt = "mp4/best" if kind == "reel" else "best"
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "outtmpl": stem + ".%(ext)s",
        "format": fmt,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info)
    except yt_dlp.utils.DownloadError as exc:
        raise RuntimeError(f"yt-dlp failed for {url!r}: {exc}") from exc
    if not os.path.exists(path):
        raise RuntimeError(f"yt-dlp returned success but file missing: {path}")
    caption = (info.get("description") or "").strip()
    if TWITTER_URL_RE.search(url):
        # Twitter auto-appends a `https://t.co/<id>` shortlink at the
        # end of the tweet body that redirects to the media itself —
        # not user content, strip it so the IG caption doesn't end on a
        # bare redirect URL.
        caption = re.sub(r"\s*https?://t\.co/\w+\s*$", "", caption).rstrip()
    return os.path.abspath(path), caption


# Backwards-compat alias — early callers used `download_reel` and only
# expected the file path back.
def download_reel(url: str, dest_path: str) -> str:
    path, _caption = download_media(url, dest_path, kind="reel")
    return path
