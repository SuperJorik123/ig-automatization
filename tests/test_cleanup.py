"""modules/telegram/cleanup.py — the weekly control-group wipe, against a stub
bot. Offline as always."""

import asyncio

from modules.telegram import cleanup, reactions


class StubBot:
    """Records deletions; `fail_batches` makes delete_messages raise so the
    per-message fallback path runs; `fail_ids` makes individual deletes fail
    (already deleted / older than the bot's rights allow)."""

    def __init__(self, fail_batches=False, fail_ids=()):
        self.batches, self.singles = [], []
        self.fail_batches = fail_batches
        self.fail_ids = set(fail_ids)

    async def delete_messages(self, chat_id, message_ids):
        if self.fail_batches:
            raise RuntimeError("batch delete refused")
        self.batches.append(list(message_ids))

    async def delete_message(self, chat_id, message_id):
        if message_id in self.fail_ids:
            raise RuntimeError("message can't be deleted")
        self.singles.append(message_id)


def wipe(bot, chat_id="-100777"):
    return asyncio.run(cleanup.wipe_chat(bot, chat_id))


def test_wipe_deletes_in_batches_of_100(store):
    for i in range(1, 251):
        store.track_group_message("-100777", i)
    bot = StubBot()

    deleted, failed = wipe(bot)

    assert deleted == 250 and failed == 0
    assert [len(b) for b in bot.batches] == [100, 100, 50]
    assert store.tracked_message_ids("-100777") == []   # table cleared


def test_wipe_falls_back_to_single_deletes(store):
    for i in (1, 2, 3):
        store.track_group_message("-100777", i)
    bot = StubBot(fail_batches=True, fail_ids={2})

    deleted, failed = wipe(bot)

    assert deleted == 2 and failed == 1
    assert bot.singles == [1, 3]
    # failed ids are cleared too — they'll never become deletable
    assert store.tracked_message_ids("-100777") == []


def test_wipe_closes_open_asks_as_skipped(store):
    ask_id = store.open_ask(41, reactions.new_state(41, [
        {"chat_id": "@c", "link": "https://t.me/c/1"}]))

    wipe(StubBot())

    assert store.get_ask(ask_id)["status"] == "skipped"
    assert store.open_asks() == []


def test_wipe_leaves_other_chats_alone(store):
    store.track_group_message("-100777", 1)
    store.track_group_message("-100999", 2)

    wipe(StubBot(), "-100777")

    assert store.tracked_message_ids("-100999") == [2]


def test_monday_is_the_ptb_day_index_for_monday():
    """python-telegram-bot 20.0 changed run_daily's `days` mapping from
    monday-sunday to sunday-saturday. A 0 here would move the whole wipe to
    Sunday, silently — so pin the intent."""
    week = ("sunday", "monday", "tuesday", "wednesday",
            "thursday", "friday", "saturday")
    assert week[cleanup.MONDAY] == "monday"
