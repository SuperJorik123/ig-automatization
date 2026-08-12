# WordPress → Telegram News Bot (client) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Branch:** `client/wp-newsbot`, cut from `master` at `7c2daf6`. **This branch never merges back.** It is a separate product that happens to share an ancestor: the client bot's runtime flow is WordPress → Telegram → BulkFollows, with no Instagram, no YouTube, no Twitter, no phone, no web UI.

**Goal:** For each of 6-7 WordPress site ↔ Telegram channel pairs, watch the site for newly published articles, rewrite each into a Telegram post, append the article link, publish to that site's channel, and place the BulkFollows orders (per-post views, per-channel bonus every 5th post, random reactions).

---

## Design

### The flow

```
poll site N (WP REST)  →  unseen article?  →  store as `new`
                                                   ↓
                                    rewrite: article → TG text (one LLM call)
                                                   ↓
                                    + featured image/video + article link
                                                   ↓
                                    send to channel N → store message_id + t.me link
                                                   ↓
                             phase 1: views order, qty random 500-5000
                                                   ↓
                             bump channel counter; every 5th → bonus order
                             (10 000, against the CHANNEL link — the panel's
                              "last 5 posts" service takes a channel URL)
                                                   ↓
                             after NR_REACTION_DELAY_S: random emoji orders
```

### Confirmed decisions

| Question | Answer | Consequence |
| --- | --- | --- |
| Trigger | **Website, not Instagram** | Steps 1-3 of the client's workflow are human. The bot has no IG leg at all. |
| Sites | 6-7 WordPress, **1 site ↔ 1 channel** | Config-driven; adding site #8 is a JSON file, not a code change. |
| Language | **All the same** | No translation leg. `translator.py` is NOT carried over. |
| Phase 2 views | **Same as the existing bot** — one 10 000 order against the *channel* link every 5th post | `reactions.order_channel_threshold` is already correct; copy as-is. |
| BulkFollows | **Separate key** for the client | Own `NR_BULKFOLLOWS_API_KEY`, own balance. |

### What is copied from `master`'s code

| Source | Destination | Treatment |
| --- | --- | --- |
| `modules/telegram/smm.py` | `modules/newsroom/smm.py` | Verbatim, except `config.BULKFOLLOWS_*` → `config.NR_BULKFOLLOWS_*`. |
| `modules/telegram/reactions.py` lines 45-158 (catalogue + orders) | `modules/newsroom/orders.py` | Copy; thread per-site config instead of module globals. |
| `modules/telegram/reactions.py` lines 166-291 (the ask) | — | **Not copied.** Commented-out marker only (see Task 8). |
| `modules/telegram/publisher.py` `_ref`/`_fit`/`_send` | `modules/newsroom/publish.py` | Copy; drop the `shorts_format` import, inline a small ffprobe helper. |
| `modules/telegram/translator.py` | `modules/newsroom/rewrite.py` | Same OpenRouter client + never-raises contract; new system prompt. |
| `modules/telegram/queue_store.py` | `modules/newsroom/store.py` | Shape only (`init()` migrates in place, `bump_channel_posts`). New schema. |

Genuinely new code: `wp.py` (the poller) and the site-config loader. Everything else is copy-and-adapt.

### Nothing is deleted

Per the operator's instruction: every existing module stays on disk, untouched and importable. The client flow is a **new entrypoint** (`modules/newsroom/main.py`) — the existing entrypoints (`news_bot.py`, `collector.py`, `dispatcher.py`, `upload_post.py`, `server.py`, `ui/`) are simply not started on this branch. Where a copied module drops a capability that may return (the reactions ask, the translation leg, video probing), the code is **commented out in place with a `# DORMANT:` marker and a one-line note on what re-enabling costs** — not removed. Task 8 covers this.

### Tech stack

Python 3.11+, python-telegram-bot v20 (JobQueue), `requests` (WP REST + BulkFollows), `openai` SDK against OpenRouter, sqlite3 (stdlib), pytest. No ffmpeg dependency unless video posts turn out to need probing (Task 5).

