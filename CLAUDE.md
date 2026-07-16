# IG Automatization — project guide

PC-side queue manager + Android driver for posting photos and scrolling reels on Instagram. Posts live in `posts/` as `NNN.jpg` + `NNN.json` (caption + hashtags); the Angular UI in `ui/` queues new ones; `modules/instagram/upload_post.py` drives IG over ADB via uiautomator2. Code is organised per platform under `modules/` (`instagram`, `telegram`, `twitter`, `youtube`, `facebook`), with cross-cutting pieces in `shared/` and the web layer (`server.py`, `ui/`) + media queue (`posts/`) at the repo root.

## Layout

Per-platform modules under `modules/`, cross-cutting code under `shared/`, the web layer (`server.py`, `ui/`) and the media queue (`posts/`) at the repo root:

```
modules/
  instagram/   upload_post.py, main.py, dump_ui.py, human_swipe.py, analyze_swipes.py, visualize_swipes.py
  telegram/    telegram_bot.py (IG trigger), news_bot.py (manual broadcaster + YouTube picker), collector.py, scorer.py, dispatcher.py, publisher.py, translator.py, queue_store.py
  twitter/     poster.py — Tweepy video+photo posting (v1.1 media upload + v2 tweet); download-from-X lives in shared/reel_downloader.py
  youtube/     uploader.py (Data API v3 upload, per-account OAuth under credentials/youtube/), publisher.py (translate-per-channel fan-out)
  facebook/    poster.py (stub) — nothing implemented yet
shared/
  config.py            .env loading + PHONE_ADDRESS / POSTS_DIR / credentials
  reel_downloader.py   yt-dlp downloader (Instagram + Twitter/X URLs)
server.py    FastAPI backend (root)
ui/          Angular frontend (root)
posts/       shared media queue (root); posts/posted/ holds archives
```

Every entrypoint puts the repo root on `sys.path` (the scripts via a 3×-`dirname` bootstrap at the top of the file; `server.py` via its existing `sys.path.insert`) so `from shared import …` / `from modules.instagram import …` resolve whether the file is run directly (`py modules/instagram/upload_post.py`) or imported.

## Files

