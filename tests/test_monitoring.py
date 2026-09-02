"""Offline tests for shared/monitoring — no SMTP, no HTTP, no psutil sampling.

Covers the three pieces with logic worth breaking: the alert state machine in
checks.py (crossing / daily reminder / recovery), the error-email handler
(subject shape, self-exclusion, flood valve), and the balance parsers.
"""

import logging
import threading
import time

import pytest

from shared import config
from shared.monitoring import checks, errmail, mailer


# --------------------------------------------------------------------------- #
# checks.transition — the crossing / remind / recover state machine           #
# --------------------------------------------------------------------------- #


def test_transition_fires_on_fresh_crossing():
    state = {}
    assert checks.transition(state, "cpu", True, now=1000.0) == (True, False)


def test_transition_silent_while_still_bad_inside_remind_window():
    state = {}
    checks.transition(state, "cpu", True, now=1000.0)
    assert checks.transition(state, "cpu", True, now=1000.0 + 60) == (False, False)


def test_transition_reminds_after_a_day_still_bad():
    state = {}
    checks.transition(state, "cpu", True, now=1000.0)
    fire, recovered = checks.transition(state, "cpu", True, now=1000.0 + checks.REMIND_S)
    assert (fire, recovered) == (True, False)


def test_transition_reports_recovery_once():
    state = {}
    checks.transition(state, "cpu", True, now=1000.0)
    assert checks.transition(state, "cpu", False, now=2000.0) == (False, True)
    # and only once — staying good is silent
    assert checks.transition(state, "cpu", False, now=3000.0) == (False, False)


def test_transition_good_from_the_start_is_silent():
    assert checks.transition({}, "cpu", False, now=1000.0) == (False, False)


# --------------------------------------------------------------------------- #
# errmail — the ERROR -> email handler                                        #
# --------------------------------------------------------------------------- #


def _record(msg, name="modules.telegram.news_bot", exc_info=None):
    return logging.LogRecord(name=name, level=logging.ERROR, pathname=__file__,
                             lineno=1, msg=msg, args=None, exc_info=exc_info)


def test_install_is_a_noop_when_mail_unconfigured(monkeypatch):
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "")
    before = list(logging.getLogger().handlers)
    assert errmail.install("test") is None
    assert logging.getLogger().handlers == before


def test_error_record_becomes_one_email(monkeypatch):
    sent, done = [], threading.Event()

    def fake_send(subject, body):
        sent.append((subject, body))
        done.set()
        return True

    monkeypatch.setattr(mailer, "send_alert", fake_send)
    handler = errmail.ErrorEmailHandler("news_bot")
    handler.emit(_record("boom first line\nsecond line"))
    assert done.wait(timeout=5), "worker thread never sent"
    subject, body = sent[0]
    assert subject == "[news_bot] boom first line"
    assert "second line" in body


def test_traceback_rides_in_the_body(monkeypatch):
    sent, done = [], threading.Event()
    monkeypatch.setattr(mailer, "send_alert",
                        lambda s, b: (sent.append((s, b)), done.set(), True)[-1])
    try:
        raise ValueError("kaput")
    except ValueError:
        import sys
        rec = _record("upload failed", exc_info=sys.exc_info())
    errmail.ErrorEmailHandler("dispatcher").emit(rec)
    assert done.wait(timeout=5)
    assert "Traceback" in sent[0][1] and "kaput" in sent[0][1]


def test_own_package_records_are_skipped(monkeypatch):
    monkeypatch.setattr(mailer, "send_alert",
                        lambda s, b: pytest.fail("must not mail about itself"))
    handler = errmail.ErrorEmailHandler("test")
    handler.emit(_record("smtp broken", name="shared.monitoring.mailer"))
    time.sleep(0.2)  # give the worker a chance to (wrongly) pick something up
    assert handler._q.empty()


def test_flood_valve_counts_suppressed_and_reports_them(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "send_alert", lambda s, b: (sent.append((s, b)), True)[-1])
    handler = errmail.ErrorEmailHandler("test")
    monkeypatch.setattr(config, "ALERT_MAX_PER_HOUR", 1)
    handler._send_one("a", "first")     # fills the 1/hour budget
    handler._send_one("b", "second")    # suppressed
    handler._send_one("c", "third")     # suppressed
    assert [s for s, _ in sent] == ["a"] and handler._suppressed == 2
    monkeypatch.setattr(config, "ALERT_MAX_PER_HOUR", 0)  # valve opened again
    handler._send_one("d", "fourth")
    assert sent[-1][0] == "d" and "2 earlier error email(s) suppressed" in sent[-1][1]


# --------------------------------------------------------------------------- #
# mailer — the off switch                                                     #
# --------------------------------------------------------------------------- #


def test_send_alert_refuses_without_config(monkeypatch):
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "")
    import smtplib
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: pytest.fail("no SMTP allowed"))
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *a, **k: pytest.fail("no SMTP allowed"))
    assert mailer.send_alert("s", "b") is False


# --------------------------------------------------------------------------- #
# checks — balance parsing and the disable switches                           #
# --------------------------------------------------------------------------- #


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_bulkfollows_balance_parses_dollars(monkeypatch):
    monkeypatch.setattr(checks.requests, "post", lambda *a, **k: _Resp({"balance": "12.34"}))
    assert checks.bulkfollows_balance("https://x/api/v2", "key") == (12.34, None)


def test_bulkfollows_balance_surfaces_panel_refusal(monkeypatch):
    monkeypatch.setattr(checks.requests, "post", lambda *a, **k: _Resp({"error": "bad key"}))
    bal, err = checks.bulkfollows_balance("https://x/api/v2", "key")
    assert bal is None and "bad key" in err


