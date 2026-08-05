# Photos, Branded-Post Asks, Manual Reaction Amounts — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the five features of `docs/superpowers/specs/2026-08-04-photos-reactions-amounts-design.md`: gallery-dl photo downloads from IG/Twitter links, a reactions ask after branded publishes, Random/Manual per-emoji reaction amounts with a 20-minute order delay, Twitter edge-trim before branding, and the weekly control-group cleanup.

**Architecture:** Pure logic goes into offline-testable modules (`shared/reel_downloader.py`, `modules/telegram/reactions.py`, `modules/telegram/queue_store.py`, `shared/branding.py`, new `modules/telegram/cleanup.py`); `modules/telegram/news_bot.py` gets only thin wiring (it exits at import without `.env`, so it stays untested — every decision it makes must live in a testable module). Delayed reaction orders and tracked group messages persist in the existing SQLite store so restarts lose nothing.

**Tech Stack:** Python 3.11+, python-telegram-bot v20 (JobQueue), sqlite3 (stdlib), yt-dlp, gallery-dl (new), ffmpeg, pytest.

## Global Constraints

- Python launcher is `py`, **never** `python` (multi-install PATH gotcha on this machine).
- Shell is PowerShell 5.1 — no `&&`; chain with `;` or separate commands.
- All tests must run offline: no Telegram, OpenRouter, BulkFollows, or network calls. `py -m pytest tests -q`.
- **Pre-existing failures:** 3 tests in `tests/test_autopilot.py` fail because the operator's real `.env` sets `TG_FIRST_TICK=21:00`. They are NOT caused by this work — leave them alone; "suite green" means no NEW failures.
- `.env` contains secrets (bot tokens, API keys). Never print its values — key names only, values masked.
- The news bot runs in the operator's own terminal. **Never start a bot instance** (one getUpdates poller per token; a second one breaks the operator's).
- Git commits end with the trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Emoji glyphs must not go into log strings (Windows console can't encode them) — log the emoji's `name` field, as `reactions.py` already does.
- New env keys default to safe values so the bot starts unchanged with an untouched `.env`: `GALLERY_DL_COOKIES_BROWSER` default `""`, `REACTION_DELAY_S` default `1200`.
- Comment style: comments state constraints the code can't show; match the existing prose-heavy docstring style of the repo.

---

### Task 1: gallery-dl photo downloader

**Files:**
- Modify: `requirements.txt` (add gallery-dl)
- Modify: `shared/config.py` (add `GALLERY_DL_COOKIES_BROWSER`)
- Modify: `shared/reel_downloader.py` (add `download_photos`, `MAX_PHOTOS`)
- Test: `tests/test_reel_downloader.py` (new file)

**Interfaces:**
- Consumes: `shared.config` (env loading), gallery-dl CLI via `subprocess.run`.
- Produces: `reel_downloader.download_photos(url: str, dest_stub: str) -> tuple[list[str], str]` — absolute photo paths (≤10) named `<stub>_1.jpg`, `<stub>_2.png`, … plus a best-effort caption. Raises `RuntimeError` when gallery-dl is missing or finds nothing. `reel_downloader.MAX_PHOTOS = 10`. `config.GALLERY_DL_COOKIES_BROWSER: str`.

- [ ] **Step 1: Add the dependency**

In `requirements.txt`, after the `yt-dlp>=2024.1.1` line add:

```
gallery-dl>=1.27      # photo fallback for IG/Twitter links (shared/reel_downloader.py)
```

Run: `py -m pip install gallery-dl`
Expected: installs cleanly; `gallery-dl --version` prints a version.

- [ ] **Step 2: Add the config knob**

In `shared/config.py`, directly under the `POSTS_DIR` block (before `DEVICE_ID`), add:

```python
# Browser whose cookie jar gallery-dl may read for logged-in Instagram photo
# downloads (e.g. "chrome"). Used ONLY by the photo fallback in
# shared/reel_downloader.py; blank = anonymous requests.
GALLERY_DL_COOKIES_BROWSER = os.environ.get("GALLERY_DL_COOKIES_BROWSER", "").strip()
```

- [ ] **Step 3: Write the failing tests**

Create `tests/test_reel_downloader.py`:

```python
"""shared/reel_downloader.py — the gallery-dl photo fallback and the
video-first download chain. Fully offline: subprocess and yt-dlp are mocked."""

import json
import os

import pytest

from shared import config, reel_downloader


def fake_gdl(files, stderr=""):
    """subprocess.run stand-in: stages `files` ({name: bytes | dict}) into the
    directory the -D flag points at, exactly as the gallery-dl CLI would."""

    class Proc:
        returncode = 0
        stdout = ""

    Proc.stderr = stderr

    def run(cmd, capture_output=True, text=True):
        run.cmd = cmd
        out_dir = cmd[cmd.index("-D") + 1]
        os.makedirs(out_dir, exist_ok=True)
        for name, content in files.items():
            path = os.path.join(out_dir, name)
            if isinstance(content, dict):
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(content, fh)
            else:
                with open(path, "wb") as fh:
                    fh.write(content)
        return Proc()

    return run


def test_download_photos_renames_onto_stub(tmp_path, monkeypatch):
    run = fake_gdl({"insta_01.jpg": b"a", "insta_02.png": b"b"})
    monkeypatch.setattr(reel_downloader.subprocess, "run", run)
    stub = str(tmp_path / "manual_url_7.mp4")

    paths, caption = reel_downloader.download_photos("https://x.com/u/status/1", stub)

    assert [os.path.basename(p) for p in paths] == [
        "manual_url_7_1.jpg", "manual_url_7_2.png",
    ]
    assert all(os.path.isfile(p) for p in paths)
    assert caption == ""
    # the per-call temp dir must not be left behind
    assert not os.path.isdir(str(tmp_path / "manual_url_7_gdl"))


def test_download_photos_caption_from_info_json(tmp_path, monkeypatch):
    run = fake_gdl({
        "a.jpg": b"a",
        "a.jpg.json": {"description": "Breaking news caption"},
    })
    monkeypatch.setattr(reel_downloader.subprocess, "run", run)

    paths, caption = reel_downloader.download_photos(
        "https://instagram.com/p/abc", str(tmp_path / "s.mp4"))

    assert caption == "Breaking news caption"
    assert len(paths) == 1


def test_download_photos_caps_at_telegram_album_limit(tmp_path, monkeypatch):
    files = {f"img_{i:02d}.jpg": b"x" for i in range(14)}
    monkeypatch.setattr(reel_downloader.subprocess, "run", fake_gdl(files))

    paths, _ = reel_downloader.download_photos(
        "https://instagram.com/p/abc", str(tmp_path / "s.mp4"))

    assert len(paths) == reel_downloader.MAX_PHOTOS == 10


def test_download_photos_passes_cookie_flag_when_configured(tmp_path, monkeypatch):
    run = fake_gdl({"a.jpg": b"a"})
    monkeypatch.setattr(reel_downloader.subprocess, "run", run)
    monkeypatch.setattr(config, "GALLERY_DL_COOKIES_BROWSER", "chrome")

    reel_downloader.download_photos("https://instagram.com/p/abc",
                                    str(tmp_path / "s.mp4"))

    i = run.cmd.index("--cookies-from-browser")
    assert run.cmd[i + 1] == "chrome"


def test_download_photos_no_cookie_flag_by_default(tmp_path, monkeypatch):
    run = fake_gdl({"a.jpg": b"a"})
    monkeypatch.setattr(reel_downloader.subprocess, "run", run)
    monkeypatch.setattr(config, "GALLERY_DL_COOKIES_BROWSER", "")

    reel_downloader.download_photos("https://instagram.com/p/abc",
                                    str(tmp_path / "s.mp4"))

    assert "--cookies-from-browser" not in run.cmd


def test_download_photos_raises_when_nothing_downloaded(tmp_path, monkeypatch):
    monkeypatch.setattr(reel_downloader.subprocess, "run",
                        fake_gdl({}, stderr="login required"))

    with pytest.raises(RuntimeError, match="login required"):
        reel_downloader.download_photos("https://instagram.com/p/abc",
                                        str(tmp_path / "s.mp4"))
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `py -m pytest tests/test_reel_downloader.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'download_photos'` (and `subprocess` missing from the module).

- [ ] **Step 5: Implement `download_photos`**

In `shared/reel_downloader.py`: extend the imports at the top to

```python
import json
import logging
import os
import re
import shutil
import subprocess

import yt_dlp

from shared import config

log = logging.getLogger(__name__)
```

Then append at the end of the file:

```python
# Telegram albums cap at 10 items; extra carousel photos are dropped (logged).
MAX_PHOTOS = 10


def download_photos(url: str, dest_stub: str) -> tuple[list[str], str]:
    """Photo fallback via the gallery-dl CLI — handles IG photo posts (which
    yt-dlp can't, login-gated) and photo-only tweets. Downloads into a
    per-call temp dir next to `dest_stub`, then renames onto
    `<stub>_1.jpg`-style names so the caller's cleanup conventions (the
    manual_url_* sweep) keep working.

    Cookies: with GALLERY_DL_COOKIES_BROWSER set (e.g. "chrome"), gallery-dl
    reads that browser's logged-in Instagram session; blank = anonymous.

    Returns (absolute photo paths, caption). Caption is best-effort from
    gallery-dl's metadata sidecars — empty string when absent. Raises
    RuntimeError when gallery-dl is missing or nothing was downloaded."""
    dest_dir = os.path.dirname(dest_stub) or "."
    os.makedirs(dest_dir, exist_ok=True)
    stem, _ = os.path.splitext(dest_stub)
    tmp_dir = stem + "_gdl"

    cmd = ["gallery-dl", "-D", tmp_dir, "--write-info-json", url]
    if config.GALLERY_DL_COOKIES_BROWSER:
        cmd[1:1] = ["--cookies-from-browser", config.GALLERY_DL_COOKIES_BROWSER]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "gallery-dl is not installed (py -m pip install gallery-dl)"
        ) from exc

    try:
        names = sorted(os.listdir(tmp_dir)) if os.path.isdir(tmp_dir) else []
        images = [n for n in names if not n.endswith(".json")]

        caption = ""
        for name in names:
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(tmp_dir, name), encoding="utf-8") as fh:
                    meta = json.load(fh)
                # IG calls it description, Twitter content — take whichever.
                caption = (meta.get("description") or meta.get("content") or "").strip()
            except (OSError, ValueError):
                continue
            if caption:
                break

        if not images:
            tail = (proc.stderr or "").strip()[-300:]
            raise RuntimeError(f"gallery-dl found no media for {url!r}: {tail}")

        if len(images) > MAX_PHOTOS:
            log.warning("carousel has %d photos — keeping the first %d "
                        "(Telegram album cap)", len(images), MAX_PHOTOS)
        paths = []
        for i, name in enumerate(images[:MAX_PHOTOS], start=1):
            ext = os.path.splitext(name)[1] or ".jpg"
            target = f"{stem}_{i}{ext}"
            os.replace(os.path.join(tmp_dir, name), target)
            paths.append(os.path.abspath(target))
        return paths, caption
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `py -m pytest tests/test_reel_downloader.py -v`
Expected: 6 PASS.

