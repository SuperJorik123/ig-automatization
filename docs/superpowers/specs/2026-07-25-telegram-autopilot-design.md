# Telegram autopilot + shared smart filter

**Date:** 2026-07-25
**Status:** approved, ready for implementation

## Goal

The Telegram side runs itself: collected news is scored once, and the best
unposted story is dripped to the channels whose region it matters to, at
unpredictable intervals. Every published post immediately triggers its
BulkFollows order; the operator is then asked, per channel, which reactions to
buy. Manual posting through the existing picker keeps working untouched.

## The one-smart-filter decision

There is already a smart filter in the repo — `scorer.py` returns
`{score, regions}` with nothing platform-specific in it, and `queue_store`
persists both. The only platform knowledge lives in one line of
`dispatcher.py` (`score >= YT_AUTO_MIN_SCORE and has_video -> YouTube`).

So Telegram becomes a **second consumer of the existing filter**, not a second
filter. The routing rule is lifted out of the dispatcher into a new
`smart_filter.py` that both platforms call. When the real smart filter is
built, `evaluate()` is replaced and both routes inherit it.

```
smart_filter.is_news(item)                 -> bool   # media required
smart_filter.evaluate(text, source_hint) -> {"score", "regions", "tier"} | None
smart_filter.telegram_targets(item, dests) -> [dest, ...]
smart_filter.youtube_targets(item, dests)  -> [dest, ...]
smart_filter.best_of(pairs, top)           -> (item, targets)
```

`is_news` is the cheapest gate and runs first: a source post with no photo or
video is commentary, not news. The dispatcher rejects those before they cost a
scoring call (`NEWS_REQUIRE_MEDIA=0` disables the rule). Manual posting is
never subject to it.

`evaluate` is the brain (today: one OpenRouter call via `scorer.py`).
The `*_targets` functions are per-platform policy. They are kept apart because
"is this story important" and "does @news_ru want it" change on different
schedules.

`best_of` is the second half of the filter, and it runs at **publish** time.
Scoring judges each story alone, the moment it arrives, so a day's queue
clusters at 70-80 with no way to rank inside that cluster. With roughly one
slot a day, the finalists are compared head-to-head (`scorer.compare`, one
call) when the slot is actually being filled. A failed comparison falls back
to the top score — it costs the comparison, never the post.

## Module map

```
NEW  modules/telegram/smart_filter.py   decision layer (above)
NEW  modules/telegram/autopilot.py      the drip: select -> publish -> order -> open ask
NEW  modules/telegram/reactions.py      emoji catalogue, ask keyboard, order application

MOD  modules/telegram/queue_store.py    + posts table, + asks table, + selection queries
MOD  modules/telegram/news_bot.py       registers the autopilot job + ask callbacks + commands
MOD  modules/telegram/dispatcher.py     routes via smart_filter, records posts via record_post
MOD  shared/config.py                   region field in TG_DESTINATIONS + autopilot vars

--   collector.py, publisher.py, translator.py, smm.py, scorer.py   unchanged
```

`publisher.publish` already translates per language, isolates per-channel
failures and returns the public `t.me/...` links. `smm.place_order` already
never raises. The autopilot composes both as-is.

## Runtime

Three processes, as today:

```
collector.py   sources            -> queue (status=new)
dispatcher.py  smart_filter       -> score + regions -> YouTube route
news_bot.py    manual picker (unchanged) + autopilot job (new) + ask callbacks
```

The dispatcher stays the only process that *scores*: one writer, no races. The
autopilot consumes already-scored items only. With the dispatcher stopped, the
autopilot goes quiet and logs why rather than publishing unscored news.

The autopilot lives inside `news_bot.py` as a `JobQueue` job because the emoji
ask needs callback handling, and one bot token allows exactly one poller.

## Configuration

`TG_DESTINATIONS` gains an optional third field:

```
TG_DESTINATIONS=@news_us1:en:us,@news_eu:en:eu,@news_ru:ru:ru,@news_world:en:us+eu
```

- `chat_id:lang:region` — `+` separates regions for a multi-region channel.
- A two-field entry (`chat_id:lang`) parses exactly as today and becomes a
  **catch-all**: it matches every item over the threshold.
- Parsed into `{"chat_id", "lang", "regions": set()}`. `regions` empty = catch-all.

New environment variables:

