# Telegram module

| File | What it is |
| ---- | ---------- |
| `telegram_bot.py` | The original **IG trigger bot** — paste an IG/Twitter URL in a group, it downloads + reposts to Instagram. Unrelated to everything below. |
| **News broadcaster** (`news_bot.py`) | Post news into your control group → pick which of your channels get it → it posts, translated per channel language. Nothing publishes without your tap. |
| **The news filter** | `collector.py` → `queue_store.py` → `smart_filter.py` ← `dispatcher.py`. Collects and scores; publishes nothing. |

```
collector.py   source channels ──▶ SQLite queue (status=new)
dispatcher.py  queue ──▶ smart_filter.is_news ──▶ smart_filter.evaluate ──▶ score + regions
news_bot.py    manual picker (you drive it)
```

`smart_filter.py` is the decision layer over the queue:

- `is_news()` — the cheapest gate: a story must carry media.
- `evaluate()` — the brain. Scores a story 0–100 and tags its regions when it
  arrives (today a demo: one OpenRouter call via `scorer.py`).
- `best_of()` — a head-to-head comparison of the strongest scored stories.

The automatic posting that used to sit on top of this (the Telegram autopilot
drip and the dispatcher's YouTube auto-upload) was removed on 2026-11-19 at the
owner's request.

---

## News broadcaster

### How it works

```
you post text / photo / video / album into the control group (TELEGRAM_CHAT_ID)
  → bot replies with a channel picker (☐ per channel, All / None)
  → you tap "▶ Post to selected"
  → caption translated per channel language (OpenRouter, cached per language)
  → posted to every selected channel; per-channel ✅/❌ status edited into the prompt
```

- **`news_bot.py`** — the bot. Watches the control group, shows the picker, publishes. Long-running.
- **`publisher.py`** — translate-per-language + fan-out to the chosen destinations.
- **`translator.py`** — OpenRouter translation. Failure falls back to posting the original text (never blocks).

Albums (multiple photos/videos sent together) are buffered for ~1.5 s and
handled as one post with one picker. Media is re-sent by Telegram `file_id`,
so nothing is downloaded.

### One-time setup

1. **Install deps** (from the repo root):

       py -m pip install -r requirements.txt

2. **Create the bot** via `@BotFather` → `/newbot`, then `/setprivacy` →
   **Disable** so it sees plain group messages. Put the token in `.env`
   (`TELEGRAM_BOT_TOKEN`).

3. **Control group**: add the bot to the group where you'll drop news; put the
   group's numeric id in `.env` (`TELEGRAM_CHAT_ID` — get it from
   `https://api.telegram.org/bot<TOKEN>/getUpdates` after posting a message).

4. **Destination channels**: add the bot as **admin** (Post messages) in every
   channel, then list them in `.env` as `chat_id:lang:region` entries:

       TG_DESTINATIONS=@news_us1:en:us,@news_eu:en:eu,@news_ru:ru:ru,@news_world:en:us+eu

   The region is informational: manual posting ignores it.

5. **Translation**: set `OPENROUTER_API_KEY` (openrouter.ai). Channels whose
   `lang` equals `SOURCE_LANG` are posted untranslated.

### Running

    py modules/telegram/news_bot.py

Then post something in the control group and use the picker.

### Notes & current limits

- **Caption limit** — Telegram caps media captions at 1024 chars. Longer
  captions are trimmed (with a `…` and a log line) rather than failing the
  channel.
- **Picker state is in-memory** — restarting the bot expires open *pickers*
  (tapping one says "prompt expired — post the news again").
- **Don't run `news_bot.py` and `telegram_bot.py` at the same time** — they
  share `TELEGRAM_BOT_TOKEN`, and two pollers on one token conflict. Use a
  second bot token if both are ever needed at once.

### What gets bought, and when

| When | Order |
| ---- | ----- |
| Immediately on publish, per channel | `BULKFOLLOWS_SERVICE_ID`, quantity random 500–5000, against the post link |
| Immediately on publish, per channel × reaction you ticked on the picker | the emoji's own service, quantity random 10–40 |
| Every 5th post to a channel | `BULKFOLLOWS_SERVICE_ID_BONUS`, quantity 10000, against the channel itself |

The per-channel count is a lifetime total in SQLite, so it survives restarts.

---

## News filter (dispatcher)

`dispatcher.py` is the only process that scores, so there is one brain and no
race over the queue. A source post qualifies as news only if it carries
**photo(s), video(s), or both**; text-only posts are set to `rejected` *before*
they cost a scoring call (`NEWS_REQUIRE_MEDIA=0` allows them). Everything else
is scored once and set to `queued` with its score and regions. A scoring
failure leaves the item `new` for a retry. Nothing is published.

---

## Source collector

`collector.py` logs in as *you* (Telethon — bots can't read channels they don't
own), watches `TG_SOURCES`, dedups, downloads media, and writes each post into
the SQLite queue (`queue_store.py`, `data/news.db`).

Setup: get `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` from https://my.telegram.org,
list `TG_SOURCES` in `.env`, then run the first login interactively:

    py modules/telegram/collector.py

This creates `modules/telegram/data/collector.session`; later runs are
non-interactive. `data/` (SQLite, media, `.session`) stays git-ignored — the
`.session` file is a login credential.

### The queue

`items` holds the stories; `posts` is the per-(item, platform, target)
publication log; `items.status` is a coarse overview. `init()` migrates
existing databases in place on startup.

## Tests

    py -m pytest tests -q

Offline: the news filter, the queue store against a temp DB, caption fitting,
and the news bot's flows against stub bots.
