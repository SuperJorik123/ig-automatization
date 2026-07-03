"""
shared/config.py — one place that loads .env and resolves shared paths.

Every entrypoint (root server.py, the platform scripts under modules/, the
Telegram bot) imports from here so PHONE_ADDRESS, the posts/ queue, and the
credentials resolve to the repo root no matter which subfolder the calling
file now lives in. Before the restructure each script did its own
`load_dotenv(<its own dir>/.env)`; once files moved into modules/<platform>/
that path pointed at a non-existent .env, so the lookup is centralised here.
"""

import os

from dotenv import load_dotenv

# shared/config.py -> shared/ -> <repo root>
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# .env lives at the repo root and is git-ignored (holds the bot token etc.).
load_dotenv(os.path.join(ROOT_DIR, ".env"))

# Single shared media queue. posts/ stays at the repo root; posts/posted/
# holds archives. Both server.py and the platform posters read/write here.
POSTS_DIR = os.path.join(ROOT_DIR, "posts")

# Phone address for uiautomator2 / adb. WiFi-debugging IP:port is preferred;
# the USB serial is the fallback so re-tethering still works if .env is wiped
# or PHONE_ADDRESS is unset. Update PHONE_ADDRESS in .env when the IP drifts.
DEVICE_ID = os.environ.get("PHONE_ADDRESS", "R5CX235CF9A")

# Telegram bot credentials (consumed by modules/telegram). Kept as raw
# strings; the bot validates + parses them so its error messages stay put.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Instagram accounts logged in on the phone, comma-separated in .env, no
# leading `@`. Order is preserved — it drives the Telegram keyboard rows and
# the per-URL upload order.
IG_ACCOUNTS = [
    a.strip().lstrip("@")
    for a in os.environ.get("IG_ACCOUNTS", "").strip().split(",")
    if a.strip()
]


# --------------------------------------------------------------------------- #
# Telegram news aggregator (modules/telegram)                                 #
# --------------------------------------------------------------------------- #

# MTProto credentials for the collector's USER client (https://my.telegram.org).
# Bots can't read other channels' history, so collection runs as your account.
TELEGRAM_API_ID = os.environ.get("TELEGRAM_API_ID", "").strip()
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "").strip()

# Your numeric Telegram user id — the ONLY account allowed to post manually
# through the bot. Find it via @userinfobot. Manual posting is disabled until set.
TG_OPERATOR_ID = os.environ.get("TG_OPERATOR_ID", "").strip()

# Source channels the collector reads (comma-separated @usernames or numeric ids).
# You must already be a member of each private one.
TG_SOURCES = [s.strip() for s in os.environ.get("TG_SOURCES", "").split(",") if s.strip()]

# Language the source posts are written in (ISO code or name). When a
# destination's language matches this, translation is skipped. Blank = never skip.
SOURCE_LANG = os.environ.get("SOURCE_LANG", "").strip()

# Daily auto-post time "HH:MM" (24h) in TIMEZONE (IANA name, e.g. Europe/Bucharest).
# Only used in throttled mode (POSTS_PER_DAY is a number); ignored when unlimited.
DAILY_TIME = os.environ.get("DAILY_TIME", "09:00").strip()
TIMEZONE = os.environ.get("TIMEZONE", "UTC").strip()


def _parse_post_limit(raw: str):
    """How many auto-collected posts to publish per day.
      - a number  -> throttle the daily drip to N (1 = classic once-a-day)
      - `false` (also off/no/none/all/unlimited) -> no throttle: post everything
        continuously as it's collected (returns None)
      - unset -> 1; unparseable -> 1 (safe default)."""
    raw = (raw or "").strip().lower()
    if raw in ("false", "off", "no", "none", "all", "unlimited"):
        return None
    if raw == "":
        return 1
    try:
        return max(1, int(raw))
    except ValueError:
        return 1


# None => unlimited ("post everything"); otherwise a positive int = N per day.
POSTS_PER_DAY = _parse_post_limit(os.environ.get("POSTS_PER_DAY", ""))

# Claude translation. Model defaults to Opus per Anthropic guidance; set
# TRANSLATE_MODEL=claude-haiku-4-5 in .env for a cheaper/faster option.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
TRANSLATE_MODEL = os.environ.get("TRANSLATE_MODEL", "claude-opus-4-8").strip()

# Collector's working dir: SQLite queue + downloaded media + login session.
TG_DATA_DIR = os.path.join(ROOT_DIR, "modules", "telegram", "data")


def _parse_destinations(raw: str):
    """Parse TG_DESTINATIONS: comma-separated "chat_id:lang" pairs, e.g.
    "@my_news_en:en, @my_news_ru:ru, -1001234567890:es". A bare chat with no
    ":lang" gets an empty lang (posted untranslated). rpartition splits on the
    LAST colon, so numeric ids like -100123... (no colon) parse cleanly."""
    out = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        chat, sep, lang = item.rpartition(":")
        if sep and chat.strip() and lang.strip():
            out.append({"chat_id": chat.strip(), "lang": lang.strip()})
        else:
            out.append({"chat_id": item, "lang": ""})
    return out


# Destination channels the bot posts to (each with a target language). Add the
# bot as an admin in every one of these.
TG_DESTINATIONS = _parse_destinations(os.environ.get("TG_DESTINATIONS", ""))
