"""
modules/telegram/dispatcher.py — the smart-filter dispatcher.

Drains the collector's queue: each status='new' item is scored (scorer.py, one
OpenRouter call), then routed:

  score >= YT_AUTO_MIN_SCORE (default 70) AND the item has a video
      → auto-upload to EVERY YT_DESTINATIONS channel via
        modules/youtube/publisher (title+caption translated per channel),
        then mark_posted('youtube')
  everything else
      → status='queued' with score+regions recorded, waiting for future
        platform integrations (telegram drip, twitter, ...)

A scoring failure leaves the item status='new' so it is retried on a later
pass — items are never published unscored. An upload failure on one channel
doesn't abort the others (publisher semantics); if EVERY channel fails the
item goes back to 'queued' so it isn't lost.

Run alongside the collector (separate terminal):
    py modules/telegram/dispatcher.py
"""

import logging
import os
import sys
import time

# Repo-root bootstrap for direct runs (`py modules/telegram/dispatcher.py`).
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shared import config  # noqa: E402
from modules.telegram import queue_store, scorer  # noqa: E402
from modules.youtube import publisher as yt_publisher  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dispatcher")

POLL_S = 15        # queue empty → sleep this long
SCORE_RETRY_S = 60  # scoring failed (API hiccup) → back off before retrying


def _first_video(item: dict) -> str | None:
    """Local path of the item's first video, or None."""
    for m in item["media"]:
        if m.get("type") == "video" and m.get("path") and os.path.exists(m["path"]):
            return m["path"]
    return None


def process_one() -> bool:
    """Score + route the oldest 'new' item. Returns False when the queue has
    no 'new' items (caller sleeps), True when an item was handled."""
    item = queue_store.next_by_status("new")
    if item is None:
        return False

    result = scorer.score(item["text"], source_hint=item["source"])
    if result is None:
        log.warning("item %d: scoring failed — will retry in %ds", item["id"], SCORE_RETRY_S)
        time.sleep(SCORE_RETRY_S)
        return True

    s, regions = result["score"], result["regions"]
    video = _first_video(item)
    log.info(
        "item %d from %s: score=%d tier=%s regions=%s video=%s",
        item["id"], item["source"], s, scorer.tier(s), ",".join(regions),
        "yes" if video else "no",
    )

    auto_yt = s >= config.YT_AUTO_MIN_SCORE and video and config.YT_DESTINATIONS
    queue_store.set_score(item["id"], s, regions, "queued")
    if not auto_yt:
        return True

    posted, errors = yt_publisher.publish_shorts(video, item["text"], config.YT_DESTINATIONS)
    if posted:
        queue_store.mark_posted(item["id"], "youtube")
        log.info("item %d: uploaded to YouTube: %s", item["id"], ", ".join(posted))
    for account, err in errors:
        log.error("item %d: YouTube %s failed: %s", item["id"], account, err)
    return True


def main() -> None:
    if not config.OPENROUTER_API_KEY:
        raise SystemExit("OPENROUTER_API_KEY missing in .env — the scorer can't run")
    if not config.YT_DESTINATIONS:
        log.warning("YT_DESTINATIONS empty — items will be scored + queued but nothing auto-uploads")
    queue_store.init()
    log.info(
        "dispatcher running: auto-upload at score >= %d to %d YouTube channel(s). Ctrl+C to stop.",
        config.YT_AUTO_MIN_SCORE, len(config.YT_DESTINATIONS),
    )
    while True:
        try:
            if not process_one():
                time.sleep(POLL_S)
        except KeyboardInterrupt:
            raise
        except Exception:
            log.exception("dispatcher pass crashed — continuing after %ds", POLL_S)
            time.sleep(POLL_S)


if __name__ == "__main__":
    main()
