"""Run the vision driver's carousel-with-music flow on the phone.

DRY RUN by default — goes to the final composer and stops before Share:

    py modules/instagram/vision/run.py --account vestra.24 --track "Lazy River" \
        --files a.jpg b.jpg c.jpg --caption "text" --hashtags news world
    py modules/instagram/vision/run.py --account vestra.24 --carousel     # next queued carousel in posts/

Dry-run EVERY account on the phone (switch → slides → music-library probe →
track → caption → verify → discard → delete the slides), one report at the end,
the phone left on the account it started on:

    py modules/instagram/vision/run.py --sweep [--accounts a b] [--track "Lazy River"]

The run switches IG to --account through the account switcher when another
account is active (and re-reads the Profile title before going on);
--no-switch stops instead. Just switch:

    py modules/instagram/vision/run.py --account frontiva24 --switch-only

Back out of a half-made post (Discard), e.g. after a dry run:

    py modules/instagram/vision/run.py --discard

Sharing needs BOTH flags AND typing the account name at the prompt, so it
only ever happens from a terminal a human is sitting at:

    py modules/instagram/vision/run.py ... --share --i-confirm-share

A share is never retried; an unconfirmed one says "check the profile first".
Every screen the run passes is recorded to data/ig_screens/ (git-ignored),
and every fallback/stop is appended to data/ig_screens/drift.jsonl.
"""

import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from modules.instagram.vision import flows  # noqa: E402
from modules.instagram.vision.driver import Driver, Stop  # noqa: E402
from modules.instagram.vision.recorder import DEFAULT_DIR, Recorder  # noqa: E402

# Plain grey "TEST n/3" slides used by --sweep when no --files are given
# (git-ignored; any 2-20 images there will do).
TEST_SLIDES_DIR = os.path.join(_ROOT, "data", "ig_test")


def build_parser():
    ap = argparse.ArgumentParser(description="Vision driver: carousel with IG music (dry run by default).")
    ap.add_argument("--account", help="IG username the post is for (no @) — the run stops on any other")
    ap.add_argument("--files", nargs="+", help="slides in order (2-20 photos/videos)")
    ap.add_argument("--carousel", action="store_true", help="use the next queued carousel in posts/")
    ap.add_argument("--caption", help="caption body (default: the queued carousel's JSON)")
    ap.add_argument("--hashtags", nargs="*", default=None, help="hashtags (no #)")
    ap.add_argument("--track", help="music track title to search and add")
    ap.add_argument("--artist", help="artist, to pick the right one of several same-titled tracks")
    ap.add_argument("--device", help="ADB serial / IP:port (default: PHONE_ADDRESS from .env)")
    ap.add_argument("--discard", action="store_true", help="back out of a half-made post and stop")
    ap.add_argument("--switch-only", action="store_true",
                    help="just switch IG to --account (verified on Profile) and stop")
    ap.add_argument("--no-switch", action="store_true",
                    help="stop on a wrong account instead of switching to --account")
    ap.add_argument("--sweep", action="store_true",
                    help="dry run on EVERY account in the switcher (or --accounts), never shares")
    ap.add_argument("--accounts", nargs="+", help="with --sweep: only these accounts, in this order")
    ap.add_argument("--probe", default="Ed Sheeran",
                    help="with --sweep: music query that tells a Creator library from a Business one")
    ap.add_argument("--no-record", action="store_true", help="don't save screens (drift log still kept)")
    ap.add_argument("--share", action="store_true", help="tap Share at the end (needs --i-confirm-share)")
    ap.add_argument("--i-confirm-share", action="store_true", help="second switch for --share")
    return ap