- [ ] **Step 7: Commit**

```bash
git add requirements.txt shared/config.py shared/reel_downloader.py tests/test_reel_downloader.py
git commit -m "feat: gallery-dl photo fallback for IG/Twitter links"
```

---

### Task 2: video-first download chain `download_any`

**Files:**
- Modify: `shared/reel_downloader.py` (add `download_any`)
- Test: `tests/test_reel_downloader.py` (extend)

**Interfaces:**
- Consumes: `download_media(url, dest_path, kind)` and `download_photos(url, dest_stub)` from Task 1.
- Produces: `reel_downloader.download_any(url: str, dest_stub: str) -> tuple[list[str], str]` — chain yt-dlp reel → yt-dlp post → gallery-dl photos; on total failure re-raises the FIRST yt-dlp error. News-bot wiring in Task 3 calls this instead of its private `_download_any`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_reel_downloader.py`:

```python
def test_download_any_video_first(monkeypatch):
    monkeypatch.setattr(reel_downloader, "download_media",
                        lambda url, dest, kind: ("/v.mp4", "vid cap"))
    monkeypatch.setattr(reel_downloader, "download_photos",
                        lambda url, stub: (_ for _ in ()).throw(AssertionError))

    assert reel_downloader.download_any("u", "/s.mp4") == (["/v.mp4"], "vid cap")


def test_download_any_falls_back_to_post_then_photos(monkeypatch):
    calls = []

    def dm(url, dest, kind):
        calls.append(kind)
        raise RuntimeError(f"yt-dlp {kind} failed")

    monkeypatch.setattr(reel_downloader, "download_media", dm)
    monkeypatch.setattr(reel_downloader, "download_photos",
                        lambda url, stub: (["/p_1.jpg", "/p_2.jpg"], "photo cap"))

    paths, caption = reel_downloader.download_any("u", "/s.mp4")

    assert calls == ["reel", "post"]
    assert paths == ["/p_1.jpg", "/p_2.jpg"] and caption == "photo cap"


def test_download_any_raises_the_first_error_when_all_fail(monkeypatch):
    def dm(url, dest, kind):
        raise RuntimeError(f"yt-dlp {kind} failed")

    monkeypatch.setattr(reel_downloader, "download_media", dm)
    monkeypatch.setattr(reel_downloader, "download_photos",
                        lambda url, stub: (_ for _ in ()).throw(
                            RuntimeError("gallery-dl failed")))

    with pytest.raises(RuntimeError, match="yt-dlp reel failed"):
        reel_downloader.download_any("u", "/s.mp4")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -m pytest tests/test_reel_downloader.py -v`
Expected: the 3 new tests FAIL with `AttributeError: ... no attribute 'download_any'`.

- [ ] **Step 3: Implement `download_any`**

Append to `shared/reel_downloader.py`:

```python
def download_any(url: str, dest_stub: str) -> tuple[list[str], str]:
    """Everything-downloader, video-first: yt-dlp "reel" (mp4-steered) →
    yt-dlp "post" (best format) → gallery-dl photos. A URL doesn't say what
    it holds, so the chain just tries in order of likelihood. When every
    stage fails the FIRST yt-dlp error is re-raised — it names the real
    problem (login gate, dead link); the fallbacks usually just repeat it.

    Returns (paths, caption): one path for a video, up to MAX_PHOTOS for a
    photo carousel."""
    try:
        path, caption = download_media(url, dest_stub, kind="reel")
        return [path], caption
    except RuntimeError as first:
        try:
            path, caption = download_media(url, dest_stub, kind="post")
            return [path], caption
        except RuntimeError:
            try:
                return download_photos(url, dest_stub)
            except RuntimeError:
                raise first
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -m pytest tests/test_reel_downloader.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add shared/reel_downloader.py tests/test_reel_downloader.py
git commit -m "feat: download_any chain — yt-dlp video-first, gallery-dl photo fallback"
```

---

### Task 3: news-bot URL flow — photo albums + URL-origin recording

**Files:**
- Modify: `modules/telegram/news_bot.py` — `_download_any` (delete, lines 250-262), `_handle_url` (lines 265-308)

**Interfaces:**
- Consumes: `reel_downloader.download_any(url, dest_stub) -> (paths, caption)` (Task 2), `reel_downloader.is_twitter_url(text) -> bool` (exists).
- Produces: picker state gains `"origin": "twitter" | "instagram"` on URL posts — Task 11 reads it to decide the edge-trim. Multi-photo URL posts flow through the existing album path (`publisher._send` → `send_media_group`) with no publisher change.

- [ ] **Step 1: Delete the private chain**

Remove the whole `_download_any` function from `news_bot.py` (lines 250-262, including its docstring).

- [ ] **Step 2: Rewire `_handle_url`**

Replace the body of `_handle_url` from the `note = await msg.reply_text(...)` line down (keep the URL/user-text extraction above it) with:

```python
    note = await msg.reply_text("⏳ downloading …")
    dest = os.path.join(config.TG_DATA_DIR, "media", f"manual_url_{msg.message_id}.mp4")
    try:
        # yt-dlp / gallery-dl are blocking — keep the event loop free.
        paths, link_caption = await asyncio.to_thread(
            reel_downloader.download_any, url, dest)
    except Exception as exc:
        log.error("URL download failed for %s: %s", url, exc)
        await note.edit_text(f"❌ download failed: {str(exc)[:300]}")
        return

    is_video = (len(paths) == 1
                and os.path.splitext(paths[0])[1].lower() in _VIDEO_EXTS)
    state = {
        "text": user_text or link_caption,
        "cap_src": "your text" if user_text else "from link",
        # One media item per file: a photo carousel becomes a Telegram album
        # via the existing publisher album path.
        "media": [{"path": p, "type": "video" if is_video else "photo"}
                  for p in paths],
        "files": list(paths),
        # Twitter clips carry hairline edge artifacts; the brand render reads
        # this to decide the edge-trim (branding.render_branded trim_edges).
        "origin": "twitter" if reel_downloader.is_twitter_url(url) else "instagram",
        "sel_tg": set(),
        "sel_yt": set(),
        "sel_em": set(),
    }
    if is_video and config.BRANDS:
        state["mode"] = "gate"
        cap = state["text"]
        await note.edit_text(
            "Brand this clip, or post it as-is?" + (f"\n\n📝 {cap}" if cap else ""),
            reply_markup=branded.gate_keyboard())
    else:
        await note.edit_text(_prompt_text(state), reply_markup=_keyboard(state))
    _pending[note.message_id] = state
```

- [ ] **Step 3: Verify — suite + import**

Run: `py -m pytest tests -q`
Expected: no new failures (the 3 pre-existing `test_autopilot.py` ones only).
Run: `py -c "import modules.telegram.news_bot"`
Expected: exits 0 silently (the operator's `.env` is present; import only defines handlers).

- [ ] **Step 4: Commit**

```bash
git add modules/telegram/news_bot.py
git commit -m "feat: URL posts fall back to photos (albums) and record their origin"
```

---

### Task 4: "manual post" rendering for `item_id=0` asks

**Files:**
- Modify: `modules/telegram/reactions.py` — `render` (lines 242, 250), `summary` (lines 282, 283)
- Test: `tests/test_reactions.py` (extend)

**Interfaces:**
- Consumes: existing `render(ask_id, state)` / `summary(state, applied)`.
- Produces: `reactions._item_label(state) -> str` — `"item N"` normally, `"manual post"` when `item_id` is 0/absent. Task 5's branded-publish ask relies on this.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_reactions.py`:

```python
def test_manual_post_ask_says_manual_post_not_item_zero():
    s = reactions.new_state(0, CHANS)
    text, _ = reactions.render(7, s)
    assert "manual post" in text and "item 0" not in text
    assert "manual post" in reactions.summary(s, applied=True)
    assert "manual post" in reactions.summary(s, applied=False)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -m pytest tests/test_reactions.py::test_manual_post_ask_says_manual_post_not_item_zero -v`
Expected: FAIL — text contains `item 0`.

- [ ] **Step 3: Implement**

In `reactions.py`, above `render`, add:

```python
def _item_label(state: dict) -> str:
    """Asks opened by the branded/manual flow have no queue item (item_id=0);
    "item 0" would read like a bug."""
    item_id = state.get("item_id") or 0
    return f"item {item_id}" if item_id else "manual post"
```

