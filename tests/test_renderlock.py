"""shared/renderlock.py — the machine-wide render slot. Offline: the lock is
a file in the system temp dir, no ffmpeg involved."""

from shared import renderlock


def test_render_slot_excludes_a_second_holder_and_releases():
    with renderlock.render_slot():
        fh = open(renderlock.LOCK_PATH, "a+b")
        try:
            assert renderlock._try_lock(fh) is False   # held -> refused
        finally:
            fh.close()
    # Released on exit: a fresh holder gets it immediately.
    fh = open(renderlock.LOCK_PATH, "a+b")
    try:
        assert renderlock._try_lock(fh) is True
        renderlock._unlock(fh)
    finally:
        fh.close()


def test_render_slot_times_out_instead_of_deadlocking():
    """The lock is an optimisation: when the slot stays busy past the
    timeout, the block must run anyway (unserialised), never hang."""
    with renderlock.render_slot():
        with renderlock.render_slot(timeout=0):
            pass   # reaching here IS the assertion
