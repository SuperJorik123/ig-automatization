"""
modules/telegram/collector.py — reads your source channels via a Telethon USER
client and queues each new post (text + downloaded media) for the daily drip.

Why a user client: a bot cannot read channels it doesn't own/admin, so
collection runs as YOUR account. First run does an interactive login (Telegram
texts you a code); it saves a .session file under modules/telegram/data/ so
later runs are non-interactive.

Run (do the first login yourself so you can enter the code):
    py modules/telegram/collector.py
"""

import logging
import os
import sys

# Make the repo root importable so `from shared` / `from modules` resolve when
# run directly (`py modules/telegram/collector.py`).
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from telethon import TelegramClient, events  # noqa: E402

from shared import config  # noqa: E402
from modules.telegram import queue_store  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("collector")

_SESSION = os.path.join(config.TG_DATA_DIR, "collector.session")
_MEDIA_DIR = os.path.join(config.TG_DATA_DIR, "media")


async def _handle(event, messages) -> None:
    """Queue one logical post. `messages` is [message] for a single post or the
    full list for an album."""
    os.makedirs(_MEDIA_DIR, exist_ok=True)
    text = ""
    media = []
    for m in messages:
        if not text and m.message:
            text = m.message  # caption usually sits on the first message
        if m.media:
            stem = os.path.join(_MEDIA_DIR, f"{m.chat_id}_{m.id}")
            path = await m.download_media(file=stem)  # Telethon adds the real ext
            if path:
                media.append({"path": path, "type": "photo" if m.photo else "video"})
    source = getattr(event.chat, "username", None) or str(event.chat_id)
    queue_store.enqueue(source, text, media)
    queue_store.set_cursor(source, max(m.id for m in messages))
    log.info("queued a post from %s (%d media)", source, len(media))


def main() -> None:
    if not (config.TELEGRAM_API_ID and config.TELEGRAM_API_HASH):
        raise SystemExit("TELEGRAM_API_ID / TELEGRAM_API_HASH missing in .env (get them at my.telegram.org)")
    if not config.TG_SOURCES:
        raise SystemExit("TG_SOURCES empty in .env — list the source channels to collect from")
    os.makedirs(config.TG_DATA_DIR, exist_ok=True)
    queue_store.init()

    client = TelegramClient(_SESSION, int(config.TELEGRAM_API_ID), config.TELEGRAM_API_HASH)

    @client.on(events.Album(chats=config.TG_SOURCES))
    async def _on_album(event):
        await _handle(event, event.messages)

    @client.on(events.NewMessage(chats=config.TG_SOURCES))
    async def _on_message(event):
        if event.grouped_id:  # part of an album — handled by _on_album
            return
        await _handle(event, [event.message])

    log.info("collector connecting; watching %d source(s)", len(config.TG_SOURCES))
    client.start()  # interactive phone-code login on first run, then reuses the session
    log.info("collector running — new posts from sources will be queued. Ctrl+C to stop.")
    client.run_until_disconnected()


if __name__ == "__main__":
    main()
