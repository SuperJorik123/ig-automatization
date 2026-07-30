# Telegram module

| File | What it is |
| ---- | ---------- |
| `telegram_bot.py` | The original **IG trigger bot** — paste an IG/Twitter URL in a group, it downloads + reposts to Instagram. Unrelated to everything below. |
| **News broadcaster** (`news_bot.py`) | Post news into your control group → pick which of your channels get it → it posts, translated per channel language. Also **hosts the autopilot**. |
| **The pipeline** | `collector.py` → `queue_store.py` → `smart_filter.py` → `dispatcher.py` (YouTube) + `autopilot.py` (Telegram). |

```
collector.py   source channels ──▶ SQLite queue (status=new)
dispatcher.py  queue ──▶ smart_filter.evaluate ──▶ score + regions
                                  └─▶ smart_filter.youtube_targets ──▶ Shorts upload
news_bot.py    manual picker (you drive it)
               autopilot job ──▶ telegram_targets ──▶ best_of ──▶ channels
                                 (region match)      (the pick)  └─▶ reaction ask
```

**One filter, many platforms.** `smart_filter.py` is the single decision layer:

- `evaluate()` — the brain. Scores a story 0–100 and tags its regions when it
  arrives (today a demo: one OpenRouter call via `scorer.py`).
- `telegram_targets()` / `youtube_targets()` — per-platform routing rules.
- `best_of()` — the head-to-head choice made **at publish time**, when the slot
  is actually being filled.

Replacing the brain upgrades every platform at once; nothing else changes.
Twitter and Instagram routes plug in by adding a `*_targets` function and a
consumer.

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

   The region drives **autopilot** routing only (manual posting ignores it).
   Omit it — `@news_eu:en` — and the channel becomes a catch-all that receives
   every story over the score threshold.

5. **Translation**: set `OPENROUTER_API_KEY` (openrouter.ai). Channels whose
   `lang` equals `SOURCE_LANG` are posted untranslated.

### Running

    py modules/telegram/news_bot.py

Then post something in the control group and use the picker.

### Notes & current limits

- **Caption limit** — Telegram caps media captions at 1024 chars. Longer
  captions are trimmed (with a `…` and a log line) rather than failing the
  channel, so an unattended autopilot post always ships.
- **Picker state is in-memory** — restarting the bot expires open *pickers*
  (tapping one says "prompt expired — post the news again"). Autopilot
  **reaction asks are not affected**: they live in SQLite.
- **Don't run `news_bot.py` and `telegram_bot.py` at the same time** — they
  share `TELEGRAM_BOT_TOKEN`, and two pollers on one token conflict. Use a
  second bot token if both are ever needed at once.

---

## Autopilot (the drip)

Runs inside `news_bot.py`; the code is `autopilot.py`. Every random
`TG_DRIP_MIN_S`..`TG_DRIP_MAX_S` (**18–24 hours** by default — about one story
a day, at an hour nobody can predict):

1. Gather the **eligible stories** — carrying media (see below), score ≥
   `TG_AUTO_MIN_SCORE`, collected within `TG_MAX_AGE_H` hours, not yet posted
   to Telegram, and aimed at a region you actually run. Unscored items are
   never posted.
2. **Compare the top `TG_COMPARE_TOP` head-to-head** and pick the winner
   (`smart_filter.best_of`). Scoring judges each story alone as it arrives, so
   a day's queue clusters at 70–80 with no way to rank within it; with one slot
   a day, the finalists are weighed against each other at publish time. If the
   comparison can't run, the top score goes out — a hiccup costs you the
   comparison, never the post.
3. Send it to every matching channel (`global` stories reach all of them),
   translated per channel language.
4. Fire the per-post BulkFollows order immediately.
5. Ask you **which reactions to buy** — pick one set for all channels, or tap
   *Per-channel…* to give each channel its own.

The rhythm survives restarts: the first tick after boot waits out the
remainder of a normal gap since the last real post, so restarting the bot five
times doesn't produce five posts.

### Only media posts are news

A source post qualifies as news only if it carries **photo(s), video(s), or
both**. Text-only posts — commentary, link dumps, announcements — are rejected
by the dispatcher *before* they cost a scoring call and show up as `rejected`
in `/queue`. Set `NEWS_REQUIRE_MEDIA=0` to allow them. **Manual posting is
never affected** — the picker will post whatever you drop in the group.

If an item's media files are deleted between collection and publishing, the
item is marked `failed` rather than posted as bare text.

### What gets bought, and when

| When | Order |
| ---- | ----- |
| Immediately on publish, per channel | `BULKFOLLOWS_SERVICE_ID`, quantity random 500–5000, against the post link |
| Every 5th post to a channel | `BULKFOLLOWS_SERVICE_ID_BONUS`, quantity 10000, against the channel itself |
| When you tap **Apply** on the ask | one order per (channel × emoji), quantity random 10–40 each |

The first two fire without you; only the reactions wait for your tap. The
per-channel count is a lifetime total in SQLite — at one post a day an
in-memory counter would never survive to the 5th post. `/queue` shows where
each channel stands.

```
🎛 Reactions · item 41 · 2 channel(s)      ⚙ ─▸   🎛 1/2 · @news_eu
☑❤️ ☑👍  ☐👎 ☐💩  ☐🤡 ☐🤮  ☐🙂 ☐😃              ☑❤️ ☐👍  ☐👎 ☐💩 …
[✅ Apply to all 2 channel(s)]                    [◂ prev]  [next ▸]
[⚙ Per-channel…]   [✕ Skip]                      [✅ Apply all]  [✕ Skip]
```

An ask waits **indefinitely** — it is stored in SQLite with its post links, so
restarting the bot doesn't lose it, and `/asks` re-renders anything unanswered.
Reaction orders are placed only when you tap Apply; the per-post order has
already gone out regardless.

**It needs `dispatcher.py` running** — that's the only process that scores, so
one brain and no race over the queue. With it stopped, the drip finds no
candidates and says so.

### Commands (control group)

| Command | Effect |
| ------- | ------ |
| `/queue` | Status counts, autopilot settings, and the current front-runner (no model call — the final pick is compared at post time) |
| `/autopilot on\|off` | Pause or resume the drip without a restart |
| `/asks` | Re-render every unanswered reaction ask |
| `/next` | Run one drip immediately (the schedule is untouched) |

### Preview without posting

    py modules/telegram/autopilot.py --once --dry-run   # prints the pick, sends nothing
    py modules/telegram/autopilot.py --once             # actually posts one item

### Failure behaviour

| Situation | What happens |
| --------- | ------------ |
| One channel fails | Logged; the other channels still get the post |
| Every channel fails | Item stays eligible, `attempts` +1; 3 strikes → `failed` |
| Story targets a region you don't run | Stepped over — it never blocks the drip |
| Media file vanished from disk | Posted without it rather than failing |
| BulkFollows down | Logged only; publishing is unaffected |
| Bot restarts mid-tick | Published posts are recorded; `/asks` recovers the ask |

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

`items` holds the stories; **`posts` records each publication** as one row per
(item, platform, target). Eligibility is per platform — an item already on
YouTube is still a Telegram candidate — and `items.status` is only a coarse
overview. `asks` holds open reaction asks. `init()` migrates existing
databases in place on startup.

## Tests

    py -m pytest tests -q

Offline: region matching, the queue store against a temp DB, the reaction-ask
state machine, caption fitting, and a full autopilot tick against a stub bot.