| Var | Default | Meaning |
| --- | ------- | ------- |
| `NEWS_REQUIRE_MEDIA` | `1` | Only posts with photo(s)/video(s) count as news |
| `TG_AUTO_MIN_SCORE` | `60` | Minimum score to be eligible for an autopilot post |
| `TG_DRIP_MIN_S` | `64800` | Shortest gap between drips (18 h) |
| `TG_DRIP_MAX_S` | `86400` | Longest gap between drips (24 h) |
| `TG_MAX_AGE_H` | `48` | Items older than this are never auto-posted |
| `TG_COMPARE_TOP` | `5` | Finalists compared head-to-head at publish time (`1` disables) |
| `TG_ASK_CHAT_ID` | `TELEGRAM_CHAT_ID` | Where the reaction ask is sent |
| `TG_AUTOPILOT` | `1` | `0` starts the bot with the drip paused |

**`TG_MAX_AGE_H` must exceed `TG_DRIP_MAX_S` in hours.** A freshness window
shorter than the gap between ticks means everything collected since the last
post has already expired when the next one fires, and the drip starves.

## Data model

`init()` creates the new tables idempotently; existing rows are untouched.

```sql
CREATE TABLE posts(              -- one row per (item, platform, target)
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id   INTEGER NOT NULL,
    platform  TEXT NOT NULL,     -- 'telegram' | 'youtube'
    target    TEXT NOT NULL,     -- chat_id or YouTube account
    link      TEXT,              -- public t.me/... URL when Telegram gave one
    posted_at TEXT NOT NULL);
CREATE UNIQUE INDEX idx_posts_unique ON posts(item_id, platform, target);

CREATE TABLE asks(               -- one reaction ask per published story
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    INTEGER NOT NULL,
    chat_id    TEXT,             -- where the ask message lives
    message_id INTEGER,          -- NULL until the message is sent
    state      TEXT NOT NULL,    -- JSON, see below
    status     TEXT NOT NULL,    -- 'open' | 'applied' | 'skipped'
    created_at TEXT NOT NULL);
```

```sql
CREATE TABLE channel_counters(    -- lifetime posts per channel
    target TEXT PRIMARY KEY,
    posts  INTEGER NOT NULL DEFAULT 0);
```

`items` gains `attempts INTEGER DEFAULT 0` (added by `init()` via a guarded
`ALTER TABLE`, so existing databases migrate in place).

`channel_counters` drives the every-5th-post BulkFollows channel order. It has
to be durable: at roughly one post a day, an in-memory counter would need five
days of unbroken uptime to reach the threshold, and any restart would zero it.
The count is monotonic and callers test `n % 5`, so nothing is lost mid-cycle.
Both autopilot and manual posts increment it — the SMM panel doesn't care
which filled the channel.

`posts` becomes the source of truth for "where has this gone". The existing
`posted` JSON column is still stamped by `record_post` so `counts()` and the
coarse `status` display keep working, but no query reads it for routing.

This fixes a live bug: `mark_posted` sets `status='posted'`, so an item that
went to YouTube would be invisible to the Telegram route. Eligibility is now
per-platform.

### Selection query

```sql
SELECT * FROM items
 WHERE score IS NOT NULL
   AND score >= :min_score
   AND status NOT IN ('failed', 'rejected')
   AND collected_at >= :age_cutoff
   AND NOT EXISTS (SELECT 1 FROM posts
                    WHERE posts.item_id = items.id AND posts.platform = :platform)
 ORDER BY score DESC, collected_at DESC
 LIMIT :limit
```

The autopilot takes the first candidate that has at least one matching
channel; a top-scoring item aimed only at regions you don't run does not block
the drip.

## The drip

A self-rescheduling `JobQueue` job (`run_once` with a fresh random delay each
time, so the gap is genuinely random rather than a fixed period). The first
tick after a restart is derived from the last recorded post
(`startup_delay()`), so a daily cadence doesn't fire once per restart:

```
tick:
  if not autopilot_enabled:        reschedule; return
  candidates = queue_store.candidates("telegram", min_score, max_age_h, limit=10)
  pairs      = [(item, targets) for each candidate with a matching channel]
  item, targets = smart_filter.best_of(pairs)     # head-to-head, top TG_COMPARE_TOP
  if none:                         reschedule (short retry); return

  posted, errors, links = await publisher.publish(bot, item.text, item.media, targets)
  for chat_id in posted:           queue_store.record_post(item.id, 'telegram', chat_id, link)
  if not posted:                   queue_store.bump_attempts(item.id)   # 3 strikes -> 'failed'
  to_thread: one BulkFollows per-post order per link (autopilot's own _on_post)
  ask_id = queue_store.open_ask(item.id, links)
  send the ask message; queue_store.bind_ask(ask_id, chat_id, message_id)
  reschedule random(TG_DRIP_MIN_S, TG_DRIP_MAX_S)
```