Replace every `item {state['item_id']}` interpolation with `{_item_label(state)}`:
- `render`, all-mode head: `f"🎛 Reactions · {_item_label(state)} · {n} channel(s)"`
- `render`, per-mode head: `f"🎛 Reactions · {_item_label(state)}"`
- `summary`, skipped: `f"✕ reactions skipped · {_item_label(state)}"`
- `summary`, applied: `f"✅ reactions ordered · {_item_label(state)}"`

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -m pytest tests/test_reactions.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add modules/telegram/reactions.py tests/test_reactions.py
git commit -m "feat: reaction asks label item_id=0 as 'manual post'"
```

---

### Task 5: reactions ask after a branded publish

**Files:**
- Modify: `modules/telegram/news_bot.py` — `_do_publish` (lines 652-706)

**Interfaces:**
- Consumes: `reactions.new_state(0, channels)` + `_item_label` (Task 4), `queue_store.open_ask(item_id, state) -> ask_id`, `autopilot.deliver_ask(bot, ask_id, state)` (all exist; `autopilot` already imported in news_bot).
- Produces: after any branded publish with ≥1 successful Telegram leg, one open ask row (`item_id=0`) delivered to the ask chat — answered by the existing `_on_ask_callback` (`r:` namespace), restart-safe via `/asks`.

- [ ] **Step 1: Collect the successful Telegram links**

In `_do_publish`, before the `lines = []` line add `tg_links = []`, and in the `p["platform"] == "tg"` branch, right after the `if posted:` block's `record_posts` call, add:

```python
                    tg_links.extend(links)
```

- [ ] **Step 2: Open the ask after the loop**

Replace the final two lines of `_do_publish`:

```python
    _cleanup(state)
    await q.edit_message_text("\n".join(lines))
```

with:

```python
    _cleanup(state)

    # Branded posts get the same post-publish reaction ask as autopilot posts
    # (SQLite-backed, so a restart doesn't strand it). item_id=0 = manual post.
    if tg_links:
        ask_state = reactions.new_state(
            0, [{"chat_id": c, "link": u} for c, u in tg_links])
        ask_id = queue_store.open_ask(0, ask_state)
        await autopilot.deliver_ask(context.bot, ask_id, ask_state)
        lines.append("🎛 reaction ask opened")

    await q.edit_message_text("\n".join(lines))
```

- [ ] **Step 3: Verify — suite + import**

Run: `py -m pytest tests -q` then `py -c "import modules.telegram.news_bot"`
Expected: no new failures; import exits 0.

- [ ] **Step 4: Commit**

```bash
git add modules/telegram/news_bot.py
git commit -m "feat: branded publishes open a reaction ask over their Telegram posts"
```

---

### Task 6: ask mode screen (Random / Manual)

**Files:**
- Modify: `modules/telegram/reactions.py` — `new_state`, `reduce`, `render`
- Test: `tests/test_reactions.py` (extend + update existing)

**Interfaces:**
- Consumes: existing state machine.
- Produces: `state["amode"]`: `None` (mode screen pending — only on NEW asks) · `"random"` · `"manual"`; verbs `mr` / `mm`. `reactions._mode_pending(state) -> bool`. Old SQLite asks lack the key entirely → behave exactly as today (random, no mode screen). Tasks 7/10 build on `amode`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_reactions.py`:

```python
# --- mode screen (Random / Manual) -----------------------------------------

def test_new_ask_opens_on_the_mode_screen():
    s = reactions.new_state(41, CHANS)
    assert s["amode"] is None
    text, kb = reactions.render(7, s)
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "r:7:mr" in data and "r:7:mm" in data and "r:7:sk" in data
    assert f"r:7:t{HEART}" not in data  # no emoji buttons yet


def test_choosing_a_mode_reaches_the_emoji_screen():
    s, _ = press(reactions.new_state(41, CHANS), "mr")
    assert s["amode"] == "random"
    _, kb = reactions.render(7, s)
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"r:7:t{HEART}" in data

    s, _ = press(reactions.new_state(41, CHANS), "mm")
    assert s["amode"] == "manual"


def test_mode_screen_ignores_emoji_and_apply_verbs():
    """Stale taps on a pre-redraw keyboard must not skip the mode choice."""
    s = reactions.new_state(41, CHANS)
    s, action = press(s, f"t{HEART}", "ap")
    assert action is None
    assert s["amode"] is None and s["sel"] == {"0": [], "1": []}


def test_mode_screen_skip_still_works():
    assert press(reactions.new_state(41, CHANS), "sk")[1] == "skip"


def test_legacy_ask_without_amode_behaves_as_random():
    """Old SQLite asks predate the mode screen — no key at all."""
    s = reactions.new_state(41, CHANS)
    del s["amode"]
    s, _ = press(s, f"t{HEART}")
    assert s["sel"] == {"0": [HEART], "1": [HEART]}
    _, kb = reactions.render(7, s)
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"r:7:t{HEART}" in data  # emoji screen, not the mode screen
```

Then update the existing tests that assume `new_state` lands on the emoji screen — every `press(reactions.new_state(...), ...)` sequence that starts with a toggle/navigation verb needs `"mr"` prepended, and `test_new_state` gains the new key. Concretely:
- `test_new_state`: add `assert s["amode"] is None`.
- `test_all_mode_toggles_every_channel_at_once`, `test_toggling_twice_clears_it`, `test_per_channel_mode_starts_from_the_shared_selection`, `test_navigation_wraps`, `test_per_channel_toggle_only_touches_the_current_channel`, `test_orders_from_pairs_every_channel_with_every_emoji`, `test_orders_skip_channels_without_a_public_link`, `test_summary_names_each_channel`: prepend `"mr"` as the first verb in their `press(...)` call (e.g. `press(reactions.new_state(41, CHANS), "mr", f"t{HEART}")`).
- `test_apply_and_skip_are_reported_as_actions`: change to `press(reactions.new_state(41, CHANS), "mr", "ap")` / `("mr", "sk")`.
- `test_render_produces_a_keyboard_for_both_modes`: add `s, _ = press(s, "mr")` before the first `render` call.
- `test_no_selection_orders_nothing` and `test_reduce_does_not_mutate_the_input` and `test_unknown_verb_is_ignored`: unchanged.
- `test_callback_data_fits_telegrams_64_byte_cap`: unchanged (mode screen has its own short verbs).
- `test_manual_post_ask_says_manual_post_not_item_zero` (Task 4): unchanged — the mode screen head also uses `_item_label`.

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `py -m pytest tests/test_reactions.py -v`
Expected: the 5 new mode-screen tests FAIL (`KeyError: 'amode'` / emoji buttons present); updated old tests FAIL until the implementation lands (the `"mr"` verb is currently ignored — those still pass, which is fine).

- [ ] **Step 3: Implement**

In `reactions.py`:

`new_state` — add one key to the returned dict:

```python
        "amode": None,  # mode screen pending; "random" | "manual" once chosen
```

Top of `reduce`, right after the `state = {**state, "sel": dict(state["sel"])}` line:

```python
    # Until Random/Manual is chosen, only the mode buttons (and Skip) count —
    # a stale tap on an old keyboard must not bypass the choice. Asks from
    # before the mode screen have no "amode" key at all and skip this gate.
    if "amode" in state and state["amode"] is None and verb not in ("mr", "mm", "sk"):
        return state, None
    if verb == "mr":
        return {**state, "amode": "random"}, None
    if verb == "mm":
        return {**state, "amode": "manual"}, None
```

Add the helper above `render`:

```python
def _mode_pending(state: dict) -> bool:
    return "amode" in state and state["amode"] is None
```

Top of `render`, before the existing `if state["mode"] == "all":` branch:

```python
    if _mode_pending(state):
        head = [f"🎛 Reactions · {_item_label(state)} · {n} channel(s)"]
        for c in chans:
            head.append(f"• {c['chat_id']} — {c.get('link') or 'no public link'}")
        head.append("Random rolls 10-40 per reaction; Manual lets you type "
                    "each amount.")
        rows = [
            [InlineKeyboardButton("🎲 Random", callback_data=f"r:{ask_id}:mr"),
             InlineKeyboardButton("✏️ Manual", callback_data=f"r:{ask_id}:mm")],
            [InlineKeyboardButton("✕ Skip", callback_data=f"r:{ask_id}:sk")],
        ]
        return "\n".join(head), InlineKeyboardMarkup(rows)
```

