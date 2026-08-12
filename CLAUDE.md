# IG Automatization — project guide

> ## ⚠ You are on branch `client/wp-newsbot`
>
> This branch carries a **second, unrelated product**: the client's WordPress →
> Telegram news bot, in `modules/newsroom/`. It is cut from `master` and
> **never merges back**.
>
> - **Its flow is WordPress → Telegram only.** No Instagram, no YouTube, no
>   Twitter, no phone, no web UI, no branding, no MTProto, no scoring.
> - **Entrypoint:** `py modules/newsroom/main.py`. **Database:**
>   `modules/newsroom/data/newsroom.db`. **Token:** `NR_BOT_TOKEN`. **Panel:**
>   `NR_BULKFOLLOWS_API_KEY` — a different balance from the operator's.
> - **Everything else in this tree is inherited and dormant.** It is fully
>   functional and simply not started; nothing on `master` was modified or
>   deleted. Where a capability was dropped from a module copied out of
>   `modules/telegram/`, it is commented out in place behind a `# DORMANT:`
>   marker — grep for it before rebuilding anything.
> - **Deploy it from its own checkout**, venv and service. A `git pull` for the
>   operator's own work must not be able to restart a client's seven live
>   channels.
>
> See `modules/newsroom/README.md`. The rest of this document describes the
> inherited tree and still applies to it.

PC-side queue manager + Android driver for posting photos and scrolling reels on Instagram. Posts live in `posts/` as `NNN.jpg` + `NNN.json` (caption + hashtags); the Angular UI in `ui/` queues new ones; `modules/instagram/upload_post.py` drives IG over ADB via uiautomator2. Code is organised per platform under `modules/` (`instagram`, `telegram`, `twitter`, `youtube`, `facebook`), with cross-cutting pieces in `shared/` and the web layer (`server.py`, `ui/`) + media queue (`posts/`) at the repo root.

## Layout

Per-platform modules under `modules/`, cross-cutting code under `shared/`, the web layer (`server.py`, `ui/`) and the media queue (`posts/`) at the repo root:

```
modules/
  instagram/   upload_post.py, main.py, dump_ui.py, human_swipe.py, analyze_swipes.py, visualize_swipes.py
  telegram/    telegram_bot.py (IG trigger), news_bot.py (manual broadcaster + autopilot host), collector.py, scorer.py, smart_filter.py, dispatcher.py, autopilot.py, reactions.py, publisher.py, translator.py, queue_store.py, smm.py, branded.py (brand-it flow's pure half)
  twitter/     poster.py — Tweepy video+photo posting (v1.1 media upload + v2 tweet); download-from-X lives in shared/reel_downloader.py
  youtube/     uploader.py (Data API v3 upload, per-account OAuth under credentials/youtube/), publisher.py (translate-per-channel fan-out)
  facebook/    poster.py (stub) — nothing implemented yet
shared/
  config.py            .env loading + PHONE_ADDRESS / POSTS_DIR / credentials
  reel_downloader.py   yt-dlp downloader (Instagram + Twitter/X URLs)
  branding.py          branded-clip renderer (logo + lower-third headline via ffmpeg)
server.py    FastAPI backend (root)
ui/          Angular frontend (root)
posts/       shared media queue (root); posts/posted/ holds archives
brands/      per-brand logo.png (brands/<name>/logo.png); assets/fonts/ holds the repo-shipped font used for headlines
```

Every entrypoint puts the repo root on `sys.path` (the scripts via a 3×-`dirname` bootstrap at the top of the file; `server.py` via its existing `sys.path.insert`) so `from shared import …` / `from modules.instagram import …` resolve whether the file is run directly (`py modules/instagram/upload_post.py`) or imported.

## Files

