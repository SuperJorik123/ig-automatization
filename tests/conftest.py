"""Shared fixtures. Every test here is offline: no Telegram, no OpenRouter, no
BulkFollows — only the pure decision logic and the SQLite store."""

import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """queue_store pointed at a throwaway database.

    _DB is resolved at import time, so it has to be patched alongside
    TG_DATA_DIR — patching the config alone would still write to the real
    news.db."""
    from shared import config
    from modules.telegram import queue_store

    monkeypatch.setattr(config, "TG_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(queue_store, "_DB", str(tmp_path / "news.db"))
    queue_store.init()
    return queue_store