(`chans` / `n` are already assigned at the top of `render` — move the `chans = state["chans"]` / `n = len(chans)` lines above this block if they aren't already.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -m pytest tests/test_reactions.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add modules/telegram/reactions.py tests/test_reactions.py
git commit -m "feat: reaction asks open on a Random/Manual mode screen"
```

---

### Task 7: manual per-emoji amounts in the state machine

**Files:**
- Modify: `modules/telegram/reactions.py` — `new_state`, `reduce`, new `set_qty` / `missing_qty`, `orders_from`, `order_emoji`, `render`
- Test: `tests/test_reactions.py` (extend + update `orders_from` unpacking)

**Interfaces:**
- Consumes: `amode` from Task 6.
- Produces:
  - `state["qty"]`: `{channel idx (str) → {emoji idx (str) → int}}`.
  - `reduce` new actions: `"ask_qty:<emoji idx>"` (manual-mode select → caller sends a ForceReply prompt) and `"incomplete"` (Done pressed with amounts missing → caller alerts).
  - `reactions.set_qty(state, emoji_idx: int, amount: int) -> dict` — clamped to `QTY_MIN=1 .. QTY_MAX=100_000`; writes every channel in "all" mode, the current one in "per" mode.
  - `reactions.missing_qty(state) -> list[tuple[int, int]]` — `(channel idx, emoji idx)` pairs still without an amount (empty unless manual mode).
  - `orders_from(state) -> list[tuple[chat_id, link, emoji, qty | None]]` — **now 4-tuples**; `qty` is `None` outside manual mode.
  - `order_emoji(chat_id, link, emoji, qty: int | None = None)` — `qty` overrides the random roll.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_reactions.py`:

```python
# --- manual amounts ---------------------------------------------------------

def manual(*verbs):
    return press(reactions.new_state(41, CHANS), "mm", *verbs)


def test_manual_select_asks_for_a_quantity():
    s, action = manual(f"t{HEART}")
    assert action == f"ask_qty:{HEART}"
    assert s["sel"] == {"0": [HEART], "1": [HEART]}


def test_random_select_does_not_ask():
    _, action = press(reactions.new_state(41, CHANS), "mr", f"t{HEART}")
    assert action is None


def test_set_qty_writes_every_channel_in_all_mode():
    s, _ = manual(f"t{HEART}")
    s = reactions.set_qty(s, HEART, 100)
    assert s["qty"] == {"0": {str(HEART): 100}, "1": {str(HEART): 100}}


def test_set_qty_clamps():
    s, _ = manual(f"t{HEART}")
    assert reactions.set_qty(s, HEART, 0)["qty"]["0"][str(HEART)] == 1
    assert reactions.set_qty(s, HEART, 10**9)["qty"]["0"][str(HEART)] == 100_000


def test_set_qty_per_channel_mode_touches_only_the_current_channel():
    s, _ = manual(f"t{HEART}")
    s = reactions.set_qty(s, HEART, 50)
    s, _ = press(s, "pc", "nx")
    s = reactions.set_qty(s, HEART, 200)
    assert s["qty"]["0"][str(HEART)] == 50
    assert s["qty"]["1"][str(HEART)] == 200


def test_deselect_clears_the_amount():
    s, _ = manual(f"t{HEART}")
    s = reactions.set_qty(s, HEART, 100)
    s, action = press(s, f"t{HEART}")
    assert action is None
    assert s["qty"] == {"0": {}, "1": {}}


def test_done_requires_every_amount():
    s, _ = manual(f"t{HEART}")
    s2, action = press(s, "ap")
    assert action == "incomplete"
    assert reactions.missing_qty(s) == [(0, HEART), (1, HEART)]

    s = reactions.set_qty(s, HEART, 100)
    assert reactions.missing_qty(s) == []
    assert press(s, "ap")[1] == "apply"


def test_random_mode_never_reports_incomplete():
    assert press(reactions.new_state(41, CHANS), "mr", f"t{HEART}", "ap")[1] == "apply"


def test_orders_from_carries_manual_amounts():
    s, _ = manual(f"t{HEART}")
    s = reactions.set_qty(s, HEART, 77)
    orders = reactions.orders_from(s)
    assert all(qty == 77 for _, _, _, qty in orders)


def test_orders_from_random_mode_has_no_amounts():
    s, _ = press(reactions.new_state(41, CHANS), "mr", f"t{HEART}")
    assert all(qty is None for _, _, _, qty in reactions.orders_from(s))


def test_manual_render_shows_amounts_and_done():
    s, _ = manual(f"t{HEART}")
    s = reactions.set_qty(s, HEART, 100)
    text, kb = reactions.render(7, s)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert any("×100" in lbl for lbl in labels)
    assert any("Done" in lbl for lbl in labels)
```

And update the two existing `orders_from` tests to the 4-tuple shape:
- `test_orders_from_pairs_every_channel_with_every_emoji`: `{(c, e["name"]) for c, _, e, _ in orders}`.
- `test_orders_skip_channels_without_a_public_link`: `[c for c, _, _, _ in reactions.orders_from(s)]`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -m pytest tests/test_reactions.py -v`
Expected: new tests FAIL (`ask_qty` never returned, `set_qty` missing, 3-tuples).

- [ ] **Step 3: Implement**

In `reactions.py`:

Constants near `CHANNEL_POST_THRESHOLD`:

```python
# Bounds for a manually typed reaction amount (per emoji, per channel).
QTY_MIN, QTY_MAX = 1, 100_000
```

`new_state` — add:

```python
        "qty": {},   # manual mode: channel idx (str) -> {emoji idx (str) -> amount}
```

`reduce` — change the copy line to deep-copy `qty` too, and rewrite the toggle branch:

```python
    state = {**state, "sel": dict(state["sel"]),
             "qty": {k: dict(v) for k, v in state.get("qty", {}).items()}}
```

```python
    if verb.startswith("t") and verb[1:].isdigit():
        i = int(verb[1:])
        # "all" mode keeps every channel in sync, so switching to per-channel
        # later starts from what you already picked.
        targets = list(range(n)) if state["mode"] == "all" else [state["cur"]]
        adding = i not in _selected(state, targets[0])
        for t in targets:
            sel = list(_selected(state, t))
            state["sel"][str(t)] = sorted(sel + [i]) if adding \
                else [x for x in sel if x != i]
            if not adding:
                state["qty"].get(str(t), {}).pop(str(i), None)
        # Manual mode: a fresh selection immediately wants its amount — the
        # caller sends the ForceReply prompt. Deselecting just clears it.
        if adding and state.get("amode") == "manual":
            return state, f"ask_qty:{i}"
        return state, None
```

`reduce` — replace the `ap` branch:

```python
    if verb == "ap":
        if state.get("amode") == "manual" and missing_qty(state):
            return state, "incomplete"
        return state, "apply"
```

New functions after `reduce`:

```python
def set_qty(state: dict, emoji_idx: int, amount: int) -> dict:
    """Write a typed amount (clamped to QTY_MIN..QTY_MAX) for an emoji. Same
    fan-out rule as toggles: every channel in "all" mode, the current one in
    "per" mode — so amounts carry over into per-channel mode as starting
    points. Pure: returns a new state."""
    state = {**state, "qty": {k: dict(v) for k, v in state.get("qty", {}).items()}}
    amount = max(QTY_MIN, min(QTY_MAX, int(amount)))
    n = len(state["chans"])
    targets = range(n) if state["mode"] == "all" else [state["cur"]]
    for t in targets:
        state["qty"].setdefault(str(t), {})[str(emoji_idx)] = amount
    return state


def missing_qty(state: dict) -> list[tuple[int, int]]:
    """(channel idx, emoji idx) pairs selected in manual mode with no amount
    yet — Done is refused until this is empty. Always empty outside manual."""
    if state.get("amode") != "manual":
        return []
    out = []
    for i in range(len(state["chans"])):
        for e in _selected(state, i):
            if str(e) not in state.get("qty", {}).get(str(i), {}):
                out.append((i, e))
    return out
```

`orders_from` — 4-tuples:

```python
def orders_from(state: dict) -> list:
    """[(chat_id, link, emoji, qty)] the current selection would order; qty is
    the typed manual amount, or None for the random roll. Channels that never
    produced a public link are skipped — there is nothing to order against —
    and logged by the caller via summary()."""
    manual = state.get("amode") == "manual"
    out = []
    for i, chan in enumerate(state["chans"]):
        if not chan.get("link"):
            continue
        for e in _selected(state, i):
            if 0 <= e < len(EMOJI_SERVICES):
                qty = state.get("qty", {}).get(str(i), {}).get(str(e)) if manual else None
                out.append((chan["chat_id"], chan["link"], EMOJI_SERVICES[e], qty))
    return out
```

`order_emoji` — optional exact quantity:

```python
def order_emoji(chat_id: str, link: str, emoji: dict, qty: int | None = None) -> None:
    """One reaction order: the emoji's own service id against the post link.
    `qty` is the operator's typed manual amount; None rolls fresh so no two
    channels get identical counts."""
    log.info("[%s] %s reaction order", chat_id, emoji["name"])
    smm.place_order(link, qty or emoji_quantity(), emoji["service"])
```

`apply_orders` — pass the amount through (fully rewritten again in Task 9; this keeps it correct in between):

```python
    for chat_id, link, emoji, qty in orders:
        order_emoji(chat_id, link, emoji, qty)
```

`render` — in the emoji-button loop, show the amount and rename Apply in manual mode. Replace the loop body and the apply rows:

```python
    manual = state.get("amode") == "manual"
    qty_for = state.get("qty", {}).get(
        "0" if state["mode"] == "all" else str(state["cur"]), {})
    rows = []
    for i in range(0, len(EMOJI_SERVICES), 2):
        row = []
        for j, em in enumerate(EMOJI_SERVICES[i:i + 2], start=i):
            mark = "☑" if j in shown else "☐"
            label = f"{mark} {face(em)}"
            if manual and qty_for.get(str(j)):
                label += f" ×{qty_for[str(j)]}"
            row.append(InlineKeyboardButton(label, callback_data=f"r:{ask_id}:t{j}"))
        rows.append(row)

    if state["mode"] == "all":
        apply_label = "✅ Done" if manual else f"✅ Apply to all {n} channel(s)"
        rows.append([InlineKeyboardButton(apply_label, callback_data=f"r:{ask_id}:ap")])
        rows.append([InlineKeyboardButton("⚙ Per-channel…", callback_data=f"r:{ask_id}:pc"),
                     InlineKeyboardButton("✕ Skip", callback_data=f"r:{ask_id}:sk")])
    else:
        rows.append([InlineKeyboardButton("◂ prev", callback_data=f"r:{ask_id}:pv"),
                     InlineKeyboardButton("next ▸", callback_data=f"r:{ask_id}:nx")])
        rows.append([InlineKeyboardButton("✅ Done" if manual else "✅ Apply all",
                                          callback_data=f"r:{ask_id}:ap"),
                     InlineKeyboardButton("✕ Skip", callback_data=f"r:{ask_id}:sk")])
```

And the closing hint line:

```python
    head.append("Tap each reaction and reply with its amount, then Done."
                if manual else "Pick the reactions to buy, then Apply.")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -m pytest tests/test_reactions.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add modules/telegram/reactions.py tests/test_reactions.py
git commit -m "feat: per-emoji manual reaction amounts in the ask state machine"
```

---

### Task 8: durable delayed-order queue in the store

**Files:**
- Modify: `shared/config.py` (add `REACTION_DELAY_S`)
- Modify: `modules/telegram/queue_store.py` — `init` + four new functions
- Test: `tests/test_queue_store.py` (extend)

**Interfaces:**
- Consumes: existing `_conn` / `_now` helpers, `store` fixture from `tests/conftest.py`.
- Produces:
  - `config.REACTION_DELAY_S: int` (default 1200).
  - `queue_store.add_pending_order(link: str, service: str, quantity: int, name: str, due_at: str) -> int` (row id; `name` is the emoji's log name).
  - `queue_store.due_pending_orders(now: str | None = None) -> list[dict]` — rows with `due_at <= now` (default: current UTC), each with keys `id, link, service, quantity, name, due_at, attempts`.
  - `queue_store.bump_order_attempts(order_id: int) -> int` — new attempt count.
  - `queue_store.delete_pending_order(order_id: int) -> None`.

- [ ] **Step 1: Add the config knob**

In `shared/config.py`, in the autopilot section (after `TG_ASK_CHAT_ID`), add:

```python
# Emoji reaction orders fire this many seconds AFTER the post went out, so a
# post doesn't get reactions the second it appears. The per-post base order
# and the every-5th channel order stay immediate. 20 minutes by default.
REACTION_DELAY_S = _int_env("REACTION_DELAY_S", 1200)
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_queue_store.py`:

```python
# --- delayed reaction orders ------------------------------------------------

def test_pending_orders_due_and_future(store):
    store.add_pending_order("https://t.me/c/1", "5108", 40, "heart",
                            "2000-01-01T00:00:00+00:00")
    store.add_pending_order("https://t.me/c/2", "5110", 20, "like",
                            "2999-01-01T00:00:00+00:00")

    due = store.due_pending_orders()
    assert [d["link"] for d in due] == ["https://t.me/c/1"]
    assert due[0]["service"] == "5108" and due[0]["quantity"] == 40
    assert due[0]["name"] == "heart" and due[0]["attempts"] == 0


def test_pending_order_delete_and_attempts(store):
    oid = store.add_pending_order("https://t.me/c/1", "5108", 40, "heart",
                                  "2000-01-01T00:00:00+00:00")
    assert store.bump_order_attempts(oid) == 1
    assert store.bump_order_attempts(oid) == 2
    store.delete_pending_order(oid)
    assert store.due_pending_orders() == []


def test_pending_orders_survive_reinit(store):
    """A restart (init on an existing DB) must not lose queued orders."""
    store.add_pending_order("https://t.me/c/1", "5108", 40, "heart",
                            "2000-01-01T00:00:00+00:00")
    store.init()
    assert len(store.due_pending_orders()) == 1
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `py -m pytest tests/test_queue_store.py -v`
Expected: the 3 new tests FAIL (`AttributeError: ... add_pending_order`).

- [ ] **Step 4: Implement**

In `queue_store.init()`, after the `channel_counters` CREATE, add:

```python
        # Delayed BulkFollows reaction orders: written when an ask is applied,
        # placed by the news bot's per-minute flush once due_at has passed.
        # Durable so a restart between apply and due time loses nothing.
        c.execute(
            """CREATE TABLE IF NOT EXISTS pending_orders(
                   id       INTEGER PRIMARY KEY AUTOINCREMENT,
                   link     TEXT NOT NULL,
                   service  TEXT NOT NULL,
                   quantity INTEGER NOT NULL,
                   name     TEXT,           -- emoji log name, for the flush log
                   due_at   TEXT NOT NULL,  -- ISO UTC
                   attempts INTEGER NOT NULL DEFAULT 0)"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_pending_due ON pending_orders(due_at)")
```

At the end of the file (before the reaction-asks section or after it — keep sections tidy, add a new commented section):

```python
# --------------------------------------------------------------------------- #
# Delayed reaction orders                                                     #
# --------------------------------------------------------------------------- #


def add_pending_order(link: str, service: str, quantity: int, name: str,
                      due_at: str) -> int:
    """Queue one BulkFollows order to be placed once `due_at` (ISO UTC) has
    passed. Returns the row id."""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO pending_orders(link, service, quantity, name, due_at)"
            " VALUES(?,?,?,?,?)",
            (link, service, quantity, name, due_at),
        )
        return cur.lastrowid


def due_pending_orders(now: str | None = None) -> list[dict]:
    """Every order whose due_at has passed, oldest first."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM pending_orders WHERE due_at <= ? ORDER BY id",
            (now or _now(),),
        )
        return [dict(r) for r in rows]


def bump_order_attempts(order_id: int) -> int:
    """Count one failed placement attempt; the flush drops the row after a
    handful so a permanently rejected order can't retry forever."""
    with _conn() as c:
        c.execute("UPDATE pending_orders SET attempts = attempts + 1 WHERE id=?",
                  (order_id,))
        row = c.execute("SELECT attempts FROM pending_orders WHERE id=?",
                        (order_id,)).fetchone()
        return row["attempts"] if row else 0


def delete_pending_order(order_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM pending_orders WHERE id=?", (order_id,))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `py -m pytest tests/test_queue_store.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add shared/config.py modules/telegram/queue_store.py tests/test_queue_store.py
git commit -m "feat: durable pending_orders table + REACTION_DELAY_S"
```

---

### Task 9: delayed placement — schedule, flush, and the record_posts leg

**Files:**
- Modify: `modules/telegram/reactions.py` — imports, `apply_orders`, `record_posts`; new `schedule_emoji_orders`, `flush_due_orders`
- Test: `tests/test_reactions.py` (extend)

**Interfaces:**
- Consumes: Task 8's `queue_store` functions, `config.REACTION_DELAY_S`, `smm.place_order` (never raises; `None` = failed/skipped).
- Produces:
  - `reactions.schedule_emoji_orders(orders: list[tuple[chat_id, link, emoji, qty | None]], posted_at: str | None = None) -> int` — writes pending rows due at `posted_at` (ISO UTC, default now) + `REACTION_DELAY_S`; rolls `emoji_quantity()` NOW for `qty=None` so the stored row is concrete.
  - `reactions.apply_orders(state: dict, posted_at: str | None = None) -> int` — **no longer places orders**; queues them via `schedule_emoji_orders`. SQLite-only, safe to call on the event loop.
  - `reactions.flush_due_orders() -> int` — places every due order via `smm.place_order` (blocking — callers use `asyncio.to_thread`), deletes on success, drops after `MAX_ORDER_ATTEMPTS = 5` failures. Returns orders placed.
  - `record_posts(posted, links, emojis)` — the emoji leg now schedules instead of ordering immediately; per-post + every-5th orders unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_reactions.py` (note: these need the `store` fixture, which repoints `queue_store` at a temp DB — `reactions` calls `queue_store` module functions, so the fixture's monkeypatching covers them):

```python
# --- delayed orders ---------------------------------------------------------

def _applied_state(qty=None):
    s, _ = press(reactions.new_state(41, CHANS), "mm" if qty else "mr", f"t{HEART}")
    if qty:
        s = reactions.set_qty(s, HEART, qty)
    return s


def test_apply_orders_queues_instead_of_placing(store, monkeypatch):
    placed = []
    monkeypatch.setattr(reactions.smm, "place_order",
                        lambda link, q, svc: placed.append((link, q, svc)) or {})
    # pin the delay — the operator's real .env may override the default
    monkeypatch.setattr(reactions.config, "REACTION_DELAY_S", 1200)

    n = reactions.apply_orders(_applied_state(qty=77),
                               posted_at="2026-08-05T10:00:00+00:00")

    assert n == 2 and placed == []          # nothing hit the panel yet
    rows = store.due_pending_orders("2026-08-05T10:20:00+00:00")
    assert len(rows) == 2
    assert all(r["quantity"] == 77 for r in rows)
    assert all(r["due_at"] == "2026-08-05T10:20:00+00:00" for r in rows)
    # a minute early: not due yet
    assert store.due_pending_orders("2026-08-05T10:19:00+00:00") == []


def test_apply_orders_random_mode_rolls_a_concrete_quantity(store, monkeypatch):
    monkeypatch.setattr(reactions, "emoji_quantity", lambda: 33)
    reactions.apply_orders(_applied_state(), posted_at="2000-01-01T00:00:00+00:00")
    assert all(r["quantity"] == 33 for r in store.due_pending_orders())


def test_flush_places_due_orders_and_deletes_them(store, monkeypatch):
    placed = []
    monkeypatch.setattr(reactions.smm, "place_order",
                        lambda link, q, svc: placed.append((link, q, svc)) or {"order": 1})
    store.add_pending_order("https://t.me/c/1", "5108", 40, "heart",
                            "2000-01-01T00:00:00+00:00")
    store.add_pending_order("https://t.me/c/2", "5110", 20, "like",
                            "2999-01-01T00:00:00+00:00")

    assert reactions.flush_due_orders() == 1
    assert placed == [("https://t.me/c/1", 40, "5108")]
    # the future order is untouched, the placed one is gone
    assert len(store.due_pending_orders("2999-06-01T00:00:00+00:00")) == 1


def test_flush_drops_an_order_after_max_attempts(store, monkeypatch):
    monkeypatch.setattr(reactions.smm, "place_order", lambda *a: None)
    store.add_pending_order("https://t.me/c/1", "5108", 40, "heart",
                            "2000-01-01T00:00:00+00:00")

    for _ in range(reactions.MAX_ORDER_ATTEMPTS):
        reactions.flush_due_orders()
    assert store.due_pending_orders() == []   # dropped, not retried forever


def test_record_posts_delays_the_emoji_leg(store, monkeypatch):
    placed = []
    monkeypatch.setattr(reactions.smm, "place_order",
                        lambda link, q, svc: placed.append(svc) or {"order": 1})

    reactions.record_posts(["@news_eu"], [("@news_eu", "https://t.me/news_eu/1")],
                           [reactions.EMOJI_SERVICES[0]])

    # the per-post base order fired immediately, the emoji one is queued
    assert placed and all(s != "5108" for s in placed)
    future = store.due_pending_orders("2999-01-01T00:00:00+00:00")
    assert [r["service"] for r in future] == ["5108"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -m pytest tests/test_reactions.py -v`
Expected: new tests FAIL (`schedule_emoji_orders` / `flush_due_orders` / `MAX_ORDER_ATTEMPTS` missing; `apply_orders` places immediately).

- [ ] **Step 3: Implement**

In `reactions.py`, add to the imports:

```python
from datetime import datetime, timedelta, timezone
```

Add near `THRESHOLD_QUANTITY`:

```python
# A pending order is dropped after this many failed placement attempts (one
# per flush minute) — a rejected service id must not retry forever.
MAX_ORDER_ATTEMPTS = 5
```

Replace `apply_orders` and add the two new functions:

```python
def schedule_emoji_orders(orders: list, posted_at: str | None = None) -> int:
    """Queue [(chat_id, link, emoji, qty)] as pending rows due REACTION_DELAY_S
    after `posted_at` (ISO UTC, default: now) — a post shouldn't get reactions
    the second it appears. Random quantities are rolled NOW so the stored row
    is concrete. SQLite-only; the flush does the actual (blocking) placing."""
    try:
        base = datetime.fromisoformat(posted_at) if posted_at \
            else datetime.now(timezone.utc)
    except ValueError:
        base = datetime.now(timezone.utc)
    due = (base + timedelta(seconds=config.REACTION_DELAY_S)) \
        .isoformat(timespec="seconds")
    n = 0
    for chat_id, link, emoji, qty in orders:
        if not link:
            continue
        queue_store.add_pending_order(link, emoji["service"],
                                      qty or emoji_quantity(), emoji["name"], due)
        log.info("[%s] %s reaction queued, due %s", chat_id, emoji["name"], due)
        n += 1
    return n


def apply_orders(state: dict, posted_at: str | None = None) -> int:
    """Queue every (channel, emoji) order the ask selected, due
    REACTION_DELAY_S after `posted_at`. Returns the number queued. Pure
    SQLite — no HTTP, safe on the event loop."""
    return schedule_emoji_orders(orders_from(state), posted_at)


def flush_due_orders() -> int:
    """Place every pending order whose due time has passed. Blocking HTTP —
    async callers use asyncio.to_thread. Success (or MAX_ORDER_ATTEMPTS
    failures) deletes the row; anything transient stays for the next flush."""
    placed = 0
    for row in queue_store.due_pending_orders():
        result = smm.place_order(row["link"], row["quantity"], row["service"])
        if result is not None:
            queue_store.delete_pending_order(row["id"])
            placed += 1
        elif queue_store.bump_order_attempts(row["id"]) >= MAX_ORDER_ATTEMPTS:
            log.error("pending order %d (%s, %s) dropped after %d attempts",
                      row["id"], row["name"], row["link"], MAX_ORDER_ATTEMPTS)
            queue_store.delete_pending_order(row["id"])
    return placed
```

In `record_posts`, replace the emoji loop:

```python
        if link:
            order_post(chat_id, link)
            if emojis:
                # Pre-selected reactions (manual picker) obey the same delay
                # as ask-applied ones — the post just went out, so due = now
                # + REACTION_DELAY_S.
                schedule_emoji_orders([(chat_id, link, e, None) for e in emojis])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -m pytest tests/test_reactions.py -v` then `py -m pytest tests -q`
Expected: all reactions tests PASS; no new failures elsewhere.

- [ ] **Step 5: Commit**

```bash
git add modules/telegram/reactions.py tests/test_reactions.py
git commit -m "feat: emoji orders queue durably and fire 20 min after the post"
```

---

### Task 10: news-bot wiring — ForceReply amounts + per-minute flush

**Files:**
- Modify: `modules/telegram/news_bot.py` — imports, `_on_ask_callback` (lines 714-752), `on_message` (reply router at the top), `_on_start`, `main` (startup warning)

**Interfaces:**
- Consumes: `reactions.reduce` actions `"ask_qty:<i>"` / `"incomplete"` (Task 7), `reactions.set_qty`, `reactions.face`, `reactions.EMOJI_SERVICES`, `reactions.apply_orders(state, posted_at)`, `reactions.flush_due_orders`, `config.REACTION_DELAY_S`, `autopilot.ask_chat_id()`.
- Produces: module-global `_qty_prompts: dict[int, tuple[int, int]]` (ForceReply prompt message_id → (ask_id, emoji idx)); JobQueue job `"order-flush"` every 60 s. Restart behavior: an orphaned prompt is recovered by tapping the emoji again (deselect, reselect → fresh prompt); typed amounts are already in SQLite.

- [ ] **Step 1: Import ForceReply and add the prompt map**

Change the telegram import to:

```python
from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update  # noqa: E402
```

Below `_albums = {}`, add:

```python
# Open manual-amount prompts: ForceReply message_id → (ask_id, emoji index).
# In-memory: a restart orphans a prompt, but the ask itself (and every amount
# already typed) is safe in SQLite — tapping the emoji again re-prompts.
_qty_prompts = {}
```

- [ ] **Step 2: Rework `_on_ask_callback`**

Replace everything from the existing `ask = queue_store.get_ask(ask_id)` line down to the end of the function (the whole guard + `await q.answer()` + reduce + action handling — answering must wait until we know whether to alert) with:

```python
    ask = queue_store.get_ask(ask_id)
    if ask is None or ask["status"] != "open":
        await q.answer("That ask is already closed.", show_alert=True)
        return

    state, action = reactions.reduce(ask["state"], verb)
    queue_store.save_ask_state(ask_id, state)

    if action == "incomplete":
        await q.answer("Every selected emoji needs an amount — tap each one "
                       "and reply with a number, then Done.", show_alert=True)
        return
    await q.answer()

    if isinstance(action, str) and action.startswith("ask_qty:"):
        emoji_idx = int(action.split(":", 1)[1])
        text, keyboard = reactions.render(ask_id, state)
        try:
            await q.edit_message_text(text, reply_markup=keyboard,
                                      disable_web_page_preview=True)
        except Exception:
            pass
        prompt = await q.message.chat.send_message(
            f"How many {reactions.face(reactions.EMOJI_SERVICES[emoji_idx])}? "
            "(reply to this message with a number)",
            reply_markup=ForceReply(selective=True))
        _qty_prompts[prompt.message_id] = (ask_id, emoji_idx)
        return

    if action is None:
        text, keyboard = reactions.render(ask_id, state)
        try:
            await q.edit_message_text(text, reply_markup=keyboard,
                                      disable_web_page_preview=True)
        except Exception:  # "message is not modified" — same tap twice
            pass
        return

    if action == "apply":
        # Orders are queued in SQLite (due REACTION_DELAY_S after the post) —
        # the per-minute flush places them. SQLite-only, no thread needed.
        n = reactions.apply_orders(state, ask.get("created_at"))
        queue_store.close_ask(ask_id, "applied")
        await q.edit_message_text(
            f"{reactions.summary(state, True)}\n"
            f"({n} order(s) queued — firing ~{config.REACTION_DELAY_S // 60} min "
            "after the post)")
    else:
        queue_store.close_ask(ask_id, "skipped")
        await q.edit_message_text(reactions.summary(state, False))
```

(The existing `try/except ValueError` verb parse above stays untouched.)

- [ ] **Step 3: Route ForceReply answers in `on_message`**

At the top of `on_message`, right after `text = msg.caption or msg.text or ""`, BEFORE the caption-replace block, add:

```python
    # Reply to a manual-amount prompt = the typed quantity for one emoji.
    reply = msg.reply_to_message
    if reply is not None and reply.message_id in _qty_prompts:
        ask_id, emoji_idx = _qty_prompts.pop(reply.message_id)
        ask = queue_store.get_ask(ask_id)
        if ask is None or ask["status"] != "open":
            return
        try:
            amount = int(text.strip())
            if amount <= 0:
                raise ValueError
        except ValueError:
            prompt = await msg.reply_text(
                f"Not a number — how many "
                f"{reactions.face(reactions.EMOJI_SERVICES[emoji_idx])}?",
                reply_markup=ForceReply(selective=True))
            _qty_prompts[prompt.message_id] = (ask_id, emoji_idx)
            return
        state = reactions.set_qty(ask["state"], emoji_idx, amount)
        queue_store.save_ask_state(ask_id, state)
        body, keyboard = reactions.render(ask_id, state)
        try:
            await context.bot.edit_message_text(
                chat_id=ask["chat_id"], message_id=ask["message_id"], text=body,
                reply_markup=keyboard, disable_web_page_preview=True)
        except Exception:
            pass
        try:  # the prompt has served its purpose
            await context.bot.delete_message(msg.chat.id, reply.message_id)
        except Exception:
            pass
        return
```

The existing `reply = msg.reply_to_message` line a few lines below becomes redundant — delete it (the caption-replace block reuses the `reply` variable just assigned).

- [ ] **Step 4: Schedule the flush + startup warning**

Replace `_on_start`:

```python
async def _flush_orders_job(context) -> None:
    """Per-minute: place every delayed reaction order that has come due."""
    try:
        n = await asyncio.to_thread(reactions.flush_due_orders)
        if n:
            log.info("order flush: %d placed", n)
    except Exception:
        log.exception("order flush crashed — next minute retries")


async def _on_start(app) -> None:
    """Queue the autopilot's first tick and the order flush once the event
    loop is running."""
    autopilot.schedule(app)
    if app.job_queue is not None:
        app.job_queue.run_repeating(_flush_orders_job, interval=60, first=15,
                                    name="order-flush")
```

In `main()`, after the two `log.info(...)` calls, add:

```python
    if str(autopilot.ask_chat_id()) != str(CHAT_ID):
        # The ForceReply amount router only listens in the control group.
        log.warning("TG_ASK_CHAT_ID differs from the control group — manual "
                    "reaction amounts can only be typed in the control group")
```

- [ ] **Step 5: Verify — suite + import**

Run: `py -m pytest tests -q` then `py -c "import modules.telegram.news_bot"`
Expected: no new failures; import exits 0.

- [ ] **Step 6: Commit**

```bash
git add modules/telegram/news_bot.py
git commit -m "feat: ForceReply manual amounts + per-minute delayed-order flush"
```

---

### Task 11: Twitter edge-trim before branding

**Files:**
- Modify: `shared/branding.py` — `EDGE_TRIM`, `_filter_graph`, `render_branded`
- Modify: `modules/telegram/news_bot.py` — `_do_render` (line ~565, the `branding.render_branded` call)
- Test: `tests/test_branding.py` (extend)

**Interfaces:**
- Consumes: `state["origin"]` recorded by Task 3.
- Produces: `branding.render_branded(video_path, headline, logo_path, out_path, trim_edges: bool = False)`; `branding.EDGE_TRIM = 0.015`; `branding._filter_graph(font_path, text_path, trim_edges: bool = False)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_branding.py` (filter-graph section):

```python
def test_filter_graph_edge_trim_crops_both_source_chains():
    graph = branding._filter_graph("/f/font.ttf", "/t/text.txt", trim_edges=True)
    # 1.5 % per side off the width, height untouched — applied to BOTH the
    # blur background and the foreground so the strip can't leak back in.
    assert graph.count("crop=iw*0.97:ih:iw*0.015:0") == 2


def test_filter_graph_no_edge_trim_by_default():
    graph = branding._filter_graph("/f/font.ttf", "/t/text.txt")
    assert "crop=iw*" not in graph
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -m pytest tests/test_branding.py -v`
Expected: the 2 new tests FAIL (`_filter_graph` takes 2 args / no crop).

- [ ] **Step 3: Implement**

In `shared/branding.py`, after `FADE_DUR`, add:

```python
EDGE_TRIM = 0.015           # trim_edges: fraction of the WIDTH cut per side —
                            # Twitter clips carry ~1 mm hairlines on both edges
```

Change `_filter_graph` to:

```python
def _filter_graph(font_path: str, text_path: str, trim_edges: bool = False) -> str:
    # The trim is applied to BOTH chains that read [0:v] (blur background and
    # foreground) so the cropped strip can't leak back in via the blur.
    trim = f"crop=iw*{1 - 2 * EDGE_TRIM}:ih:iw*{EDGE_TRIM}:0," if trim_edges else ""
    return (
        f"[0:v]{trim}scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
        f"crop={OUT_W}:{OUT_H},gblur=sigma=30[bg];"
        f"[0:v]{trim}scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease[fg];"
```

(the rest of the graph string is unchanged).

Change `render_branded`'s signature and the ffmpeg call:

```python
def render_branded(video_path: str, headline: str, logo_path: str, out_path: str,
                   trim_edges: bool = False) -> str:
```

and in the docstring add one line: `trim_edges crops EDGE_TRIM of the width off each side first (Twitter's hairline edge artifacts).` The `-filter_complex` argument becomes `_filter_graph(FONT_PATH, text_path, trim_edges)`.

- [ ] **Step 4: Wire the origin in `_do_render`**

In `news_bot.py` `_do_render`, the render call becomes:

```python
            path = await asyncio.to_thread(
                branding.render_branded, src, cache[lang], b["logo"], out,
                trim_edges=state.get("origin") == "twitter")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `py -m pytest tests/test_branding.py -v` (the end-to-end test needs ffmpeg on PATH — it is on this machine) then `py -c "import modules.telegram.news_bot"`
Expected: all PASS; import exits 0.

- [ ] **Step 6: Commit**

```bash
git add shared/branding.py modules/telegram/news_bot.py tests/test_branding.py
git commit -m "feat: edge-trim Twitter clips before branding (1.5% per side)"
```

---

### Task 12: control-group message tracking

**Files:**
- Modify: `modules/telegram/queue_store.py` — `init` + three new functions
- Modify: `modules/telegram/news_bot.py` — `_track` helper + wrap every control-group send site
- Modify: `modules/telegram/autopilot.py` — track the ask message in `deliver_ask`
- Test: `tests/test_queue_store.py` (extend)

**Interfaces:**
- Consumes: `store` fixture.
- Produces:
  - `queue_store.track_group_message(chat_id, message_id: int) -> None` (idempotent; chat_id coerced to `str`).
  - `queue_store.tracked_message_ids(chat_id) -> list[int]` (ascending).
  - `queue_store.clear_group_messages(chat_id, message_ids: list[int]) -> None`.
  - news_bot `_track(msg) -> msg` — records any message living in the control group; returns it unchanged so send sites wrap in place. Task 13's weekly job consumes the table.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_queue_store.py`:

```python
# --- control-group message tracking -----------------------------------------

def test_track_and_clear_group_messages(store):
    store.track_group_message("-100777", 5)
    store.track_group_message("-100777", 3)
    store.track_group_message("-100777", 3)      # duplicate — idempotent
    store.track_group_message("-100999", 8)      # different chat

    assert store.tracked_message_ids("-100777") == [3, 5]
    store.clear_group_messages("-100777", [3])
    assert store.tracked_message_ids("-100777") == [5]
    assert store.tracked_message_ids("-100999") == [8]


def test_track_group_message_accepts_int_chat_id(store):
    store.track_group_message(-100777, 12)
    assert store.tracked_message_ids("-100777") == [12]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -m pytest tests/test_queue_store.py -v`
Expected: 2 new FAIL (`track_group_message` missing).

- [ ] **Step 3: Implement the store side**

In `queue_store.init()`, after the `pending_orders` block:

```python
        # Every message the bot sees or sends in the control group, for the
        # weekly cleanup. The Bot API can't list chat history, so the bot can
        # only ever delete what it recorded here.
        c.execute(
            """CREATE TABLE IF NOT EXISTS group_messages(
                   chat_id     TEXT NOT NULL,
                   message_id  INTEGER NOT NULL,
                   recorded_at TEXT NOT NULL,
                   PRIMARY KEY(chat_id, message_id))"""
        )
```

New section at the end of the file:

```python
# --------------------------------------------------------------------------- #
# Control-group message tracking (weekly cleanup)                             #
# --------------------------------------------------------------------------- #


def track_group_message(chat_id, message_id: int) -> None:
    """Remember one control-group message id for the weekly wipe. Idempotent."""
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO group_messages(chat_id, message_id, recorded_at)"
            " VALUES(?,?,?)",
            (str(chat_id), message_id, _now()),
        )


def tracked_message_ids(chat_id) -> list[int]:
    with _conn() as c:
        rows = c.execute(
            "SELECT message_id FROM group_messages WHERE chat_id=?"
            " ORDER BY message_id",
            (str(chat_id),),
        )
        return [r["message_id"] for r in rows]


def clear_group_messages(chat_id, message_ids: list[int]) -> None:
    with _conn() as c:
        c.executemany(
            "DELETE FROM group_messages WHERE chat_id=? AND message_id=?",
            [(str(chat_id), m) for m in message_ids],
        )
```

- [ ] **Step 4: Run store tests**

Run: `py -m pytest tests/test_queue_store.py -v`
Expected: all PASS.

- [ ] **Step 5: Wire the news bot**

In `news_bot.py`, below `_extract_media`, add:

```python
def _track(msg):
    """Record a control-group message for the weekly cleanup (incoming AND
    outgoing — the Bot API can't list history, so anything not recorded here
    survives the wipe). Returns `msg` so send sites can wrap in place."""
    if msg is not None and msg.chat.id == CHAT_ID:
        queue_store.track_group_message(CHAT_ID, msg.message_id)
    return msg
```

Wrap every place a message is CREATED in the control group (edits reuse the same id — no re-track needed):

- `on_message` top, right after the `if msg is None: return` guard: `_track(msg)` (records every incoming operator message).
- `_prompt`: `prompt = _track(await msg.reply_text(...))`
- `_gate`: `prompt = _track(await msg.reply_text(...))`
- `_handle_url`: `note = _track(await msg.reply_text("⏳ downloading …"))`
- `_do_render`: the two `q.message.chat.send_message(...)` calls (too-big note and no-destinations note) and the `send_video(...)` call — wrap each in `_track(...)`; the publish-picker `prompt = await q.message.chat.send_message(...)` becomes `prompt = _track(await q.message.chat.send_message(...))`.
- `_on_ask_callback` (Task 10): `prompt = _track(await q.message.chat.send_message(...))` for the ForceReply prompt.
- `on_message` qty router (Task 10): `prompt = _track(await msg.reply_text(...))` for the re-prompt.
- `cmd_queue`, `cmd_autopilot`, `cmd_asks`: wrap the `reply_text` results, e.g. `_track(await update.effective_message.reply_text("\n".join(lines)))`. Also `_track(update.effective_message)` at the top of each so the operator's `/command` message itself is wiped too.
- `cmd_next`: `msg = _track(await update.effective_message.reply_text("⏳ running one drip …"))` and `_track(update.effective_message)`.

In `autopilot.py` `deliver_ask`, after the successful `send_message`, before `queue_store.bind_ask(...)`:

```python
    # Ask messages count for the weekly control-group wipe too.
    queue_store.track_group_message(ask_chat_id(), msg.message_id)
```

- [ ] **Step 6: Verify — suite + import**

Run: `py -m pytest tests -q` then `py -c "import modules.telegram.news_bot"`
Expected: no new failures; import exits 0.

- [ ] **Step 7: Commit**

```bash
git add modules/telegram/queue_store.py modules/telegram/news_bot.py modules/telegram/autopilot.py tests/test_queue_store.py
git commit -m "feat: track every control-group message for the weekly cleanup"
```

---

### Task 13: weekly cleanup job (Mondays 04:00 local)

**Files:**
- Create: `modules/telegram/cleanup.py`
- Modify: `modules/telegram/news_bot.py` — `_on_start` (register the job)
- Test: `tests/test_cleanup.py` (new file)

**Interfaces:**
- Consumes: `queue_store.open_asks` / `close_ask` / `tracked_message_ids` / `clear_group_messages` (Tasks 8/12).
- Produces: `cleanup.wipe_chat(bot, chat_id) -> tuple[int, int]` (deleted, failed) — async; closes open asks as skipped, deletes tracked messages in batches of `cleanup.BATCH = 100` via `bot.delete_messages`, falls back to per-message `bot.delete_message` when a batch call fails (old messages, missing method on old PTB), always clears the attempted rows. news_bot registers it as JobQueue job `"weekly-cleanup"`, Mondays 04:00 **local** time.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cleanup.py`:

```python
"""modules/telegram/cleanup.py — the weekly control-group wipe, against a stub
bot. Offline as always."""

import asyncio

from modules.telegram import cleanup, reactions


class StubBot:
    """Records deletions; `fail_batches` makes delete_messages raise so the
    per-message fallback path runs; `fail_ids` makes individual deletes fail
    (already deleted / older than the bot's rights allow)."""

    def __init__(self, fail_batches=False, fail_ids=()):
        self.batches, self.singles = [], []
        self.fail_batches = fail_batches
        self.fail_ids = set(fail_ids)

    async def delete_messages(self, chat_id, message_ids):
        if self.fail_batches:
            raise RuntimeError("batch delete refused")
        self.batches.append(list(message_ids))

    async def delete_message(self, chat_id, message_id):
        if message_id in self.fail_ids:
            raise RuntimeError("message can't be deleted")
        self.singles.append(message_id)


def wipe(bot, chat_id="-100777"):
    return asyncio.run(cleanup.wipe_chat(bot, chat_id))


def test_wipe_deletes_in_batches_of_100(store):
    for i in range(1, 251):
        store.track_group_message("-100777", i)
    bot = StubBot()

    deleted, failed = wipe(bot)

    assert deleted == 250 and failed == 0
    assert [len(b) for b in bot.batches] == [100, 100, 50]
    assert store.tracked_message_ids("-100777") == []   # table cleared


def test_wipe_falls_back_to_single_deletes(store):
    for i in (1, 2, 3):
        store.track_group_message("-100777", i)
    bot = StubBot(fail_batches=True, fail_ids={2})

    deleted, failed = wipe(bot)

    assert deleted == 2 and failed == 1
    assert bot.singles == [1, 3]
    # failed ids are cleared too — they'll never become deletable
    assert store.tracked_message_ids("-100777") == []


def test_wipe_closes_open_asks_as_skipped(store):
    ask_id = store.open_ask(41, reactions.new_state(41, [
        {"chat_id": "@c", "link": "https://t.me/c/1"}]))

    wipe(StubBot())

    assert store.get_ask(ask_id)["status"] == "skipped"
    assert store.open_asks() == []


def test_wipe_leaves_other_chats_alone(store):
    store.track_group_message("-100777", 1)
    store.track_group_message("-100999", 2)

    wipe(StubBot(), "-100777")

    assert store.tracked_message_ids("-100999") == [2]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -m pytest tests/test_cleanup.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'modules.telegram.cleanup'`.

- [ ] **Step 3: Implement `cleanup.py`**

Create `modules/telegram/cleanup.py`:

```python
"""
modules/telegram/cleanup.py — the weekly control-group wipe.

Every Monday at 04:00 local time (registered by news_bot as a JobQueue job)
the control group is emptied: open reaction asks are closed as skipped, then
every message the bot has TRACKED there (queue_store.group_messages — the Bot
API can't list chat history, so tracked = seen incoming + sent outgoing) is
deleted in batches of 100. Messages posted while the bot was down were never
tracked and survive.

The bot needs the "Delete messages" admin right in the group to remove the
operator's messages; without it only its own recent (<48 h) ones are
deletable — failures are counted, logged and moved past, never raised.

Destination channels are never touched: only the chat id passed in is wiped.
"""

import logging

from modules.telegram import queue_store

log = logging.getLogger(__name__)

# Bot API delete_messages caps at 100 ids per call.
BATCH = 100


async def wipe_chat(bot, chat_id) -> tuple[int, int]:
    """Close every open ask as skipped, then delete every tracked message in
    `chat_id`. Returns (deleted, failed). Rows are cleared for every id
    attempted — an id that can't be deleted now never will be (too old), so
    keeping it would just re-fail forever."""
    for ask in queue_store.open_asks():
        queue_store.close_ask(ask["id"], "skipped")
        log.info("cleanup: ask %d closed as skipped", ask["id"])

    ids = queue_store.tracked_message_ids(str(chat_id))
    deleted = failed = 0
    for i in range(0, len(ids), BATCH):
        batch = ids[i:i + BATCH]
        try:
            # AttributeError (PTB < 20.8 has no delete_messages) falls through
            # to the per-message path along with any API refusal.
            await bot.delete_messages(chat_id=chat_id, message_ids=batch)
            deleted += len(batch)
        except Exception:
            for mid in batch:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=mid)
                    deleted += 1
                except Exception:
                    failed += 1
        queue_store.clear_group_messages(str(chat_id), batch)

    log.info("cleanup: %d message(s) deleted, %d failed", deleted, failed)
    return deleted, failed
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -m pytest tests/test_cleanup.py -v`
Expected: 4 PASS.

- [ ] **Step 5: Register the weekly job**

In `news_bot.py`: add `cleanup` to the `from modules.telegram import (...)` list. Add `import datetime as dt` to the stdlib imports. Then extend `_on_start` (from Task 10) and add the callback:

```python
async def _weekly_cleanup_job(context) -> None:
    """Mondays 04:00 local: wipe the control group (tracked messages + open
    asks). Needs the "Delete messages" admin right for the operator's own
    messages."""
    try:
        deleted, failed = await cleanup.wipe_chat(context.bot, CHAT_ID)
        log.info("weekly cleanup: %d deleted, %d failed", deleted, failed)
    except Exception:
        log.exception("weekly cleanup crashed — next Monday retries")
```

and inside `_on_start`'s `if app.job_queue is not None:` block:

```python
        # PTB v20: run_daily days are 0-6 = Monday-Sunday; a tz-aware time is
        # required for "local", naive would be read as UTC.
        local_tz = dt.datetime.now().astimezone().tzinfo
        app.job_queue.run_daily(_weekly_cleanup_job,
                                time=dt.time(4, 0, tzinfo=local_tz),
                                days=(0,), name="weekly-cleanup")
```

- [ ] **Step 6: Verify — suite + import**

Run: `py -m pytest tests -q` then `py -c "import modules.telegram.news_bot"`
Expected: no new failures; import exits 0.

- [ ] **Step 7: Commit**

```bash
git add modules/telegram/cleanup.py modules/telegram/news_bot.py tests/test_cleanup.py
git commit -m "feat: weekly control-group wipe — Mondays 04:00, tracked messages + open asks"
```

---

### Task 14: docs + final verification

**Files:**
- Modify: `CLAUDE.md` (project guide at repo root)
- Modify: `docs/superpowers/specs/2026-08-04-photos-reactions-amounts-design.md` (mark implemented)

- [ ] **Step 1: Update CLAUDE.md**

In the files table / layout section:
- `shared/reel_downloader.py` row: mention `download_any` (yt-dlp video-first → gallery-dl photo fallback, ≤10 photos, `GALLERY_DL_COOKIES_BROWSER` for IG login) alongside the existing text.
- `modules/telegram/reactions.py` row: add the mode screen (Random/Manual), `set_qty`/`missing_qty`, the `ask_qty:`/`incomplete` actions, and that `apply_orders` now QUEUES orders (due `REACTION_DELAY_S` after the post) placed by news_bot's per-minute flush.
- `modules/telegram/news_bot.py` row: branded publishes open a reaction ask; URL posts fall back to photo albums and record their origin; Twitter-origin brand renders are edge-trimmed; weekly Monday-04:00 control-group wipe.
- New row for `modules/telegram/cleanup.py` (weekly wipe, tracked-messages constraint, Delete-messages admin right).
- `shared/branding.py` row: `trim_edges` / `EDGE_TRIM`.
- `modules/telegram/queue_store.py` row: `pending_orders` + `group_messages` tables.

- [ ] **Step 2: Mark the spec implemented**

At the top of the spec file, change the intro line to note: `Implemented 2026-08-05 — see docs/superpowers/plans/2026-08-05-photos-reactions-amounts.md.`

- [ ] **Step 3: Full verification**

Run: `py -m pytest tests -q`
Expected: everything passes except the 3 pre-existing `tests/test_autopilot.py` failures (TG_FIRST_TICK=21:00 in the operator's `.env`).
Run: `py -c "import modules.telegram.news_bot, modules.telegram.cleanup, shared.reel_downloader, shared.branding"`
Expected: exits 0.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-08-04-photos-reactions-amounts-design.md
git commit -m "docs: photos + reactions + amounts feature batch shipped"
```

- [ ] **Step 5: Tell the operator**

Remind the user (not a code step): restart the news bot to pick everything up; optionally set `GALLERY_DL_COOKIES_BROWSER=chrome` in `.env` for logged-in IG photo downloads; grant the bot the **"Delete messages" admin right** in the control group for the weekly wipe to remove their messages too.
