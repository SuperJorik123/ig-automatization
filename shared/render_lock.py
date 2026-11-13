"""
shared/render_lock.py — one render at a time across the two news bots.

The full news bot and the lite render bot run from the same checkout on the
same 2 vCPU / 1.9 GB VPS. One branded render is ~0.5 GB per brand in the pass
plus the bot itself; two at once can push the box into the OOM killer, and the
process it picks is whichever is biggest — usually the full bot's encode. So a
render (the `edit:` pass, the branding pass, a photo card) runs only while
holding this lock.

Rules (owner's call, 2026-11-13):
  * A render that has STARTED always finishes — nobody preempts anybody. If
    the lite bot is mid-render, the full bot waits for it (a lite render is
    one brand, about a minute; ffrun's time limit bounds a hung one).
  * When both are WAITING, the full bot goes first: it leaves a "want" file
    next to the lock while it waits, and the lite bot doesn't take the lock
    while that file is fresh.

`flock`, not a marker file: the kernel drops the lock when its process dies,
so a crash or a `systemctl restart` mid-render can never leave a stale lock
behind. The want file is refreshed every poll and ignored once it is older
than WANT_STALE_S, so a full bot killed while waiting can't block the lite one
either.

Windows has no fcntl: there the lock is a no-op (local dev runs one bot).

Not the same thing as shared/renderlock.py, which serialises single ffmpeg
CALLS machine-wide (and gives up after 15 min). This one is held for a whole
render — edit pass, every brand batch, a photo card's rembg — has no timeout,
knows which bot goes first, and lets the bot tell its group it is waiting. The
two nest without deadlock: the inner lock is only ever held for one ffmpeg run.
"""

import asyncio
import contextlib
import logging
import os
import time

try:
    import fcntl
except ImportError:  # Windows — local dev only ever runs one bot
    fcntl = None

log = logging.getLogger("render_lock")

POLL_S = 1.0
WANT_STALE_S = 10.0


def _want_path(path: str) -> str:
    return path + ".want"


def _want_fresh(path: str) -> bool:
    try:
        return time.time() - os.path.getmtime(_want_path(path)) < WANT_STALE_S
    except OSError:
        return False


def _touch_want(path: str) -> None:
    try:
        with open(_want_path(path), "w") as fh:
            fh.write(f"{os.getpid()}\n")
    except OSError as exc:
        log.warning("render lock: couldn't write the want file (%s)", exc)


def _drop_want(path: str) -> None:
    try:
        os.remove(_want_path(path))
    except OSError:
        pass


def _try_lock(fh) -> bool:
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


@contextlib.asynccontextmanager
async def hold(path: str, priority: bool, on_wait=None):
    """Hold the render lock for the body of the `async with`.

    `priority`: True for the full bot (goes first when both wait), False for
    the lite bot. `on_wait`: an async callable run ONCE, the first time this
    has to wait — the bot uses it to tell its group why nothing is happening.
    Its failure is logged and ignored: a status message must never stop a
    render."""
    if fcntl is None:
        yield
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "a+")
    waited = False
    wrote_want = False  # only ever remove OUR want, never another waiter's
    try:
        while True:
            if (priority or not _want_fresh(path)) and _try_lock(fh):
                break
            if priority:
                _touch_want(path)  # refreshed every poll — stale once we die
                wrote_want = True
            if not waited:
                waited = True
                log.info("render lock busy — waiting (%s)",
                         "priority" if priority else "after the full bot")
                if on_wait is not None:
                    try:
                        await on_wait()
                    except Exception as exc:
                        log.warning("render lock: wait notice failed (%s)", exc)
            await asyncio.sleep(POLL_S)
        if wrote_want:
            _drop_want(path)
            wrote_want = False
        if waited:
            log.info("render lock acquired")
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        if wrote_want:  # cancelled while waiting
            _drop_want(path)
        fh.close()
