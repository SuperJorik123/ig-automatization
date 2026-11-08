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

# Reddit: a post (www./old./new./no subdomain, r/<sub>/ or user/<name>/ or
# bare comments/<id>), the app's share link r/<sub>/s/<code>, and the two
# short hosts redd.it/<id> and v.redd.it/<id>. yt-dlp's Reddit extractor only
# matches the comments/ form itself; the others are redirects that land there.
REDDIT_URL_RE = re.compile(
    r"https?://(?:"
    r"(?:\w+\.)?reddit\.com/(?:(?:r|u|user)/[A-Za-z0-9_-]+/)?(?:comments|s)/[A-Za-z0-9_-]+"
    r"|(?:v\.)?redd\.it/[A-Za-z0-9_-]+"
    r")",
    re.IGNORECASE,
)

# Combined matcher used by the Telegram bot. Order matters only for the
# `match.group(0)` slice — every alternate captures the full URL.
SUPPORTED_URL_RE = re.compile(
    f"(?:{INSTAGRAM_URL_RE.pattern})|(?:{TWITTER_URL_RE.pattern})"
    f"|(?:{REDDIT_URL_RE.pattern})",
    re.IGNORECASE,
)


def is_instagram_url(text: str) -> bool:
    """True if `text` contains a recognisable Instagram content URL."""
    return bool(text) and bool(INSTAGRAM_URL_RE.search(text))


def is_twitter_url(text: str) -> bool:
    """True if `text` contains a recognisable Twitter / X status URL."""
    return bool(text) and bool(TWITTER_URL_RE.search(text))


def is_reddit_url(text: str) -> bool:
    """True if `text` contains a recognisable Reddit post / share / v.redd.it URL."""
    return bool(text) and bool(REDDIT_URL_RE.search(text))


def reddit_cookies(url: str) -> str | None:
    """The Reddit cookies.txt to send with `url`, or None — only for Reddit
    URLs (an IG/X request must never carry a Reddit session) and only when
    the file exists."""
    if is_reddit_url(url) and os.path.isfile(config.REDDIT_COOKIES_FILE):
        return config.REDDIT_COOKIES_FILE
    return None


def is_supported_url(text: str) -> bool:
    """True if `text` contains any URL the downloader can handle (IG, X, Reddit)."""
    return bool(text) and bool(SUPPORTED_URL_RE.search(text))


def download_media(url: str, dest_path: str, kind: str = "reel") -> tuple[str, str]:
    """`_download_media` without yt-dlp's info dict — (absolute path, caption),
    for the callers that only want the file and its text."""
    path, caption, _ = _download_media(resolve_reddit_url(url), dest_path, kind)
    return path, caption


