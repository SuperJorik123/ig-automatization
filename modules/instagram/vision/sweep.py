"""Dry-run sweep: the carousel-with-music flow on EVERY account on the phone.

For each account: switch to it (verified on Profile), push the test slides,
pick them, probe the music library (Creator = full library, Business =
royalty-free only), add the track, write the caption, verify the composer,
STOP before Share — then discard the draft and delete the slides from the
phone. Nothing is ever shared; this module has no share path at all.

Per-account outcome (ok / the stop's kind), its drift, the library and the
post count go into data/ig_screens/sweep_<stamp>.json. One account stopping
doesn't end the sweep — except a security check, the lock screen or another
app in front, which end it at once without touching anything.
"""

import json
import os
from datetime import datetime

from modules.instagram.vision import flows
from modules.instagram.vision import screens as scr
from modules.instagram.vision.driver import Stop

ABORT = {"security", "lockscreen", "other_app"}


def list_accounts(drv):
    """(handles in switcher order, active handle) — read off the account
    switcher, scrolled to the end, then closed again."""
    flows.open_fresh(drv)
    info = flows.read_profile(drv)
    drv.step = "list_accounts"
    drv.act("account switcher", flows.F_SWITCHER, "account_switcher")
    seen = []
    top = min((n.bounds[1] for n in drv.screen.nodes if n.desc == "Dismiss"), default=600)
    for _ in range(6):
        for r in scr.account_rows(drv.screen):
            if r["handle"] not in seen:
                seen.append(r["handle"])
        before = len(seen)
        drv.dev.swipe(540, 1900, 545, max(top + 200, 1100), 0.4)
        drv.sleep(1.0)
        drv.look()
        for r in scr.account_rows(drv.screen):
            if r["handle"] not in seen:
                seen.append(r["handle"])
        if len(seen) == before:
            break
    drv.dev.back()
    drv.sleep(1.0)
    drv.resolve(("profile", "home"))
    return seen, info["account"]


def sweep(drv, paths, caption, track, artist=None, accounts=None, probe=None):
    """Run the dry run on each account. Returns a list of per-account dicts."""
    results = []
    for acct in accounts:
        drv.events, drv.notes = [], {}
        entry = {"account": acct, "started": datetime.now().isoformat(timespec="seconds")}
        drv.log(f"\n=== @{acct} ===")
        aborted = False
        try:
            res = flows.carousel_with_music(drv, paths, caption, acct, track=track, artist=artist,
                                            probe=probe)
            entry.update(status="ok", posts=res["posts_before"])
        except Stop as stop:
            entry.update(status=stop.kind, message=stop.message, capture=stop.capture)
            aborted = stop.kind in ABORT
        entry["library"] = drv.notes.get("library")
        entry["library_probe"] = drv.notes.get("library_probe")
        if not aborted:
            try:
                flows.abandon_creation(drv)
            except Stop as stop:
                entry["cleanup"] = f"{stop.kind}: {stop.message}"
                aborted = stop.kind in ABORT
        try:
            flows.remove_slides(drv.dev, drv.notes.get("pushed"))
        except Exception as exc:  # shell only; never fatal
            entry["cleanup_files"] = str(exc)
        entry["drift"] = [list(e) for e in drv.events if e[0] != "stop"]
        results.append(entry)
        if aborted:
            drv.log(f"■ sweep aborted on @{acct}: {entry['status']}")
            break
    return results


def save_report(results, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"sweep_{datetime.now():%Y%m%d_%H%M}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    return path


def summary(results):
    """Plain-text table for the terminal."""
    lines = [f"{'account':18} {'result':24} {'music library':18} drift"]
    for r in results:
        drift = "; ".join(f"{k} {t}" for k, t, *_ in r.get("drift", [])) or "-"
        lines.append(f"{r['account']:18} {r['status']:24} {str(r.get('library') or '?'):18} {drift}")
        if r["status"] != "ok":
            lines.append(f"{'':18} └ {r.get('message', '')}")
    return "\n".join(lines)