def test_bulkfollows_balance_survives_network_death(monkeypatch):
    def boom(*a, **k):
        raise OSError("dns down")
    monkeypatch.setattr(checks.requests, "post", boom)
    bal, err = checks.bulkfollows_balance("https://x/api/v2", "key")
    assert bal is None and "dns down" in err


def test_openrouter_credits_is_total_minus_usage(monkeypatch):
    payload = {"data": {"total_credits": 10.0, "total_usage": 9.6}}
    monkeypatch.setattr(checks.requests, "get", lambda *a, **k: _Resp(payload))
    credits, err = checks.openrouter_credits()
    assert err is None and credits == pytest.approx(0.4)


def test_panels_cover_both_keys(monkeypatch):
    monkeypatch.setattr(config, "BULKFOLLOWS_API_KEY", "op-key")
    monkeypatch.setenv("NR_BULKFOLLOWS_API_KEY", "client-key")
    monkeypatch.delenv("NR_BULKFOLLOWS_API_URL", raising=False)
    panels = checks._panels()
    assert [p[0] for p in panels] == ["bulkfollows", "bulkfollows_nr"]
    assert panels[1][2] == "https://bulkfollows.com/api/v2"


def test_cpu_leg_disabled_by_zero_threshold(monkeypatch):
    monkeypatch.setattr(config, "ALERT_CPU_PCT", 0.0)
    monkeypatch.setattr(checks.psutil, "cpu_percent",
                        lambda **k: pytest.fail("CPU must not be sampled when disabled"))
    checks.check_cpu({})


# --------------------------------------------------------------------------- #
# checks — disk / memory legs and the "only stamp when the mail left" rule    #
# --------------------------------------------------------------------------- #


class _Usage:
    def __init__(self, percent, free=0):
        self.percent = percent
        self.free = free
        self.total = 100


def test_disk_leg_alerts_above_floor(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "send_alert", lambda s, b: (sent.append(s), True)[-1])
    monkeypatch.setattr(config, "ALERT_DISK_PCT", 85.0)
    monkeypatch.setattr(checks.psutil, "disk_usage", lambda p: _Usage(91.0, free=3 * 2**30))
    state = {}
    checks.check_disk(state)
    assert state["disk"]["bad"] is True
    assert sent and "91%" in sent[0] and "disk" in sent[0].lower()


def test_disk_leg_disabled_by_zero_threshold(monkeypatch):
    monkeypatch.setattr(config, "ALERT_DISK_PCT", 0.0)
    monkeypatch.setattr(checks.psutil, "disk_usage",
                        lambda p: pytest.fail("disk must not be sampled when disabled"))
    checks.check_disk({})


def test_mem_leg_alerts_above_floor(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "send_alert", lambda s, b: (sent.append(s), True)[-1])
    monkeypatch.setattr(config, "ALERT_MEM_PCT", 90.0)
    monkeypatch.setattr(checks.psutil, "virtual_memory", lambda: _Usage(95.0))
    state = {}
    checks.check_mem(state)
    assert state["mem"]["bad"] is True and sent and "95%" in sent[0]


def test_mem_leg_disabled_by_zero_threshold(monkeypatch):
    monkeypatch.setattr(config, "ALERT_MEM_PCT", 0.0)
    monkeypatch.setattr(checks.psutil, "virtual_memory",
                        lambda: pytest.fail("memory must not be sampled when disabled"))
    checks.check_mem({})


def test_failed_send_does_not_stamp_the_alert(monkeypatch):
    """SMTP down (or unconfigured) on the crossing tick: the next tick must try
    again, not wait a day for the reminder."""
    calls = []
    monkeypatch.setattr(mailer, "send_alert", lambda s, b: (calls.append(s), False)[-1])
    state = {}
    checks._alert(state, "cpu", True, "bad", "body", "ok")
    checks._alert(state, "cpu", True, "bad", "body", "ok")
    assert len(calls) == 2, "second tick must retry the unsent crossing alert"


def test_successful_send_stamps_and_silences(monkeypatch):
    calls = []
    monkeypatch.setattr(mailer, "send_alert", lambda s, b: (calls.append(s), True)[-1])
    state = {}
    checks._alert(state, "cpu", True, "bad", "body", "ok")
    checks._alert(state, "cpu", True, "bad", "body", "ok")
    assert calls == ["bad"]


def test_failed_recovery_send_is_retried(monkeypatch):
    results = iter([True, False, True])
    calls = []
    monkeypatch.setattr(mailer, "send_alert",
                        lambda s, b: (calls.append(s), next(results))[-1])
    state = {}
    checks._alert(state, "cpu", True, "bad", "body", "ok")    # crossing, sent
    checks._alert(state, "cpu", False, "bad", "body", "ok")   # recovery, send FAILS
    checks._alert(state, "cpu", False, "bad", "body", "ok")   # recovery retried, sent
    checks._alert(state, "cpu", False, "bad", "body", "ok")   # then quiet
    assert calls == ["bad", "ok", "ok"]


def test_send_test_email_reports_result(monkeypatch):
    monkeypatch.setattr(config, "ALERT_SMTP_HOST", "smtp.example")
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "op@example")
    monkeypatch.setattr(mailer, "send_alert", lambda s, b: True)
    assert checks.send_test_email() == 0
    monkeypatch.setattr(mailer, "send_alert", lambda s, b: False)
    assert checks.send_test_email() == 1


def test_send_test_email_refuses_when_unconfigured(monkeypatch):
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "")
    monkeypatch.setattr(mailer, "send_alert", lambda s, b: pytest.fail("must not send"))
    assert checks.send_test_email() == 1