| File | Purpose |
| ---- | ------- |
| `server.py` | FastAPI backend on `:8000` (repo root). Receives uploads from the UI, writes them into `posts/`, optionally fires `modules.instagram.upload_post`. Also accepts a `url` form field — when an Instagram URL is pasted, calls `reel_downloader.download_media(url, stub, kind=post_type)` first instead of expecting a multipart file. The stub picks `.mp4` for reels / `.jpg` for posts; yt-dlp overrides the extension with the real one. CORS open to `http://localhost:4200`. |
| `shared/config.py` | Loads the repo-root `.env` and exposes `DEVICE_ID` (from `PHONE_ADDRESS`, USB-serial fallback), `POSTS_DIR`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `IG_ACCOUNTS`. Every entrypoint imports it so `.env` and `posts/` resolve to the root no matter which subfolder the file lives in. |
| `shared/reel_downloader.py` | Fetches Instagram/Twitter media by URL via `yt-dlp` (no token / login / Selenium required — pulls the file directly from `fbcdn.net`). Exposes `is_instagram_url(s)`, `is_twitter_url(s)`, `download_media(url, dest_path, kind="reel"\|"post")`; `download_reel` is kept as a backwards-compat alias. **`download_any(url, dest_stub) -> (paths, caption)` is what callers should use**: yt-dlp reel → yt-dlp post → `download_photos` (gallery-dl), re-raising the FIRST yt-dlp error when all three fail. `download_photos` shells out to `py -m gallery_dl` (the console script isn't on PATH here), renames the result onto `<stub>_1.jpg`, `<stub>_2.png`, … and caps a carousel at `MAX_PHOTOS = 10` (Telegram album limit). yt-dlp can't fetch photo-only tweets or most IG photo posts — that's what the gallery-dl leg is for; login-gated IG posts additionally need `GALLERY_DL_COOKIES_BROWSER=chrome` (or firefox/edge) in `.env` so gallery-dl reuses that browser's session. **If a public reel throws that same error**, yt-dlp is outdated (IG breaks it every few months): `py -m pip install -U yt-dlp`, then restart any running bots — a live process keeps the old module in memory. |
| `shared/branding.py` | Branded-clip renderer: one ffmpeg pass burns `brands/<name>/logo.png` (205 px wide, 82 px from the right edge, 102 px from the top) and a lower-third headline onto a 1080×1920 blur-fill canvas. All geometry is measured off the reference clips at the repo root (`example.MP4`, `newlinebug.mp4`) — see the module docstring before changing a constant. Three things are easy to get wrong: **(1)** the banner box is **fixed** (x 110, 856 px wide, via drawtext `boxw` + 4-value `boxborderw`) — it must not shrink to the headline, so rows wrap by measured **pixel** width (`wrap_to_px`, advance widths via fontTools), not character count; **(2)** rows are separated by a bare **CR** — ffmpeg 8's drawtext breaks on a LF *and draws it* as a missing-glyph box; **(3)** `strip_unrenderable` drops emoji/keycaps/invisibles the font has no glyph for (letters are never dropped, so a CJK headline shows boxes instead of silently losing words). Font is the system Segoe UI Bold → Arial Bold → shipped DejaVu (much heavier — a fallback, not the design). Headline goes in via drawtext `textfile=` — never interpolated into the filter. Per-brand colors/font/size come from `brands/<name>/style.json`. Pure/blocking; callers use `asyncio.to_thread`. |
| `modules/telegram/branded.py` | Brand-it flow's pure half (offline-testable — news_bot exits at import without env): `pairs_for` (brand→TG/YT/X pairs, YouTube hidden past the 180 s Shorts cap) and the gate / brand-picker / publish-picker keyboards, callback namespace `b:`. |
| `modules/instagram/upload_post.py` | Main poster. Pushes media to phone, drives IG's UI through the new flow (see below), archives to `posts/posted/`. Re-exports `DEVICE_ID` from `shared.config` so callers keep using `ig.DEVICE_ID`. |
| `modules/instagram/main.py` | Reels scrolling loop. Uses `human_swipe.py` for realistic swipes. **Do not touch when working on the post flow.** |
| `modules/instagram/human_swipe.py` | Empirical swipe model fit to a real trace; powers `main.py`. |
| `modules/instagram/dump_ui.py` | Connects to the test phone and dumps the current IG screen to `ui_dump.xml`. Use whenever a selector breaks. |
| `modules/instagram/analyze_swipes.py` / `visualize_swipes.py` | Offline analysis for the swipe model — not used at runtime. |
| `modules/telegram/telegram_bot.py` | Telegram bot: scans a configured group for IG/Twitter URLs, downloads via `shared/reel_downloader.py`, queues into `posts/`, and posts to IG via `modules.instagram.upload_post` for each selected account (`IG_ACCOUNTS`). |
| `modules/twitter/poster.py` | Twitter/X poster (ported from the news-bot project). `post_media(file_path, caption, account_name)` detects video vs photo by extension: videos go through Tweepy v1.1 `chunked_upload` + async-processing poll, photos through `media_upload`; the tweet itself is created with the v2 Client. Credentials: five `TWITTER_<ACCOUNT>_*` vars per account in `.env` (account name uppercased, non-alphanumerics → `_`; app needs Read+Write). Keeps a back-compat `upload_post(d, ...)` wrapper (ignores `d`). CLI: `py modules/twitter/poster.py clip.mp4 --caption "..." --account name`. |
| `modules/youtube/uploader.py` | YouTube Shorts uploader (ported from the news-bot project). `upload_short(file_path, title, description, account_name, privacy_status)` does a resumable Data API v3 upload; vertical videos ≤3 min are auto-classified as Shorts. OAuth per account: `credentials/youtube/<account>/client_secrets.json` + `token.pickle` (git-ignored). First run per account must be interactive — it opens a browser for consent and will hang headless. Quota: 1600 units/upload of a 10,000/day default budget per Google Cloud project (~6 uploads/day) — one project per account. CLI: `py modules/youtube/uploader.py clip.mp4 --title "..." --account name`. |
| `modules/youtube/publisher.py` | YouTube twin of the telegram publisher. `publish_shorts(video_path, caption, dests)` first runs `shorts_format.ensure_short` (once per video, not per channel), then translates the caption per channel language (reuses `modules/telegram/translator`, cached per lang), splits it (first line → title ≤100 chars, full caption → description) and uploads to each `YT_DESTINATIONS` channel. Blocking — async callers use `asyncio.to_thread`. Returns `(posted, errors)`. |
| `modules/youtube/shorts_format.py` | Shorts guarantee: YouTube auto-classifies Shorts by the file alone (≤3 min + aspect; no API flag) and square 1:1 uploads have landed as regular videos, so only strictly vertical (h > w) passes through. `ensure_short(path)` probes with ffprobe (rotation-aware); square/horizontal videos are re-rendered by ffmpeg to 1080×1920 — clip centered over a blurred zoomed copy — as `<name>_short.mp4` (caller deletes after upload; publisher + uploader CLI both do). Videos over 180 s raise instead of silently publishing a regular video. Requires ffmpeg+ffprobe on PATH. |
| `modules/telegram/news_bot.py` | Manual broadcaster: post text/photo/video/album in the control group → inline picker of `TG_DESTINATIONS` channels **plus `YT_DESTINATIONS` YouTube channels when the post has a video** → caption translated per destination → Telegram fan-out and/or Shorts upload. Also accepts **IG reel / Twitter-X URLs**: downloads via `shared/reel_downloader` (video-first, photo fallback), caption = your text around the URL if any, else the link's own caption (the picker shows which); **replying to an open picker replaces the caption** (any post type). Telegram channel fan-out has **no size limit** (the file_id is re-sent, the bytes never leave Telegram); branding and the YouTube leg need the file on disk and go through `mtproto.py` past 20 MB, falling back to the Bot API's `get_file` with a clear error when that client isn't logged in. Downloaded files (`tg_data/media/manual_url_*`) are deleted on post/cancel and swept at startup. Single-video posts with `BRANDS` configured gate through "Post as-is / Brand it"; brand-it renders per-brand variants and publishes only explicitly selected brand→platform pairs. **Also hosts the Telegram autopilot** (`autopilot.schedule`) and the `/queue`, `/autopilot`, `/asks`, `/next` commands — one bot token allows exactly one poller, and the reaction ask needs callbacks. |
| `modules/telegram/smart_filter.py` | **The one filter every platform routes through.** `evaluate(text, hint)` is the brain (today a demo: one OpenRouter call via `scorer.py`) returning `{score, regions, tier}`; `telegram_targets(item, dests)` / `youtube_targets(item, dests)` are the per-platform routing rules; `is_news(item)` enforces the **media-only rule** (a text-only source post isn't news — `NEWS_REQUIRE_MEDIA=0` disables it); `best_of(pairs)` makes the final head-to-head choice **at publish time** via `scorer.compare` (scores are assigned in isolation on arrival, so a day's queue clusters at 70-80 — this picks the winner from inside that cluster, falling back to the top score if the call fails). Pure policy — the only I/O is the comparison call. New platforms add a `*_targets` function plus a consumer. |
| `modules/telegram/dispatcher.py` | The **only process that scores** (one writer, no race on the queue): polls for `new` items, drops text-only ones to `rejected` **before** any API call (`smart_filter.is_news`), calls `smart_filter.evaluate` on the rest, records score+regions, then uploads whatever `smart_filter.youtube_targets` returns (score `>= YT_AUTO_MIN_SCORE`, default 70, **with video**) via `modules/youtube/publisher` and writes one `record_post` row per channel. Scoring failures stay `new` and retry. Must be running for the Telegram autopilot to have anything to post. Run alongside `collector.py`. |
| `modules/telegram/autopilot.py` | The **Telegram drip**, hosted by `news_bot.py` as a JobQueue job. Every random `TG_DRIP_MIN_S`..`TG_DRIP_MAX_S` (**default 18-24 h**, ~one story a day) it gathers eligible items Telegram hasn't seen (score `>= TG_AUTO_MIN_SCORE`, newer than `TG_MAX_AGE_H`, region-matched), picks the winner via `smart_filter.best_of`, sends it to every channel whose **region** matches (`global` matches all; a channel with no region is a catch-all), records each post, fires the per-post BulkFollows order, and opens a reaction ask. Never posts unscored items. `startup_delay()` resumes the rhythm from the last real post so restarts don't each trigger a post. **`TG_MAX_AGE_H` must exceed `TG_DRIP_MAX_S` in hours** or the queue expires between ticks. CLI: `py modules/telegram/autopilot.py --once [--dry-run]`. |
| `modules/telegram/mtproto.py` | **Big-file transport** for `news_bot.py`. The HTTP Bot API refuses `getFile` over 20 MB and uploads over 50 MB; a Telethon **user** client has no such ceiling (2 GB). A Bot-API `file_id` is meaningless to a user client, so the bot records `chat_id`+`msg_id` on arrival and this module re-fetches the original message. Own session (`data/bigfile.session` — Telethon sessions can't be shared with a live `collector.py`), one interactive login: `py modules/telegram/mtproto.py --login`. `ensure_ready()` never raises and memoises its verdict; every caller falls back to the Bot API when it returns False. Gated by `TG_BIG_FILES` (default on when `TELEGRAM_API_ID`/`_HASH` are set). |
| `modules/telegram/reactions.py` | Emoji catalogue (`EMOJI_SERVICES`, one BulkFollows service id per reaction), the ordering helpers lifted out of `news_bot.py` (`record_posts`: per-post order on publish + the every-5th-post channel order, counted durably via `queue_store.bump_channel_posts` — an in-memory counter never survived to the 5th post at a daily cadence), and the reaction ask's **pure state machine** (`new_state`/`reduce`/`orders_from`/`render`). Shared by the manual picker and the autopilot so the two can't drift. |
| `modules/telegram/smm.py` | BulkFollows SMM-panel client: one form-encoded `action=add` POST. Never raises — a panel outage must not affect a post that already shipped. |
| `modules/telegram/cleanup.py` | **Weekly control-group wipe**, registered by `news_bot.py` as JobQueue job `weekly-cleanup` — Mondays 04:00 **local**. `wipe_chat(bot, chat_id) -> (deleted, failed)` closes every open ask as skipped, then deletes each tracked message in batches of `BATCH=100`, falling back to per-message deletes when a batch call fails; attempted rows are always cleared (an id too old to delete never becomes deletable). Needs the bot's "Delete messages" admin right to remove *your* messages. **`MONDAY = 1`, not 0** — PTB 20 changed `run_daily(days=…)` from monday-sunday to **sunday-saturday**, so a 0 silently moves the wipe to Sunday. Only the chat id passed in is touched; destination channels never are. |
| `modules/telegram/queue_store.py` | SQLite store (`data/news.db`). `items` = collected stories (+ `score`/`regions`/`attempts`); **`posts` = one row per (item, platform, target)**, which is what decides eligibility — an item already on YouTube is still a Telegram candidate, and `items.status` is only a coarse overview; `asks` = open reaction asks (state as JSON, so restarts lose nothing); `group_messages` = every control-group message id the bot saw or sent, which is the only thing the weekly cleanup can delete (the Bot API can't list chat history, so anything sent while the bot was down survives). `init()` migrates existing DBs in place. Key queries: `candidates(platform, min_score, max_age_h)`, `record_post`, `bump_attempts`, `track_group_message`/`tracked_message_ids`/`clear_group_messages`. |
| `modules/facebook/poster.py` | Posting **stub** mirroring the IG `upload_post(...)` signature; raises `NotImplementedError`. |
| `modules/newsroom/` | **The client bot (this branch only)** — WordPress → Telegram. `main.py` (one JobQueue job per site; `tick()` sequences the flow and holds the **backfill guard**: a site's first tick records what it finds as seen and posts none of it, or enabling a site dumps twenty back-articles into a live channel), `wp.py` (REST poller — refetches the last 20 with `_embed` and filters by stored id, **never a `?after=` watermark**, which loses backdated and scheduled posts permanently), `rewrite.py` (article → post, one OpenRouter call, degrades to the article's own lede; `--sample` is the prompt-tuning harness), `publish.py` (media passed to Telegram **as a URL** — no download, no big-file path; `compose()` reserves the article link's length before trimming so the link can't be truncated off a long post), `orders.py` (views per post, bonus every 5th against the **channel** link, delayed random reactions — every function takes a `site` because service ids are per site, and a global would bill one client's growth to another's balance), `smm.py`, `store.py`, `sites/*.json`. See `modules/newsroom/README.md`. |
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

News pipeline (each in its own terminal — needs `.env` credentials). All three
are needed for the Telegram autopilot: the collector supplies stories, the
dispatcher scores them, the news bot posts them.

    py modules/telegram/collector.py    # collects source-channel posts into the SQLite queue (first run: phone-code login)
    py modules/telegram/dispatcher.py   # the smart filter: scores queued items, auto-uploads >=YT_AUTO_MIN_SCORE videos to YouTube
    py modules/telegram/news_bot.py     # manual broadcaster + Telegram autopilot (drip + reaction asks)

Big videos (branding / YouTube past 20 MB) need one interactive login for the
news bot's MTProto client — same account as the collector, separate session:

    py modules/telegram/mtproto.py --login

Autopilot control, in the news bot's control group: `/queue` (what's queued and
what posts next), `/autopilot on|off`, `/asks` (unanswered reaction asks),
`/next` (drip one item now). Preview without posting:

    py modules/telegram/autopilot.py --once --dry-run

Tests (offline — no Telegram, OpenRouter or BulkFollows calls):

    py -m pytest tests -q

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
