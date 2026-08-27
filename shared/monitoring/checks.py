"""
shared/monitoring/checks.py — the 5-minute machine + balance check.

Run by a systemd timer (or any cron) every 5 minutes:

    py shared/monitoring/checks.py

Every run samples CPU against ALERT_CPU_PCT; once an hour it also checks the
BulkFollows balance(s) and OpenRouter credits against their ALERT_*_MIN
floors. Whichever BulkFollows keys the .env holds are checked — the
operator's BULKFOLLOWS_API_KEY on master's checkout, the client's
NR_BULKFOLLOWS_API_KEY on the newsroom branch — so this file is identical on
both branches and each checkout monitors its own money.

Both checkouts live on the SAME VPS, so per-machine / per-account legs must
run in only ONE of them: a threshold of 0 disables that leg. The newsroom
checkout's .env sets ALERT_CPU_PCT=0 and ALERT_OPENROUTER_MIN=0 (master's
timer already covers the machine and the shared OpenRouter account).

Alert shape — deliberately NOT per occurrence like the error emails: a low
balance or a pegged CPU stays true for hours, so each condition emails once
when it turns bad, one reminder a day while it stays bad, and a short
all-clear when it recovers. A *failing check* (panel unreachable, bad key) is
its own condition with the same shape — silent blindness is how a balance
quietly hits zero. State lives in data/monitor_state.json.
"""

import json
import logging
import os
import sys
import time

# Repo-root bootstrap for direct runs (`py shared/monitoring/checks.py`).
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import psutil  # noqa: E402
import requests  # noqa: E402

from shared import config  # noqa: E402
from shared.monitoring import mailer  # noqa: E402

log = logging.getLogger("monitor")

STATE_PATH = os.path.join(config.ROOT_DIR, "data", "monitor_state.json")
BALANCE_EVERY_S = 3500  # "hourly" with slack for timer jitter
REMIND_S = 24 * 3600    # while a condition stays bad, one reminder a day
CPU_SAMPLE_S = 5
TIMEOUT = 20


# --------------------------------------------------------------------------- #
# State machine                                                               #
# --------------------------------------------------------------------------- #


def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)


def transition(state: dict, key: str, bad: bool, now: float | None = None):
    """Advance one condition. Returns (fire, recovered): `fire` = send the
    alert now (fresh crossing, or the daily reminder while still bad),
    `recovered` = send the all-clear."""
    now = time.time() if now is None else now
    entry = state.get(key) or {"bad": False, "last_alert": 0.0}
    fire = bad and (not entry["bad"] or now - entry["last_alert"] >= REMIND_S)
    recovered = (not bad) and entry["bad"]
    if fire:
        entry["last_alert"] = now
    entry["bad"] = bad
    state[key] = entry
    return fire, recovered


def _alert(state: dict, key: str, bad: bool, subject: str, body: str,
           subject_ok: str) -> None:
    fire, recovered = transition(state, key, bad)
    if fire:
        mailer.send_alert(subject, body)
    elif recovered:
        mailer.send_alert(subject_ok, "The condition cleared on its own — no action needed.")


# --------------------------------------------------------------------------- #
# The checks                                                                  #
# --------------------------------------------------------------------------- #


def check_cpu(state: dict) -> None:
    if config.ALERT_CPU_PCT <= 0:
        return
    pct = psutil.cpu_percent(interval=CPU_SAMPLE_S)
    log.info("cpu: %.0f%% (limit %.0f%%)", pct, config.ALERT_CPU_PCT)
    _alert(state, "cpu", pct > config.ALERT_CPU_PCT,
           f"[monitor] CPU at {pct:.0f}% (limit {config.ALERT_CPU_PCT:.0f}%)",
           f"CPU usage is {pct:.0f}%, sampled over {CPU_SAMPLE_S}s on the VPS.\n"
           f"Reminder comes daily while it stays above the limit.",
           "[monitor] CPU back under the limit")


