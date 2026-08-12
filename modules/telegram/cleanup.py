"""
modules/telegram/cleanup.py — the weekly control-group wipe.

Every Monday at 04:00 local time (registered by news_bot as a JobQueue job)
the control group is emptied: open reaction asks are closed as skipped, then
every message the bot has TRACKED there (queue_store.group_messages — the Bot
API can't list chat history, so tracked = seen incoming + sent outgoing) is
deleted in batches of 100. Messages posted while the bot was down were never
tracked and survive.

The bot needs the "Delete messages" admin right in the group to remove the
operator's messages; without it only its own recent (<48 h) ones are
deletable — failures are counted, logged and moved past, never raised.

Destination channels are never touched: only the chat id passed in is wiped.
"""

import logging

from modules.telegram import queue_store

log = logging.getLogger(__name__)

# Bot API delete_messages caps at 100 ids per call.
BATCH = 100

# Day index for JobQueue.run_daily. python-telegram-bot 20.0 CHANGED this
# mapping from monday-sunday to **sunday-saturday**, so Monday is 1, not 0 —
# a 0 here silently moves the whole wipe to Sunday.
MONDAY = 1


async def wipe_chat(bot, chat_id) -> tuple[int, int]:
    """Close every open ask as skipped, then delete every tracked message in
    `chat_id`. Returns (deleted, failed). Rows are cleared for every id
    attempted — an id that can't be deleted now never will be (too old), so
    keeping it would just re-fail forever."""
    for ask in queue_store.open_asks():
        queue_store.close_ask(ask["id"], "skipped")
        log.info("cleanup: ask %d closed as skipped", ask["id"])

    ids = queue_store.tracked_message_ids(str(chat_id))
    deleted = failed = 0
    for i in range(0, len(ids), BATCH):
        batch = ids[i:i + BATCH]
        try:
            # AttributeError (PTB < 20.8 has no delete_messages) falls through
            # to the per-message path along with any API refusal.
            await bot.delete_messages(chat_id=chat_id, message_ids=batch)
            deleted += len(batch)
        except Exception:
            for mid in batch:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=mid)
                    deleted += 1
                except Exception:
                    failed += 1
        queue_store.clear_group_messages(str(chat_id), batch)

    log.info("cleanup: %d message(s) deleted, %d failed", deleted, failed)
    return deleted, failed