Media from the collector is `{"path", "type"}`; `publisher._ref` already
accepts a local path, so nothing new is needed for photos, videos or albums.

Captions are truncated after translation to Telegram's limits (1024 with
media, 4096 without) instead of failing that channel — an unattended post
should ship, and the truncation is logged.

The 5-post-per-channel bonus order that `news_bot` already applies to manual
posts also applies to autopilot posts: both call the same
`reactions.record_channel_post` helper so the counter is shared.

## The reaction ask

One message per story, sent to `TG_ASK_CHAT_ID` after the fan-out.

```
🎛 Reactions · item 41 · 2 channels
☑❤️ ☑👍  ☐👎 ☐💩  ☐🤡 ☐🤮  ☐🙂 ☐😃
[✅ Apply to all 2 channels]
[⚙ Per-channel…]   [✕ Skip]
```

`⚙ Per-channel…` switches the same message to a one-channel-at-a-time view,
carrying the current selection over as the starting point for each channel:

```
🎛 1/2 · @news_eu · t.me/news_eu/412
☑❤️ ☐👍  ☐👎 ☐💩  ☐🤡 ☐🤮  ☐🙂 ☐😃
[◂ prev]  [next ▸]
[✅ Apply all]   [✕ Skip]
```

State (SQLite `asks.state`, so a restart loses nothing):

```json
{"mode": "all" | "per",
 "cur": 0,
 "chans": [{"chat_id": "@news_eu", "link": "https://t.me/news_eu/412"}, ...],
 "sel": {"0": [0, 1], "1": [0]}}      // channel index -> emoji indices
```

`callback_data` is `r:<ask_id>:<verb>[:<arg>]` — well inside Telegram's 64
bytes, and it carries everything needed, so taps survive a bot restart.
Verbs: `t<i>` toggle, `all` apply-to-all, `pc` per-channel mode, `nx`/`pv`
navigate, `ap` apply, `sk` skip.

Applying places one order per (channel, emoji) via `smm.place_order` in a
worker thread, then edits the message into a summary. An ask stays `open`
forever until answered; `/asks` re-renders any open ask (including one whose
message never sent because the bot died mid-tick).

The emoji catalogue and the ordering helpers move from `news_bot.py` into
`reactions.py`, shared by the manual picker and the autopilot ask. The manual
picker's pre-post emoji selection is unchanged.

## Operator commands

| Command | Effect |
| ------- | ------ |
| `/queue` | Status counts, and what the next drip would pick |
| `/autopilot` `on`\|`off` | Pause/resume the drip at runtime (also reports state) |
| `/asks` | Re-render every open reaction ask |
| `/next` | Run one drip tick immediately, without disturbing the schedule |

## Error handling

| Failure | Behaviour |
| ------- | --------- |
| Item unscored | Not selectable; the dispatcher scores it later |
| Head-to-head comparison fails | Top-scoring finalist is posted instead |
| One channel fails to publish | Logged, other channels proceed (publisher semantics) |
| Every channel fails | No `posts` rows, `attempts` bumped; 3 strikes → `status='failed'` |
| Caption over the Telegram cap | Truncated after translation, logged |
| Translation fails | `translator` already falls back to the original text |
| BulkFollows order fails | Logged only; a panel outage never affects publishing |
| Ask message fails to send | Order already placed; ask row stays `open`, recoverable via `/asks` |
| Bot restarts | Job reschedules on startup; open asks answerable from `callback_data` |

## Testing

The repo has no test setup, so this adds `pytest` and a `tests/` directory
covering the parts that are pure logic and need no network:

- `test_smart_filter.py` — region matching (exact, multi-region, catch-all,
  `global` items, no match), threshold behaviour.
- `test_queue_store.py` — against a temp DB: candidate selection honours
  score/age/platform, an item posted to YouTube stays eligible for Telegram,
  `record_post` is idempotent, ask state round-trips.
- `test_reactions.py` — the ask state reducer as a pure function: toggling,
  mode switching, navigation, and which (channel, emoji) orders an apply
  produces.
- `test_config.py` — `TG_DESTINATIONS` parsing, including two-field
  backwards compatibility and `+` multi-region.

Plus a manual smoke path: `py modules/telegram/autopilot.py --once --dry-run`
prints the item and channels it *would* post to, and exits without sending.

## Out of scope

Semantic (reworded) dedup, per-channel drip rhythms, auto-defaulting the
reactions on a timeout, Twitter/Instagram routes through the shared filter,
and any change to the manual picker's own emoji flow.
