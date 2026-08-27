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

# Browser whose cookie jar gallery-dl may read for logged-in Instagram photo
# downloads (e.g. "chrome"). Used ONLY by the photo fallback in
# shared/reel_downloader.py; blank = anonymous requests.
GALLERY_DL_COOKIES_BROWSER = os.environ.get("GALLERY_DL_COOKIES_BROWSER", "").strip()

# Phone address for uiautomator2 / adb. WiFi-debugging IP:port is preferred;
# the USB serial is the fallback so re-tethering still works if .env is wiped
# or PHONE_ADDRESS is unset. Update PHONE_ADDRESS in .env when the IP drifts.
DEVICE_ID = os.environ.get("PHONE_ADDRESS", "R5CX235CF9A")

# Telegram bot credentials (consumed by modules/telegram). Kept as raw
# strings; the bot validates + parses them so its error messages stay put.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Separate token for the news bot. Telegram allows ONE getUpdates poller per
# token, so telegram_bot.py (IG trigger) and news_bot.py can only run at the
# same time on different tokens — make a second bot via @BotFather and put it
# here. Falls back to the shared token when unset (then run one bot at a time).
NEWS_BOT_TOKEN = os.environ.get("NEWS_BOT_TOKEN", "").strip() or TELEGRAM_BOT_TOKEN

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

# Big files in the news bot: reuse the MTProto credentials above to download
# (and send back) videos past the Bot API's 20 MB download / 50 MB upload caps,
# up to Telegram's own 2 GB. Needs a one-off login of its own session:
#   py modules/telegram/mtproto.py --login
# Set to 0 to stay on the Bot API alone (bigger clips then fail with a clear
# error instead of being fetched). Ignored when API_ID/API_HASH are unset.
TG_BIG_FILES = os.environ.get("TG_BIG_FILES", "1").strip().lower() not in (
    "0", "false", "no", "off"
)

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
TRANSLATE_MODEL = os.environ.get("TRANSLATE_MODEL", "google/gemini-2.5-flash").strip()
SCORER_MODEL = os.environ.get("SCORER_MODEL", "").strip() or TRANSLATE_MODEL

# Collector's working dir: SQLite queue + downloaded media + login session.
TG_DATA_DIR = os.path.join(ROOT_DIR, "modules", "telegram", "data")

# BulkFollows (SMM panel) — orders placed per published Telegram post, and one
# extra order per channel every BULKFOLLOWS_POST_THRESHOLD posts. The two
# SERVICE ids are panel-specific numbers you copy from the BulkFollows services
# list; with either one unset its leg is skipped (logged, never fatal).
BULKFOLLOWS_API_KEY = os.environ.get("BULKFOLLOWS_API_KEY", "").strip()
BULKFOLLOWS_API_URL = os.environ.get(
    "BULKFOLLOWS_API_URL", "https://bulkfollows.com/api/v2"
).strip()
# Ordered per post, quantity = random 500-5000.
BULKFOLLOWS_SERVICE_ID = os.environ.get("BULKFOLLOWS_SERVICE_ID", "").strip()
# Ordered per channel every 5th post, quantity 10000, link = the channel.
BULKFOLLOWS_SERVICE_ID_BONUS = os.environ.get("BULKFOLLOWS_SERVICE_ID_BONUS", "").strip()


def _parse_destinations(raw: str):
    """Parse TG_DESTINATIONS: comma-separated "chat_id:lang:region" entries,
    e.g. "@my_news_en:en:us, @my_news_ru:ru:ru, -1001234567890:es".

    Each field after the chat id is optional and older two-field config keeps
    working unchanged:
      "@chan"            → posted untranslated, matches every item
      "@chan:en"         → translated to en, matches every item (catch-all)
      "@chan:en:eu"      → translated to en, only items the scorer tagged "eu"
      "@chan:en:us+eu"   → several regions, "+"-separated

    Splitting on ":" is safe — chat ids are numeric (-100123…, no colon) or
    @usernames. An empty `regions` set means catch-all; the smart filter reads
    it that way. YT_DESTINATIONS uses the same parser (its region field is
    unused today) so both configs stay one shape."""
    out = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        parts = [p.strip() for p in item.split(":")]
        chat = parts[0]
        lang = parts[1] if len(parts) > 1 else ""
        regions = {r for r in (parts[2].split("+") if len(parts) > 2 else []) if r}
        out.append({"chat_id": chat, "lang": lang, "regions": regions})
    return out