| File | Purpose |
| ---- | ------- |
| `server.py` | FastAPI backend on `:8000` (repo root). Receives uploads from the UI, writes them into `posts/`, optionally fires `modules.instagram.upload_post`. Also accepts a `url` form field — when an Instagram URL is pasted, calls `reel_downloader.download_media(url, stub, kind=post_type)` first instead of expecting a multipart file. The stub picks `.mp4` for reels / `.jpg` for posts; yt-dlp overrides the extension with the real one. CORS open to `http://localhost:4200`. |
| `shared/config.py` | Loads the repo-root `.env` and exposes `DEVICE_ID` (from `PHONE_ADDRESS`, USB-serial fallback), `POSTS_DIR`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `IG_ACCOUNTS`. Every entrypoint imports it so `.env` and `posts/` resolve to the root no matter which subfolder the file lives in. |
| `shared/reel_downloader.py` | Fetches Instagram/Twitter media by URL via `yt-dlp` (no token / login / Selenium required — pulls the file directly from `fbcdn.net`). Exposes `is_instagram_url(s)`, `is_twitter_url(s)`, `download_media(url, dest_path, kind="reel"\|"post")`; `download_reel` is kept as a backwards-compat alias. **Limitation**: IG gates many photo posts behind a login — yt-dlp surfaces this as `Instagram sent an empty media response`. Reels are reliable, photo posts are best-effort. |
| `modules/instagram/upload_post.py` | Main poster. Pushes media to phone, drives IG's UI through the new flow (see below), archives to `posts/posted/`. Re-exports `DEVICE_ID` from `shared.config` so callers keep using `ig.DEVICE_ID`. |
| `modules/instagram/main.py` | Reels scrolling loop. Uses `human_swipe.py` for realistic swipes. **Do not touch when working on the post flow.** |
| `modules/instagram/human_swipe.py` | Empirical swipe model fit to a real trace; powers `main.py`. |
| `modules/instagram/dump_ui.py` | Connects to the test phone and dumps the current IG screen to `ui_dump.xml`. Use whenever a selector breaks. |
| `modules/instagram/analyze_swipes.py` / `visualize_swipes.py` | Offline analysis for the swipe model — not used at runtime. |
| `modules/telegram/telegram_bot.py` | Telegram bot: scans a configured group for IG/Twitter URLs, downloads via `shared/reel_downloader.py`, queues into `posts/`, and posts to IG via `modules.instagram.upload_post` for each selected account (`IG_ACCOUNTS`). |
| `modules/twitter/poster.py` | Twitter/X poster (ported from the news-bot project). `post_media(file_path, caption, account_name)` detects video vs photo by extension: videos go through Tweepy v1.1 `chunked_upload` + async-processing poll, photos through `media_upload`; the tweet itself is created with the v2 Client. Credentials: five `TWITTER_<ACCOUNT>_*` vars per account in `.env` (account name uppercased, non-alphanumerics → `_`; app needs Read+Write). Keeps a back-compat `upload_post(d, ...)` wrapper (ignores `d`). CLI: `py modules/twitter/poster.py clip.mp4 --caption "..." --account name`. |
| `modules/youtube/uploader.py` | YouTube Shorts uploader (ported from the news-bot project). `upload_short(file_path, title, description, account_name, privacy_status)` does a resumable Data API v3 upload; vertical videos ≤3 min are auto-classified as Shorts. OAuth per account: `credentials/youtube/<account>/client_secrets.json` + `token.pickle` (git-ignored). First run per account must be interactive — it opens a browser for consent and will hang headless. Quota: 1600 units/upload of a 10,000/day default budget per Google Cloud project (~6 uploads/day) — one project per account. CLI: `py modules/youtube/uploader.py clip.mp4 --title "..." --account name`. |
| `modules/youtube/publisher.py` | YouTube twin of the telegram publisher. `publish_shorts(video_path, caption, dests)` translates the caption per channel language (reuses `modules/telegram/translator`, cached per lang), splits it (first line → title ≤100 chars, full caption → description) and uploads to each `YT_DESTINATIONS` channel. Blocking — async callers use `asyncio.to_thread`. Returns `(posted, errors)`. |
| `modules/telegram/news_bot.py` | Manual broadcaster: post text/photo/video/album in the control group → inline picker of `TG_DESTINATIONS` channels **plus `YT_DESTINATIONS` YouTube channels when the post has a video** → caption translated per destination → Telegram fan-out and/or Shorts upload. Manual YouTube posting is limited to videos ≤20 MB (Bot API `get_file` cap); bigger ones get a clear error. |
| `modules/telegram/dispatcher.py` | Smart-filter dispatcher: polls the SQLite queue for `new` items, scores each via `scorer.py` (OpenRouter, 0-100 + regions), and auto-uploads scored-`>= YT_AUTO_MIN_SCORE` (default 70) items **with video** to every `YT_DESTINATIONS` channel via `modules/youtube/publisher`, then `mark_posted('youtube')`. Everything else goes to `status='queued'` with score recorded for future platforms. Scoring failures stay `new` and retry. Run alongside `collector.py`. |
| `modules/facebook/poster.py` | Posting **stub** mirroring the IG `upload_post(...)` signature; raises `NotImplementedError`. |
| `swipe_stats.json` / `swipes.txt` | Calibration data feeding `human_swipe.py` (generated by `analyze_swipes.py`; live alongside it in `modules/instagram/`). |
| `ui/` | Angular frontend (`ng serve` on `:4200`, proxies `/api/*` → `:8000`). |
| `posts/` | Shared queue at the repo root. `posts/posted/` holds archives (move-on-success). |

## Running the system

Install Python deps (first time):

    py -m pip install -r requirements.txt

Backend (one terminal):

    py -m uvicorn server:app --reload --port 8000

UI (another terminal):

    cd ui
    npm start

Telegram bot (optional, another terminal — needs `.env` credentials):

    py modules/telegram/telegram_bot.py

News pipeline (each in its own terminal — needs `.env` credentials):

    py modules/telegram/news_bot.py     # manual broadcaster: control group → TG channels + YouTube Shorts
    py modules/telegram/collector.py    # collects source-channel posts into the SQLite queue (first run: phone-code login)
    py modules/telegram/dispatcher.py   # smart filter: scores queued items, auto-uploads >=YT_AUTO_MIN_SCORE videos to YouTube

Post manually (skip the UI, uses the next queued image):

    py modules/instagram/upload_post.py

Dump the current IG screen for selector hunting:

    py modules/instagram/dump_ui.py
    # → writes ui_dump.xml; grep it for 'Create', 'Photo', etc.