def _download_media(url: str, dest_path: str, kind: str = "reel") -> tuple[str, str, dict]:
    """Download the media at `url` next to `dest_path`, replacing the
    extension with whatever yt-dlp picks. `dest_path` is a suggestion —
    we use its stem so callers can pre-decide the NNN naming.

    `kind` is "reel" (video) or "post" (photo). For reels we steer
    yt-dlp toward an mp4 format; for posts we let it pick "best", which
    is typically a single .jpg.

    Heads up: IG gates many photo posts behind a login. yt-dlp surfaces
    that as `Instagram sent an empty media response`; we re-raise as
    RuntimeError so the server can return a 502 with the message.

    Returns (absolute_path, caption, info). Caption is the IG post's
    description text as yt-dlp parsed it (may be empty string if the
    post had no caption or IG didn't return one); info is yt-dlp's own
    dict, which carries the author (`source_post`). Raises RuntimeError
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
    if is_reddit_url(url):
        # Reddit serves video and audio as separate DASH streams: "mp4"
        # alone picks the best video-only mp4 and the post goes out silent.
        # Ask for the pair and let ffmpeg mux it into an mp4.
        opts["format"] = "bv*+ba/b"
        opts["merge_output_format"] = "mp4"
        cookies = reddit_cookies(url)
        if cookies:
            opts["cookiefile"] = cookies
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
        elif is_reddit_url(url) and ("authentication is required" in str(exc)
                                     or "unable to access the Reddit API" in str(exc)):
            # yt-dlp's extractor opens an API session that Reddit blocks for
            # this IP even with good cookies, while the post's plain .json
            # still answers (2026-10) — read the media URL from that.
            direct = _reddit_direct(url, opts)
            if direct:
                return direct
            # Lead with the fix: the bot shows only the first 300 chars, and
            # yt-dlp's own message is longer than that.
            raise RuntimeError(
                f"Reddit needs a logged-in session — put a cookies.txt at "
                f"{config.REDDIT_COOKIES_FILE}" if not reddit_cookies(url) else
                "Reddit refused the download (cookies expired or logged out?) — "
                "export a fresh cookies.txt") from exc
        raise RuntimeError(f"yt-dlp failed for {url!r}: {exc}{hint}") from exc
    if not os.path.exists(path):
        raise RuntimeError(f"yt-dlp returned success but file missing: {path}")
    caption = (info.get("description") or "").strip()
    if is_reddit_url(url):
        # A Reddit post's headline is its TITLE; yt-dlp's "description" is
        # the body text, which goes to reddit_context() instead.
        caption = (info.get("title") or "").strip()
    if TWITTER_URL_RE.search(url):
        # Twitter auto-appends a `https://t.co/<id>` shortlink at the
        # end of the tweet body that redirects to the media itself —
        # not user content, strip it so the IG caption doesn't end on a
        # bare redirect URL.
        caption = re.sub(r"\s*https?://t\.co/\w+\s*$", "", caption).rstrip()
    return os.path.abspath(path), caption, info


REDDIT_POST_ID_RE = re.compile(r"/comments/([A-Za-z0-9]+)")
# Reddit 403s the default python-requests agent; a descriptive one passes.
_REDDIT_UA = "windows:ig-automatization:1.0"
REDDIT_CONTEXT_MAX = 1500


def resolve_reddit_url(url: str) -> str:
    """The /comments/<id>/ URL behind a Reddit share link (r/<sub>/s/<code>),
    redd.it or v.redd.it link; any other URL comes back unchanged.

    yt-dlp's Reddit extractor only matches /comments/ URLs — a share link
    falls through to the generic extractor, which fetches the page itself and
    dies on Reddit's "403 Blocked" without ever trying the cookies-aware
    path. The redirect lands on the /comments/ URL even when the page 403s.
    Never raises: on any failure the original URL is returned."""
    if not is_reddit_url(url) or REDDIT_POST_ID_RE.search(url):
        return url
    try:
        import requests
        r = requests.get(url, headers={"User-Agent": _REDDIT_UA}, timeout=20)
        if REDDIT_POST_ID_RE.search(r.url):
            return r.url.split("?", 1)[0]
    except Exception as exc:
        log.warning("reddit share link not resolved for %s: %s", url, exc)
    return url


def _reddit_listing(url: str, **params):
    """The post's /comments/<id>.json listing ([post, comments]), sent with
    the saved cookies; None when `url` names no post. Raises on HTTP or
    network trouble — callers decide what a failure means."""
    import http.cookiejar
    m = REDDIT_POST_ID_RE.search(resolve_reddit_url(url))
    if m is None:
        return None
    jar = http.cookiejar.MozillaCookieJar()
    if reddit_cookies(url):
        jar.load(config.REDDIT_COOKIES_FILE, ignore_discard=True,
                 ignore_expires=True)
    r = _reddit_get(f"https://www.reddit.com/comments/{m.group(1)}.json",
                    params={"raw_json": 1, **params},
                    cookies={c.name: c.value for c in jar},
                    headers={"User-Agent": _REDDIT_UA}, timeout=20)
    r.raise_for_status()
    return r.json()


def _reddit_get(url: str, **kw):
    """GET through curl_cffi with a Chrome TLS fingerprint when it is
    installed, plain requests otherwise. Reddit answers "403 Blocked" to
    every Python TLS client — requests, urllib, yt-dlp — with good cookies
    and any headers, while curl gets 200 (VPS, 2026-10)."""
    try:
        from curl_cffi import requests as cffi
    except ImportError:
        import requests
        return requests.get(url, **kw)
    return cffi.get(url, impersonate="chrome", **kw)


def _reddit_direct(url: str, opts: dict):
    """`_download_media`'s result for a Reddit post fetched around yt-dlp's
    Reddit extractor: the media URL comes from the post's own .json (which
    Reddit still serves where the extractor's API session is blocked) and
    yt-dlp downloads that — the HLS playlist for a video, so the audio
    stream comes too, or the i.redd.it file for a photo. Info carries the
    title and the author like the extractor's would. None on any failure,
    so the caller raises its own (clearer) error."""
    try:
        listing = _reddit_listing(url)
        if listing is None:
            return None
        post = listing[0]["data"]["children"][0]["data"]
        video = (post.get("secure_media") or post.get("media") or {}).get("reddit_video") or {}
        media = video.get("hls_url") or video.get("fallback_url")
        if not media and re.match(r"https://i\.redd\.it/", post.get("url") or ""):
            media = post["url"]
        if not media:
            return None
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(media, download=True)
            path = ydl.prepare_filename(info)
        if not os.path.exists(path):
            return None
    except Exception as exc:
        log.warning("reddit direct download failed for %s: %s", url, exc)
        return None
    info = {**info, "title": post.get("title") or "",
            "uploader": post.get("author") or ""}
    return os.path.abspath(path), info["title"].strip(), info


def reddit_context(url: str) -> str:
    """What the Reddit post SAYS about its media, as a block for the
    caption's `info:` — the subreddit, flair, title, the body text and the
    uploader's own comments (where OP usually posts the context on a video
    post). Other users' comments are left out: they are guesses, and this
    feeds a news caption. Labelled unverified, because a title is the
    uploader's claim, not a source.

    Never raises: no cookies, a dead link, a changed JSON shape → "" — the
    download already worked, and the post just goes on without the extra
    facts."""
    if not is_reddit_url(url):
        return ""
    try:
        listing = _reddit_listing(url, limit=50, sort="top")
        if listing is None:
            return ""
        post = listing[0]["data"]["children"][0]["data"]
        lines = [f"Reddit post in r/{post.get('subreddit', '?')} "
                 "(the uploader's own words — unverified):"]
        if post.get("link_flair_text"):
            lines.append(f"Flair: {post['link_flair_text']}")
        lines.append(f"Title: {post.get('title', '').strip()}")
        body = (post.get("selftext") or "").strip()
        if body:
            lines.append(f"Post text: {body}")
        for c in listing[1]["data"]["children"]:
            d = c.get("data") or {}
            text = (d.get("body") or "").strip()
            if d.get("is_submitter") and text:
                lines.append(f"Uploader's comment: {text}")
        out = "\n".join(lines)
        return out if len(out) <= REDDIT_CONTEXT_MAX else out[:REDDIT_CONTEXT_MAX] + "…"
    except Exception as exc:
        log.warning("reddit context failed for %s: %s", url, exc)
        return ""


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
    """`_download_photos` without gallery-dl's metadata — (paths, caption)."""
    paths, caption, _ = _download_photos(url, dest_stub)
    return paths, caption