---

## Global Constraints

- Python launcher is `py`, **never** `python` (multi-install PATH gotcha on this machine).
- Shell is PowerShell 5.1 — no `&&`; chain with `;` or separate commands.
- All tests run offline: no Telegram, OpenRouter, WordPress or BulkFollows calls. `py -m pytest tests -q`.
- **Pre-existing failures:** 3 tests in `tests/test_autopilot.py` fail because the operator's real `.env` sets `TG_FIRST_TICK=21:00`. Not caused by this work — "suite green" means no NEW failures.
- **`NR_DRY_RUN` defaults to `1`.** An unconfigured deployment must not be able to publish to 7 live client channels. Turning it off is a deliberate act.
- **Never start a bot instance.** One `getUpdates` poller per token; the client bot uses `NR_BOT_TOKEN`, which must be a *different* bot from `TELEGRAM_BOT_TOKEN`/`NEWS_BOT_TOKEN`.
- **Own database.** `data/newsroom.db`, never `modules/telegram/data/news.db`.
- Emoji glyphs must not go into log strings (Windows console can't encode them) — log the emoji's `name` field, as `reactions.py` already does.
- Never print `.env` values — key names only, API keys masked (`smm._mask`).
- New env keys default to safe values so nothing starts unexpectedly.
- Comment style: comments state constraints the code can't show; match the repo's prose-heavy docstring style.

---

### Task 1: Branch scaffolding, config, and site definitions

**Files:**
- Create: `modules/newsroom/__init__.py`
- Create: `modules/newsroom/sites/example.json`
- Modify: `shared/config.py` (new `NR_*` block at the end)
- Modify: `.env.example`
- Test: `tests/test_newsroom_config.py` (new)

**Interfaces:**
- Produces: `config.NR_SITES: list[dict]` — one entry per site, loaded from `modules/newsroom/sites/*.json`; `config.NR_BOT_TOKEN`, `NR_BULKFOLLOWS_API_KEY`, `NR_BULKFOLLOWS_API_URL`, `NR_POLL_S`, `NR_DRY_RUN`, `NR_REACTION_DELAY_S`, `NR_DATA_DIR`, `NR_BACKFILL`.

- [ ] **Step 1: Site config shape**

Create `modules/newsroom/sites/example.json`:

```json
{
  "name": "example",
  "wp_base": "https://example.com/wp-json/wp/v2",
  "chat_id": "@example_channel",
  "enabled": true,
  "views_phase1": [500, 5000],
  "service_views": "",
  "service_bonus": "",
  "emoji_pool": ["heart", "like", "grinning", "positive"],
  "emoji_count": [2, 4],
  "emoji_quantity": [10, 40],
  "rewrite_hint": ""
}
```

Every knob that could plausibly differ per client channel is per site, **including the BulkFollows service ids** — the client may want a different views service on a channel with different geo targeting, and retrofitting that later means touching every order call.

`emoji_pool` is deliberately explicit rather than "all of `EMOJI_SERVICES`": 💩/🤡/🤮 landing on a serious news post is a client-facing mistake, and the default must be opt-in.

The real site files are committed alongside (they are configuration, not secrets — all keys live in `.env`). If the client's channel list must stay out of git, add `modules/newsroom/sites/*.json` to `.gitignore` with `!example.json` and document it in `START.md` instead.

- [ ] **Step 2: Config block**

Append to `shared/config.py`, after the autopilot block:

```python
# --------------------------------------------------------------------------- #
# Client newsroom bot: WordPress -> Telegram (modules/newsroom)               #
# --------------------------------------------------------------------------- #
```

with, in order: `NR_DATA_DIR` (`<root>/modules/newsroom/data`), `NR_BOT_TOKEN`, `NR_DRY_RUN` (default **on**), `NR_POLL_S` (default 300), `NR_REACTION_DELAY_S` (default 1200), `NR_BACKFILL` (default **off**), `NR_BULKFOLLOWS_API_KEY`, `NR_BULKFOLLOWS_API_URL`, `NR_REWRITE_MODEL` (falls back to `TRANSLATE_MODEL`), and `NR_SITES` via a `_load_sites()` helper.

`_load_sites()` reads every `*.json` under `modules/newsroom/sites/` except `example.json`, skips `enabled: false`, and fills defaults for missing keys. It must **never raise on a malformed file** — log the filename and skip it, the same way `_int_env` swallows a bad tuning knob. One typo in site #4 must not stop the other six from posting.

- [ ] **Step 3: Tests**

`tests/test_newsroom_config.py`: `_load_sites` on a tmp dir — defaults filled, `enabled: false` skipped, malformed JSON skipped not raised, `example.json` ignored.

Run: `py -m pytest tests/test_newsroom_config.py -q`

---

### Task 2: The store

**Files:**
- Create: `modules/newsroom/store.py`
- Test: `tests/test_newsroom_store.py`

**Interfaces:**
- `init()`, `seen_ids(site) -> set[int]`, `add_article(...) -> int|None`, `pending(site) -> list[Row]`, `mark(article_id, status)`, `record_post(article_id, site, chat_id, message_id, link) -> int`, `bump_channel_posts(chat_id) -> int`, `record_order(post_id, kind, service, quantity, result)`, `recent_posts(chat_id, n)`.

- [ ] **Step 1: Schema**

```sql
articles(id, site, wp_id, url, title, body, media_url, media_type,
         published_at, seen_at, status)          -- UNIQUE(site, wp_id)
posts(id, article_id, site, chat_id, message_id, link, posted_at)
orders(id, post_id, kind, service, quantity, panel_order, error, placed_at)
counters(chat_id PRIMARY KEY, n)
```

`status`: `new` → `posted` | `failed` | `skipped`.

Two things this schema exists to guarantee:

1. **`UNIQUE(site, wp_id)` is the duplicate guard**, keyed on WordPress's own post id — not the URL (slugs get edited), not the title (republished posts reuse them). Re-inserting a seen article is a no-op, so a poll that overlaps a previous poll is free.
2. **`orders` is written before the panel call and updated after.** `smm.place_order` never raises by design, which means a failed order is otherwise invisible; this table is both the crash-recovery record and the replay list when the panel is down.

- [ ] **Step 2: `init()` migrates in place**

Same contract as `queue_store.init()` — `CREATE TABLE IF NOT EXISTS` plus additive `ALTER TABLE` guarded by a column check. Safe on every startup.

- [ ] **Step 3: Tests**

Point `config.NR_DATA_DIR` at `tmp_path`. Cover: double-insert of the same `(site, wp_id)` returns None the second time; `bump_channel_posts` counts per chat_id independently (**the every-5th trigger must not leak across the 7 channels** — this is the single easiest thing to get wrong here); `init()` twice is a no-op.

---

### Task 3: The WordPress poller

**Files:**
- Create: `modules/newsroom/wp.py`
- Test: `tests/test_newsroom_wp.py`

**Interfaces:**
- `fetch_recent(site: dict, limit: int = 20) -> list[dict]` — normalised `{wp_id, url, title, body, media_url, media_type, published_at}`. Raises `RuntimeError` on HTTP/parse failure (caller logs and skips that site this tick).

- [ ] **Step 1: The request**

```
GET {wp_base}/posts?per_page=20&orderby=date&order=desc&status=publish&_embed
```

`_embed` returns the featured media in the same response — no second request per article.

**Do not use `?after=<watermark>`.** WordPress allows backdated posts, and scheduled posts appear at their scheduled time; either can land *behind* a date watermark and be skipped forever. Fetching the last 20 every tick and filtering against `seen_ids()` cannot miss anything and costs one request.

- [ ] **Step 2: Normalisation**

- `title.rendered` and `excerpt.rendered` / `content.rendered` arrive as **HTML with entities** (`&#8217;`, `<p>`, `&nbsp;`). Strip tags and unescape entities before the text ever reaches the LLM or Telegram.
- Featured media: `_embedded["wp:featuredmedia"][0]["source_url"]`, with `media_type` from the mime type. **Every level of that path can be missing** — a post with no featured image is normal, not an error; it becomes a text-only Telegram post.
- `date_gmt` is the timestamp to store (`date` is site-local and ambiguous).

- [ ] **Step 3: Tests**

Mock `requests.get` with a recorded WP payload fixture. Cover: entity/tag stripping, missing `wp:featuredmedia` at each level, video mime type detection, HTTP 500 → `RuntimeError`, non-JSON body → `RuntimeError`.

---

### Task 4: The rewrite

**Files:**
- Create: `modules/newsroom/rewrite.py`
- Test: `tests/test_newsroom_rewrite.py`

**Interfaces:**
- `to_telegram(article: dict, site: dict) -> str` — the post body, link **not** appended (Task 5 owns length-fitting). Returns a deterministic fallback (title + first paragraph) when the API key is missing or the call fails — never raises, exactly like `translator.translate`.

- [ ] **Step 1: The prompt**

Built on `translator.py`'s structure. Constraints to encode: FAITHFUL (no fact not in the source — this is the client's reputation), length target ~600 chars so the post still fits Telegram's 1024-char *caption* limit once the link is appended, a strong lede, no invented quotes, no hashtag invention, plain text only (no Markdown — Telegram parse modes and LLM output are a bad mix). `site["rewrite_hint"]` is appended for per-channel tone.

