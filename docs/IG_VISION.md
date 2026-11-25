# IG vision driver — `modules/instagram/vision/`

A second, more careful ADB driver for Instagram that lives **alongside**
`modules/instagram/upload_post.py` and never replaces it. Today it posts — in
dry run — a **carousel with Instagram's own music**, switches accounts, and
sweeps every account on the phone. Built and verified 2026-11-25 on a Galaxy
S23 FE (SM-S711B, 1080×2340, Android 16), Instagram 457.0.0.55.73, dark mode.

Despite the name there is **no model in it yet**: every step runs on the
uiautomator hierarchy dump. The vision phases (labelling, YOLO, detector) wait
for evidence from the drift log — see "Does it need AI?" below.

## Rules it is built around

1. **Nothing publishes without a human.** The default is a dry run: the flow
   goes to the final composer, verifies it and stops before Share. Sharing
   needs `--share --i-confirm-share` **and** typing the account name at a
   terminal prompt (no terminal → refused). `--sweep` has no share path.
2. **Never post twice.** A share is tapped once and confirmed by the
   profile's post count going up by one; an unconfirmed share stops with
   "check the profile by hand" and is never retried.
3. **Account check before anything.** The Profile title is read at the start
   of every run; a different account is switched through the account switcher
   and the title is read **again** — the run goes on only if it now matches.
4. **Stop, don't guess.** A security check ("confirm it's you", captcha,
   action blocked…), the lock screen, another app in front, an unknown popup
   or an unexpected screen stops the run with a saved screenshot. The driver
   never tries to solve a security check.
5. **Screens stay local.** Screenshots and dumps go to `data/ig_screens/`
   (git-ignored — they show account data). Only **scrubbed** XML goes into
   `tests/fixtures/ig_vision/`.

## Files

| File | What it does |
| ---- | ------------ |
| `device.py` | The only code that talks to the phone (uiautomator2). Text via accessibility `set_text`; reads retried once on a dropped connection, taps never. |
| `uixml.py` | Pure dump parsing: nodes, bounds, `find`, `guess_account`. |
| `recorder.py` / `record.py` | Screenshot + dump + metadata JSON per capture; the CLI for capturing by hand. |
| `screens.py` | `classify(dump)` → which screen; readers for slide numbers, track rows, composer music/caption, profile, account rows; the known popups. |
| `driver.py` | Look-until-stable, resolve (dismiss known popups, stop on the rest), act (tap + check the transition, one retry for a swallowed tap), scroll-find, the drift log. |
| `flows.py` | The carousel-with-music flow and its steps; account switching; push/remove of the test slides. |
| `sweep.py` | Dry run on every account, one report. |
| `run.py` | The CLI. |
| `scrub.py` | Recorded dump → committable fixture (handles, names, counts, bio and status bar removed). |

## Running it

Use the USB serial while `PHONE_ADDRESS` in `.env` points elsewhere:

```
# dry run on one account (switches to it if needed)
py modules/instagram/vision/run.py --device R5CX235CF9A --account vestra.24 \
    --track "Lazy River" --artist Phury \
    --files a.jpg b.jpg c.jpg --caption "text" --hashtags news world

# the next queued carousel from posts/ (<name>_1.jpg, <name>_2.jpg … + <name>.json)
py modules/instagram/vision/run.py --device R5CX235CF9A --account vestra.24 --carousel --track "Lazy River"

# just switch account (verified on Profile)
py modules/instagram/vision/run.py --device R5CX235CF9A --account frontiva24 --switch-only

# back out of a half-made post (Discard / Start over)
py modules/instagram/vision/run.py --device R5CX235CF9A --discard

# dry run on EVERY account in the switcher; report in data/ig_screens/sweep_*.json
py modules/instagram/vision/run.py --device R5CX235CF9A --sweep [--accounts a b] [--probe "Ed Sheeran"]
```

`--sweep` uses the grey test slides in `data/ig_test/` when no `--files` are
given, searches `--probe` first to tell the account's music library (licensed
tracks = **Creator**, royalty-free only = **Business**), adds `--track`
(default "Lazy River"), discards each draft, deletes the slides it pushed
(`/sdcard/DCIM/Camera/igv_*` — nothing else) and leaves the phone on the
account it started on. A security check, lock screen or other app ends the
whole sweep without touching anything.

### Phone setup (after a reset)