def _download_photos(url: str, dest_stub: str) -> tuple[list[str], str, dict]:
    """Photo fallback via the gallery-dl CLI — handles IG photo posts (which
    yt-dlp can't, login-gated) and photo-only tweets. Downloads into a
    per-call temp dir next to `dest_stub`, then renames onto
    `<stub>_1.jpg`-style names so the caller's cleanup conventions (the
    manual_url_* sweep) keep working.

    Cookies: with GALLERY_DL_COOKIES_BROWSER set (e.g. "chrome"), gallery-dl
    reads that browser's logged-in Instagram session; blank = anonymous.

    Returns (absolute photo paths, caption, metadata). Caption is
    best-effort from gallery-dl's metadata sidecars — empty string when
    absent; metadata is the sidecar the caption came from ({} when none).
    Raises RuntimeError when gallery-dl is missing or nothing was
    downloaded."""
    dest_dir = os.path.dirname(dest_stub) or "."
    os.makedirs(dest_dir, exist_ok=True)
    stem, _ = os.path.splitext(dest_stub)
    tmp_dir = stem + "_gdl"

    cmd = GALLERY_DL_CMD + ["-D", tmp_dir, "--write-info-json", url]
    i = len(GALLERY_DL_CMD)
    if reddit_cookies(url):
        cmd[i:i] = ["--cookies", reddit_cookies(url)]
    elif config.GALLERY_DL_COOKIES_BROWSER:
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

        caption, found = "", {}
        for name in names:
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(tmp_dir, name), encoding="utf-8") as fh:
                    meta = json.load(fh)
                # IG calls it description, Twitter content — take whichever.
                caption = (meta.get("description") or meta.get("content") or "").strip()
            except (OSError, ValueError, AttributeError):
                continue
            if not found or caption:
                found = meta
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
        return paths, caption, found
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def download_any(url: str, dest_stub: str) -> tuple[list[str], str]:
    """`fetch` without the source post — (paths, caption)."""
    paths, caption, _ = fetch(url, dest_stub)
    return paths, caption


