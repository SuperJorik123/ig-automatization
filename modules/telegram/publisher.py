"""
modules/telegram/publisher.py — translate a post per destination language and
fan it out to every configured news channel via the bot.

Called by news_bot.py when the operator confirms the channel picker.

Media items are dicts:
  {"path": "<local file>", "type": "photo"|"video"}   # collected posts (copy-as-own)
  {"file_id": "<telegram id>", "type": "photo"|"video"}  # group posts (reuse the upload)
Telegram accepts a local path string or a file_id for photo=/video=/media=, so
both shapes flow through `_ref` unchanged.
"""

import logging

from telegram import InputMediaPhoto, InputMediaVideo

from shared import config
from modules.telegram import translator

log = logging.getLogger(__name__)


# Telegram's hard caps. A media caption is far shorter than a plain message,
# which is what long news items run into.
TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024


def _ref(m: dict):
    """The value to hand Telegram: a reusable file_id, else the local path."""
    return m.get("file_id") or m["path"]


def _fit(text: str, has_media: bool) -> str:
    """Trim a caption to what Telegram will accept.

    Truncating beats the alternative: an over-long caption is rejected per
    channel, and for an unattended autopilot post that means the story simply
    never ships. Every trim is logged."""
    limit = CAPTION_LIMIT if has_media else TEXT_LIMIT
    if len(text or "") <= limit:
        return text
    log.warning("caption trimmed from %d to %d chars (%s limit)",
                len(text), limit, "caption" if has_media else "message")
    return text[: limit - 1].rstrip() + "…"


async def _send(bot, chat_id, caption: str, media: list):
    """Send the post and return the resulting Message (the first one for an
    album) so the caller can read its .link — the public t.me/<channel>/<id>
    URL of what was just posted."""
    if not media:
        return await bot.send_message(chat_id=chat_id, text=caption or "")
    if len(media) == 1:
        m = media[0]
        if m["type"] == "video":
            return await bot.send_video(chat_id=chat_id, video=_ref(m), caption=caption or None)
        return await bot.send_photo(chat_id=chat_id, photo=_ref(m), caption=caption or None)
    # Album: caption rides on the first item only.
    group = []
    for i, m in enumerate(media):
        cap = caption if i == 0 else None
        cls = InputMediaVideo if m["type"] == "video" else InputMediaPhoto
        group.append(cls(media=_ref(m), caption=cap))
    msgs = await bot.send_media_group(chat_id=chat_id, media=group)
    return msgs[0] if msgs else None


async def publish(bot, text: str, media: list, dests: list | None = None):
    """Send `text` (+ optional media) to each destination channel (default: all
    of TG_DESTINATIONS), translating the caption into each destination's
    language first. Translations are cached per language so N same-language
    channels cost one API call. A failure on one channel is logged and skipped
    — it does not abort the rest.

    Returns (posted, errors, links): posted is the list of chat_ids that
    succeeded, errors is a list of (chat_id, message), links is a list of
    (chat_id, url) for the posts that produced a public t.me link (channels
    with a username, or private channels via t.me/c/…)."""
    if dests is None:
        dests = config.TG_DESTINATIONS
    cache: dict[str, str] = {}
    posted, errors, links = [], [], []
    for dest in dests:
        chat_id, lang = dest["chat_id"], dest["lang"]
        if lang not in cache:
            translated = translator.translate(text, lang, config.SOURCE_LANG) if lang else text
            # Trim after translating — the same story is longer in some
            # languages, and only the final string has to fit.
            cache[lang] = _fit(translated, bool(media))
        try:
            sent = await _send(bot, chat_id, cache[lang], media)
            posted.append(chat_id)
            link = getattr(sent, "link", None)
            if link:
                links.append((chat_id, link))
        except Exception as exc:  # network, permissions, bad chat id, ...
            log.error("publish to %s failed: %s", chat_id, exc)
            errors.append((chat_id, str(exc)))
    return posted, errors, links