## Test device

Galaxy S23, ADB ID `R5CX235CF9A`. Resolved once in `shared/config.py` (`DEVICE_ID` from `PHONE_ADDRESS` in `.env`, falling back to the USB serial `R5CX235CF9A`) and re-exported by `modules/instagram/upload_post.py` as `ig.DEVICE_ID`. The swipe model in `human_swipe.py` is calibrated for 1080×2340 — other geometries need a recalibration.

## Current IG creation flow

`modules.instagram.upload_post.upload_post(d, image_path, caption_body, hashtags, kind=...)` drives the new creation UI end to end. `kind` is `"post"` (photo, default) or `"reel"` (video); the only divergence is which row gets tapped in step 4 — the rest of the flow is assumed identical and we fix selectors as IG diverges.

1. Open IG (`app_start`).
2. Tap **Profile** (bottom-right tab) — `tap_profile_tab`.
3. Tap **+** at the top of the profile screen — `tap_create_button`.
4. Tap **Post** or **Reel** in the bottom-sheet picker, per `kind`. Row order: reel / edits / post / story / highlights / live / AI / Ad — `tap_post_type`.
5. Tap the **latest thumbnail** in the gallery (auto-points at the freshly-pushed file) — `tap_latest_gallery_item`. Selectors cover both `Photo` and `Video` content-descriptions.
6. Tap **Next** after the gallery — `tap_next` (covers `text="Next"` and `description="Next"`, position-agnostic).
7. **Post only**: tap **Next** again on the filter/edit screen. Reels skip this step and land directly on the composer.
8. Tap the caption field (placeholder "Add a caption...") and inject the full caption (body + blank line + space-separated `#tags`) in one shot via uiautomator2's FastInput IME — see `inject_text`.
9. If the caption contained hashtags, the last `#tag` typically triggers IG's autocomplete strip — which overlays Share and blocks the tap. `tap_first_hashtag_suggestion` polls `dump_hierarchy()` for the row's resource-id (`row_hashtag_textview_tag_name`) and taps inside the live bounds (selector RPC silently returns count=0 for this strip on the S23, so we read XML directly). The call is **best-effort** — if the strip never appears within ~6 s we just log and continue.
10. **Dismiss the soft keyboard first** (`hide_keyboard_if_shown`) — on the reel composer Share sits in the right half of the footer at y≈2100 and the Samsung keyboard overlaps it; without this step the synthetic tap lands on the keyboard and IG never sees it. Then tap **Share**.
11. Verify the publish by dumping the hierarchy and looking for `resource-id="com.instagram.android:id/share_button"`. If it's still there the post didn't ship and we raise (`ui_dump_share_fail.xml` for inspection). Note: don't check for the caption hint text — `Write a caption…` / `Add a caption…` is the field's `hint=` attribute and stays in the XML even after the caption is filled, so it false-fires.

On success, `main()` calls `archive_post(...)` to move the image + JSON sidecar from `posts/` to `posts/posted/` so it isn't re-uploaded.

### Why FastInput IME instead of the system clipboard

On modern Samsung / OneUI, `d.set_clipboard(text)` raises `java.lang.SecurityException: Package android does not belong to 2000` — AppOpsManager won't let the shell UID claim the `android` package to call `ClipboardManager.setPrimaryClip`. We sidestep the clipboard entirely: FastInput IME (shipped inside `uiautomator2`) is activated, `send_keys` broadcasts the text to it, the IME injects it into the focused field instantly. The previous IME is restored on the way out so the user's normal keyboard isn't left swapped.

### Side notes / TODOs

- **Audio for posts/reels.** The new IG flow may surface an audio/music picker on some post types (reels, possibly some post variants). When we add **reel** or **carousel** support, remember to handle — or explicitly skip — the audio step on the way through. The current photo-post flow does not seem to include it, but verify with `modules/instagram/dump_ui.py` after step 5 if a music screen appears.
- **Carousel.** Will also start by tapping **Post** in the creation sheet, then multi-select on the gallery screen. Not implemented yet.
- **Selector hunting.** Content-description (`description=`, `descriptionContains=`) and visible text (`text=`) are far more stable across IG versions than resource IDs. When a selector breaks, start there before reaching for `resourceId=`.
- **Python launcher.** Use `py`, never `python` — multi-install PATH gotcha on this machine.
