# IG Automatization — project guide

PC-side queue manager + Android driver for posting photos and scrolling reels on Instagram. Posts live in `posts/` as `NNN.jpg` + `NNN.json` (caption + hashtags); the Angular UI in `ui/` queues new ones; `upload_post.py` drives IG over ADB via uiautomator2.

## Files

| File | Purpose |
| ---- | ------- |
| `server.py` | FastAPI backend on `:8000`. Receives uploads from the UI, writes them into `posts/`, optionally fires `upload_post.py`. Also accepts a `url` form field — when an Instagram URL is pasted, calls `reel_downloader.download_media(url, stub, kind=post_type)` first instead of expecting a multipart file. The stub picks `.mp4` for reels / `.jpg` for posts; yt-dlp overrides the extension with the real one. CORS open to `http://localhost:4200`. |
| `reel_downloader.py` | Fetches Instagram media by URL via `yt-dlp` (no token / login / Selenium required — pulls the file directly from `fbcdn.net`). Exposes `is_instagram_url(s)` and `download_media(url, dest_path, kind="reel"\|"post")`; `download_reel` is kept as a backwards-compat alias. **Limitation**: IG gates many photo posts behind a login — yt-dlp surfaces this as `Instagram sent an empty media response`. Reels are reliable, photo posts are best-effort. |
| `upload_post.py` | Main poster. Pushes media to phone, drives IG's UI through the new flow (see below), archives to `posts/posted/`. |
| `main.py` | Reels scrolling loop. Uses `human_swipe.py` for realistic swipes. **Do not touch when working on the post flow.** |
| `human_swipe.py` | Empirical swipe model fit to a real trace; powers `main.py`. |
| `dump_ui.py` | Connects to the test phone and dumps the current IG screen to `ui_dump.xml`. Use whenever a selector breaks. |
| `analyze_swipes.py` / `visualize_swipes.py` | Offline analysis for the swipe model — not used at runtime. |
| `swipe_stats.json` / `swipes.txt` | Calibration data feeding `human_swipe.py`. |
| `ui/` | Angular frontend (`ng serve` on `:4200`, proxies `/api/*` → `:8000`). |
| `posts/` | Queue. `posts/posted/` holds archives (move-on-success). |

## Running the system

Install Python deps (first time):

    py -m pip install -r requirements.txt

Backend (one terminal):

    py -m uvicorn server:app --reload --port 8000

UI (another terminal):

    cd ui
    npm start

Post manually (skip the UI, uses the next queued image):

    py upload_post.py

Dump the current IG screen for selector hunting:

    py dump_ui.py
    # → writes ui_dump.xml; grep it for 'Create', 'Photo', etc.

## Test device

Galaxy S23, ADB ID `R5CX235CF9A`. Hardcoded in `upload_post.py`, `main.py`, `dump_ui.py`. The swipe model in `human_swipe.py` is calibrated for 1080×2340 — other geometries need a recalibration.

## Current IG creation flow

`upload_post.upload_post(d, image_path, caption_body, hashtags, kind=...)` drives the new creation UI end to end. `kind` is `"post"` (photo, default) or `"reel"` (video); the only divergence is which row gets tapped in step 4 — the rest of the flow is assumed identical and we fix selectors as IG diverges.

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

- **Audio for posts/reels.** The new IG flow may surface an audio/music picker on some post types (reels, possibly some post variants). When we add **reel** or **carousel** support, remember to handle — or explicitly skip — the audio step on the way through. The current photo-post flow does not seem to include it, but verify with `dump_ui.py` after step 5 if a music screen appears.
- **Carousel.** Will also start by tapping **Post** in the creation sheet, then multi-select on the gallery screen. Not implemented yet.
- **Selector hunting.** Content-description (`description=`, `descriptionContains=`) and visible text (`text=`) are far more stable across IG versions than resource IDs. When a selector breaks, start there before reaching for `resourceId=`.
- **Python launcher.** Use `py`, never `python` — multi-install PATH gotcha on this machine.
