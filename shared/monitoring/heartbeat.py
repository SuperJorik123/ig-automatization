"""
shared/monitoring/heartbeat.py — dead-man's-switch pings (healthchecks.io).

A monitored process proves it is ALIVE by GET-ing its check URL every
INTERVAL_S; healthchecks.io emails the operator when the pings go quiet.
Nothing here ever alerts — absence does — which is what makes it the one
"server down" monitor that also catches a hung process, a crashed service on
a healthy host, and a dead network.

Three call shapes for the three kinds of host process:
    ping(url)        one blocking GET, swallows everything
    maybe_ping(url)  for poll loops (dispatcher): call it every iteration,
                     it actually pings at most once per INTERVAL_S
    run(url)         asyncio task for event-loop hosts (collector)

news_bot pings from a JobQueue job. In every case the ping is issued from
inside the process's own loop on purpose: a wedged loop must stop the pings.
"""

import asyncio
import logging
import time

import requests

log = logging.getLogger(__name__)

INTERVAL_S = 300
TIMEOUT = 10

_last: dict = {}  # url -> monotonic time of the last maybe_ping send


def ping(url: str) -> None:
    if not url:
        return
    try:
        requests.get(url, timeout=TIMEOUT)
    except Exception as exc:
        # debug, not warning: a healthchecks outage would otherwise spam the
        # log every 5 minutes, and the whole point of the service is that
        # MISSING pings are what raise the alarm.
        log.debug("heartbeat ping failed: %s", exc)


def maybe_ping(url: str) -> None:
    """Throttled ping for tight poll loops. First call always pings."""
    if not url:
        return
    now = time.monotonic()
    if now - _last.get(url, float("-inf")) >= INTERVAL_S:
        _last[url] = now
        ping(url)


async def run(url: str) -> None:
    """Ping forever from an asyncio loop. Cancels cleanly with the loop."""
    if not url:
        return
    while True:
        await asyncio.to_thread(ping, url)
        await asyncio.sleep(INTERVAL_S)
