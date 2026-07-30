"""
modules/youtube/publisher.py — translate a caption per YouTube channel language
and upload one video to every selected channel as a Short.

The YouTube twin of modules/telegram/publisher.py, and shaped the same way:
destinations are {"chat_id": <account>, "lang": <lang>} dicts (YT_DESTINATIONS
parsed by shared.config — "chat_id" holds the account name, i.e. the folder
under credentials/youtube/). Translation reuses modules/telegram/translator so
both platforms share one OpenRouter path, with the same per-language cache so
N same-language channels cost one API call.

Title/description split follows the news-bot convention: the TRANSLATED
caption's first non-empty line becomes the title (YouTube caps it at 100
chars), the full caption becomes the description.

Uploads are blocking network calls — async callers (the news bot) must run
publish_shorts in a thread (asyncio.to_thread).
"""

import logging
import os
import sys

# Repo-root bootstrap for direct runs / imports from anywhere.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shared import config  # noqa: E402
from modules.telegram import translator  # noqa: E402
from modules.youtube import shorts_format, uploader  # noqa: E402

log = logging.getLogger(__name__)

_FALLBACK_TITLE = "News update"


def split_caption(text: str) -> tuple[str, str]:
    """(title, description): title = first non-empty line, description = the
    whole caption. Emoji stripping and hard limits happen in the uploader."""
    text = (text or "").strip()
    title = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return (title or _FALLBACK_TITLE)[:100], text


def publish_shorts(video_path: str, caption: str, dests: list | None = None):
    """Upload `video_path` to each YouTube destination (default: all of
    YT_DESTINATIONS), translating the caption into each channel's language
    first. A failure on one channel is logged and skipped — it does not abort
    the rest.

    Returns (posted, errors): posted is the list of account names that
    succeeded, errors is a list of (account, message)."""
    if dests is None:
        dests = config.YT_DESTINATIONS

    # One conversion for all channels: horizontal videos are re-rendered to
    # vertical so YouTube classifies the upload as a Short.
    try:
        upload_path, converted = shorts_format.ensure_short(video_path)
    except Exception as exc:  # too long, no video stream, ffmpeg missing, ...
        log.error("Shorts conversion failed for %s: %s", video_path, exc)
        return [], [(d["chat_id"], f"can't make a Short: {exc}") for d in dests]

    cache: dict[str, str] = {}
    posted, errors = [], []
    try:
        for dest in dests:
            account, lang = dest["chat_id"], dest["lang"]
            if lang not in cache:
                cache[lang] = (
                    translator.translate(caption, lang, config.SOURCE_LANG) if lang else caption
                )
            title, description = split_caption(cache[lang])
            try:
                result = uploader.upload_short(
                    upload_path, title=title, description=description, account_name=account
                )
            except Exception as exc:  # missing creds folder, unreadable file, ...
                result = {"status": "failed", "error": str(exc)}
            if result["status"] == "success":
                posted.append(account)
            else:
                err = result.get("error", "unknown error")
                log.error("YouTube upload to %s failed: %s", account, err)
                errors.append((account, err))
    finally:
        if converted:
            try:
                os.remove(upload_path)
            except OSError:
                pass
    return posted, errors
