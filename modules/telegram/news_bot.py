"""
modules/telegram/news_bot.py — the publisher bot.

Two jobs:
  1. Manual posts: you DM the bot (text and/or a photo/video); it broadcasts to
     ALL destination channels immediately, translating the caption per channel.
     Only TG_OPERATOR_ID may do this.
  2. Daily drip: once a day (DAILY_TIME / TIMEZONE) it pops the oldest
     auto-collected post from the queue and broadcasts it the same way.

The collector (collector.py) is the other half — it fills the queue from source
channels. Add this bot as an ADMIN in every destination channel.

Run:  py modules/telegram/news_bot.py
Commands (operator only):  /queue  → how many posts are waiting
"""

import asyncio
import datetime
import json
import logging
import os
import sys
import zoneinfo

# Make the repo root importable so `from shared` / `from modules` resolve when
# run directly (`py modules/telegram/news_bot.py`).
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from telegram import Update  # noqa: E402
from telegram.ext import (  # noqa: E402
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from shared import config  # noqa: E402
from modules.telegram import publisher, queue_store  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("news_bot")


def _is_operator(update: Update) -> bool:
    user = update.effective_user
    return bool(
        config.TG_OPERATOR_ID
        and user
        and str(user.id) == str(config.TG_OPERATOR_ID)
    )


# Safety cap on a single unlimited-mode flush tick, so a large backlog doesn't
# hammer Telegram's rate limits in one burst (the rest goes out next tick).
_FLUSH_BURST_CAP = 20


async def _publish_next(bot) -> bool:
    """Publish the oldest pending post. Returns False when the queue is empty."""
    item = queue_store.next_pending()
    if not item:
        return False
    media = json.loads(item["media"] or "[]")
    ok, errors = await publisher.publish(bot, item["text"], media)
    if ok == 0 and errors:
        queue_store.mark_failed(item["id"])
        log.error("post %s failed on every destination", item["id"])
    else:
        queue_store.mark_posted(item["id"])
        log.info("post %s → %d channel(s), %d error(s)", item["id"], ok, len(errors))
    return True


async def daily_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Throttled mode: post up to POSTS_PER_DAY oldest posts, then wait for tomorrow."""
    limit = config.POSTS_PER_DAY or 1
    posted = 0
    while posted < limit and await _publish_next(context.bot):
        posted += 1
        if posted < limit:
            await asyncio.sleep(1)  # gentle on Telegram rate limits
    log.info("daily run: posted %d/%d; %d still queued", posted, limit, queue_store.pending_count())


async def flush_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unlimited mode (POSTS_PER_DAY=false): drain the queue on every tick."""
    posted = 0
    while posted < _FLUSH_BURST_CAP and await _publish_next(context.bot):
        posted += 1
        await asyncio.sleep(1)
    if posted:
        log.info("flush: posted %d; %d still queued", posted, queue_store.pending_count())


async def on_dm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manual post: broadcast the operator's DM to all destinations now."""
    msg = update.effective_message
    if not config.TG_OPERATOR_ID:
        await msg.reply_text(
            "Manual posting is disabled. Set TG_OPERATOR_ID in .env to your numeric "
            "Telegram user id (get it from @userinfobot) and restart the bot."
        )
        return
    if not _is_operator(update):
        return  # ignore everyone else silently

    text = msg.caption or msg.text or ""
    media = []
    if msg.photo:
        media.append({"file_id": msg.photo[-1].file_id, "type": "photo"})
    elif msg.video:
        media.append({"file_id": msg.video.file_id, "type": "video"})
    if not text and not media:
        return

    ok, errors = await publisher.publish(context.bot, text, media)
    reply = f"Sent to {ok} channel(s)"
    if errors:
        reply += f", {len(errors)} failed: " + "; ".join(f"{c}: {e}" for c, e in errors[:3])
    await msg.reply_text(reply)


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_operator(update):
        return
    await update.effective_message.reply_text(f"{queue_store.pending_count()} post(s) queued")


def main() -> None:
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN missing in .env")
    if not config.TG_DESTINATIONS:
        raise SystemExit("TG_DESTINATIONS empty in .env — list your news channels as chat_id:lang")
    queue_store.init()

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    if app.job_queue is None:
        raise SystemExit("JobQueue unavailable — install python-telegram-bot[job-queue]")

    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, on_dm))

    if config.POSTS_PER_DAY is None:
        # Unlimited: poll the queue and post everything as it's collected.
        app.job_queue.run_repeating(flush_job, interval=60, first=5)
        log.info(
            "news bot up: %d destination(s), mode=POST EVERYTHING (queue flushed ~every 60s)",
            len(config.TG_DESTINATIONS),
        )
    else:
        hh, mm = (int(x) for x in config.DAILY_TIME.split(":"))
        tz = zoneinfo.ZoneInfo(config.TIMEZONE)
        app.job_queue.run_daily(daily_job, time=datetime.time(hh, mm, tzinfo=tz))
        log.info(
            "news bot up: %d destination(s), mode=%d/day at %s %s",
            len(config.TG_DESTINATIONS),
            config.POSTS_PER_DAY,
            config.DAILY_TIME,
            config.TIMEZONE,
        )
    app.run_polling()


if __name__ == "__main__":
    main()
