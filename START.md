# Run

**First-time setup:** `py -m pip install -r requirements.txt` then `cd ui && npm install`.

**Backend** (terminal 1): `py -m uvicorn server:app --reload --port 8000`
**Frontend** (terminal 2): `cd ui && npm start` — open http://localhost:4200.
**Telegram bot** (terminal 3, optional): `py telegram_bot.py` — needs `.env` with `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`. Pastes IG reel URLs into the configured group → bot downloads, strips `#mirnews`, posts to IG.

**Phone:** Galaxy S23 (ADB ID `R5CX235CF9A`) plugged in via USB with USB debugging enabled.

**Post manually** (skip the UI, takes next image in `posts/`): `py upload_post.py`
**Dump current IG screen** for selector hunting: `py dump_ui.py`

## How the Telegram bot works

One-time setup: create a bot via `@BotFather` → `/newbot`, then `/setprivacy` → **Disable** so the bot sees plain group messages (not just `/commands`). Add it to your group, grab the group's numeric chat ID from `https://api.telegram.org/bot<TOKEN>/getUpdates`, and put both values in `.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).

Once `py telegram_bot.py` is running, every message in the configured group is scanned for an Instagram or Twitter/X URL (`SUPPORTED_URL_RE` in `reel_downloader.py` — matches `instagram.com/{reel,reels,p,tv}/…`, `twitter.com/…/status/…`, and `x.com/…/status/…`). On a hit:
1. **Download** the video via `yt-dlp` — returns the `.mp4` + the source caption. For Twitter, the trailing `https://t.co/…` redirect that Twitter auto-appends to tweet bodies is stripped so it doesn't leak onto IG.
2. **Strip** every `#mirnews` from the caption (case-insensitive, word-bounded; `#mirnews1` survives).
3. **Queue** the file into `posts/NNN.mp4` with a `posts/NNN.json` sidecar holding the cleaned caption.
4. **Publish** by calling `upload_post.upload_post(kind="reel")` — drives the phone through Profile → + → Reel → latest thumbnail → Next → caption paste → Share, then `archive_post` moves the file into `posts/posted/`.
5. **Reply** in the Telegram group: `✅ posted as NNN.mp4` on success, `❌ failed: <error>` on failure.

Concurrency: a single `asyncio.Lock` serialises uploads (the phone can only do one IG flow at a time); blocking download + uiautomator2 work runs in a worker thread via `asyncio.to_thread` so the bot stays responsive to new messages. No dedup — pasting the same URL twice posts it twice.

See `CLAUDE.md` for full architecture and the IG creation flow walkthrough.
