# Run

**First-time setup:** `py -m pip install -r requirements.txt` then `cd ui && npm install`.

**Backend** (terminal 1): `py -m uvicorn server:app --reload --port 8000`
**Frontend** (terminal 2): `cd ui && npm start` — open http://localhost:4200.
**Telegram bot** (terminal 3, optional): `py modules/telegram/telegram_bot.py` — needs `.env` with `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`. Pastes IG reel URLs into the configured group → bot downloads, strips `#mirnews`, posts to IG.

**Phone:** Galaxy S23 (ADB ID `R5CX235CF9A`) plugged in via USB with USB debugging enabled.

**Post manually** (skip the UI, takes next image in `posts/`): `py modules/instagram/upload_post.py`
**Dump current IG screen** for selector hunting: `py modules/instagram/dump_ui.py`

## How the Telegram bot works

One-time setup: create a bot via `@BotFather` → `/newbot`, then `/setprivacy` → **Disable** so the bot sees plain group messages (not just `/commands`). Add it to your group, grab the group's numeric chat ID from `https://api.telegram.org/bot<TOKEN>/getUpdates`, and put both values in `.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).

Once `py modules/telegram/telegram_bot.py` is running, every message in the configured group is scanned for an Instagram or Twitter/X URL (`SUPPORTED_URL_RE` in `shared/reel_downloader.py` — matches `instagram.com/{reel,reels,p,tv}/…`, `twitter.com/…/status/…`, and `x.com/…/status/…`). On a hit:
1. **Download** the video via `yt-dlp` — returns the `.mp4` + the source caption. For Twitter, the trailing `https://t.co/…` redirect that Twitter auto-appends to tweet bodies is stripped so it doesn't leak onto IG.
2. **Strip** every `#mirnews` from the caption (case-insensitive, word-bounded; `#mirnews1` survives).
3. **Queue** the file into `posts/NNN.mp4` with a `posts/NNN.json` sidecar holding the cleaned caption.
4. **Publish** by calling `modules.instagram.upload_post.upload_post(kind="reel")` — drives the phone through Profile → + → Reel → latest thumbnail → Next → caption paste → Share, then `archive_post` moves the file into `posts/posted/`.
5. **Reply** in the Telegram group: `✅ posted as NNN.mp4` on success, `❌ failed: <error>` on failure.

Concurrency: a single `asyncio.Lock` serialises uploads (the phone can only do one IG flow at a time); blocking download + uiautomator2 work runs in a worker thread via `asyncio.to_thread` so the bot stays responsive to new messages. No dedup — pasting the same URL twice posts it twice.

See `CLAUDE.md` for full architecture and the IG creation flow walkthrough.

## Client newsroom bot (`modules/newsroom/`)

A separate product in `modules/newsroom/`: watches N WordPress sites and posts
each new article to that site's Telegram channel, then buys views and
reactions. Nothing above is involved — its own bot token, database and
BulkFollows balance. Full detail in `modules/newsroom/README.md`.

**Setup:** `.env` needs `NR_BOT_TOKEN` (a *different* bot from the two above —
one getUpdates poller per token), `NR_BULKFOLLOWS_API_KEY` (the client's panel
account) and `OPENROUTER_API_KEY`. One JSON file per site under
`modules/newsroom/sites/`, copied from `example.json`. Add the bot as an admin
of every channel.

**Run:** `py modules/newsroom/main.py`

**Verify a site before enabling it:**
`py modules/newsroom/main.py --once --site <name> --dry-run`

**Tune the rewrite prompt:** `py modules/newsroom/rewrite.py --sample <name> -n 10`
— prints source next to generated post, publishes nothing. This is the part
that takes real time; everything else is deterministic.

**First run:** keep `NR_DRY_RUN=1` until you have read a week of generated
posts. A site's first live tick is backfill-guarded, so it starts from the next
article the site publishes rather than dumping its back catalogue.

**Deploy** as its own systemd service (`newsroom-bot`) from the same checkout
and venv as the operator's bots; a code push restarts it with them, and the
SQLite store guarantees nothing already posted goes out twice.