# Destination channels the bot posts to (each with a target language and,
# optionally, the audience regions it serves). Add the bot as an admin in
# every one of these.
TG_DESTINATIONS = _parse_destinations(os.environ.get("TG_DESTINATIONS", ""))

# Master kill switch for ALL YouTube uploading (dispatcher auto-upload, the
# news bot's manual picker, brand-it). OFF unless YT_UPLOADS_ENABLED=1 — three
# accounts were terminated for community-guideline strikes on auto-uploaded
# clips, so uploading is opt-in until a pre-upload policy check exists (see
# the TODO in CLAUDE.md). publisher.publish_shorts also enforces this itself,
# so a caller passing explicit destinations cannot bypass it.
YT_UPLOADS_ENABLED = os.environ.get("YT_UPLOADS_ENABLED", "0") == "1"

# YouTube destination channels (news bot picker + dispatcher auto-upload):
# comma-separated "account:lang" pairs, e.g. "mirnews:en,rusnews:ru". Same
# parser as TG_DESTINATIONS — here "chat_id" holds the ACCOUNT NAME, which
# must match a folder under credentials/youtube/. Emptied when uploads are
# disabled so the picker shows no YouTube rows and the dispatcher logs its
# "nothing auto-uploads" warning at startup.
YT_DESTINATIONS = (_parse_destinations(os.environ.get("YT_DESTINATIONS", ""))
                   if YT_UPLOADS_ENABLED else [])


# --------------------------------------------------------------------------- #
# Branded clips (shared/branding.py, news bot "Brand it" flow)                #
# --------------------------------------------------------------------------- #


def _parse_brands(raw: str, env):
    """Parse BRANDS: comma-separated "name:lang" entries, e.g.
    "mirnews:en,rusnews:ru" (lang optional). Each brand's platform accounts
    come from BRAND_<NAME>_TG / _YT / _TW (name uppercased, non-alphanumerics
    -> "_", same rule as TWITTER_<ACCOUNT>_*); an unset platform means the
    brand has no pair for it in the publish picker. The logo is always
    brands/<name>/logo.png — a missing file disables the brand in the picker
    (checked at use time, not here, so config import never touches disk)."""
    out = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        parts = [p.strip() for p in item.split(":")]
        name = parts[0]
        key = "".join(c if c.isalnum() else "_" for c in name).upper()
        out.append({
            "name": name,
            "lang": parts[1] if len(parts) > 1 else "",
            "tg": (env.get(f"BRAND_{key}_TG") or "").strip(),
            "yt": (env.get(f"BRAND_{key}_YT") or "").strip(),
            "tw": (env.get(f"BRAND_{key}_TW") or "").strip(),
            "logo": os.path.join(ROOT_DIR, "brands", name, "logo.png"),
        })
    return out


BRANDS = _parse_brands(os.environ.get("BRANDS", ""), os.environ)


# --------------------------------------------------------------------------- #
# Telegram autopilot (modules/telegram/autopilot.py, hosted by news_bot.py)   #
# --------------------------------------------------------------------------- #


def _int_env(name: str, default: int) -> int:
    """Int from .env, falling back to `default` on anything unparseable — a
    typo in one tuning knob must not stop the bot from starting."""
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


# A collected story only counts as news if it carries MEDIA — one or more
# photos and/or videos. Text-only posts (source-channel commentary, link
# dumps, announcements) are rejected BEFORE scoring, so they cost nothing.
# Set to 0 to let text-only stories through. Manual posting via the news bot's
# picker is never affected — you can always post whatever you want by hand.
NEWS_REQUIRE_MEDIA = os.environ.get("NEWS_REQUIRE_MEDIA", "1").strip().lower() not in (
    "0", "false", "no", "off"
)

# Minimum smart-filter score for an autopilot Telegram post. Lower than the
# YouTube floor: a Telegram post is cheap, a Shorts upload costs API quota.
TG_AUTO_MIN_SCORE = _int_env("TG_AUTO_MIN_SCORE", 60)

# The drip sleeps a fresh random gap in this range between posts, so the
# channels never look metronomic. 18-24 hours by default — roughly one
# carefully chosen story a day, at an hour nobody can predict.
TG_DRIP_MIN_S = _int_env("TG_DRIP_MIN_S", 18 * 3600)
TG_DRIP_MAX_S = _int_env("TG_DRIP_MAX_S", 24 * 3600)

