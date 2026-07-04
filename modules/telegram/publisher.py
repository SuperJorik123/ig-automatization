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


def _ref(m: dict):
    """The value to hand Telegram: a reusable file_id, else the local path."""
    return m.get("file_id") or m["path"]


async def _send(bot, chat_id, caption: str, media: list) -> None:
    if not media:
        await bot.send_message(chat_id=chat_id, text=caption or "")
        return
    if len(media) == 1:
        m = media[0]
        if m["type"] == "video":
            await bot.send_video(chat_id=chat_id, video=_ref(m), caption=caption or None)
        else:
            await bot.send_photo(chat_id=chat_id, photo=_ref(m), caption=caption or None)
        return
    # Album: caption rides on the first item only.
    group = []
    for i, m in enumerate(media):
        cap = caption if i == 0 else None
        cls = InputMediaVideo if m["type"] == "video" else InputMediaPhoto
        group.append(cls(media=_ref(m), caption=cap))
    await bot.send_media_group(chat_id=chat_id, media=group)


async def publish(bot, text: str, media: list, dests: list | None = None):
    """Send `text` (+ optional media) to each destination channel (default: all
    of TG_DESTINATIONS), translating the caption into each destination's
    language first. Translations are cached per language so N same-language
    channels cost one API call. A failure on one channel is logged and skipped
    — it does not abort the rest.

    Returns (posted, errors): posted is the list of chat_ids that succeeded,
    errors is a list of (chat_id, message)."""
    if dests is None:
        dests = config.TG_DESTINATIONS
    cache: dict[str, str] = {}
    posted, errors = [], []
    for dest in dests:
        chat_id, lang = dest["chat_id"], dest["lang"]
        if lang not in cache:
            cache[lang] = translator.translate(text, lang, config.SOURCE_LANG) if lang else text
        try:
            await _send(bot, chat_id, cache[lang], media)
            posted.append(chat_id)
        except Exception as exc:  # network, permissions, bad chat id, ...
            log.error("publish to %s failed: %s", chat_id, exc)
            errors.append((chat_id, str(exc)))
    return posted, errors