- [ ] **Step 2: The client**

Copy `translator.py`'s module-level `OpenAI(base_url="https://openrouter.ai/api/v1")` construction and its `APIError` handling verbatim. `temperature=0.3`.

- [ ] **Step 3: The eyeball harness**

`py modules/newsroom/rewrite.py --sample <site> [-n 10]` prints the last N articles' source next to the generated post. **Build this in Task 4, not later.** Everything else in this plan is deterministic and unit-testable; "article → good Telegram post" is taste work that needs 50 outputs reviewed side by side, and it is the only part of this project whose calendar time is measured in weeks rather than hours.

- [ ] **Step 4: Tests**

Mock the OpenAI client. Cover: fallback when `OPENROUTER_API_KEY` is unset, fallback on `APIError`, empty completion falls back, `rewrite_hint` reaches the system prompt.

---

### Task 5: Publish

**Files:**
- Create: `modules/newsroom/publish.py`
- Test: `tests/test_newsroom_publish.py`

**Interfaces:**
- `async publish(bot, site, article, text) -> tuple[int|None, str|None]` — `(message_id, t.me link)`.

- [ ] **Step 1: Copy the sending core**

`_ref`, `_fit`, and the single-media branch of `_send` from `publisher.py`. The album branch is **commented out with a `# DORMANT:` marker** — WP featured media is one item, but a future gallery-post feature would want it back.

