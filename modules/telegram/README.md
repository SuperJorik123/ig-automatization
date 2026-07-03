# Telegram module

Two independent tools live here:

| File | What it is |
| ---- | ---------- |
| `telegram_bot.py` | The original **IG trigger bot** — paste an IG/Twitter URL in a group, it downloads + reposts to Instagram. Unrelated to the news aggregator below. |
| **News aggregator** (`collector.py` + `news_bot.py`) | Collects posts from other Telegram channels and republishes them to your own news channels — a configurable **N posts/day (or everything)**, plus **manual posts on demand**, translated per channel language. |

---

## News aggregator

### How it works

Telegram splits the job across two identities, because a **bot can't read other
channels' history** — only a **user account** (MTProto/Telethon) can.

```
AUTO (N/day — configurable via POSTS_PER_DAY, or "everything"):
  @source1 ┐
  @source2 ┼─▶ collector.py ───▶ SQLite queue ───▶ news_bot daily job ─┬─▶ @news_en
  @sourceN ┘  (Telethon USER)   (data/news.db)     (translate + fan-out) ├─▶ @news_ru
              downloads media                                            └─▶ @news_..

MANUAL (on demand):
  you ──DM──▶ news_bot.py (BOT) ──▶ translate per language ──▶ all @news_* immediately
```

- **`collector.py`** — logs in as *you* (Telethon), watches `TG_SOURCES`, dedups,
  downloads media, and writes each post to the queue. Long-running.
- **`news_bot.py`** — the bot. Handles your DMs (manual broadcast) and runs the
  daily job that pops the oldest queued post. Long-running.
- **`translator.py`** — translates each caption into a destination's language via
  Claude. Failure falls back to posting the original text (never blocks).
- **`queue_store.py`** — the SQLite queue + per-source cursor.
- **`publisher.py`** — translate-per-language + fan-out, shared by both paths.

Collected posts are reposted **as your own** (media downloaded + re-uploaded, no
"Forwarded from"). Manual posts reuse Telegram's `file_id`, so no download.

### One-time setup

1. **Install deps** (from the repo root):

       py -m pip install -r requirements.txt

2. **MTProto credentials** — go to https://my.telegram.org → *API development
   tools* → create an app → copy `api_id` + `api_hash` into `.env`
   (`TELEGRAM_API_ID`, `TELEGRAM_API_HASH`).

3. **Fill in `.env`** (see `.env.example` for the full list):
   `TG_OPERATOR_ID`, `TG_SOURCES`, `TG_DESTINATIONS`, `SOURCE_LANG`, `POSTS_PER_DAY`,
   `DAILY_TIME`, `TIMEZONE`, `ANTHROPIC_API_KEY`. Reuses the existing `TELEGRAM_BOT_TOKEN`.

4. **Add the bot as an admin** in every destination channel (with "Post
   messages" permission). Make sure your user account is a **member** of every
   private source channel.

5. **First login for the collector** (interactive — enter the code Telegram
   sends you). Run it yourself in a terminal:

       py modules/telegram/collector.py

   This creates `modules/telegram/data/collector.session`; later runs are
   non-interactive.

### Running

Two terminals (both long-running):

    py modules/telegram/collector.py      # fills the queue from sources
    py modules/telegram/news_bot.py       # daily drip + manual DMs

- **Manual post:** DM the bot text and/or one photo/video → it broadcasts to all
  destinations immediately (translated per channel). Only `TG_OPERATOR_ID` works.
- **Check the queue:** send `/queue` to the bot.

### Notes & current limits

- **Post rate** — `POSTS_PER_DAY` in `.env`: a number throttles the daily drip
  (`1` = once a day at `DAILY_TIME`); `false` posts **everything** continuously
  (queue flushed ~every 60s; `DAILY_TIME`/`TIMEZONE` are ignored in this mode).
- **Translation model** defaults to `claude-opus-4-8`; set
  `TRANSLATE_MODEL=claude-haiku-4-5` in `.env` for a cheaper/faster option.
- **Manual media** is one photo/video per DM (albums via DM are not handled yet;
  collected albums *are* handled).
- **No web dashboard** — sources/destinations are configured in `.env`. Adding a
  channel = edit `TG_DESTINATIONS` and restart `news_bot.py`.
- `data/` (SQLite, media, `.session`) should stay git-ignored — the `.session`
  file is a login credential.
