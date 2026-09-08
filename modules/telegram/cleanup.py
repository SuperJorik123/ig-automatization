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
import os
import time

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


def sweep_media(media_dir: str, max_age_h: int) -> tuple[int, int, int]:
    """Delete every file in `media_dir` older than `max_age_h`. Returns
    (deleted, bytes_freed, failed).

    Age off the filesystem, deliberately — NOT off the queue. The DB-driven
    alternative (expired items -> their media paths) needs a `purged` column
    to stop rescanning the same rows nightly and still misses files no item
    ever claimed. One mtime rule collects everything that lands here:
    collector downloads, brand_/card_ renders orphaned by a restart, manual_
    URL fetches — including whatever future code drops in.

    Flat and non-recursive: only plain files directly in `media_dir` are
    considered, so a subdirectory (and anything under it) is never touched.

    One scandir pass, one stat per entry, cutoff precomputed — the cost is a
    few ms even at thousands of files. Failures are counted, never raised: a
    file still held open must not abort the run. They are NOT logged per file
    either, because errmail turns every ERROR into an email."""
    cutoff = time.time() - max_age_h * 3600
    deleted = freed = failed = 0
    try:
        entries = list(os.scandir(media_dir))
    except OSError:
        return 0, 0, 0          # no media dir yet — nothing to sweep
    for entry in entries:
        try:
            if not entry.is_file():
                continue
            st = entry.stat()   # one stat: mtime and size come together
            if st.st_mtime >= cutoff:
                continue
            os.unlink(entry.path)
        except OSError:
            failed += 1
            continue
        deleted += 1
        freed += st.st_size
    log.info("media sweep: %d file(s) deleted, %.2f GB freed, %d failed "
             "(keeping < %dh)", deleted, freed / 2**30, failed, max_age_h)
    return deleted, freed, failed
