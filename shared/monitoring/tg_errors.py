"""Telegram errors for every bot's error handler: a blip is a WARNING, not a mail.

errmail mails every ERROR. In 14 days (to 2026-11-23) ~125 of ~280 alert mails
were Telegram's own network blips - "Bad Gateway", connect / read errors from
getUpdates - which python-telegram-bot retries a second later by itself. They
arrived as "No error handlers are registered" (newsroom bot, radar: no handler)
or "unhandled error while processing None" (news bot, render bot: the handler
logged everything at ERROR), so nothing in the mail said they were harmless.

`transient(err)` says whether an error is such a blip. `handled(err, logger)`
logs it at WARNING and returns True — but when blips keep coming (BURST in
WINDOW_S, i.e. Telegram is really unreachable, not hiccuping) it logs ONE
ERROR, at most every QUIET_S, so a real outage still reaches the inbox.
`on_error` is a complete PTB error handler for bots that need nothing else.

BadRequest is a NetworkError subclass in PTB but means OUR request was wrong
(a deleted message, a bad parameter) — never a blip, always an ERROR.
"""
import collections
import logging
import time

from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

WINDOW_S = 15 * 60
BURST = 30          # PTB retries a dead connection every few seconds
QUIET_S = 3600


def transient(err) -> bool:
    if isinstance(err, BadRequest):
        return False
    return isinstance(err, (NetworkError, TimedOut, RetryAfter))


class _Burst:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.times = collections.deque()
        self.last_alarm = None

    def note(self) -> int:
        """Record one blip; the count in the window when an alarm is due, else 0."""
        now = self.clock()
        self.times.append(now)
        while self.times and now - self.times[0] > WINDOW_S:
            self.times.popleft()
        if len(self.times) >= BURST and (self.last_alarm is None
                                         or now - self.last_alarm >= QUIET_S):
            self.last_alarm = now
            return len(self.times)
        return 0


_burst = _Burst()


def handled(err, logger: logging.Logger) -> bool:
    """True when `err` was a Telegram blip and has been logged (WARNING, or one
    ERROR when they keep coming); False = the caller logs it as a real error."""
    if not transient(err):
        return False
    logger.warning("Telegram network blip, retried automatically: %s", err)
    n = _burst.note()
    if n:
        logger.error("Telegram unreachable: %d network errors in the last %d min "
                     "(last: %s) — the bot keeps retrying", n, WINDOW_S // 60, err)
    return True


def handler(logger: logging.Logger):
    """A PTB error handler: blips via `handled`, anything else at ERROR with
    its traceback (the cause, not "No error handlers are registered")."""
    async def on_error(update, context) -> None:
        err = context.error
        if handled(err, logger):
            return
        logger.error("unhandled error while processing %s", update, exc_info=err)
    return on_error
