"""Screen recorder — the training data starts here.

`Recorder(d).save(step)` writes three files side by side for the device's
CURRENT screen:

    data/ig_screens/<YYYY-MM-DD>/<HHMMSS_mmm>_<serial>_<step>.png   screenshot
                                                       ....xml   uiautomator dump
                                                       ....json  metadata

The metadata (schema 1): timestamp, step, device serial, screenshot
resolution, IG version, dark mode, foreground package/activity, the account
(given by the caller, plus a best-effort guess read off the dump) and a free
note. Everything about the device that doesn't change between captures (IG
version, model) is read once per Recorder.

The folder is under the repo-root `data/`, which .gitignore already
excludes: screenshots and dumps show usernames, captions and DMs — they must
never be committed.

`d` is a uiautomator2 Device or anything with the same few methods
(`screenshot()`, `dump_hierarchy()`, `shell()`, `app_current()`, `serial`) —
the tests pass a fake. Capture failures of the optional fields never fail the
capture; a failed screenshot or dump does (there would be nothing to save).
"""

import json
import os
import re
import time
from datetime import datetime

from modules.instagram.vision import uixml

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DEFAULT_DIR = os.path.join(_ROOT, "data", "ig_screens")
SCHEMA = 1
PKG = uixml.PKG

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(value, fallback="x"):
    """A string usable inside a file name on Windows (`192.168.1.5:5555` →
    `192.168.1.5_5555`)."""
    value = _SAFE_RE.sub("_", str(value or "")).strip("._")
    return value[:60] or fallback


def _shell(d, cmd):
    """Shell output as a string, '' on any failure (uiautomator2 3.x returns
    a ShellResponse, older ones a tuple)."""
    try:
        res = d.shell(cmd)
    except Exception:
        return ""
    if isinstance(res, str):
        return res
    out = getattr(res, "output", None)
    if out is None and isinstance(res, (tuple, list)) and res:
        out = res[0]
    return out or ""


def parse_ig_version(dumpsys):
    """(versionName, versionCode) out of `dumpsys package com.instagram.android`."""
    name = re.search(r"versionName=(\S+)", dumpsys or "")
    code = re.search(r"versionCode=(\d+)", dumpsys or "")
    return (name.group(1) if name else None, int(code.group(1)) if code else None)


def parse_night_mode(output):
    """`cmd uimode night` → True / False / None (auto or unreadable)."""
    m = re.search(r"Night mode:\s*(\w+)", output or "")
    if not m:
        return None
    return {"yes": True, "no": False}.get(m.group(1).lower())


class Recorder:
    def __init__(self, d, out_dir=None, account=None):
        self.d = d
        self.out_dir = out_dir or DEFAULT_DIR
        self.account = account
        self.serial = str(getattr(d, "serial", None) or "unknown")
        self.ig_version, self.ig_version_code = parse_ig_version(
            _shell(d, f"dumpsys package {PKG} | grep version")
        )
        self.model = _shell(d, "getprop ro.product.model").strip() or None
        self.count = 0

    def save(self, step, note="", account=None, xml=None, image=None):
        """Capture the current screen. Returns the metadata dict (with
        `files`: absolute paths of png/xml/json). `xml` / `image` let a caller
        that has just read the screen pass it in instead of a second read."""
        now = time.time()
        stamp = datetime.fromtimestamp(now)
        if image is None:
            image = self.d.screenshot()
        if xml is None:
            xml = self.d.dump_hierarchy()

        day_dir = os.path.join(self.out_dir, stamp.strftime("%Y-%m-%d"))
        os.makedirs(day_dir, exist_ok=True)
        base = "{}_{:03d}_{}_{}".format(
            stamp.strftime("%H%M%S"), int(now * 1000) % 1000,
            safe_name(self.serial, "device"), safe_name(step, "screen"),
        )
        png, xml_path, meta_path = (os.path.join(day_dir, base + ext) for ext in (".png", ".xml", ".json"))
        image.save(png)
        with open(xml_path, "w", encoding="utf-8") as f:
            f.write(xml)

        try:
            current = self.d.app_current() or {}
        except Exception:
            current = {}
        meta = {
            "schema": SCHEMA,
            "timestamp": stamp.isoformat(timespec="milliseconds"),
            "epoch": round(now, 3),
            "step": step,
            "note": note or "",
            "serial": self.serial,
            "model": self.model,
            "resolution": list(image.size),  # [width, height] of the PNG
            "ig_version": self.ig_version,
            "ig_version_code": self.ig_version_code,
            "dark_mode": parse_night_mode(_shell(self.d, "cmd uimode night")),
            "package": current.get("package"),
            "activity": current.get("activity"),
            "account": account or self.account,
            "account_guess": uixml.guess_account(xml),
            "files": {"png": os.path.basename(png), "xml": os.path.basename(xml_path)},
        }
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        self.count += 1
        meta["paths"] = {"png": png, "xml": xml_path, "json": meta_path}
        return meta