* Developer options → USB debugging on; tap "Allow" with "Always allow" ticked.
* Screen lock: None (the driver can't pass a PIN).
* The driver keeps the screen on while on USB (`svc power stayon usb`);
  `adb shell svc power stayon false` undoes it.
* No helper keyboard app is needed. uiautomator2's FastInput IME lives in the
  "ATX" app, which **Play Protect blocks on Android 16** — that also means
  `upload_post.py` can't type captions on this phone.

## The flow on IG 457

Cold start → Home → Profile (account, post count) → [switcher → account →
Profile again] → push slides → Home → **Create** (`desc="Create"`) → gallery,
POST preselected → **Select multiple** (`multi_select_slide_button_alt`) →
each slide tapped and confirmed by its own "Selected media number k" before
the next → Next → carousel editor (Audio / Text / Overlay / Filter / Edit /
Ratio, one Next) → **Audio** → search box (`row_search_edit_text`, set_text)
→ tap the track row (preview only) → `audio_bar_select_tap_target` (default
segment kept) → editor → Next → ["Sharing posts" sheet: OK, first time only]
→ composer → caption field → full-screen Caption editor → set_text → Done →
composer: caption (`caption_input_text_view`), music (`music_track_title` /
`_subtitle`) and "no music nobody asked for" verified → stop.

Things learnt the hard way (all handled in the driver):

* **Share is `share_footer_button`** on IG 457. `upload_post.py` checks for
  `share_button`, which no longer exists — so its "did it publish?" check
  passes whether or not anything was posted.
* Slide taps closer than ~1 s apart are dropped while IG loads the preview.
* The selector RPC misses some views the dump shows (the music select
  button) — the driver taps coordinates read from the dump.
* The dump writes an emoji as `..` (one dot per UTF-16 half): compare with
  `screens.dump_form`.
* `set_text` raises no hashtag autocomplete strip.
* The editor shows a "Suggested Audio" pill: never tapped.
* Backing out of the editor asks "Start over?" — answered Start over.
* The account switcher is a sheet ON Profile (the profile stays in the dump);
  rows are `desc="handle[, 5 comments and 4 more]"`, the active one selected;
  after a switch IG lands on the new account's Profile. 9 accounts were logged
  in on one IG app here — the often-quoted limit of 5 doesn't hold.
* Autoplaying video (Home feed) never gives two identical screenshots; a dump
  unchanged over three reads counts as settled.

## The drift log — does it need AI?

`data/ig_screens/drift.jsonl` gets one JSON line for every fallback selector,
retried tap, screen that wouldn't settle, failed scroll-search and stop, with
the screenshot that goes with it. On one account and one IG version the flow
needed **no vision model at all**. The plan:

* run `--sweep` regularly (and after every IG update);
* **near-empty drift log** → the vision phases are not worth building;
* **drift on elements the dump can't see** (no id/text/description, or a
  layout experiment the selectors don't cover) → those screenshots are the
  training set: label them, train a small detector (YOLO nano/small, ONNX on
  CPU), and use it only as the fallback after the selectors.

## Recording and fixtures

```
py modules/instagram/vision/record.py --device R5CX235CF9A --interactive --account vestra.24
py modules/instagram/vision/record.py --device R5CX235CF9A --label music_picker
```

Every run of `run.py` records each step too. To turn a recorded dump into a
test fixture:

```
py modules/instagram/vision/scrub.py data/ig_screens/<day>/<file>.xml \
    -o tests/fixtures/ig_vision/<screen>.xml --account <handle on screen> --show
```

Read the `--show` output (every text that survived) before committing; music
library uploader names ("Original audio by …") are personal handles too.

## Tests

`tests/test_ig_vision_recorder.py`, `tests/test_ig_vision_driver.py` — offline.
A fake phone replays the scrubbed screens through the whole flow, a switch, a
sweep, and the failure paths (lost tap, wrong account, account not on the
phone, switch that doesn't take, missing track, unrequested music, security
screen, wrong photos at the top of the gallery, unknown popup).

```
py -m pytest tests/test_ig_vision_driver.py tests/test_ig_vision_recorder.py -q
```

## Not built yet

* Reels with music (same building blocks).
* The vision phases (auto-labeller, training, ONNX detector, live viewer) —
  waiting on the drift log.
* Several phones: per-device worker thread, one shared detector, coordinates
  as screen fractions (the driver already taps dump bounds, not fixed
  pixels). One IP per phone (SIM data) is recommended.
