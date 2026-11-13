"""shared/render_lock.py — the cross-bot render lock with full-bot priority.

Offline and OS-independent: a fake fcntl stands in for flock (Windows has
none), so what is tested is the waiting / priority / want-file logic."""

import asyncio
import os
import time
import types

import pytest

from shared import render_lock


class _FakeFcntl:
    """flock semantics enough for the logic: one exclusive holder per path."""
    LOCK_EX, LOCK_NB, LOCK_UN = 2, 4, 8

    def __init__(self):
        self.held = {}  # path -> fileno

    def flock(self, fileno, op):
        path = self.paths[fileno]
        if op & self.LOCK_UN:
            if self.held.get(path) == fileno:
                del self.held[path]
            return
        if path in self.held and self.held[path] != fileno:
            raise BlockingIOError
        self.held[path] = fileno


@pytest.fixture
def fake(monkeypatch, tmp_path):
    f = _FakeFcntl()
    f.paths = {}
    real_open = open

    def tracking_open(path, mode="r", *a, **kw):
        fh = real_open(path, mode, *a, **kw)
        f.paths[fh.fileno()] = path
        return fh

    monkeypatch.setattr(render_lock, "fcntl", f)
    monkeypatch.setattr(render_lock, "open", tracking_open, raising=False)
    monkeypatch.setattr(render_lock, "POLL_S", 0.01)
    return types.SimpleNamespace(fcntl=f, path=str(tmp_path / "render.lock"))


def _run(coro):
    return asyncio.run(coro)


def test_no_fcntl_is_a_no_op(monkeypatch, tmp_path):
    monkeypatch.setattr(render_lock, "fcntl", None)

    async def go():
        async with render_lock.hold(str(tmp_path / "l"), priority=False):
            async with render_lock.hold(str(tmp_path / "l"), priority=True):
                return "both ran"
    assert _run(go()) == "both ran"


def test_a_started_render_finishes_before_the_full_bot_gets_in(fake):
    """Lite holds the lock; the full bot waits (with its notice) and runs
    only after lite released it — nobody is preempted."""
    order, notices = [], []

    async def lite():
        async with render_lock.hold(fake.path, priority=False):
            order.append("lite start")
            await asyncio.sleep(0.1)
            order.append("lite end")

    async def full():
        await asyncio.sleep(0.02)

        async def notice():
            notices.append("full waits")
        async with render_lock.hold(fake.path, priority=True, on_wait=notice):
            order.append("full start")

    async def go():
        await asyncio.gather(lite(), full())
    _run(go())
    assert order == ["lite start", "lite end", "full start"]
    assert notices == ["full waits"]
    assert not os.path.exists(fake.path + ".want")


def test_full_bot_goes_first_when_both_wait(fake):
    """A third holder has the lock; full and lite both queue. Lite stays out
    while the full bot's want file is fresh, so full runs next."""
    order = []

    async def holder():
        async with render_lock.hold(fake.path, priority=True):
            await asyncio.sleep(0.1)
            order.append("holder")

    async def lite():
        await asyncio.sleep(0.01)
        async with render_lock.hold(fake.path, priority=False):
            order.append("lite")

    async def full():
        await asyncio.sleep(0.03)  # arrives AFTER lite started waiting
        async with render_lock.hold(fake.path, priority=True):
            order.append("full")
            await asyncio.sleep(0.03)

    async def go():
        await asyncio.gather(holder(), lite(), full())
    _run(go())
    assert order == ["holder", "full", "lite"]


def test_a_stale_want_file_does_not_block_lite(fake):
    with open(fake.path + ".want", "w") as fh:
        fh.write("12345\n")
    old = time.time() - render_lock.WANT_STALE_S - 5
    os.utime(fake.path + ".want", (old, old))

    async def go():
        async with render_lock.hold(fake.path, priority=False):
            return "ran"
    assert _run(asyncio.wait_for(go(), 2)) == "ran"


def test_a_failing_wait_notice_never_stops_the_render(fake):
    order = []

    async def boom():
        raise RuntimeError("telegram down")

    async def first():
        async with render_lock.hold(fake.path, priority=False):
            await asyncio.sleep(0.05)

    async def second():
        await asyncio.sleep(0.01)
        async with render_lock.hold(fake.path, priority=True, on_wait=boom):
            order.append("rendered")

    async def go():
        await asyncio.gather(first(), second())
    _run(go())
    assert order == ["rendered"]
