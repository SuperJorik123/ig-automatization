"""Radar settings — read from its OWN env file, never the bots' `.env`.

The radar is a separate process with its own bot token, group and Telegram
listening account. It deliberately does not import `shared.config`: that module
loads every brand's credentials into the environment, and the radar has no
business holding a single posting token. `ENV_FILE` (default `.env.radar`)
names the file; see `deploy/radar.env.example`.
"""
import os

from dotenv import load_dotenv

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENV_FILE = os.environ.get("ENV_FILE", "").strip() or ".env.radar"
if not os.path.isabs(ENV_FILE):
    ENV_FILE = os.path.join(ROOT_DIR, ENV_FILE)
load_dotenv(ENV_FILE)


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def _int(name: str, default: int) -> int:
    try:
        return int(_str(name) or default)
    except ValueError:
        return default


def _list(name: str, default: str) -> list:
    return [s.strip() for s in _str(name, default).split(",") if s.strip()]


HERE = os.path.dirname(os.path.abspath(__file__))

# --- Telegram: the alert bot + the group it writes to ----------------------
BOT_TOKEN = _str("RADAR_BOT_TOKEN")
CHAT_ID = _int("RADAR_CHAT_ID", 0)  # a SUPERgroup (-100…) — see docs/RADAR_PLAN.md

# The NEWS BOT's control group (its TELEGRAM_CHAT_ID). 📤 Send to bot has the
# listening account post there — it must be a member. Unset = button refuses.
NEWS_BOT_CHAT_ID = _int("RADAR_NEWS_BOT_CHAT_ID", 0)

# --- Telegram: the LISTENING user account (a spare one, not the bots') ------
API_ID = _str("RADAR_API_ID") or _str("TELEGRAM_API_ID")
API_HASH = _str("RADAR_API_HASH") or _str("TELEGRAM_API_HASH")
DATA_DIR = _str("RADAR_DATA_DIR", os.path.join(HERE, "data"))
SESSION = os.path.join(DATA_DIR, "listener")
DB_PATH = os.path.join(DATA_DIR, "radar.db")
CHANNELS_FILE = _str("RADAR_CHANNELS_FILE", os.path.join(HERE, "channels.json"))
CALENDAR_FILE = _str("RADAR_CALENDAR_FILE", os.path.join(HERE, "calendar.json"))

# --- AI labelling (OpenRouter) ---------------------------------------------
OPENROUTER_API_KEY = _str("OPENROUTER_API_KEY")
LABEL_MODEL = _str("RADAR_LABEL_MODEL", "google/gemini-2.5-flash")
AI_MAX_PER_DAY = _int("RADAR_AI_MAX_PER_DAY", 400)  # 0 = no AI, rules only

# --- Alerting ---------------------------------------------------------------
# Alerts go out the moment an incident crosses the bar — any hour (owner,
# 2026-11-19). MAX_PER_HOUR is only a runaway guard; 0 = no cap.
ALERT_MIN_SCORE = _int("RADAR_ALERT_MIN_SCORE", 60)
# THE gate (owner, 2026-11-23): a video alerts only when the AI calls it a
# real event AND rates it at least AI_MIN — AI_MIN_US for one in the US. The
# bonuses in score.py only rank what passes; on 4 days of live alerts the old
# "score >= 60" let 325 through (124 the AI itself said weren't events), this
# lets 77. No AI verdict (budget, outage) = no alert.
AI_MIN = _int("RADAR_AI_MIN", 75)
AI_MIN_US = _int("RADAR_AI_MIN_US", 60)
MAX_PER_HOUR = _int("RADAR_MAX_PER_HOUR", 0)
# Categories never alerted (labels from label.CATEGORIES), e.g. "war,politics".
BLOCK_CATEGORIES = set(_list("RADAR_BLOCK_CATEGORIES", ""))
US_BONUS = _int("RADAR_US_BONUS", 10)
# Videos up to this size are copied into the group when a channel forbids
# forwarding; bigger ones are linked.
MAX_COPY_MB = _int("RADAR_MAX_COPY_MB", 50)

