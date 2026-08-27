"""
modules/telegram/dispatcher.py — the smart-filter dispatcher.

Drains the collector's queue. A text-only post is not news: with
NEWS_REQUIRE_MEDIA on (the default) it goes straight to status='rejected'
without a scoring call. Everything else is scored ONCE — this is the only
process that writes scores, so there is no race over the queue — and then
routed by modules/telegram/smart_filter:

  smart_filter.youtube_targets(item)  (score >= YT_AUTO_MIN_SCORE + a video)
      → upload to those YT_DESTINATIONS channels via modules/youtube/publisher
        (title+caption translated per channel), one record_post per channel
  everything else
      → status='queued' with score+regions recorded

Scored items stay available to the other platforms: the Telegram autopilot
(inside news_bot.py) picks its own candidates from this same queue, and
eligibility is per platform — an item already on YouTube is still a Telegram
candidate. Twitter/Instagram routes will plug in the same way.

A scoring failure leaves the item status='new' so it is retried on a later
pass — items are never published unscored. An upload failure on one channel
doesn't abort the others (publisher semantics); a channel with no record_post
row is simply still unpublished there.

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
from shared.monitoring import errmail, heartbeat  # noqa: E402
from modules.telegram import queue_store, smart_filter  # noqa: E402
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

    # Cheapest filter first: a text-only post isn't news, so it's dropped
    # before it can cost a scoring call.
    if not smart_filter.is_news(item):
        queue_store.set_status(item["id"], "rejected")
        log.info("item %d from %s: no media — rejected unscored", item["id"], item["source"])
        return True

    result = smart_filter.evaluate(item["text"], source_hint=item["source"])
    if result is None:
        log.warning("item %d: scoring failed — will retry in %ds", item["id"], SCORE_RETRY_S)
        time.sleep(SCORE_RETRY_S)
        return True

    s, regions = result["score"], result["regions"]
    video = _first_video(item)
    log.info(
        "item %d from %s: score=%d tier=%s regions=%s video=%s",
        item["id"], item["source"], s, result["tier"], ",".join(regions),
        "yes" if video else "no",
    )

    # Record the verdict before routing: the Telegram autopilot picks its own
    # candidates out of the queue and only ever sees scored items.
    queue_store.set_score(item["id"], s, regions, "queued")

    scored = {**item, "score": s, "regions": regions}
    targets = smart_filter.youtube_targets(scored) if video else []
    if not targets:
        return True

    posted, errors = yt_publisher.publish_shorts(video, item["text"], targets)
    for account in posted:
        queue_store.record_post(item["id"], "youtube", account)
    if posted:
        log.info("item %d: uploaded to YouTube: %s", item["id"], ", ".join(posted))
    for account, err in errors:
        log.error("item %d: YouTube %s failed: %s", item["id"], account, err)
    return True


def main() -> None:
    if not config.OPENROUTER_API_KEY:
        raise SystemExit("OPENROUTER_API_KEY missing in .env — the scorer can't run")
    if not config.YT_DESTINATIONS:
        log.warning("YT_DESTINATIONS empty — items will be scored + queued but nothing auto-uploads")
    errmail.install("dispatcher")  # every logged ERROR -> one email to the operator
    queue_store.init()
    log.info(
        "dispatcher running: auto-upload at score >= %d to %d YouTube channel(s). Ctrl+C to stop.",
        config.YT_AUTO_MIN_SCORE, len(config.YT_DESTINATIONS),
    )
    while True:
        heartbeat.maybe_ping(config.HEALTHCHECK_URL_DISPATCHER)
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
