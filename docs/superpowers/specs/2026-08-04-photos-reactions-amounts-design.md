# Photos from links, reactions for branded posts, manual reaction amounts

Design approved in conversation on 2026-08-04. Three independent features in
the news bot, one spec because they ship together as one mini-project. The
web-UI / Mini App direction was explicitly parked as a separate future
project and is out of scope here.

## 1. Photo downloads from IG / Twitter links

**Problem.** The URL flow (`news_bot._handle_url`) is yt-dlp-based, and
yt-dlp is a video tool: an Instagram photo post usually dies with
"Instagram sent an empty media response" (IG gates photos behind login) and
a photo-only tweet has no video for it to find. The operator sees a download
error where they expect a photo post.

**Fix.** A photo fallback via **gallery-dl** (new dependency in
`requirements.txt`), which handles IG and Twitter/X photo posts and can read
a logged-in Instagram session from the operator's browser.

- `shared/reel_downloader.py` gains `download_photos(url, dest_stub) ->
  (paths, caption)`, shelling out to the `gallery-dl` CLI:
  - downloads into a per-call temp dir next to `dest_stub`, then renames the
    files onto `dest_stub`-derived names (`<stub>_1.jpg`, …) so the caller's
    cleanup conventions (`manual_url_*` sweep) keep working;
  - caps at **10 files** (Telegram's album limit); extra carousel items are
    dropped with a log line;
  - caption comes from gallery-dl's metadata (`--write-info-json`, the
    post's description field), best-effort — empty string when absent;
  - cookies: when `GALLERY_DL_COOKIES_BROWSER` is set in `.env` (e.g.
    `chrome`), pass `--cookies-from-browser <value>`; unset = anonymous.
    Used only by this fallback, so the IG account sees light traffic.
- `news_bot._download_any` chain becomes: yt-dlp reel → yt-dlp post →
  gallery-dl photos. When every stage fails, the **first yt-dlp error** is
  still the one raised (it names the real problem; the fallbacks usually
  just repeat it).
- Multi-photo result: the picker state gets one media item per photo, and
  the existing album path (`publisher._send` → `send_media_group`) posts
  them as one album. Photos never hit the brand-it gate (nothing to brand)
  — unchanged behavior.
- `shared/config.py` exposes `GALLERY_DL_COOKIES_BROWSER` (default `""`).

## 2. Reactions ask after a branded publish

**Problem.** `news_bot._do_publish` fires the BulkFollows per-post order for
each brand→Telegram publish but passes no reactions and never opens the
"🎛 Reactions" ask — the operator can't order reactions on branded posts.

**Fix.** After the publish loop, collect every successful Telegram publish
as `(chat_id, link)` across all brand pairs. If any exist, open **one**
reactions ask over those channels — the same SQLite-backed ask the
autopilot uses (`reactions.new_state` + `queue_store` ask row + `render`),
so it survives restarts and supports the existing per-channel mode.

- Manual/branded posts have no queue item: the ask is stored with
  `item_id = 0`, and `reactions.render` / `reactions.summary` show
  "manual post" instead of "item 0".
- The per-post base order (`record_posts`) keeps firing at publish time as
  today; the ask only adds the emoji orders, exactly like autopilot asks.
- YouTube / X publishes are unaffected.
- The manual (non-branded) picker keeps its existing pick-before-posting
  emoji flow — no change.

## 3. Random / manual reaction amounts in the ask

**Problem.** Reaction order quantity is always random (10–40, rolled per
channel). The operator wants to choose an exact amount.

**Fix.** One new row in the ask keyboard: **🎲 Random · 25 · 50 · 100 ·
250 · ✏️ Custom**, with the active choice marked. The amount is part of the
ask's persisted state, and the state machine is shared, so autopilot asks
gain the feature automatically.

- State: `state["qty"]` — absent/`None` = random (old asks in SQLite lack
  the key and therefore keep behaving as before), else a positive int
  applied to **every** selected reaction on every channel.
- `reduce` verbs: `qr` (random), `q25`/`q50`/`q100`/`q250` (presets) —
  redraw; `qc` returns a new action `"custom"`: the caller sends a
  **ForceReply** prompt ("Send the amount:") tied to the ask id, so the
  operator's reply box opens automatically.
- New reply handler in `news_bot`: a reply to that prompt parses a positive
  int (clamped to 1..100000), writes it into the ask state in SQLite,
  redraws the ask, and deletes the prompt. Non-numeric replies re-prompt.
- Orders: `order_emoji` gains an optional `qty` param;
  `apply_orders` passes `state["qty"]` when set, else the existing
  `emoji_quantity()` roll per order.

## Testing

Offline as always (no Telegram / BulkFollows / network):

- `reduce`/`orders_from`/`render` unit tests for the qty verbs, the custom
  action, backward compatibility of qty-less states, and the manual-post
  (`item_id=0`) rendering.
- `download_photos` tested with a mocked `subprocess.run` (files staged on
  disk by the test), covering the 10-file cap, caption extraction, cookie
  flag presence/absence, and rename-onto-stub behavior.
- `_download_any` chain order tested with monkeypatched downloader
  functions.

## Out of scope

- Web dashboard / Telegram Mini App (separate future brainstorm).
- Branding photos (gate stays video-only).
- Per-emoji or per-channel amounts — one amount per ask.