`_vid_kwargs`'s ffprobe call is likewise commented out: without width/height/duration Telegram lays the inline player out from defaults and plays the video visibly squashed, so if the client's sites post video this must be re-enabled — and that is the moment the branch grows an ffmpeg dependency. **Verify early whether these sites publish video at all**; if they are photo-only, this stays dormant and the deployment stays dependency-free.

- [ ] **Step 2: Media by URL**

Telegram accepts a **remote URL** for `photo=`/`video=` — the featured-media URL can be passed straight through with no download step, which is why this bot needs no media directory and no MTProto big-file path. Fall back to a text-only send when Telegram rejects the URL (it will, occasionally, for large files or slow origins); a post without its image still ships.

- [ ] **Step 3: Append the link and fit**

Append `\n\n🔗 {article.url}` **after** the rewrite, then `_fit(..., has_media=bool(media))`. Order matters: `_fit` truncates the tail, so appending afterwards would push the link back over the limit — and a post whose link got truncated fails step 6 of the client's flow silently.

- [ ] **Step 4: `NR_DRY_RUN`**

When set, log the exact text, chat_id and media URL, return `(None, None)`, and place no orders. This is the mode the operator reviews output in for the first week.

- [ ] **Step 5: Tests**

Fake bot object recording calls. Cover: link appended before trimming; caption over 1024 with media is trimmed and the link survives; no media → `send_message` with the 4096 limit; dry-run sends nothing; `message.link` absent → `(id, None)`.

