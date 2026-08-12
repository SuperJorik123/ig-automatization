"""modules/telegram/mtproto.py — the offline half: when the big-file client is
considered available, and what the operator is told when it isn't. No Telegram
connection is made here."""

import asyncio

import pytest

from shared import config
from modules.telegram import mtproto


@pytest.fixture(autouse=True)
def _reset():
    """ensure_ready() memoises its verdict for the process — clear it so each
    test starts from "never tried"."""
    mtproto._client, mtproto._ready = None, None
    yield
    mtproto._client, mtproto._ready = None, None


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setattr(config, "TG_BIG_FILES", True)
    monkeypatch.setattr(config, "TELEGRAM_API_ID", "12345")
    monkeypatch.setattr(config, "TELEGRAM_API_HASH", "deadbeef")


# --- human_size ------------------------------------------------------------

def test_human_size_switches_to_gb_past_a_gigabyte():
    assert mtproto.human_size(20 * 1024 * 1024) == "20 MB"
    assert mtproto.human_size(2 * 1024 * 1024 * 1024) == "2.0 GB"


# --- is_configured / unavailable_reason ------------------------------------

def test_configured_needs_both_credentials(monkeypatch, creds):
    assert mtproto.is_configured() is True
    monkeypatch.setattr(config, "TELEGRAM_API_HASH", "")
    assert mtproto.is_configured() is False
    assert "my.telegram.org" in mtproto.unavailable_reason()


def test_kill_switch_beats_credentials(monkeypatch, creds):
    monkeypatch.setattr(config, "TG_BIG_FILES", False)
    assert mtproto.is_configured() is False
    assert "TG_BIG_FILES" in mtproto.unavailable_reason()


def test_configured_but_unauthorised_reason_points_at_the_login(creds):
    """Credentials alone don't mean a session exists."""
    assert "--login" in mtproto.unavailable_reason()


# --- ensure_ready ----------------------------------------------------------

def test_ensure_ready_false_without_credentials(monkeypatch):
    monkeypatch.setattr(config, "TG_BIG_FILES", True)
    monkeypatch.setattr(config, "TELEGRAM_API_ID", "")
    monkeypatch.setattr(config, "TELEGRAM_API_HASH", "")
    assert asyncio.run(mtproto.ensure_ready()) is False


def test_ensure_ready_swallows_connection_failure(monkeypatch, creds, tmp_path):
    """A dead network must cost the caller its big-file path, not raise into
    the middle of a post."""
    monkeypatch.setattr(config, "TG_DATA_DIR", str(tmp_path))

    class _Boom:
        def __init__(self, *a, **kw):
            raise OSError("no route to host")

    monkeypatch.setattr(mtproto, "TelegramClient", _Boom)
    assert asyncio.run(mtproto.ensure_ready()) is False


def test_download_refuses_when_not_ready(monkeypatch):
    monkeypatch.setattr(config, "TG_BIG_FILES", False)
    with pytest.raises(RuntimeError, match="TG_BIG_FILES"):
        asyncio.run(mtproto.download(-100123, 5, "/tmp/x"))


# --- normalize_phone -------------------------------------------------------

def test_local_number_gets_the_country_code():
    """Telegram rejects the local form outright (PhoneNumberInvalidError)."""
    assert mtproto.normalize_phone("078424624", "373") == "+37378424624"


def test_already_international_forms_are_kept():
    for raw in ("+37378424624", "37378424624", "0037378424624",
                "+373 78 424 624", "+373-78-424-624"):
        assert mtproto.normalize_phone(raw, "373") == "+37378424624"


def test_leading_plus_wins_over_the_default_country_code():
    assert mtproto.normalize_phone("+4478424624", "373") == "+4478424624"


def test_empty_input_is_rejected():
    import pytest as _pytest
    with _pytest.raises(ValueError):
        mtproto.normalize_phone("   ", "373")
