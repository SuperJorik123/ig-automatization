"""
modules/telegram/news_bot.py — group-triggered news broadcaster.

Post a photo/video/album with a caption (or a plain text message) into the
control group (TELEGRAM_CHAT_ID). The bot replies with a channel picker
listing every TG_DESTINATIONS channel — and, when the post contains a video,
every YT_DESTINATIONS YouTube channel too. Toggle the ones you want (All /
None shortcuts included) and hit "Post to selected" — the caption is
translated into each destination's language and the post fans out: Telegram
channels get the message/media, YouTube channels get the video uploaded as a
Short (caption first line → title, full caption → description).

YouTube limitation: the Bot API cannot download files over 20 MB, so manual
YouTube posting fails with a clear error for bigger videos. (The collector →
dispatcher auto-upload path has no such limit — it downloads via MTProto.)

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
from modules.youtube import publisher as yt_publisher  # noqa: E402

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
if not config.TG_DESTINATIONS and not config.YT_DESTINATIONS:
    raise SystemExit(
        "No destinations in .env — set TG_DESTINATIONS (chat_id:lang) and/or "
        "YT_DESTINATIONS (yt_account:lang)"
    )

# Bot API refuses get_file above this size; bigger videos can't be posted to
# YouTube through the manual flow.
_BOT_FILE_LIMIT = 20 * 1024 * 1024


# --------------------------------------------------------------------------- #
# Pending prompts + album buffering                                           #
# --------------------------------------------------------------------------- #

# Maps the message_id of a picker prompt the bot sent → the post waiting on it:
# {"text": str, "media": [{"file_id","type","file_size"}...],
#  "sel_tg": set[int], "sel_yt": set[int]}.
# sel_tg indexes TG_DESTINATIONS, sel_yt indexes YT_DESTINATIONS (YouTube rows
# are only offered when the post contains a video).
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
        return {
            "file_id": msg.video.file_id,
            "type": "video",
            "file_size": msg.video.file_size or 0,
        }
    return None


def _has_video(media: list) -> bool:
    return any(m["type"] == "video" for m in media)


def _keyboard(state: dict) -> InlineKeyboardMarkup:
    """Channel picker: one row per Telegram destination ("t:<i>" indexes
    TG_DESTINATIONS) plus, for posts that contain a video, one row per YouTube
    channel ("y:<i>" indexes YT_DESTINATIONS). Indices keep callback_data tiny
    — Telegram caps it at 64 bytes."""
    rows = []
    for i, dest in enumerate(config.TG_DESTINATIONS):
        mark = "☑" if i in state["sel_tg"] else "☐"
        label = f"{mark} {dest['chat_id']}"
        if dest["lang"]:
            label += f" · {dest['lang']}"
        rows.append([InlineKeyboardButton(label, callback_data=f"t:{i}")])
    if _has_video(state["media"]):
        for i, dest in enumerate(config.YT_DESTINATIONS):
            mark = "☑" if i in state["sel_yt"] else "☐"
            label = f"{mark} ▶️ YT {dest['chat_id']}"
            if dest["lang"]:
                label += f" · {dest['lang']}"
            rows.append([InlineKeyboardButton(label, callback_data=f"y:{i}")])
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
    state = {"text": text, "media": media, "sel_tg": set(), "sel_yt": set()}
    prompt = await msg.reply_text(
        f"Post this ({what}) to which channels?",
        reply_markup=_keyboard(state),
    )
    _pending[prompt.message_id] = state


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


async def _post_to_youtube(bot, text: str, media: list, dests: list):
    """Download the post's video from Telegram and upload it as a Short to
    each selected YouTube channel (translated per channel). Returns the same
    (posted, errors) shape as the Telegram publisher."""
    video = next((m for m in media if m["type"] == "video"), None)
    if video is None:
        return [], [(d["chat_id"], "post has no video") for d in dests]

    size = video.get("file_size") or 0
    if size > _BOT_FILE_LIMIT:
        err = f"video too large for manual posting ({size / 1024 / 1024:.0f} MB > 20 MB limit)"
        return [], [(d["chat_id"], err) for d in dests]

    dl_dir = os.path.join(config.TG_DATA_DIR, "media")
    os.makedirs(dl_dir, exist_ok=True)
    # file_id chars are filename-safe (base64url alphabet); last chars suffice.
    path = os.path.join(dl_dir, f"manual_{video['file_id'][-24:]}.mp4")
    try:
        tg_file = await bot.get_file(video["file_id"])
        await tg_file.download_to_drive(path)
        # Uploads are blocking network calls — keep the bot's event loop free.
        return await asyncio.to_thread(yt_publisher.publish_shorts, path, text, dests)
    except Exception as exc:  # get_file size refusal, network, ...
        log.error("manual YouTube post failed: %s", exc)
        return [], [(d["chat_id"], str(exc)) for d in dests]
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


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

    if data == "submit" and not (state["sel_tg"] or state["sel_yt"]):
        await q.answer("Pick at least one channel.", show_alert=True)
        return

    await q.answer()

    if data.startswith("t:"):
        state["sel_tg"] ^= {int(data.split(":", 1)[1])}  # toggle
        await q.edit_message_reply_markup(_keyboard(state))
        return

    if data.startswith("y:"):
        state["sel_yt"] ^= {int(data.split(":", 1)[1])}  # toggle
        await q.edit_message_reply_markup(_keyboard(state))
        return

    if data == "all":
        state["sel_tg"] = set(range(len(config.TG_DESTINATIONS)))
        if _has_video(state["media"]):
            state["sel_yt"] = set(range(len(config.YT_DESTINATIONS)))
        await q.edit_message_reply_markup(_keyboard(state))
        return

    if data == "none":
        state["sel_tg"] = set()
        state["sel_yt"] = set()
        await q.edit_message_reply_markup(_keyboard(state))
        return

    if data == "cancel":
        _pending.pop(q.message.message_id, None)
        await q.edit_message_text("✕ cancelled")
        return

    if data == "submit":
        dests_tg = [config.TG_DESTINATIONS[i] for i in sorted(state["sel_tg"])]
        dests_yt = [config.YT_DESTINATIONS[i] for i in sorted(state["sel_yt"])]
        _pending.pop(q.message.message_id, None)
        names = [d["chat_id"] for d in dests_tg] + [f"YT {d['chat_id']}" for d in dests_yt]
        await q.edit_message_text("⏳ posting to " + ", ".join(names) + " …")

        lines = []
        if dests_tg:
            posted, errors = await publisher.publish(
                context.bot, state["text"], state["media"], dests_tg
            )
            if posted:
                lines.append("✅ posted to " + ", ".join(posted))
            for chat_id, err in errors:
                lines.append(f"❌ {chat_id}: {err}")

        if dests_yt:
            posted, errors = await _post_to_youtube(
                context.bot, state["text"], state["media"], dests_yt
            )
            if posted:
                lines.append("✅ YouTube: " + ", ".join(posted))
            for account, err in errors:
                lines.append(f"❌ YT {account}: {err}")

        await q.edit_message_text("\n".join(lines))


def main() -> None:
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    # Chat filter limits the bot to the control group; ~COMMAND skips /commands.
    app.add_handler(MessageHandler(filters.Chat(CHAT_ID) & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(on_callback))
    log.info(
        "news bot up: listening in chat %s, %d telegram channel(s): %s | %d youtube channel(s): %s",
        CHAT_ID,
        len(config.TG_DESTINATIONS),
        ", ".join(f"{d['chat_id']}({d['lang'] or 'raw'})" for d in config.TG_DESTINATIONS) or "—",
        len(config.YT_DESTINATIONS),
        ", ".join(f"{d['chat_id']}({d['lang'] or 'raw'})" for d in config.YT_DESTINATIONS) or "—",
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
