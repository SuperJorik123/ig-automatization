"""State-machine core of the vision driver: look → decide → tap → check.

* `look()` waits until the screen is STABLE — two reads in a row with the same
  Instagram dump and (unless pixel_check=False) near-identical screenshots —
  then classifies it (`screens.classify`).
* `resolve(expect)` looks until the screen is one of `expect`, dismissing the
  KNOWN popups on the way (screens.POPUPS). It stops — never guesses — on a
  security check, the lock screen, another app in front, an unknown popup, or
  a screen that isn't the expected one when the time runs out.
* `act(target, finders, expect)` = find a node, tap a random point inside it,
  check the transition; one retry when the screen didn't change at all (a
  swallowed tap), a stop otherwise.
* `scroll_find` swipes until a finder hits or two screenshots around a swipe
  are identical (the end of the list).

Every stop saves a capture (screenshot + dump) through the recorder and
raises `Stop`. Every fallback selector, retry, unstable screen and stop goes
to the DRIFT LOG (`data/ig_screens/drift.jsonl`, one JSON per line, with the
capture's file name) — that log is what decides whether a vision model is
ever needed: if it stays near-empty, selectors are enough.
"""

import json
import os
import random
import time
from datetime import datetime

from modules.instagram.vision import screens as scr
from modules.instagram.vision.device import tap_point

STOP_SCREENS = {
    "security": "Security check on screen — not touching it. Solve it by hand on the phone.",
    "lockscreen": "The phone is locked / the screen is off.",
    "other_app": "Another app is in front of Instagram.",
}


class Stop(Exception):
    """The driver stopped on purpose. `kind` says why, `capture` is the
    saved screenshot's path (None without a recorder)."""

    def __init__(self, kind, message, capture=None):
        super().__init__(f"[{kind}] {message}" + (f" (screen saved: {capture})" if capture else ""))
        self.kind = kind
        self.message = message
        self.capture = capture


def _small_gray(image):
    """A 54x117 grayscale thumbnail as a flat byte string — enough to tell
    'the screen moved' from 'the screen is still'."""
    return image.convert("L").resize((54, 117)).tobytes()


def _diff(a, b):
    if a is None or b is None or len(a) != len(b):
        return 255.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def signature(screen_nodes):
    """What has to stay the same for the IG part of the screen to count as
    stable (system UI — clock, battery — is left out)."""
    return tuple(
        (n.rid, n.text, n.desc, n.bounds, n.flag("selected"))
        for n in screen_nodes
        if n.get("package") == scr.PKG
    )


