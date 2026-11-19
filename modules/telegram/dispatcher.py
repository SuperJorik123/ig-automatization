"""
modules/telegram/dispatcher.py — the smart-filter dispatcher.

Drains the collector's queue. A text-only post is not news: with
NEWS_REQUIRE_MEDIA on (the default) it goes straight to status='rejected'
without a scoring call. Everything else is scored ONCE by
modules/telegram/smart_filter — this is the only process that writes scores,
so there is no race over the queue — and set to status='queued' with its
score + regions recorded.

It filters; it never publishes. Nothing leaves this process for any platform.

A scoring failure leaves the item status='new' so it is retried on a later
pass.

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dispatcher")

POLL_S = 15        # queue empty → sleep this long
SCORE_RETRY_S = 60  # scoring failed (API hiccup) → back off before retrying


def process_one() -> bool:
    """Score the oldest 'new' item. Returns False when the queue has
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
    log.info("item %d from %s: score=%d tier=%s regions=%s",
             item["id"], item["source"], s, result["tier"], ",".join(regions))
    queue_store.set_score(item["id"], s, regions, "queued")
    return True


def main() -> None:
    if not config.OPENROUTER_API_KEY:
        raise SystemExit("OPENROUTER_API_KEY missing in .env — the scorer can't run")
    errmail.install("dispatcher")  # every logged ERROR -> one email to the operator
    queue_store.init()
    log.info("dispatcher running: scoring the queue, publishing nothing. Ctrl+C to stop.")
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