def bulkfollows_balance(url: str, key: str):
    """-> (dollars, None) or (None, error string)."""
    try:
        resp = requests.post(url, data={"key": key, "action": "balance"},
                             timeout=TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, dict) and payload.get("error"):
            return None, str(payload["error"])
        return float(payload["balance"]), None
    except Exception as exc:
        return None, str(exc)


def openrouter_credits():
    """-> (remaining dollars, None) or (None, error string)."""
    try:
        resp = requests.get(
            "https://openrouter.ai/api/v1/credits",
            headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        return float(data["total_credits"]) - float(data["total_usage"]), None
    except Exception as exc:
        return None, str(exc)


def _panels():
    """(state key, label, api url, api key) per BulkFollows key in this
    checkout's .env. NR_* is read from the environment, not config — the
    master branch's config.py doesn't define it, the newsroom branch's .env
    does, and this keeps the file identical on both."""
    out = []
    if config.BULKFOLLOWS_API_KEY:
        out.append(("bulkfollows", "BulkFollows (operator)",
                    config.BULKFOLLOWS_API_URL, config.BULKFOLLOWS_API_KEY))
    nr_key = os.environ.get("NR_BULKFOLLOWS_API_KEY", "").strip()
    if nr_key:
        nr_url = (os.environ.get("NR_BULKFOLLOWS_API_URL", "").strip()
                  or "https://bulkfollows.com/api/v2")
        out.append(("bulkfollows_nr", "BulkFollows (client)", nr_url, nr_key))
    return out


def check_balances(state: dict) -> None:
    if config.ALERT_BULKFOLLOWS_MIN > 0:
        for key, label, url, api_key in _panels():
            bal, err = bulkfollows_balance(url, api_key)
            _alert(state, f"{key}_check", bal is None,
                   f"[monitor] {label} balance check FAILING",
                   f"Could not read the {label} balance: {err}\n"
                   f"While this fails a low balance goes unseen.",
                   f"[monitor] {label} balance check working again")
            if bal is None:
                continue
            log.info("%s balance: $%.2f (floor $%.2f)", label, bal,
                     config.ALERT_BULKFOLLOWS_MIN)
            _alert(state, key, bal < config.ALERT_BULKFOLLOWS_MIN,
                   f"[monitor] {label} balance ${bal:.2f} — top up",
                   f"{label} balance is ${bal:.2f}, below the "
                   f"${config.ALERT_BULKFOLLOWS_MIN:.2f} floor. Orders on an empty "
                   f"balance fail silently (smm.py never raises).",
                   f"[monitor] {label} balance OK again")

    if config.ALERT_OPENROUTER_MIN > 0 and config.OPENROUTER_API_KEY:
        credits, err = openrouter_credits()
        _alert(state, "openrouter_check", credits is None,
               "[monitor] OpenRouter credits check FAILING",
               f"Could not read OpenRouter credits: {err}",
               "[monitor] OpenRouter credits check working again")
        if credits is not None:
            log.info("openrouter credits: $%.2f (floor $%.2f)", credits,
                     config.ALERT_OPENROUTER_MIN)
            _alert(state, "openrouter", credits < config.ALERT_OPENROUTER_MIN,
                   f"[monitor] OpenRouter credits ${credits:.2f} — top up",
                   f"OpenRouter credits are ${credits:.2f}, below the "
                   f"${config.ALERT_OPENROUTER_MIN:.2f} floor. Rewrites/scoring "
                   f"degrade SILENTLY when credits run out — posts keep going "
                   f"out with raw ledes and nothing errors.",
                   "[monitor] OpenRouter credits OK again")


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    state = _load_state()
    check_cpu(state)
    now = time.time()
    if now - float(state.get("last_balance_check", 0) or 0) >= BALANCE_EVERY_S:
        state["last_balance_check"] = now
        check_balances(state)
    _save_state(state)


if __name__ == "__main__":
    main()
