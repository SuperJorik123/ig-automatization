"""
shared/monitoring/mailer.py — the one door alert email leaves through.

Plain SMTP against the operator's own mailbox (ALERT_SMTP_* in .env). A
Gmail / Workspace sender needs an app password — the account password is
refused with "Username and Password not accepted". Port 465 connects over
SSL; anything else (587) connects plain and upgrades with STARTTLS.

Blocking — errmail.py calls it from its worker thread, checks.py is a plain
script. Never raises, and never logs above WARNING: an ERROR logged here
would be picked up by the errmail handler and turn a broken mail server into
a feedback loop of mail about mail.
"""

import logging
import smtplib
from email.message import EmailMessage

from shared import config

log = logging.getLogger(__name__)

TIMEOUT = 20


def enabled() -> bool:
    """Alert email is configured. Every monitoring hook checks this so an
    unconfigured deployment stays a no-op."""
    return bool(config.ALERT_EMAIL_TO and config.ALERT_SMTP_HOST)


def send_alert(subject: str, body: str) -> bool:
    """One email to the operator. True on success, False on anything else."""
    if not enabled():
        log.warning("alert email off (ALERT_SMTP_HOST/ALERT_EMAIL_TO unset) — dropped: %s",
                    subject)
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.ALERT_EMAIL_FROM
    msg["To"] = config.ALERT_EMAIL_TO
    msg.set_content(body)

    try:
        if config.ALERT_SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(config.ALERT_SMTP_HOST, config.ALERT_SMTP_PORT,
                                      timeout=TIMEOUT)
        else:
            server = smtplib.SMTP(config.ALERT_SMTP_HOST, config.ALERT_SMTP_PORT,
                                  timeout=TIMEOUT)
        with server:
            if config.ALERT_SMTP_PORT != 465:
                server.starttls()
            if config.ALERT_SMTP_USER:
                server.login(config.ALERT_SMTP_USER, config.ALERT_SMTP_PASS)
            server.send_message(msg)
        log.info("alert email sent: %s", subject)
        return True
    except Exception as exc:
        log.warning("alert email failed (%s): %s", subject, exc)
        return False