# Stories older than this are never auto-posted — stale news dripping out days
# late is worse than a quiet channel. Manual posting ignores this.
# KEEP THIS ABOVE TG_DRIP_MAX_S/3600: a window shorter than the gap between
# ticks means everything collected since the last post has already expired by
# the time the next one fires, and the drip starves.
TG_MAX_AGE_H = _int_env("TG_MAX_AGE_H", 48)

# How many of the best-scoring eligible stories are compared head-to-head at
# post time to choose the one that actually goes out (smart_filter.best_of).
# 1 or 0 disables the comparison and posts the top-scoring story outright.
TG_COMPARE_TOP = _int_env("TG_COMPARE_TOP", 5)

# Where the "which reactions?" question is sent. Defaults to the control group
# the news bot already listens in; set it to your personal chat id for a DM.
TG_ASK_CHAT_ID = os.environ.get("TG_ASK_CHAT_ID", "").strip() or TELEGRAM_CHAT_ID

# Start with the drip paused (still switchable at runtime with /autopilot on).
TG_AUTOPILOT = os.environ.get("TG_AUTOPILOT", "1").strip().lower() not in ("0", "false", "no", "off")

# Pin the FIRST tick after startup to a wall-clock time, "HH:MM" in the
# machine's local timezone (e.g. "21:00"): today if that hour is still ahead,
# otherwise tomorrow. Only the first tick — every one after it goes back to
# the random TG_DRIP_MIN_S..TG_DRIP_MAX_S gap. Blank resumes the normal rhythm
# from the last post. Useful for testing, and for lining the first post of a
# fresh deployment up with a sensible hour.
TG_FIRST_TICK = os.environ.get("TG_FIRST_TICK", "").strip()


# --------------------------------------------------------------------------- #
# Monitoring / alert email (shared/monitoring — mailer, errmail, checks)      #
# --------------------------------------------------------------------------- #


def _float_env(name: str, default: float) -> float:
    """Float from .env, same fall-back contract as _int_env."""
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


# The mailbox alerts are sent FROM and the address they go TO. Both
# ALERT_SMTP_HOST and ALERT_EMAIL_TO must be set for ANY alert email to leave
# the system — with either blank every monitoring hook is a silent no-op, so
# dev runs and un-monitored machines behave exactly as before. A Gmail /
# Workspace sender needs an app password, not the account password.
ALERT_SMTP_HOST = os.environ.get("ALERT_SMTP_HOST", "").strip()
ALERT_SMTP_PORT = _int_env("ALERT_SMTP_PORT", 587)  # 465 = SSL-on-connect, else STARTTLS
ALERT_SMTP_USER = os.environ.get("ALERT_SMTP_USER", "").strip()
ALERT_SMTP_PASS = os.environ.get("ALERT_SMTP_PASS", "").strip()
ALERT_EMAIL_TO = os.environ.get("ALERT_EMAIL_TO", "").strip()
ALERT_EMAIL_FROM = os.environ.get("ALERT_EMAIL_FROM", "").strip() or ALERT_SMTP_USER

# Flood valve for the per-occurrence error emails (errmail.py). 0 = unlimited
# — one email per logged ERROR, the operator's explicit choice. Set a number
# only if a broken source ever floods the inbox.
ALERT_MAX_PER_HOUR = _int_env("ALERT_MAX_PER_HOUR", 0)

# Floors for shared/monitoring/checks.py. 0 disables that leg — used to keep
# the two checkouts sharing the VPS from double-alerting: CPU and the
# OpenRouter account are per-machine/per-account, so only master's timer
# checks them and the newsroom checkout sets both to 0.
ALERT_CPU_PCT = _float_env("ALERT_CPU_PCT", 80)
ALERT_BULKFOLLOWS_MIN = _float_env("ALERT_BULKFOLLOWS_MIN", 2.0)
ALERT_OPENROUTER_MIN = _float_env("ALERT_OPENROUTER_MIN", 0.5)

# healthchecks.io ping URLs (dead-man's switch), one per long-running
# process. Blank = that process sends no heartbeat.
HEALTHCHECK_URL_NEWSBOT = os.environ.get("HEALTHCHECK_URL_NEWSBOT", "").strip()
HEALTHCHECK_URL_COLLECTOR = os.environ.get("HEALTHCHECK_URL_COLLECTOR", "").strip()
HEALTHCHECK_URL_DISPATCHER = os.environ.get("HEALTHCHECK_URL_DISPATCHER", "").strip()