# --- Clustering + spikes ----------------------------------------------------
# A story keeps one alert (edited as it grows) for this long after its last
# post — 3 h split the Poland missile / Benghazi / Sinaloa stories in 10+.
CLUSTER_WINDOW_H = _int("RADAR_CLUSTER_WINDOW_H", 12)
SPIKE_WINDOW_H = _int("RADAR_SPIKE_WINDOW_H", 2)
SPIKE_RATIO = _int("RADAR_SPIKE_RATIO", 3)
SPIKE_MIN_HITS = _int("RADAR_SPIKE_MIN_HITS", 6)
SPIKE_MIN_SOURCES = _int("RADAR_SPIKE_MIN_SOURCES", 3)
SPIKE_COOLDOWN_H = _int("RADAR_SPIKE_COOLDOWN_H", 3)
SPIKE_CHECK_S = _int("RADAR_SPIKE_CHECK_S", 300)

# --- Reddit -----------------------------------------------------------------
REDDIT_SUBS = _list("RADAR_REDDIT_SUBS", ",".join([
    "PublicFreakout", "ActualPublicFreakouts", "CrazyFuckingVideos",
    "Damnthatsinteresting", "nextfuckinglevel", "WeatherGifs", "IdiotsInCars",
    "iamatotalpieceofshit", "instant_regret", "TikTokCringe", "Whatcouldgowrong",
    "CatastrophicFailure", "PoliceChases",
]))   # news / worldnews dropped 2026-11-23: article links, almost never a video
# SPARE Reddit accounts' sessions (Netscape cookies.txt) for live upvotes —
# never the news bot's credentials/reddit_cookies.txt. This file plus every
# reddit_cookies*.txt beside it (reddit_cookies_2.txt, …) is one account each;
# requests take turns across them. No file = Reddit off.
REDDIT_COOKIES_FILE = _str("RADAR_REDDIT_COOKIES_FILE",
                           os.path.join(DATA_DIR, "reddit_cookies.txt"))
if not os.path.isabs(REDDIT_COOKIES_FILE):
    REDDIT_COOKIES_FILE = os.path.join(ROOT_DIR, REDDIT_COOKIES_FILE)
# One Arctic Shift request per sub + one Reddit request per poll.
REDDIT_POLL_S = _int("RADAR_REDDIT_POLL_S", 600)
REDDIT_MAX_AGE_H = _int("RADAR_REDDIT_MAX_AGE_H", 4)
# Live upvote reads per post: at most CHECKS_MAX, RECHECK_M apart (owner,
# 2026-11-23: 3 checks, 30 min — ~400 Reddit requests a day, 24/7).
REDDIT_CHECKS_MAX = _int("RADAR_REDDIT_CHECKS_MAX", 3)
REDDIT_RECHECK_M = _int("RADAR_REDDIT_RECHECK_M", 30)
REDDIT_MIN_UPS = _int("RADAR_REDDIT_MIN_UPS", 50)

# --- Hazard feeds -----------------------------------------------------------
HAZARD_POLL_S = _int("RADAR_HAZARD_POLL_S", 300)
QUAKE_MIN_WORLD = float(_str("RADAR_QUAKE_MIN_WORLD", "6.0"))
QUAKE_MIN_US = float(_str("RADAR_QUAKE_MIN_US", "4.8"))
QUAKE_FLOOR_US = float(_str("RADAR_QUAKE_FLOOR_US", "4.0"))   # never below, felt or not
QUAKE_DEDUPE_H = _int("RADAR_QUAKE_DEDUPE_H", 3)   # one alert per quake area

# --- Evening briefing -------------------------------------------------------
TZ = _str("RADAR_TZ", "Europe/Chisinau")
BRIEFING_AT = _str("RADAR_BRIEFING_AT", "21:00")  # "" = off

# Contact string in the User-Agent the NWS and Reddit ask for — generic on
# purpose (no owner name or e-mail goes to outside services).
USER_AGENT = _str("RADAR_USER_AGENT", "news-radar/1.0")
