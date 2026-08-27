"""
shared/monitoring/errmail.py — every ERROR the process logs becomes one email.

install("news_bot") attaches a handler to the ROOT logger, so every module's
log.error / log.exception — and the tracebacks PTB's own error handling logs —
is mailed as it happens. Per occurrence, no digesting: that is the operator's
explicit choice (single-operator system). Two guard rails only:

  * the SMTP round-trip (seconds) happens on ONE worker thread fed by a
    queue, so sending never blocks a bot's event loop inside a logging call;
  * ALERT_MAX_PER_HOUR (default 0 = unlimited) is a flood valve for the
    "source broken, same error every tick" day; dropped mails are counted and
    the count rides along on the next one that goes out.

Records logged by shared.monitoring itself are skipped — a failing mail
server must not generate mail about itself (mailer.py also keeps its own
logging below ERROR for the same reason, this is the second belt).
"""

import logging
import queue
import threading
import time
from collections import deque

from shared import config
from shared.monitoring import mailer

_FMT = logging.Formatter("%(asctime)s %(levelname)s %(name)s\n\n%(message)s")


class ErrorEmailHandler(logging.Handler):
    def __init__(self, tag: str):
        super().__init__(level=logging.ERROR)
        self.tag = tag
        self.setFormatter(_FMT)
        self._q: queue.Queue = queue.Queue()
        self._sent: deque = deque()  # monotonic timestamps of the last hour's sends
        self._suppressed = 0
        threading.Thread(target=self._worker, name=f"errmail-{tag}", daemon=True).start()

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("shared.monitoring"):
            return
        try:
            first = (record.getMessage() or record.levelname).splitlines()[0]
            subject = f"[{self.tag}] {first[:120]}"
            self._q.put((subject, self.format(record)))
        except Exception:
            self.handleError(record)

    def _allowed(self) -> bool:
        """Sliding-window cap. 0/negative cap = always allowed."""
        cap = config.ALERT_MAX_PER_HOUR
        if cap <= 0:
            return True
        now = time.monotonic()
        while self._sent and now - self._sent[0] > 3600:
            self._sent.popleft()
        if len(self._sent) >= cap:
            return False
        self._sent.append(now)
        return True

    def _send_one(self, subject: str, body: str) -> None:
        if not self._allowed():
            self._suppressed += 1
            return
        if self._suppressed:
            body = (f"({self._suppressed} earlier error email(s) suppressed by "
                    f"ALERT_MAX_PER_HOUR={config.ALERT_MAX_PER_HOUR})\n\n") + body
            self._suppressed = 0
        mailer.send_alert(subject, body)

    def _worker(self) -> None:
        while True:
            subject, body = self._q.get()
            self._send_one(subject, body)


def install(tag: str) -> ErrorEmailHandler | None:
    """Attach the emailer to the root logger; `tag` names the process in the
    subject line. Returns None (and changes nothing) when alert email isn't
    configured."""
    if not mailer.enabled():
        logging.getLogger(__name__).info(
            "error emails off — ALERT_SMTP_HOST / ALERT_EMAIL_TO not set")
        return None
    handler = ErrorEmailHandler(tag)
    logging.getLogger().addHandler(handler)
    logging.getLogger(__name__).info("error emails on -> %s (tag %s)",
                                     config.ALERT_EMAIL_TO, tag)
    return handler
