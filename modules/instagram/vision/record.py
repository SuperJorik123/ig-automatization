"""Capture Instagram screens from the phone for the training set.

One capture of whatever is on screen now:

    py modules/instagram/vision/record.py --label composer
    py modules/instagram/vision/record.py --label music_picker --account wswire --note "creator acct"

Interactive — browse IG on the phone, press Enter at each screen (type a label
first to change it, `q` to quit):

    py modules/instagram/vision/record.py --interactive --account wswire

Every N seconds until Ctrl+C (label stays fixed):

    py modules/instagram/vision/record.py --label browse --every 3

Files go to data/ig_screens/<date>/ (git-ignored — they show account data).
Read-only: this never taps or types on the phone.
"""

import argparse
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from modules.instagram.vision.recorder import Recorder  # noqa: E402


def _report(meta):
    acct = meta["account"] or (f"guess {meta['account_guess']}" if meta["account_guess"] else "?")
    print(
        f"  saved {meta['files']['png']}  [{meta['step']}]  "
        f"{meta['resolution'][0]}x{meta['resolution'][1]}  "
        f"{meta['package'] or '?'}  account: {acct}"
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description="Record IG screens (screenshot + UI dump + metadata).")
    ap.add_argument("--label", help="screen name, e.g. composer, gallery, music_picker")
    ap.add_argument("--device", help="ADB serial / IP:port (default: PHONE_ADDRESS from .env)")
    ap.add_argument("--account", help="IG username on screen, if known (no @)")
    ap.add_argument("--note", default="", help="free text stored in the metadata")
    ap.add_argument("--out", help="output folder (default data/ig_screens)")
    ap.add_argument("--interactive", action="store_true", help="Enter = capture, type a label to change it")
    ap.add_argument("--every", type=float, help="capture every N seconds until Ctrl+C")
    args = ap.parse_args(argv)
    if not args.label and not args.interactive:
        ap.error("--label is required (or use --interactive)")

    import uiautomator2 as u2

    serial = args.device
    if not serial:
        from shared import config
        serial = config.DEVICE_ID
    print(f"Connecting to {serial}...")
    d = u2.connect(serial)
    rec = Recorder(d, out_dir=args.out, account=args.account)
    print(f"Connected: {rec.model or '?'}  IG {rec.ig_version or '?'}  → {rec.out_dir}")

    if args.interactive:
        label = args.label or "screen"
        print("Enter = capture as current label; type a new label + Enter to switch and capture; q = quit.")
        while True:
            try:
                line = input(f"[{label}] > ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if line.lower() in ("q", "quit", "exit"):
                break
            if line:
                label = line
            try:
                _report(rec.save(label, note=args.note))
            except Exception as exc:  # phone asleep, ADB hiccup — keep the session
                print(f"  ✗ capture failed: {exc}")
    elif args.every:
        print(f"Capturing every {args.every:g}s — Ctrl+C to stop.")
        try:
            while True:
                try:
                    _report(rec.save(args.label, note=args.note))
                except Exception as exc:
                    print(f"  ✗ capture failed: {exc}")
                time.sleep(args.every)
        except KeyboardInterrupt:
            pass
    else:
        _report(rec.save(args.label, note=args.note))
    print(f"{rec.count} capture(s).")


if __name__ == "__main__":
    main()
