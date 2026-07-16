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
# YouTube Shorts + Twitter/X posting (modules/youtube, modules/twitter)       #
# --------------------------------------------------------------------------- #

# YouTube OAuth credentials are FILE-based, one folder per account:
#   credentials/youtube/<account>/client_secrets.json  (from Google Cloud Console)
#   credentials/youtube/<account>/token.pickle         (created on first login)
# The whole credentials/ dir is git-ignored.
YOUTUBE_CREDS_DIR = os.path.join(ROOT_DIR, "credentials", "youtube")

# Twitter account names, comma-separated in .env (same shape as IG_ACCOUNTS).
# Each name keys its TWITTER_<ACCOUNT>_* env vars.
TWITTER_ACCOUNTS = [
    a.strip() for a in os.environ.get("TWITTER_ACCOUNTS", "").split(",") if a.strip()
]

# Minimum smart-filter score (0-100) at which the dispatcher auto-uploads a
# collected video to every YouTube channel. 70 = scorer's "high" tier floor.
try:
    YT_AUTO_MIN_SCORE = int(os.environ.get("YT_AUTO_MIN_SCORE", "70"))
except ValueError:
    YT_AUTO_MIN_SCORE = 70


# --------------------------------------------------------------------------- #
# Telegram news aggregator (modules/telegram)                                 #
# --------------------------------------------------------------------------- #

# MTProto credentials for the collector's USER client (https://my.telegram.org).
# Bots can't read other channels' history, so collection runs as your account.
TELEGRAM_API_ID = os.environ.get("TELEGRAM_API_ID", "").strip()
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "").strip()

# Source channels the collector reads (comma-separated @usernames or numeric ids).
# You must already be a member of each private one.
TG_SOURCES = [s.strip() for s in os.environ.get("TG_SOURCES", "").split(",") if s.strip()]

# Language the source posts are written in (ISO code or name). When a
# destination's language matches this, translation is skipped. Blank = never skip.
SOURCE_LANG = os.environ.get("SOURCE_LANG", "").strip()

# Translation + scoring via OpenRouter (openrouter.ai — OpenAI-compatible
# gateway). Model ids must exist on OpenRouter — gpt-4o-mini is cheap and
# handles both jobs well. SCORER_MODEL falls back to TRANSLATE_MODEL.
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
TRANSLATE_MODEL = os.environ.get("TRANSLATE_MODEL", "openai/gpt-4o-mini").strip()
SCORER_MODEL = os.environ.get("SCORER_MODEL", "").strip() or TRANSLATE_MODEL

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

# YouTube destination channels (news bot picker + dispatcher auto-upload):
# comma-separated "account:lang" pairs, e.g. "mirnews:en,rusnews:ru". Same
# parser as TG_DESTINATIONS — here "chat_id" holds the ACCOUNT NAME, which
# must match a folder under credentials/youtube/.
YT_DESTINATIONS = _parse_destinations(os.environ.get("YT_DESTINATIONS", ""))