def fetch(url: str, dest_stub: str) -> tuple[list[str], str, dict]:
    """Everything-downloader, video-first: yt-dlp "reel" (mp4-steered) →
    yt-dlp "post" (best format) → gallery-dl photos. A URL doesn't say what
    it holds, so the chain just tries in order of likelihood. When every
    stage fails the FIRST yt-dlp error is re-raised — it names the real
    problem (login gate, dead link); the fallbacks usually just repeat it.

    Returns (paths, caption, source): one path for a video, up to MAX_PHOTOS
    for a photo carousel; `source` is the post the media came from
    (`source_post`)."""
    url = resolve_reddit_url(url)
    try:
        path, caption, info = _download_media(url, dest_stub, kind="reel")
        return [path], caption, source_post(url, caption, info)
    except RuntimeError as first:
        try:
            path, caption, info = _download_media(url, dest_stub, kind="post")
            return [path], caption, source_post(url, caption, info)
        except RuntimeError:
            try:
                paths, caption, meta = _download_photos(url, dest_stub)
                return paths, caption, source_post(url, caption, meta)
            except RuntimeError:
                raise first


# ── The source post ──────────────────────────────────────────────────────────
# The caption is written from the people who saw the story first: the
# operator, then the post the clip was lifted from. That post's text and its
# author are already in what the downloaders return — yt-dlp's info dict,
# gallery-dl's sidecar — and used to be thrown away with everything but the
# caption string.

# What a handle may look like on X / Instagram / Reddit. Anything else (a
# display name with spaces, a numeric user id) is not something to put after
# an "@" in a published comment.
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


def source_platform(url: str) -> str:
    """"x", "reddit", "instagram", or "" for anything else."""
    if is_twitter_url(url):
        return "x"
    if is_reddit_url(url):
        return "reddit"
    if is_instagram_url(url):
        return "instagram"
    return ""


def _handle(value) -> str:
    value = str(value or "").strip().lstrip("@")
    if value.startswith("u/"):
        value = value[2:]
    return value if _HANDLE_RE.match(value) and not value.isdigit() else ""


def _author(platform: str, meta: dict) -> str:
    """The poster's handle out of a yt-dlp info dict or a gallery-dl sidecar,
    "" when neither carries a usable one. Never a display name — a pinned
    comment credits it after an "@", and a guess there is a false credit."""
    meta = meta if isinstance(meta, dict) else {}
    author = meta.get("author")
    nested = author.get("name") if isinstance(author, dict) else author
    keys = {
        # yt-dlp: uploader_id is the @handle; gallery-dl: author.name.
        "x": (meta.get("uploader_id"), nested),
        # yt-dlp: channel is the username (uploader is the full name,
        # uploader_id the numeric pk); gallery-dl: username.
        "instagram": (meta.get("channel"), meta.get("uploader_id"),
                      meta.get("username")),
        # yt-dlp and gallery-dl both: the account name.
        "reddit": (meta.get("uploader"), nested),
    }.get(platform, ())
    for value in keys:
        handle = _handle(value)
        if handle:
            return handle
    return ""


def source_post(url: str, caption: str, meta: dict | None = None) -> dict:
    """The post the media came from: {platform, url, text, author}.

    `text` is what that post SAYS — the tweet, the Instagram caption, or for
    Reddit the title plus the body and the uploader's own comments
    (`reddit_context`, falling back to the title alone). "" when there is
    none; it is never required. Never raises."""
    platform = source_platform(url)
    text = (caption or "").strip()
    if platform == "reddit":
        text = reddit_context(url) or text
    try:
        author = _author(platform, meta or {})
    except Exception:
        author = ""
    return {"platform": platform, "url": url, "text": text, "author": author}