def resolve_post(args, ap):
    """(paths, caption_text, meta_path) from --files/--caption or the queue."""
    from modules.instagram import upload_post as ig
    from shared import config

    meta_path = None
    if args.carousel:
        paths, body, tags, meta_path = ig.find_next_carousel(config.POSTS_DIR)
        if not paths:
            ap.error(f"No carousel queued in {config.POSTS_DIR}.")
    elif args.files:
        paths, body, tags = args.files, "", []
    else:
        ap.error("Give --files … or --carousel (or --discard).")
    if args.caption is not None:
        body = args.caption
    if args.hashtags is not None:
        tags = args.hashtags
    return paths, flows.full_caption(body, tags), meta_path


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.share and not args.i_confirm_share:
        ap.error("--share needs --i-confirm-share as well.")
    if args.sweep and args.share:
        ap.error("--sweep never shares.")
    if not (args.discard or args.sweep) and not args.account:
        ap.error("--account is required.")
    if args.sweep:
        if not args.files and os.path.isdir(TEST_SLIDES_DIR):
            args.files = sorted(os.path.join(TEST_SLIDES_DIR, f) for f in os.listdir(TEST_SLIDES_DIR)
                                if f.lower().endswith((".jpg", ".png")))
        args.track = args.track or "Lazy River"
        if args.caption is None:
            args.caption = "Dry run — not posted 🧪"

    paths = caption = meta_path = None
    if not (args.discard or args.switch_only):
        paths, caption, meta_path = resolve_post(args, ap)
        flows.validate_slides(paths)

    serial = args.device
    if not serial:
        from shared import config
        serial = config.DEVICE_ID
    from modules.instagram.vision.device import Device

    print(f"Connecting to {serial}...")
    dev = Device(serial)
    rec = None if args.no_record else Recorder(dev, account=args.account)
    drv = Driver(dev, recorder=rec, drift_path=os.path.join(DEFAULT_DIR, "drift.jsonl"))

    try:
        if args.discard:
            s = flows.abandon_creation(drv)
            print(f"Back on {s.name}.")
            return 0
        if args.sweep:
            from modules.instagram.vision import sweep

            handles, start_account = sweep.list_accounts(drv)
            accounts = args.accounts or handles
            print(f"Sweep: {len(accounts)} accounts — {', '.join(accounts)} (dry run, nothing shared)")
            results = sweep.sweep(drv, paths, caption, args.track, artist=args.artist,
                                  accounts=accounts, probe=args.probe)
            report = sweep.save_report(results, DEFAULT_DIR)
            print("\n" + sweep.summary(results))
            print(f"\nReport: {report}")
            if results and results[-1]["status"] not in sweep.ABORT and start_account:
                try:  # leave the phone on the account it started on
                    flows.open_fresh(drv)
                    flows.ensure_account(drv, start_account)
                    print(f"Back on @{start_account}.")
                except Stop as stop:
                    print(f"Couldn't switch back to @{start_account}: {stop}")
            return 0 if all(r["status"] == "ok" for r in results) else 1
        if args.switch_only:
            flows.open_fresh(drv)
            info = flows.ensure_account(drv, args.account, allow_switch=not args.no_switch)
            print(f"Instagram is on @{info['account']} ({info['posts']} posts).")
            return 0
        print(f"{len(paths)} slides for @{args.account}"
              f"{' · music: ' + args.track if args.track else ''}"
              f" · {'SHARE' if args.share else 'dry run'}")
        if args.share:
            if not sys.stdin.isatty():
                print("Refusing to share without a terminal to confirm in.")
                return 2
            typed = input(f"Type the account name to really publish to @{args.account}: ").strip().lstrip("@")
            if typed.lower() != args.account.lower().lstrip("@"):
                print("Not confirmed — nothing published.")
                return 2
        result = flows.carousel_with_music(
            drv, paths, caption, args.account, track=args.track, artist=args.artist,
            share=args.share, confirm_share=args.i_confirm_share, allow_switch=not args.no_switch,
        )
    except Stop as stop:
        print(f"\n■ STOPPED {stop}")
        return 1

    print(f"\nDone: {result['slides']} slides on @{result['account']}"
          f"{', shared' if result['shared'] else ', dry run (not shared)'}")
    if result["drift"]:
        print("Drift this run:")
        for kind, target, detail in result["drift"]:
            print(f"  - {kind}: {target} {detail}")
    if result["shared"] and meta_path is not None:
        from modules.instagram import upload_post as ig
        from shared import config
        ig.archive_carousel(config.POSTS_DIR, paths, meta_path)
        print("Archived to posts/posted/.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
