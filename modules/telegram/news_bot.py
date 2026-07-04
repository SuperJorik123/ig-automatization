"""
modules/telegram/news_bot.py — group-triggered news broadcaster.

Post a photo/video/album with a caption (or a plain text message) into the
control group (TELEGRAM_CHAT_ID). The bot replies with a channel picker
listing every TG_DESTINATIONS channel; toggle the ones you want (All / None
shortcuts included) and hit "Post to selected" — the caption is translated
into each channel's language and the post fans out.

No scheduling, no queue: what you post in the group is all that goes out.

Run:  py modules/telegram/news_bot.py
"""

import asyncio
import logging
import os
import sys

# Make the repo root importable so `from shared` / `from modules` resolve when
# run directly (`py modules/telegram/news_bot.py`).
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update  # noqa: E402
from telegram.ext import (  # noqa: E402
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from shared import config  # noqa: E402
from modules.telegram import publisher  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("news_bot")

if not config.TELEGRAM_BOT_TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN missing in .env")
if not config.TELEGRAM_CHAT_ID:
    raise SystemExit("TELEGRAM_CHAT_ID missing in .env — the control group's numeric id")
try:
    CHAT_ID = int(config.TELEGRAM_CHAT_ID)
except ValueError as exc:
    raise SystemExit(
        f"TELEGRAM_CHAT_ID must be an integer, got {config.TELEGRAM_CHAT_ID!r}"
    ) from exc
if not config.TG_DESTINATIONS:
    raise SystemExit("TG_DESTINATIONS empty in .env — list your news channels as chat_id:lang")


# --------------------------------------------------------------------------- #
# Pending prompts + album buffering                                           #
# --------------------------------------------------------------------------- #

# Maps the message_id of a picker prompt the bot sent → the post waiting on it:
# {"text": str, "media": [{"file_id","type"}...], "selected": set[int]}.
# In-memory only; a bot restart expires open pickers (tapping one then says so).
_pending = {}

# Telegram delivers an album as separate messages sharing a media_group_id.
# Buffer the parts and flush after a short quiet period so one album produces
# one picker instead of N.
_albums = {}
_ALBUM_SETTLE_S = 1.5


def _extract_media(msg):
    """file_id + kind for a photo/video message, else None (text-only)."""
    if msg.photo:
        return {"file_id": msg.photo[-1].file_id, "type": "photo"}
    if msg.video:
        return {"file_id": msg.video.file_id, "type": "video"}
    return None


def _keyboard(selected: set) -> InlineKeyboardMarkup:
    """Channel picker: one row per destination, indexed into TG_DESTINATIONS
    (indices keep callback_data tiny — Telegram caps it at 64 bytes)."""
    rows = []
    for i, dest in enumerate(config.TG_DESTINATIONS):
        mark = "☑" if i in selected else "☐"
        label = f"{mark} {dest['chat_id']}"
        if dest["lang"]:
            label += f" · {dest['lang']}"
        rows.append([InlineKeyboardButton(label, callback_data=f"t:{i}")])
    rows.append([
        InlineKeyboardButton("All", callback_data="all"),
        InlineKeyboardButton("None", callback_data="none"),
    ])
    rows.append([InlineKeyboardButton("▶ Post to selected", callback_data="submit")])
    rows.append([InlineKeyboardButton("✕ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


async def _prompt(msg, text: str, media: list) -> None:
    """Reply to the news message with the channel picker."""
    n_media = len(media)
    what = f"{n_media} media" if n_media else "text post"
    prompt = await msg.reply_text(
        f"Post this ({what}) to which channels?",
        reply_markup=_keyboard(set()),
    )
    _pending[prompt.message_id] = {"text": text, "media": media, "selected": set()}


async def _flush_album(gid) -> None:
    """Fires after the album has been quiet for _ALBUM_SETTLE_S."""
    try:
        await asyncio.sleep(_ALBUM_SETTLE_S)
    except asyncio.CancelledError:
        return  # another part arrived; its timer took over
    entry = _albums.pop(gid, None)
    if entry:
        await _prompt(entry["msg"], entry["text"], entry["media"])


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A message in the control group = a news candidate. Show the picker."""
    msg = update.effective_message
    if msg is None:
        return
    text = msg.caption or msg.text or ""
    media_item = _extract_media(msg)
    if not text.strip() and media_item is None:
        return  # stickers, joins, etc.

    gid = msg.media_group_id
    if gid is None:
        await _prompt(msg, text, [media_item] if media_item else [])
        return

    # Album part: merge into the buffer and restart the settle timer.
    entry = _albums.get(gid)
    if entry is None:
        _albums[gid] = entry = {"msg": msg, "text": text, "media": []}
    else:
        entry["task"].cancel()
        if text and not entry["text"]:
            entry["text"] = text  # caption can ride on any part
    if media_item:
        entry["media"].append(media_item)
    entry["task"] = asyncio.create_task(_flush_album(gid))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle taps on the channel picker."""
    q = update.callback_query
    if q.message is None or q.message.chat.id != CHAT_ID:
        await q.answer()
        return

    state = _pending.get(q.message.message_id)
    if state is None:
        await q.answer()
        await q.edit_message_text("(prompt expired — post the news again)")
        return

    data = q.data

    if data == "submit" and not state["selected"]:
        await q.answer("Pick at least one channel.", show_alert=True)
        return

    await q.answer()

    if data.startswith("t:"):
        i = int(data.split(":", 1)[1])
        state["selected"] ^= {i}  # toggle
        await q.edit_message_reply_markup(_keyboard(state["selected"]))
        return

    if data == "all":
        state["selected"] = set(range(len(config.TG_DESTINATIONS)))
        await q.edit_message_reply_markup(_keyboard(state["selected"]))
        return

    if data == "none":
        state["selected"] = set()
        await q.edit_message_reply_markup(_keyboard(state["selected"]))
        return

    if data == "cancel":
        _pending.pop(q.message.message_id, None)
        await q.edit_message_text("✕ cancelled")
        return

    if data == "submit":
        dests = [config.TG_DESTINATIONS[i] for i in sorted(state["selected"])]
        _pending.pop(q.message.message_id, None)
        await q.edit_message_text(
            "⏳ posting to " + ", ".join(d["chat_id"] for d in dests) + " …"
        )
        posted, errors = await publisher.publish(
            context.bot, state["text"], state["media"], dests
        )
        lines = []
        if posted:
            lines.append("✅ posted to " + ", ".join(posted))
        for chat_id, err in errors:
            lines.append(f"❌ {chat_id}: {err}")
        await q.edit_message_text("\n".join(lines))


def main() -> None:
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    # Chat filter limits the bot to the control group; ~COMMAND skips /commands.
    app.add_handler(MessageHandler(filters.Chat(CHAT_ID) & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(on_callback))
    log.info(
        "news bot up: listening in chat %s, %d destination channel(s): %s",
        CHAT_ID,
        len(config.TG_DESTINATIONS),
        ", ".join(f"{d['chat_id']}({d['lang'] or 'raw'})" for d in config.TG_DESTINATIONS),
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
