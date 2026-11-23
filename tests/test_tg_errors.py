"""shared/monitoring/tg_errors — Telegram blips are warnings, real errors stay errors."""
import asyncio
import logging

from telegram.error import BadRequest, Conflict, NetworkError, RetryAfter, TimedOut

from shared.monitoring import tg_errors


def test_blips_are_transient_but_our_own_bad_requests_are_not():
    assert tg_errors.transient(NetworkError("Bad Gateway"))
    assert tg_errors.transient(TimedOut())
    assert tg_errors.transient(RetryAfter(3))
    assert not tg_errors.transient(BadRequest("Message to be replied not found"))
    assert not tg_errors.transient(Conflict("terminated by other getUpdates request"))
    assert not tg_errors.transient(ValueError("bug"))


def test_handler_logs_a_blip_as_warning_and_a_bug_as_error(caplog, monkeypatch):
    monkeypatch.setattr(tg_errors, "_burst", tg_errors._Burst())
    log = logging.getLogger("t")
    on_error = tg_errors.handler(log)

    class Ctx:
        def __init__(self, e):
            self.error = e
    with caplog.at_level(logging.WARNING, logger="t"):
        asyncio.run(on_error(None, Ctx(NetworkError("Bad Gateway"))))
        asyncio.run(on_error(None, Ctx(ValueError("real bug"))))
    levels = [(r.levelname, r.getMessage()[:28]) for r in caplog.records]
    assert levels[0][0] == "WARNING" and "blip" in levels[0][1]
    assert levels[1][0] == "ERROR" and caplog.records[1].exc_info


def test_a_lasting_outage_still_mails_once_an_hour(caplog, monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(tg_errors, "_burst", tg_errors._Burst(clock=lambda: t["now"]))
    log = logging.getLogger("t2")
    with caplog.at_level(logging.WARNING, logger="t2"):
        for i in range(tg_errors.BURST * 3):           # one every 10 s for 15 min
            t["now"] += 10
            tg_errors.handled(NetworkError("Bad Gateway"), log)
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1 and "unreachable" in errors[0].getMessage()


def test_scattered_blips_never_mail(caplog, monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(tg_errors, "_burst", tg_errors._Burst(clock=lambda: t["now"]))
    log = logging.getLogger("t3")
    with caplog.at_level(logging.WARNING, logger="t3"):
        for i in range(200):                            # ~ every 20 min for days
            t["now"] += 1200
            tg_errors.handled(NetworkError("Bad Gateway"), log)
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
