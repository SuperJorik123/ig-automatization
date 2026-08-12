"""
reel_downloader.py — fetch an Instagram reel/post video and save it
locally so the rest of the upload pipeline can treat it like a file
the user dropped into `posts/`.

We use yt-dlp under the hood: it talks to Instagram's GraphQL endpoint
directly, returns a CDN URL on fbcdn.net, and streams the MP4 from
there. No third-party scraper, no client token to harvest, no Selenium.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys

import yt_dlp

from shared import config

log = logging.getLogger(__name__)


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
        hint = ""
        # IG breaks yt-dlp's extractor every few months and the resulting
        # "empty media response" reads like a login gate even on public
        # reels — say so, the fix is an upgrade, not cookies.
        if "empty media response" in str(exc):
            hint = (" — if the post IS public in a logged-out browser, yt-dlp "
                    "is outdated: py -m pip install -U yt-dlp, then restart the bot")
        raise RuntimeError(f"yt-dlp failed for {url!r}: {exc}{hint}") from exc
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


# Telegram albums cap at 10 items; extra carousel photos are dropped (logged).
MAX_PHOTOS = 10

# gallery-dl is invoked as a module, not as the `gallery-dl` console script:
# pip's Scripts/ dir isn't on PATH on this machine, so the bare name fails.
GALLERY_DL_CMD = [sys.executable, "-m", "gallery_dl"]


def download_photos(url: str, dest_stub: str) -> tuple[list[str], str]:
    """Photo fallback via the gallery-dl CLI — handles IG photo posts (which
    yt-dlp can't, login-gated) and photo-only tweets. Downloads into a
    per-call temp dir next to `dest_stub`, then renames onto
    `<stub>_1.jpg`-style names so the caller's cleanup conventions (the
    manual_url_* sweep) keep working.

    Cookies: with GALLERY_DL_COOKIES_BROWSER set (e.g. "chrome"), gallery-dl
    reads that browser's logged-in Instagram session; blank = anonymous.

    Returns (absolute photo paths, caption). Caption is best-effort from
    gallery-dl's metadata sidecars — empty string when absent. Raises
    RuntimeError when gallery-dl is missing or nothing was downloaded."""
    dest_dir = os.path.dirname(dest_stub) or "."
    os.makedirs(dest_dir, exist_ok=True)
    stem, _ = os.path.splitext(dest_stub)
    tmp_dir = stem + "_gdl"

    cmd = GALLERY_DL_CMD + ["-D", tmp_dir, "--write-info-json", url]
    if config.GALLERY_DL_COOKIES_BROWSER:
        i = len(GALLERY_DL_CMD)
        cmd[i:i] = ["--cookies-from-browser", config.GALLERY_DL_COOKIES_BROWSER]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "gallery-dl is not installed (py -m pip install gallery-dl)"
        ) from exc

    try:
        names = sorted(os.listdir(tmp_dir)) if os.path.isdir(tmp_dir) else []
        images = [n for n in names if not n.endswith(".json")]

        caption = ""
        for name in names:
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(tmp_dir, name), encoding="utf-8") as fh:
                    meta = json.load(fh)
                # IG calls it description, Twitter content — take whichever.
                caption = (meta.get("description") or meta.get("content") or "").strip()
            except (OSError, ValueError):
                continue
            if caption:
                break

        if not images:
            tail = (proc.stderr or "").strip()[-300:]
            raise RuntimeError(f"gallery-dl found no media for {url!r}: {tail}")

        if len(images) > MAX_PHOTOS:
            log.warning("carousel has %d photos — keeping the first %d "
                        "(Telegram album cap)", len(images), MAX_PHOTOS)
        paths = []
        for i, name in enumerate(images[:MAX_PHOTOS], start=1):
            ext = os.path.splitext(name)[1] or ".jpg"
            target = f"{stem}_{i}{ext}"
            os.replace(os.path.join(tmp_dir, name), target)
            paths.append(os.path.abspath(target))
        return paths, caption
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def download_any(url: str, dest_stub: str) -> tuple[list[str], str]:
    """Everything-downloader, video-first: yt-dlp "reel" (mp4-steered) →
    yt-dlp "post" (best format) → gallery-dl photos. A URL doesn't say what
    it holds, so the chain just tries in order of likelihood. When every
    stage fails the FIRST yt-dlp error is re-raised — it names the real
    problem (login gate, dead link); the fallbacks usually just repeat it.

    Returns (paths, caption): one path for a video, up to MAX_PHOTOS for a
    photo carousel."""
    try:
        path, caption = download_media(url, dest_stub, kind="reel")
        return [path], caption
    except RuntimeError as first:
        try:
            path, caption = download_media(url, dest_stub, kind="post")
            return [path], caption
        except RuntimeError:
            try:
                return download_photos(url, dest_stub)
            except RuntimeError:
                raise first