---

### Task 6: Orders

**Files:**
- Create: `modules/newsroom/smm.py` (copied verbatim, `NR_` config)
- Create: `modules/newsroom/orders.py`
- Test: `tests/test_newsroom_orders.py`

**Interfaces:**
- `random_emojis(site) -> list[dict]`, `order_post(site, post_id, link)`, `order_channel_threshold(site, post_id, link)`, `order_emoji(site, post_id, link, emoji)`, `after_publish(site, post_id, chat_id, link)`.

- [ ] **Step 1: Copy the catalogue and order wrappers**

`EMOJI_SERVICES`, `face`, `channel_link`, `order_post`, `order_emoji`, `order_channel_threshold`, and the `record_posts` counter logic from `reactions.py` lines 45-158.

**The per-site refactor is the substance of this task.** Every one of those functions currently reads `config.BULKFOLLOWS_SERVICE_ID` from a module global — a single-tenant assumption. Each gains a `site: dict` first parameter and reads `site["service_views"]` / `site["service_bonus"]` / `site["views_phase1"]`. Do this **while copying, not after**: retrofitting a global out of working code is how site A's views get ordered against site B's channel, and that failure is silent — the panel accepts it and the wrong client's channel grows.

`channel_link` derives the channel URL by dropping the message id off the post link. It is the only thing that yields a usable URL for a numeric `-100…` chat id, so **the bonus order depends on the post link having been captured** — a post with no public link can be counted but not ordered against.

- [ ] **Step 2: Random emoji selection**

```python
def random_emojis(site: dict) -> list[dict]:
    """The reactions to buy for one post. Sampled from the site's own pool —
    never from the full catalogue, which contains 💩/🤡/🤮."""
    pool = [e for e in EMOJI_SERVICES if e["name"] in site["emoji_pool"]]
    k = min(random.randint(*site["emoji_count"]), len(pool))
    return random.sample(pool, k) if pool else []
```

The ask state machine that used to choose these is **not** copied — a `# DORMANT:` note in `orders.py` points at `master`'s `reactions.py` for the day the client wants manual control.

- [ ] **Step 3: Every order writes an `orders` row**

Before the call (`error` NULL, `panel_order` NULL), updated after. A crash between publish and order leaves a row that says so.

- [ ] **Step 4: Tests**

Mock `smm.place_order`. Cover: phase-1 quantity inside `views_phase1`; the 5th call for a chat_id fires the bonus and the 4th does not; two chat_ids count independently; `random_emojis` never returns an emoji outside the pool and never exceeds the pool size; an emoji pool naming an unknown service yields no order.

---

### Task 7: The bot

**Files:**
- Create: `modules/newsroom/main.py`
- Test: `tests/test_newsroom_main.py` (pure helpers only)

- [ ] **Step 1: Entrypoint**

Standard 3×-`dirname` `sys.path` bootstrap, `Application.builder().token(config.NR_BOT_TOKEN)`, `store.init()`, one JobQueue `run_repeating` job per enabled site with a staggered `first=` (7 sites all firing on the same second is a needless burst against both WP and the panel).

No command handlers, no `getUpdates` need — but PTB's `Application` polls anyway, so the token must still be exclusive to this bot.

- [ ] **Step 2: The tick**

Per site: `wp.fetch_recent` → insert unseen → for each `pending` article, oldest first: `rewrite.to_telegram` → `publish.publish` → `store.record_post` → `orders.after_publish` → `mark(posted)`. Any exception marks the article `failed` and moves to the next — **one bad article must never wedge a site's queue**.

- [ ] **Step 3: The backfill guard**

On the **first tick for a site** (no rows in `articles` for it), insert the fetched articles as `skipped` and post nothing, unless `NR_BACKFILL=1`. Without this, adding site #8 dumps 20 back-articles into a live client channel in one burst — unrecoverable, and visible to the client's subscribers.

- [ ] **Step 4: Reaction delay**

