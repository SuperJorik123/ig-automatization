# newsroom — the client's WordPress → Telegram bot

A separate product that lives in the same checkout as the operator's bots
(it was the `client/wp-newsbot` branch until the 2026-09 merge into `master`).
It runs as its own process — `newsroom-bot.service` on the VPS — with its own
token, database and BulkFollows balance, and is monitored like the rest:
errors are emailed (tag `newsroom`), and `HEALTHCHECK_URL_NEWSROOM` is its
heartbeat.

For each configured site ↔ channel pair: watch the WordPress site for newly
published articles, rewrite each into a Telegram post, append the article link,
publish to that site's channel, and place the BulkFollows orders.

```
poll site (WP REST)  →  unseen article?  →  rewrite (one LLM call)
                                                  ↓
                                 + featured image + article link
                                                  ↓
                                 send to channel → store message_id + t.me link
                                                  ↓
                          views order (random 500-5000)
                          every 5th post → bonus order (10 000, channel link)
                          after 20 min → random reaction orders
```

## The rest of this repository is the operator's, not this bot's

Nothing outside `modules/newsroom/` is involved when this bot runs. Starting
`main.py` starts none of these:

| Entrypoint | What it is |
| --- | --- |
| `modules/telegram/news_bot.py` | the operator's manual broadcaster + autopilot |
| `modules/telegram/collector.py` / `dispatcher.py` | the news aggregator's collection and scoring |
| `modules/instagram/upload_post.py` / `main.py` | Instagram posting and reel scrolling (needs a phone) |
| `modules/youtube/`, `modules/twitter/` | Shorts and X posting |
| `server.py`, `ui/` | the FastAPI backend and Angular queue UI |

The client bot shares no token, no database and no BulkFollows balance with any
of them. Running `main.py` does not start any of them.

Where a capability was dropped from a module copied out of `modules/telegram/`,
it is commented out in place behind a `# DORMANT:` marker that says what it did
and what re-enabling costs — grep for `DORMANT` before rebuilding anything.

## Files

| File | Purpose |
| --- | --- |
| `main.py` | Entrypoint. One JobQueue job per site; `tick()` is the per-site poll and is the only place the flow is sequenced. Holds the **backfill guard**. |
| `wp.py` | WordPress REST poller. Fetches the last 20 published posts with `_embed` and filters against stored ids — **never a `?after=` watermark** (see below). `clean_html` turns `rendered` fields into prose. |
| `rewrite.py` | Article → Telegram post, one OpenRouter call. Never raises: any failure degrades to the article's own title and lede. `--sample <site>` is the prompt-tuning harness. |
| `publish.py` | Sends the post. Media goes to Telegram **as a URL**, never downloaded. `compose()` reserves the link's length before trimming. |
| `orders.py` | BulkFollows rules: views per post, bonus every 5th, random reactions. Every function takes a `site` — service ids are per site, not global. |
| `smm.py` | Panel client, a copy of `modules/telegram/smm.py` reading `NR_BULKFOLLOWS_*`. Never raises. |
| `store.py` | SQLite (`data/newsroom.db`): articles, posts, orders, counters. |
| `sites/*.json` | One file per site ↔ channel pair. `example.json` is the template and is never loaded. |

## Three decisions that look like details

**No date watermark on the poller.** `?after=<last seen>` is the obvious
optimisation and it loses articles permanently: WordPress lets an editor
backdate a post, and a scheduled post appears at its scheduled time. Either can
land *behind* a watermark that has already moved past it. Refetching the last 20
and filtering by id cannot miss anything, and the overlap is free because
`store.add_article` ignores a duplicate.

**The backfill guard.** On a site's first tick the articles found are recorded
as *seen* and none are posted. Without it, enabling a site publishes twenty
back-articles in one burst to a live channel, in front of the client's
subscribers. It keys on the site having no rows, not on the database being
empty, so adding site #8 is guarded too. Override with `NR_BACKFILL=1` only when
you mean it.

**Per-site service ids.** `modules/telegram/reactions.py` reads
`config.BULKFOLLOWS_SERVICE_ID` from a module global — correct for one operator
with one balance, wrong for seven client channels. Every order function here
takes a `site` dict instead. The failure mode of getting this wrong is silent:
the panel accepts an order pointing at the wrong channel, the log line looks
normal, and the wrong client's balance pays for it. The post counter is per
`chat_id` for the same reason.

## Running it

```
py modules/newsroom/main.py                      # the scheduler
py modules/newsroom/main.py --once               # one tick per site, then exit
py modules/newsroom/main.py --once --site acme   # one site
py modules/newsroom/main.py --once --dry-run     # publishing nothing
py modules/newsroom/main.py --force-latest --site acme   # re-post the site's newest
                                                 # article end to end, seen or not
py modules/newsroom/rewrite.py --sample acme -n 10   # tune the prompt
```

Tests (offline — no WordPress, Telegram, OpenRouter or BulkFollows calls):

```
py -m pytest tests/test_newsroom_*.py -q
```

## Adding a site

1. Copy `sites/example.json` to `sites/<name>.json` and fill in `name`,
   `wp_base` (…/wp-json/wp/v2, no trailing slash), `chat_id`, and the two
   BulkFollows service ids.
2. Add the bot as an admin of the channel.
3. `py modules/newsroom/main.py --once --site <name> --dry-run` and read the
   generated posts.
4. Restart the service. The site's first real tick is backfill-guarded, so it
   starts posting from the *next* article the site publishes.

A malformed site file is logged and skipped, never fatal — one typo costs one
channel, not the run.

## Setup checklist

- `NR_BOT_TOKEN` — a **different** bot from `TELEGRAM_BOT_TOKEN` and
  `NEWS_BOT_TOKEN`. One `getUpdates` poller per token; sharing one means
  whichever process starts second silently breaks the first.
- `NR_BULKFOLLOWS_API_KEY` — the client's panel account, not the operator's.
- `NR_EMOJI_SERVICES` — the reaction catalogue, `name:id` pairs (the emoji
  glyph itself, or `positive`/`negative` for the panel's mixed sets). Service
  ids are panel-account data and get renumbered; this is why they are not in
  code. Sites opt in to a subset by name via `emoji_pool`.
- `OPENROUTER_API_KEY` — shared with the rest of the repo; the rewrite falls
  back to the article's own lede without it.
- `NR_DRY_RUN=1` until a week of generated posts has been reviewed.
