"""Thin wrapper over a uiautomator2 device — the only place the vision driver
talks to the phone, so the tests can swap in a fake with the same methods.

Lessons from the Phase-2 run on IG 457 / Android 16 (2026-11-25):
* Text goes in through accessibility `set_text`, NOT uiautomator2's FastInput
  IME: that IME lives in the "ATX" helper app, which Play Protect blocks on
  Android 16. set_text handles Cyrillic, emoji and newlines, and IG reacts to
  it like typing (search results, caption) without raising the hashtag strip.
* Taps go to coordinates read from the dump: the selector RPC misses some
  views the dump shows (the music picker's select button).
* The screen must stay awake for the whole run (`svc power stayon usb`): a
  screen that times out mid-flow leaves every lookup reading the lock screen.
"""

import http.client
import random
import time


_DROPPED = (ConnectionError, OSError, http.client.HTTPException)


class Device:
    def __init__(self, serial):
        import uiautomator2 as u2

        self.serial = serial
        self.u2 = u2.connect(serial)

    def _read(self, fn, *args):
        """A read retried once: the on-phone uiautomator server sometimes
        drops a request ("Remote end closed connection without response"),
        seen twice on 2026-11-25 right after a reconnect."""
        try:
            return fn(*args)
        except _DROPPED:
            time.sleep(1.5)
            return fn(*args)

    def _act(self, fn, *args):
        """An action is NOT retried — the phone may have done it before the
        connection dropped, and a second tap can undo the first. The driver's
        after-tap check notices a lost one."""
        try:
            fn(*args)
        except _DROPPED as exc:
            print(f"    (connection dropped during an action: {exc}; the next check will tell)")
            time.sleep(1.5)

    # --- reading ---------------------------------------------------------- #
    def dump_hierarchy(self):
        return self._read(self.u2.dump_hierarchy)

    def screenshot(self):
        return self._read(self.u2.screenshot)

    def app_current(self):
        return self._read(self.u2.app_current)

    def shell(self, cmd):
        return self._read(self.u2.shell, cmd)

    def _out(self, cmd):
        res = self.shell(cmd)
        return getattr(res, "output", res if isinstance(res, str) else "") or ""

    def is_locked(self):
        return "isKeyguardShowing=true" in self._out("dumpsys window | grep isKeyguardShowing")

    # --- acting ----------------------------------------------------------- #
    def keep_awake(self):
        self.u2.shell("svc power stayon usb")
        self.u2.screen_on()

    def app_start(self, pkg, stop=False):
        self.u2.app_start(pkg, stop=stop)

    def tap(self, x, y):
        self._act(self.u2.click, int(x), int(y))

    def swipe(self, x1, y1, x2, y2, duration=0.35):
        self._act(self.u2.swipe, int(x1), int(y1), int(x2), int(y2), duration)

    def back(self):
        self._act(self.u2.press, "back")

    def set_text(self, rid, text):
        """Set an input field's whole text via accessibility. Returns True
        when a field took it. Tries the resource-id, then whatever is focused."""
        for sel in (self.u2(resourceId=rid), self.u2(focused=True)):
            try:
                if sel.exists:
                    sel.set_text(text)
                    return True
            except Exception:
                continue
        return False

    def push(self, local, remote):
        self.u2.push(local, remote)


def tap_point(bounds, rng=random):
    """A random point in the middle half of a box — never the same pixel
    twice, never near an edge where a neighbouring view starts."""
    lx, ly, rx, ry = bounds
    w, h = rx - lx, ry - ly
    return (rng.randint(lx + w // 4, rx - w // 4), rng.randint(ly + h // 4, ry - h // 4))