Schedule the emoji orders `NR_REACTION_DELAY_S` (default 1200 s) after publish via `run_once`. Reactions appearing the same second as the post is the most legible bot tell there is. The delay is best-effort and in-memory: if the process restarts inside the window those reactions are lost, which is acceptable — the post and its views already shipped. (Persisting them is what `master`'s `queue_store` delayed-order table does, if it ever matters.)

- [ ] **Step 5: CLI**

`py modules/newsroom/main.py --once [--dry-run] [--site <name>]` — one tick, no scheduler. This is how the operator verifies a new site before enabling it.

---

### Task 8: Dormant-code markers and branch documentation

**Files:**
- Modify: `modules/newsroom/publish.py`, `modules/newsroom/orders.py` (markers)
- Create: `modules/newsroom/README.md`
- Modify: `CLAUDE.md` (branch section)
- Modify: `START.md`

- [ ] **Step 1: `# DORMANT:` convention**

Every capability dropped from a copied module is commented out in place, never deleted, in one shape:

```python
# DORMANT: album send. WP featured media is a single item, so the client flow
# never builds a media group. Re-enable together with a gallery-post feature —
# the caption must ride on the first item only.
# group = []
# for i, m in enumerate(media):
#     ...
```

The marker states **what it did and what re-enabling costs**, so the next reader does not have to reconstruct the reasoning from `master`.

Inventory: album send (`publish.py`), `_vid_kwargs` ffprobe (`publish.py`), the reactions ask (`orders.py`, pointer to `master`), the translation leg (`rewrite.py`, pointer to `translator.py`).

- [ ] **Step 2: Dormant entrypoints**

Nothing on `master` is modified. `news_bot.py`, `collector.py`, `dispatcher.py`, `upload_post.py`, `server.py` and `ui/` remain fully functional and are simply **not started** on this branch. `modules/newsroom/README.md` states this explicitly with the list, so a future reader does not assume the tree is dead code.

- [ ] **Step 3: Branch section in `CLAUDE.md`**

At the top of `CLAUDE.md`, a short block: this branch is the client bot, it never merges to `master`, its flow is WP → TG only, its entrypoint is `modules/newsroom/main.py`, its DB is `data/newsroom.db`, its token is `NR_BOT_TOKEN`, and everything else in the tree is inherited and dormant. Add the `modules/newsroom/` table rows in the existing Files-table style.

- [ ] **Step 4: Run instructions in `START.md`**

Install, `.env` keys, adding a site, the dry-run first week, and the `--once --site` verification step.

---

### Task 9: Deployment

- [ ] **Step 1: Isolation checklist**

Separate checkout (or `git worktree`) of `client/wp-newsbot`, separate venv, separate `.env`, separate service. The failure this prevents: a `git pull` for the operator's own work restarting a client's 7 live channels, or a dev run publishing real articles.

- [ ] **Step 2: Service**

Windows: NSSM or Task Scheduler on `py modules/newsroom/main.py`. Linux: a systemd unit with `Restart=always`. Log to a rotating file — the BulkFollows request/response lines are the audit trail for money spent.

- [ ] **Step 3: First-run sequence**

1. `NR_DRY_RUN=1`, one site enabled, `--once` → review the rewrite output.
2. Repeat until the prompt is right (expect days, not minutes).
3. `NR_DRY_RUN=0` on one site, watch one real post and its panel orders end to end.
4. Enable the remaining sites one at a time; each gets its own backfill-guarded first tick.

---

## Open items

- **Do the client's sites publish video?** Decides whether `_vid_kwargs`/ffprobe comes out of dormancy and whether the deployment needs ffmpeg. Check before Task 5.
- **Are all 7 WP REST APIs publicly readable?** Some hosts disable `/wp-json` or require an application password. One `curl` per site settles it; a blocked API changes Task 3 into a scraping task and roughly doubles it.
- **Confirm the two BulkFollows service ids** with one manual order each on the client's key before Task 6. If the views service rejects a `t.me/c/…` URL for private channels, the schema and the link-capture step change.
