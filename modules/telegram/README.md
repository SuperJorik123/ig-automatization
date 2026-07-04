# Telegram module

Two independent tools live here:

| File | What it is |
| ---- | ---------- |
| `telegram_bot.py` | The original **IG trigger bot** — paste an IG/Twitter URL in a group, it downloads + reposts to Instagram. Unrelated to the news broadcaster below. |
| **News broadcaster** (`news_bot.py`) | Post news into your control group → pick which of your channels get it → it posts, translated per channel language. |

Plus the future smart-filter input: `collector.py` + `queue_store.py` watch source
channels and queue their posts (nothing consumes the queue yet — the smart
filter will).

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
   channel, then list them in `.env` as `chat_id:lang` pairs:

       TG_DESTINATIONS=@news_us1:en,@news_us2:en,@news_us3:en,@news_eu:en,@news_ru:ru

5. **Translation**: set `OPENROUTER_API_KEY` (openrouter.ai). Channels whose
   `lang` equals `SOURCE_LANG` are posted untranslated.

### Running

    py modules/telegram/news_bot.py

Then post something in the control group and use the picker.

### Notes & current limits

- **No scheduling / no queue** — the bot only posts what you drop in the group,
  when you confirm the picker. The old daily-drip mode was removed.
- **Caption limit** — Telegram caps media captions at 1024 chars; longer
  captions will fail on that channel (shows as ❌ in the status reply).
- **Picker state is in-memory** — restarting the bot expires open pickers
  (tapping one says "prompt expired — post the news again").
- **Don't run `news_bot.py` and `telegram_bot.py` at the same time** — they
  share `TELEGRAM_BOT_TOKEN`, and two pollers on one token conflict. Use a
  second bot token if both are ever needed at once.

---

## Source collector (future smart-filter input)

`collector.py` logs in as *you* (Telethon — bots can't read channels they don't
own), watches `TG_SOURCES`, dedups, downloads media, and writes each post into
the SQLite queue (`queue_store.py`, `data/news.db`). Nothing drains the queue
today; the planned smart filter (importance scoring + regional routing) will.

Setup: get `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` from https://my.telegram.org,
list `TG_SOURCES` in `.env`, then run the first login interactively:

    py modules/telegram/collector.py

This creates `modules/telegram/data/collector.session`; later runs are
non-interactive. `data/` (SQLite, media, `.session`) stays git-ignored — the
`.session` file is a login credential.
