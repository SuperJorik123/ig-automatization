"""
shared/renderlock.py — one ffmpeg render at a time per machine.

Why: every render is CPU-bound on the same cores (the VPS has exactly one),
and two concurrent renders don't overlap any work — they each run at half
speed and both finish late (measured 2026-08-17: the brand renderer and the
dispatcher's Shorts re-render colliding doubled the wall time of both).
Serialising them costs total throughput nothing and returns each render to
full speed.

Cross-process: the news bot (branding) and the dispatcher (shorts_format) are
separate processes, so this is an OS file lock — flock on POSIX, msvcrt on
Windows — which the OS releases automatically if the holder dies, so a
crashed render can never wedge the next one. Waiting is bounded: a render
that can't get the slot within `timeout` proceeds without it. Slow beats
deadlocked — the lock is an optimisation, never a correctness rule, which is
also why nothing in here ever raises past the caller.
"""

import logging
import os
import tempfile
import time
from contextlib import contextmanager

log = logging.getLogger("renderlock")

# System temp: the one path both processes resolve identically whether they
# run as root on the VPS or as the desktop user on the PC.
LOCK_PATH = os.path.join(tempfile.gettempdir(), "ig-automation-render.lock")
TIMEOUT_S = 15 * 60     # longest sane wait: a full multi-brand render ahead
POLL_S = 1.0

if os.name == "nt":
    import msvcrt

    def _try_lock(fh) -> bool:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fh) -> bool:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fh) -> None:
        fcntl.flock(fh, fcntl.LOCK_UN)


@contextmanager
def render_slot(timeout: float = TIMEOUT_S):
    """Hold the machine-wide render slot around an ffmpeg run. Polls until
    the slot frees up or `timeout` passes — worst case the block simply runs
    unserialised, with a warning in the log."""
    fh = None
    locked = False
    try:
        fh = open(LOCK_PATH, "a+b")
        deadline = time.monotonic() + timeout
        while not (locked := _try_lock(fh)):
            if time.monotonic() >= deadline:
                log.warning("render slot still busy after %.0fs — "
                            "rendering unserialised", timeout)
                break
            time.sleep(POLL_S)
    except OSError as exc:   # unwritable temp dir etc. — never fatal
        log.warning("render lock unavailable (%s) — rendering unserialised", exc)
    try:
        yield
    finally:
        if fh is not None:
            if locked:
                try:
                    _unlock(fh)
                except OSError:
                    pass
            try:
                fh.close()
            except OSError:
                pass