class Driver:
    STILL = 1.5  # mean grey-level difference below which two screenshots are "the same"

    def __init__(self, dev, recorder=None, log=print, drift_path=None, pixel_check=True,
                 sleep=time.sleep, clock=time.monotonic, rng=random, settle_s=8.0):
        self.dev = dev
        self.recorder = recorder
        self.log = log
        self.drift_path = drift_path
        self.pixel_check = pixel_check
        self.sleep = sleep
        self.clock = clock
        self.rng = rng
        self.settle_s = settle_s
        self.screen = None
        self.xml = ""
        self.step = "start"
        self.events = []  # (kind, target, detail) — the run's own drift summary
        self.notes = {}   # facts a flow learns on the way (music library, pushed files)

    # ------------------------------------------------------------------ #
    # observing                                                           #
    # ------------------------------------------------------------------ #
    def look(self):
        prev_sig = prev_img = None
        same_dump = 0
        deadline = self.clock() + self.settle_s
        while True:
            xml = self.dev.dump_hierarchy()
            screen = scr.classify(xml)
            self.xml, self.screen = xml, screen
            sig = signature(screen.nodes)
            img = _small_gray(self.dev.screenshot()) if self.pixel_check else None
            same_dump = same_dump + 1 if sig == prev_sig else 0
            if same_dump >= 1 and (not self.pixel_check or _diff(img, prev_img) < self.STILL):
                break
            # Autoplaying video (Home feed, gallery previews) keeps the pixels
            # moving forever; a dump unchanged over three reads is settled.
            if same_dump >= 2:
                break
            if self.clock() > deadline:
                self.drift("unstable", self.step, f"screen {screen.name} kept changing for {self.settle_s:g}s")
                break
            prev_sig, prev_img = sig, img
            self.sleep(0.4)
        return screen

    def capture(self, label, note=""):
        if not self.recorder:
            return None
        try:
            meta = self.recorder.save(label, note=note)
            return meta["paths"]["png"]
        except Exception as exc:  # a failed capture must not mask the real problem
            self.log(f"    (capture failed: {exc})")
            return None

    def drift(self, kind, target, detail="", capture=True):
        self.events.append((kind, target, detail))
        png = self.capture(f"drift_{kind}_{target}", note=detail) if capture else None
        if self.drift_path:
            try:
                os.makedirs(os.path.dirname(self.drift_path), exist_ok=True)
                with open(self.drift_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "time": datetime.now().isoformat(timespec="seconds"),
                        "kind": kind, "step": self.step, "target": target, "detail": detail,
                        "screen": self.screen.name if self.screen else None,
                        "capture": os.path.basename(png) if png else None,
                    }, ensure_ascii=False) + "\n")
            except OSError as exc:
                self.log(f"    (drift log not written: {exc})")
        self.log(f"    ⚠ drift: {kind} {target} {detail}".rstrip())
        return png

    def stop(self, kind, message):
        png = self.capture(f"stop_{kind}", note=message)
        self.events.append(("stop", kind, message))
        if self.drift_path:
            self.drift("stop", kind, message, capture=False)
        raise Stop(kind, message, png)

    # ------------------------------------------------------------------ #
    # deciding                                                            #
    # ------------------------------------------------------------------ #
    def guard(self, screen):
        if screen.name in STOP_SCREENS:
            self.stop(screen.name, STOP_SCREENS[screen.name])

    def resolve(self, expect, timeout=12.0, on_timeout="stop"):
        """Look until the screen is one of `expect`. Known popups are
        dismissed. Returns the Screen, or None on timeout when
        on_timeout='none'."""
        expect = (expect,) if isinstance(expect, str) else tuple(expect)
        deadline = self.clock() + timeout
        dismissed = 0
        while True:
            s = self.look()
            if s.name in expect:
                return s
            self.guard(s)
            if s.is_popup:
                btn = scr.popup_button(s)
                if btn is None or dismissed >= 3:
                    self.stop("unknown_popup", f"A popup the driver doesn't know ({s.name}) — "
                                               f"expected {'/'.join(expect)}.")
                self.log(f"    (dismissing {s.name}: tap '{btn.text or btn.desc}')")
                self.tap(btn.bounds, f"dismiss {s.name}")
                dismissed += 1
                self.sleep(1.0)
                continue
            if self.clock() > deadline:
                if on_timeout == "none":
                    return None
                self.stop("unexpected_screen", f"Expected {'/'.join(expect)}, the phone shows '{s.name}'.")
            self.sleep(0.5)

    def find(self, target, finders, screen=None):
        """First node any finder returns. `finders` = [(label, fn(screen) -> node|None)],
        most trusted first; a hit past the first is logged as drift."""
        screen = screen or self.screen
        for i, (label, fn) in enumerate(finders):
            node = fn(screen)
            if node is not None:
                if i > 0:
                    self.drift("fallback", target, f"matched by '{label}', not '{finders[0][0]}'", capture=False)
                return node
        return None

    # ------------------------------------------------------------------ #
    # acting                                                              #
    # ------------------------------------------------------------------ #
    def tap(self, bounds, why=""):
        x, y = tap_point(bounds, self.rng)
        self.dev.tap(x, y)
        return x, y

    def act(self, target, finders, expect, timeout=12.0):
        """Find `target` on the current screen, tap it, and wait for one of
        `expect`. A tap that changed nothing is retried once."""
        node = self.find(target, finders)
        if node is None:
            self.stop("not_found", f"Couldn't find '{target}' on '{self.screen.name}'.")
        before = signature(self.screen.nodes)
        self.log(f"  → {target}")
        self.tap(node.bounds, target)
        self.sleep(0.8)
        s = self.resolve(expect, timeout=timeout, on_timeout="none")
        if s is not None:
            return s
        if signature(self.screen.nodes) == before:
            self.drift("retry", target, "tap changed nothing — tapping again")
            node = self.find(target, finders) or node
            self.tap(node.bounds, target)
            self.sleep(0.8)
            return self.resolve(expect, timeout=timeout)
        self.stop("unexpected_screen", f"After tapping '{target}' the phone shows "
                                       f"'{self.screen.name}', expected {expect}.")

    def scroll_find(self, target, finder, max_swipes=6, area=None):
        """Swipe up through a list until `finder(screen)` returns a node.
        `area` = (left, top, right, bottom) of the list; default the middle
        of the screen. Stops at the end of the list (no movement)."""
        s = self.screen
        node = finder(s)
        if node is not None:
            return node
        for _ in range(max_swipes):
            lx, ly, rx, ry = area or (0, 400, 1080, 1900)
            x = self.rng.randint(lx + (rx - lx) // 3, rx - (rx - lx) // 3)
            y1 = ly + int((ry - ly) * self.rng.uniform(0.72, 0.8))
            y2 = ly + int((ry - ly) * self.rng.uniform(0.25, 0.32))
            before = _small_gray(self.dev.screenshot())
            self.dev.swipe(x, y1, x + self.rng.randint(-15, 15), y2, self.rng.uniform(0.3, 0.45))
            self.sleep(0.9)
            s = self.look()
            self.guard(s)
            node = finder(s)
            if node is not None:
                return node
            if _diff(before, _small_gray(self.dev.screenshot())) < self.STILL:
                break  # nothing moved: the end of the list
        self.drift("not_found_after_scroll", target, capture=True)
        return None
