"""modules/telegram/cleanup.py — the weekly control-group wipe (against a stub
bot) and the daily media sweep (against a tmp_path). Offline as always."""

import asyncio
import os
import time

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


# --------------------------------------------------------------------------- #
# sweep_media — the daily disk janitor                                        #
# --------------------------------------------------------------------------- #

def aged(d, name, hours, size=1):
    """A file in `d` whose mtime is `hours` old."""
    p = d / name
    p.write_bytes(b"x" * size)
    stamp = time.time() - hours * 3600
    os.utime(p, (stamp, stamp))
    return p


def test_sweep_deletes_files_older_than_the_window(tmp_path):
    old = aged(tmp_path, "-100777_1.mp4", 30)
    older = aged(tmp_path, "-100777_2.jpg", 100)

    deleted, freed, failed = cleanup.sweep_media(str(tmp_path), 24)

    assert (deleted, failed) == (2, 0)
    assert not old.exists() and not older.exists()


def test_sweep_keeps_files_inside_the_window(tmp_path):
    """The retention window is the whole safety mechanism: an open brand-it
    picker's renders are minutes old and must survive the nightly job."""
    fresh = aged(tmp_path, "brand_228_dailynews.mp4", 1)
    edge = aged(tmp_path, "card_src_abc_0.jpg", 23)

    deleted, freed, failed = cleanup.sweep_media(str(tmp_path), 24)

    assert deleted == 0
    assert fresh.exists() and edge.exists()


def test_sweep_reports_bytes_freed(tmp_path):
    aged(tmp_path, "a.mp4", 30, size=700)
    aged(tmp_path, "b.mp4", 30, size=300)
    aged(tmp_path, "keep.mp4", 1, size=5000)

    deleted, freed, failed = cleanup.sweep_media(str(tmp_path), 24)

    assert deleted == 2
    assert freed == 1000          # only what actually went


def test_sweep_ignores_subdirectories(tmp_path):
    """Flat by design — the sweep must never recurse into a sibling that
    happens to sit under the media dir."""
    sub = tmp_path / "nested"
    sub.mkdir()
    buried = aged(sub, "old.mp4", 100)
    stamp = time.time() - 100 * 3600
    os.utime(sub, (stamp, stamp))

    deleted, freed, failed = cleanup.sweep_media(str(tmp_path), 24)

    assert deleted == 0
    assert buried.exists() and sub.is_dir()


def test_sweep_survives_a_file_it_cannot_delete(tmp_path, monkeypatch):
    """One locked file must not abort the run — it is counted and the sweep
    carries on to the rest."""
    aged(tmp_path, "locked.mp4", 30)
    aged(tmp_path, "fine.mp4", 30)
    real = os.unlink

    def flaky(path):
        if os.path.basename(path) == "locked.mp4":
            raise OSError("in use")
        return real(path)

    monkeypatch.setattr(cleanup.os, "unlink", flaky)

    deleted, freed, failed = cleanup.sweep_media(str(tmp_path), 24)

    assert (deleted, failed) == (1, 1)


def test_sweep_on_a_missing_directory_is_a_no_op(tmp_path):
    """A fresh checkout has no media dir until the first download."""
    assert cleanup.sweep_media(str(tmp_path / "nope"), 24) == (0, 0, 0)
