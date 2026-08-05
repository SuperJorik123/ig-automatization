# Photos from links, reactions for branded posts, manual reaction amounts

Design approved in conversation on 2026-08-04. Four independent features in
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
channel). The operator wants exact, per-emoji amounts when they choose to.

**Fix.** The ask opens on a **mode screen** with two buttons (plus Skip):
**🎲 Random** and **✏️ Manual**. One ask per post covers all its channels;
the existing "Per-channel…" navigation stays available in both modes.

- **Random** → the emoji checkbox screen exactly as today → Apply → each
  selected reaction ordered with the existing fresh 10–40 roll.
- **Manual** → the same emoji screen, but tapping an emoji immediately
  sends a **ForceReply** prompt ("How many ❤️?"). The operator types a
  number; the ask redraws with the amount on the button (`❤️ ×100`).
  They tap the next emoji, type its amount, … and press **Done** when
  finished — every order then uses its typed amount. Tapping an already-
  selected emoji deselects it and clears its amount.

State and mechanics:

- `state["amode"]`: absent = mode not chosen yet (ask renders the mode
  screen) · `"random"` · `"manual"`. Old asks in SQLite lack the key but
  are already past the mode screen; their verbs keep meaning random —
  behavior unchanged.
- `state["qty"]`: `{channel index (str) → {emoji index (str) → amount}}`,
  manual mode only. In "all" mode a typed amount is written to every
  channel at once (same rule as emoji toggles), so switching to
  per-channel mode carries the amounts over as starting points.
- `reduce`: toggling an emoji in manual mode returns a new action
  (`"ask_qty"`, with the emoji index) instead of just redrawing — the
  caller sends the ForceReply prompt. A new `set_qty` transition writes a
  typed amount (clamped to 1..100000) into the state. In manual mode the
  apply button reads **Done** and requires every selected emoji to have an
  amount.
- New reply handler in `news_bot`: replies to the ForceReply prompt are
  routed to the pending (ask id, emoji) via an in-memory map, parsed as a
  positive int, persisted to the ask state in SQLite, and the ask redraws;
  the prompt message is deleted. Non-numeric replies re-prompt. A bot
  restart orphans an open prompt — tapping the emoji again re-prompts, and
  everything already typed is safe in SQLite.
- Orders: `order_emoji` gains an optional `qty` param; `apply_orders`
  passes the per-emoji amount in manual mode, else the existing
  `emoji_quantity()` roll.

## 4. Weekly control-group cleanup

**Requirement.** Every week at 04:00 local time, all messages in the
**control group** are deleted — including open pickers and unanswered
reaction asks (those asks are closed as skipped). Destination channels are
never touched.

**Constraint.** The Bot API cannot list chat history, so the bot can only
delete what it has recorded: every message it *sees* in the control group
and every message it *sends* there gets its id written to a new SQLite
table (`queue_store`), and the weekly job deletes exactly those ids.
Messages posted while the bot was down are invisible to it and survive.

- Tracking: incoming control-group updates are recorded in the message
  handlers; outgoing messages are recorded via a small `track(msg)` helper
  wrapped around every control-group send site in `news_bot`.
- The job: a `news_bot` JobQueue weekly job (Mondays 04:00 local). It
  closes any open asks as skipped, deletes the tracked messages in batches
  of 100 (`bot.delete_messages`), tolerates per-message failures (already
  deleted, too old for the bot's rights), and clears the table.
- The bot needs the **"Delete messages" admin right** in the control group
  to remove the operator's own messages; without it only the bot's own
  recent (<48 h) messages are deletable — the job logs what it couldn't
  delete and moves on.

## Testing

Offline as always (no Telegram / BulkFollows / network):

- `reduce`/`orders_from`/`render` unit tests for the mode screen, the
  manual flow (toggle → `ask_qty` action → `set_qty` → Done), amount
  carry-over into per-channel mode, backward compatibility of `amode`-less
  states, and the manual-post (`item_id=0`) rendering.
- `download_photos` tested with a mocked `subprocess.run` (files staged on
  disk by the test), covering the 10-file cap, caption extraction, cookie
  flag presence/absence, and rename-onto-stub behavior.
- `_download_any` chain order tested with monkeypatched downloader
  functions.
- Cleanup: message-id tracking and the batch-delete/ask-closing logic
  tested against a stub bot object; the weekly schedule itself is a thin
  JobQueue registration.

## Out of scope

- Web dashboard / Telegram Mini App (separate future brainstorm).
- Branding photos (gate stays video-only).
- Preset amount buttons (25/50/100/250) — superseded by the per-emoji
  manual flow.
