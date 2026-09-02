"""
modules/newsroom/smm.py — BulkFollows (SMM panel) client.

A copy of modules/telegram/smm.py, differing only in which credentials it
reads: this bot bills the CLIENT's panel account (NR_BULKFOLLOWS_*), and a
shared import would have made a change on master able to move a client's
money. The behaviour is deliberately identical.

One job: place an `add` order for a link. The panel speaks the common SMM-panel
API v2 dialect — a POST with an **x-www-form-urlencoded** body:

    key=<api key>&action=add&service=<service id>&link=<url>&quantity=<n>

and answers JSON: {"order": 123456} on success, {"error": "..."} on refusal.

Blocking (uses `requests`) — async callers wrap it in `asyncio.to_thread`.
Never raises: every failure is logged and reported as a falsy return, because a
panel hiccup must not take down a post that already went out. The cost of that
choice is that a lost order is invisible in the process — which is why every
call here is bracketed by a row in store.orders.
"""

import logging

import requests

from shared import config

log = logging.getLogger(__name__)

# The panel is a side effect of posting, not part of it — fail fast rather than
# hold the bot's thread pool on a hung connection.
TIMEOUT = 20


def _mask(key: str) -> str:
    """Api key as it appears in logs — enough to tell which key, not enough to
    use. Logs get pasted into chats."""
    return f"...{key[-4:]}" if len(key) > 4 else "..."


def place_order(link: str, quantity: int, service: str) -> dict | None:
    """Order `quantity` of `service` for `link`. Returns the panel's parsed JSON
    response on success, None if the order was skipped or failed."""
    if not config.NR_BULKFOLLOWS_API_KEY:
        log.warning("BulkFollows: no NR_BULKFOLLOWS_API_KEY set — order skipped (%s)", link)
        return None
    if not service:
        log.warning("BulkFollows: no service id configured — order skipped (%s)", link)
        return None
    if not link:
        log.warning("BulkFollows: no link — order skipped")
        return None

    body = {
        "key": config.NR_BULKFOLLOWS_API_KEY,
        "action": "add",
        "service": service,
        "link": link,
        "quantity": quantity,
    }
    # Echo the exact request, api key masked — this is the line to compare
    # against Postman when the panel answers something unexpected.
    log.info("BulkFollows -> POST %s  %s", config.NR_BULKFOLLOWS_API_URL,
             {**body, "key": _mask(body["key"])})

    try:
        # `data=` (not `json=`) is what makes this x-www-form-urlencoded.
        resp = requests.post(config.NR_BULKFOLLOWS_API_URL, data=body, timeout=TIMEOUT)
    except Exception as exc:  # network, DNS, timeout
        log.error("BulkFollows FAIL request failed (service=%s link=%s): %s", service, link, exc)
        return None

    # Log the raw body before any parsing — a panel error page or an HTML block
    # is exactly what you need to see, and .json() would swallow it.
    log.info("BulkFollows <- HTTP %s  %s", resp.status_code, resp.text.strip()[:500])

    try:
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # 4xx/5xx, non-JSON body
        log.error("BulkFollows FAIL bad response (service=%s link=%s): %s", service, link, exc)
        return None

    # HTTP 200 with an {"error": ...} body is the panel's normal way of saying
    # no (bad service id, too small a quantity, empty balance).
    if isinstance(payload, dict) and payload.get("error"):
        log.error("BulkFollows FAIL rejected (service=%s link=%s): %s",
                  service, link, payload["error"])
        return None

    log.info("BulkFollows OK order %s placed: service=%s qty=%s link=%s",
             (payload or {}).get("order") if isinstance(payload, dict) else payload,
             service, quantity, link)
    return payload
