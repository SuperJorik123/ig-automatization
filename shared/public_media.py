"""
shared/public_media.py — put a local file at a public URL for as long as an
API needs to fetch it.

The Instagram Graph API has no upload: it fetches image_url / video_url from
the open internet. On the VPS an nginx vhost serves PUBLIC_MEDIA_DIR
read-only at PUBLIC_MEDIA_BASE_URL (docs/DEPLOY.md, "Instagram media
hosting"), so exposing a file is a copy under an unguessable name:

    url, cleanup = public_media.expose("/path/to/render.mp4")
    ...publish url...
    cleanup()          # in a finally — deletes the copy, idempotent

Names are secrets.token_urlsafe(24) + the file's real extension (lower-cased;
Instagram sniffs the type but a wrong suffix confuses nginx's Content-Type).
Nothing lists the directory (autoindex off), so an unguessable name is the
whole access control. sweep() removes leftovers older than an hour — a crash
between expose() and cleanup() must not leave a video public forever; the
news bot runs it at startup.

Both settings come from .env; expose() raises NotConfigured naming them when
either is blank, so a half-configured deploy fails one publish leg loudly
instead of publishing a broken URL.
"""

import os
import secrets
import shutil
import time

from shared import config


class NotConfigured(RuntimeError):
    """PUBLIC_MEDIA_DIR / PUBLIC_MEDIA_BASE_URL unset."""


def _settings() -> tuple[str, str]:
    d = (config.PUBLIC_MEDIA_DIR or "").strip()
    base = (config.PUBLIC_MEDIA_BASE_URL or "").strip()
    if not d or not base:
        raise NotConfigured(
            "public media hosting is not configured — set PUBLIC_MEDIA_DIR "
            "(the directory nginx serves) and PUBLIC_MEDIA_BASE_URL (its "
            "public https URL) in .env; see docs/DEPLOY.md")
    return d, base.rstrip("/") + "/"


def expose(path: str):
    """Copy `path` into the public dir under a random name. Returns
    (url, cleanup); cleanup() deletes the copy and never raises."""
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    if os.path.getsize(path) == 0:
        raise ValueError(f"refusing to expose an empty file: {path}")
    directory, base = _settings()
    os.makedirs(directory, exist_ok=True)

    ext = os.path.splitext(path)[1].lower()
    name = secrets.token_urlsafe(24) + ext
    dest = os.path.join(directory, name)
    shutil.copyfile(path, dest)
    # nginx runs as another user — the copy must be world-readable whatever
    # the process umask says.
    try:
        os.chmod(dest, 0o644)
    except OSError:
        pass

    def cleanup() -> None:
        try:
            os.remove(dest)
        except FileNotFoundError:
            pass
        except OSError:
            pass

    return base + name, cleanup


def sweep(max_age_s: int = 3600) -> int:
    """Delete files in the public dir older than `max_age_s`. Returns how
    many went. No-op (0) when hosting isn't configured or the dir is absent."""
    directory = (config.PUBLIC_MEDIA_DIR or "").strip()
    if not directory or not os.path.isdir(directory):
        return 0
    cutoff = time.time() - max_age_s
    removed = 0
    for entry in os.scandir(directory):
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                os.remove(entry.path)
                removed += 1
        except OSError:
            continue
    return removed
