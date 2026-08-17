"""
shared/config.py — one place that loads .env and resolves shared paths.

Every entrypoint (root server.py, the platform scripts under modules/, the
Telegram bot) imports from here so PHONE_ADDRESS, the posts/ queue, and the
credentials resolve to the repo root no matter which subfolder the calling
file now lives in. Before the restructure each script did its own
`load_dotenv(<its own dir>/.env)`; once files moved into modules/<platform>/
that path pointed at a non-existent .env, so the lookup is centralised here.
"""

import copy
import json
import logging
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

# YouTube destination channels (news bot picker + dispatcher auto-upload):
# comma-separated "account:lang" pairs, e.g. "mirnews:en,rusnews:ru". Same
# parser as TG_DESTINATIONS — here "chat_id" holds the ACCOUNT NAME, which
# must match a folder under credentials/youtube/.
YT_DESTINATIONS = _parse_destinations(os.environ.get("YT_DESTINATIONS", ""))


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
# Client newsroom bot: WordPress -> Telegram (modules/newsroom)               #
# --------------------------------------------------------------------------- #
#
# A separate product living on the client/wp-newsbot branch: it watches N
# WordPress sites and posts each new article to that site's Telegram channel.
# Everything it owns is prefixed NR_ and nothing above this line is involved —
# separate bot token, separate database, separate BulkFollows balance.

# Its own working dir: SQLite store only (media is passed to Telegram by URL,
# so this bot never downloads a file).
NR_DATA_DIR = os.path.join(ROOT_DIR, "modules", "newsroom", "data")

# Bot token for the newsroom bot. MUST be a different bot from the two above:
# Telegram allows one getUpdates poller per token, and a shared token means
# whichever process starts second silently breaks the first.
NR_BOT_TOKEN = os.environ.get("NR_BOT_TOKEN", "").strip()

# Publish for real, or only log what would go out? Defaults to DRY — an
# unconfigured or half-deployed instance must not be able to post to seven
# live client channels. Turning this off is a deliberate act.
NR_DRY_RUN = os.environ.get("NR_DRY_RUN", "1").strip().lower() not in (
    "0", "false", "no", "off"
)

# Seconds between polls of each WordPress site.
NR_POLL_S = _int_env("NR_POLL_S", 300)

# Delay between publishing a post and ordering its reactions. Reactions
# appearing in the same second as the post is the most legible bot tell there
# is; 20 minutes reads as organic.
NR_REACTION_DELAY_S = _int_env("NR_REACTION_DELAY_S", 1200)

# On a site's FIRST tick the bot normally records the articles it finds as
# already-seen and posts none of them: without that guard, enabling a new site
# dumps twenty back-articles into a live channel in one burst, in front of the
# client's subscribers, with no way to undo it. Set to 1 only when you
# genuinely want that first batch published.
NR_BACKFILL = os.environ.get("NR_BACKFILL", "0").strip().lower() in (
    "1", "true", "yes", "on"
)

# BulkFollows credentials for the CLIENT's panel account — a different key and
# a different balance from BULKFOLLOWS_API_KEY above. Service ids are per site
# (see modules/newsroom/sites/*.json), not global: a channel with different geo
# targeting may need a different views service.
NR_BULKFOLLOWS_API_KEY = os.environ.get("NR_BULKFOLLOWS_API_KEY", "").strip()
NR_BULKFOLLOWS_API_URL = os.environ.get(
    "NR_BULKFOLLOWS_API_URL", "https://bulkfollows.com/api/v2"
).strip()


def _parse_emoji_services(raw: str) -> list:
    """NR_EMOJI_SERVICES ("name:id" pairs, comma-separated) into the catalogue
    orders.py draws reactions from: [{"name", "emoji", "service"}, ...].

    The name is what a site's emoji_pool refers to — the emoji glyph itself
    for a single reaction, a plain word ("positive") for the panel's mixed
    sets. A malformed pair is logged and dropped, never fatal: one typo must
    cost one reaction, not every channel's orders."""
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, service = part.rpartition(":")
        name, service = name.strip(), service.strip()
        if not sep or not name or not service.isdigit():
            logging.getLogger(__name__).warning(
                "NR_EMOJI_SERVICES: bad entry %r (want name:id) — skipped", part)
            continue
        out.append({"name": name, "emoji": name, "service": service})
    return out


# Reaction service catalogue for the CLIENT's panel account. Service ids are
# panel-account data, not code — they changed once already and will again,
# which is why they live here and not in orders.py. Sites still opt in to a
# subset by name via their emoji_pool; an empty catalogue means no reactions.
NR_EMOJI_SERVICES = _parse_emoji_services(os.environ.get("NR_EMOJI_SERVICES", ""))

# Model that rewrites an article into a Telegram post. Falls back to the
# translator's model, which is already tuned for faithful news prose.
NR_REWRITE_MODEL = os.environ.get("NR_REWRITE_MODEL", "").strip() or TRANSLATE_MODEL

# Where the per-site JSON configs live. sites/example.json is the documented
# template and is never loaded as a real site.
NR_SITES_DIR = os.path.join(ROOT_DIR, "modules", "newsroom", "sites")

# Defaults for every key a site file may omit. `name`, `wp_base` and `chat_id`
# have no default — a file missing any of them is not a usable site.
_SITE_DEFAULTS = {
    "enabled": True,
    "views_phase1": [500, 5000],
    "service_views": "",
    "service_bonus": "",
    "emoji_pool": [],
    "emoji_count": [2, 4],
    "emoji_quantity": [10, 40],
    "rewrite_hint": "",
    # Max characters of the generated post ("and shorter when the story is
    # small"). Telegram's media-caption ceiling is 1024 and publish.py appends
    # the article link after this, so keep well under.
    "post_chars": 500,
}


def _load_sites(directory: str) -> list:
    """Every enabled site under `directory`, ordered by filename so the
    startup log reads the same way every time.

    Never raises. A malformed or incomplete file is logged and skipped, the
    same way _int_env swallows an unparseable tuning knob: one typo in site #4
    must not stop the other six from posting. Callers get whatever parsed.
    """
    log = logging.getLogger(__name__)
    out = []
    try:
        names = sorted(n for n in os.listdir(directory) if n.endswith(".json"))
    except OSError:  # directory absent on a fresh checkout — not an error
        return out

    for name in names:
        if name == "example.json":  # the documented template, not a site
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError) as exc:
            log.error("newsroom site %s skipped — unreadable: %s", name, exc)
            continue
        if not isinstance(raw, dict):
            log.error("newsroom site %s skipped — not a JSON object", name)
            continue

        missing = [k for k in ("name", "wp_base", "chat_id") if not str(raw.get(k, "")).strip()]
        if missing:
            log.error("newsroom site %s skipped — missing %s", name, ", ".join(missing))
            continue

        # deepcopy, not {**_SITE_DEFAULTS, **raw}: the defaults hold lists, and
        # a plain merge would hand every site the SAME list object — one site
        # mutating its emoji pool would silently change another channel's.
        site = {**copy.deepcopy(_SITE_DEFAULTS), **raw}
        if not site["enabled"]:
            log.info("newsroom site %s disabled", site["name"])
            continue
        # wp_base is joined with "/posts" — a trailing slash would double it.
        site["wp_base"] = str(site["wp_base"]).rstrip("/")
        out.append(site)
    return out


NR_SITES = _load_sites(NR_SITES_DIR)
