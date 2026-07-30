# Branded Clips Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send a video + headline to the news bot, pick brands, get back one variant per brand (logo top-right, headline lower third), then pick which brand→platform pairs to publish.

**Architecture:** A pure ffmpeg renderer in `shared/branding.py`; brand config in `shared/config.py` (`BRANDS` env + `brands/<name>/logo.png`); the pure picker state/keyboards in a new `modules/telegram/branded.py` (same lift-out pattern as `reactions.py`, because `news_bot.py` is not importable in tests — it raises `SystemExit` without env); the Telegram wiring (gate, callbacks, render, publish) in `news_bot.py`.

**Tech Stack:** Python 3.11+, ffmpeg/ffprobe on PATH, python-telegram-bot (existing), pytest.

**Spec:** `docs/superpowers/specs/2026-07-31-branded-clips-design.md` — read it first.

## Global Constraints

- Canvas 1080×1920; logo scaled to 180 px wide, 40 px margin, top-right; headline `fontsize=64`, white on `black@0.55` box, `boxborderw=24`, block anchored at `y=h*0.72`, centered; ≤ 3 lines of ~24 chars, ellipsis truncation.
- Encode profile identical to `shorts_format.ensure_short`: libx264 veryfast, crf 21, yuv420p, aac 128k, `+faststart`.
- Headline text reaches ffmpeg ONLY via `textfile=` (UTF-8 temp file) — never interpolated into the filter string.
- One failed brand/pair never blocks the others.
- Publish picker: YT pairs hidden when duration > `shorts_format.MAX_SHORT_S` (180 s); all pairs default OFF.
- Bot API caps enforced: source download 20 MB (`_BOT_FILE_LIMIT`), sending a render back 50 MB (note instead of file; publishing unaffected for YT/X — TG channel post of a >50 MB file will fail and be reported per-pair).
- Callback data namespaced `b:`; stays under Telegram's 64-byte cap.
- Tests are offline; the only test allowed to invoke ffmpeg is the integration render test, and it `pytest.skip`s when ffmpeg is absent.
- Use `py` (never `python`) in all commands.

---

### Task 1: Brand config (`shared/config.py`) + scaffolding

**Files:**
- Modify: `shared/config.py` (append after the `YT_DESTINATIONS` block, ~line 155)
- Create: `brands/README.md`
- Modify: `.env.example` (append)
- Test: `tests/test_config.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces: `config.BRANDS: list[dict]` — `[{"name": str, "lang": str, "tg": str, "yt": str, "tw": str, "logo": str(abs path)}]`; helper `config._parse_brands(raw: str, env: Mapping) -> list[dict]`. Tasks 3–4 rely on these exact keys.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_config.py`):

```python
from shared import config


def test_parse_brands_names_langs_and_env_pointers():
    env = {
        "BRAND_MIRNEWS_TG": "@mir_news",
        "BRAND_MIRNEWS_YT": "mirnews",
        "BRAND_RUS_NEWS_TW": "rusnews",
    }
    brands = config._parse_brands("mirnews:en, rus-news:ru", env)
    assert [b["name"] for b in brands] == ["mirnews", "rus-news"]
    assert brands[0]["lang"] == "en" and brands[1]["lang"] == "ru"
    # env key: name uppercased, non-alphanumerics -> "_"
    assert brands[0]["tg"] == "@mir_news"
    assert brands[0]["yt"] == "mirnews"
    assert brands[0]["tw"] == ""            # unset platform -> empty string
    assert brands[1]["tw"] == "rusnews"     # rus-news -> BRAND_RUS_NEWS_TW
    assert brands[1]["tg"] == "" and brands[1]["yt"] == ""


def test_parse_brands_logo_path_and_empty_input():
    brands = config._parse_brands("mirnews:en", {})
    assert brands[0]["logo"] == config.os.path.join(
        config.ROOT_DIR, "brands", "mirnews", "logo.png"
    )
    assert config._parse_brands("", {}) == []
    assert config._parse_brands("  ,  ", {}) == []


def test_parse_brands_lang_optional():
    brands = config._parse_brands("solo", {})
    assert brands[0]["name"] == "solo" and brands[0]["lang"] == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -m pytest tests/test_config.py -q`
Expected: FAIL with `AttributeError: module 'shared.config' has no attribute '_parse_brands'`

- [ ] **Step 3: Implement** — append to `shared/config.py` (after the `YT_DESTINATIONS` assignment, before the autopilot section):

```python
# --------------------------------------------------------------------------- #
# Branded clips (shared/branding.py, news bot "Brand it" flow)                #
# --------------------------------------------------------------------------- #


def _parse_brands(raw: str, env):
    """Parse BRANDS: comma-separated "name:lang" entries, e.g.
    "mirnews:en,rusnews:ru" (lang optional). Each brand's platform accounts
    come from BRAND_<NAME>_TG / _YT / _TW (name uppercased, non-alphanumerics
    -> "_", same rule as TWITTER_<ACCOUNT>_*); an unset platform means the
    brand has no pair for it in the publish picker. The logo is always
    brands/<name>/logo.png — a missing file disables the brand in the picker
    (checked at use time, not here, so config import never touches disk)."""
    out = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        parts = [p.strip() for p in item.split(":")]
        name = parts[0]
        key = "".join(c if c.isalnum() else "_" for c in name).upper()
        out.append({
            "name": name,
            "lang": parts[1] if len(parts) > 1 else "",
            "tg": (env.get(f"BRAND_{key}_TG") or "").strip(),
            "yt": (env.get(f"BRAND_{key}_YT") or "").strip(),
            "tw": (env.get(f"BRAND_{key}_TW") or "").strip(),
            "logo": os.path.join(ROOT_DIR, "brands", name, "logo.png"),
        })
    return out


BRANDS = _parse_brands(os.environ.get("BRANDS", ""), os.environ)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -m pytest tests/test_config.py -q`
Expected: PASS (all, including the pre-existing tests)

- [ ] **Step 5: Create `brands/README.md`:**

```markdown
# brands/

One folder per brand, named exactly as in the `BRANDS` env var:

    brands/<name>/logo.png    transparent PNG, any size (the renderer scales
                              it to 180 px wide)

A brand without a logo.png shows up disabled in the news bot's brand picker.
Wire the brand's platform accounts in .env:

    BRANDS=mirnews:en,rusnews:ru
    BRAND_MIRNEWS_TG=@mir_news        # Telegram channel (bot must be admin)
    BRAND_MIRNEWS_YT=mirnews          # folder under credentials/youtube/
    BRAND_MIRNEWS_TW=mirnews          # TWITTER_<NAME>_* account name
```

- [ ] **Step 6: Append to `.env.example`:**

```
# Branded clips ("Brand it" flow in the news bot). One logo per brand at
# brands/<name>/logo.png; per-brand accounts are optional — an unset platform
# simply has no row in the publish picker.
#BRANDS=mirnews:en,rusnews:ru
#BRAND_MIRNEWS_TG=@mir_news
#BRAND_MIRNEWS_YT=mirnews
#BRAND_MIRNEWS_TW=mirnews
```

- [ ] **Step 7: Commit**

```bash
git add shared/config.py tests/test_config.py brands/README.md .env.example
git commit -m "feat: BRANDS config for branded clips"
```

---

### Task 2: The renderer — `shared/branding.py` + repo font

**Files:**
- Create: `shared/branding.py`
- Create: `assets/fonts/DejaVuSans-Bold.ttf` (downloaded, see Step 1)
- Test: `tests/test_branding.py`

**Interfaces:**
- Consumes: nothing from other tasks (pure module; constants only).
- Produces: `branding.render_branded(video_path: str, headline: str, logo_path: str, out_path: str) -> str` (returns `out_path`; raises `FileNotFoundError` for missing logo/font, `RuntimeError` with ffmpeg stderr tail on encode failure); `branding.wrap_headline(text: str, width: int = 24, max_lines: int = 3) -> str`; `branding.FONT_PATH: str`. Task 4 calls `render_branded` via `asyncio.to_thread`.

- [ ] **Step 1: Download the font** (DejaVu Sans Bold — bundled license permits redistribution; full Latin + Cyrillic):

```powershell
New-Item -ItemType Directory -Force assets\fonts
Invoke-WebRequest -Uri "https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.zip" -OutFile "$env:TEMP\dejavu.zip"
Expand-Archive "$env:TEMP\dejavu.zip" -DestinationPath "$env:TEMP\dejavu" -Force
Copy-Item "$env:TEMP\dejavu\dejavu-fonts-ttf-2.37\ttf\DejaVuSans-Bold.ttf" assets\fonts\
Copy-Item "$env:TEMP\dejavu\dejavu-fonts-ttf-2.37\LICENSE" assets\fonts\DejaVu-LICENSE.txt
```

Verify: `Test-Path assets\fonts\DejaVuSans-Bold.ttf` → `True`.

- [ ] **Step 2: Write the failing tests** — create `tests/test_branding.py`:

```python
"""shared/branding.py — offline: wrapping, path escaping, filter-graph
construction. The one end-to-end render test invokes ffmpeg and skips when
it isn't on PATH."""

import os
import shutil
import subprocess

import pytest

from shared import branding


# --- wrap_headline ---------------------------------------------------------

def test_wrap_short_headline_single_line():
    assert branding.wrap_headline("Hello world") == "Hello world"


def test_wrap_collapses_whitespace_and_newlines():
    assert branding.wrap_headline("  a \n  b\t c  ") == "a b c"


def test_wrap_splits_on_word_boundaries():
    out = branding.wrap_headline("aaaa bbbb cccc dddd eeee ffff", width=10)
    assert out.split("\n") == ["aaaa bbbb", "cccc dddd", "eeee ffff"]


def test_wrap_truncates_beyond_max_lines_with_ellipsis():
    out = branding.wrap_headline("word " * 40, width=10, max_lines=3)
    lines = out.split("\n")
    assert len(lines) == 3
    assert lines[-1].endswith("…")
    assert all(len(ln) <= 10 for ln in lines)


def test_wrap_empty_returns_empty():
    assert branding.wrap_headline("") == ""


# --- ffmpeg path escaping --------------------------------------------------

def test_ff_path_escapes_windows_drive_colon_and_backslashes():
    assert branding._ff_path(r"C:\repo\assets\f.ttf") == "C\\:/repo/assets/f.ttf"


# --- filter graph ----------------------------------------------------------

def test_filter_graph_contains_fixed_design_constants():
    graph = branding._filter_graph("/f/font.ttf", "/t/text.txt")
    assert "scale=1080:1920:force_original_aspect_ratio=increase" in graph
    assert "gblur=sigma=30" in graph                       # blur-fill canvas
    assert "scale=180:-1[logo]" in graph                   # logo width
    assert "overlay=W-w-40:40" in graph                    # top-right margin
    assert "fontsize=64" in graph
    assert "boxcolor=black@0.55" in graph
    assert "boxborderw=24" in graph
    assert "y=h*0.72" in graph
    assert "textfile='/t/text.txt'" in graph               # never inline text
    assert "fontfile='/f/font.ttf'" in graph


def test_render_raises_on_missing_logo(tmp_path):
    with pytest.raises(FileNotFoundError):
        branding.render_branded(
            str(tmp_path / "in.mp4"), "headline",
            str(tmp_path / "nope.png"), str(tmp_path / "out.mp4"),
        )


# --- end-to-end (needs ffmpeg) ---------------------------------------------

def _have_ffmpeg():
    return shutil.which("ffmpeg") and shutil.which("ffprobe")


@pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg not on PATH")
def test_render_end_to_end_geometry(tmp_path):
    src = str(tmp_path / "src.mp4")
    logo = str(tmp_path / "logo.png")
    out = str(tmp_path / "out.mp4")
    # 1-second horizontal test card + a small solid logo, both via ffmpeg.
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=size=640x360:rate=24:duration=1", src],
        check=True, capture_output=True)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "color=red:size=100x100", "-frames:v", "1", logo],
        check=True, capture_output=True)

    result = branding.render_branded(src, "Заголовок теста headline", logo, out)

    assert result == out and os.path.isfile(out)
    from modules.youtube import shorts_format
    w, h, dur = shorts_format.probe(out)
    assert (w, h) == (1080, 1920)
    assert 0.5 < dur < 2.0
    # the textfile temp must not be left behind
    assert not os.path.exists(out + ".txt")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `py -m pytest tests/test_branding.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'shared.branding'`

- [ ] **Step 4: Implement** — create `shared/branding.py`:

```python
"""
shared/branding.py — burn a brand onto a clip: logo top-right, headline in a
lower-third banner. One ffmpeg pass per call; pure — no Telegram, no network.

The design is FIXED (same font, size, position every time): 1080x1920 canvas
(blur-fill, the same treatment shorts_format gives horizontal videos — for an
exact 9:16 input the background is simply invisible), logo scaled to 180 px
wide with a 40 px top-right margin, headline centered at 72 % frame height —
white bold 64 px on a translucent black box. The font ships in the repo
(assets/fonts/) so no system-font lookup can change the look.

The headline reaches ffmpeg through drawtext's textfile= (a UTF-8 temp file):
that renders embedded newlines AND keeps quotes/colons in real headlines out
of the filter string entirely. Requires ffmpeg built with libfreetype (all
standard builds are).
"""

import os
import subprocess
import textwrap

# shared/branding.py -> shared/ -> <repo root>
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT_PATH = os.path.join(_ROOT, "assets", "fonts", "DejaVuSans-Bold.ttf")

OUT_W, OUT_H = 1080, 1920   # Shorts-safe vertical canvas
LOGO_W = 180                # logo width, aspect kept
LOGO_MARGIN = 40            # px from the top and right edges
FONT_SIZE = 64
BOX_ALPHA = 0.55
BOX_PAD = 24                # boxborderw
TEXT_Y = 0.72               # banner anchor, fraction of frame height
LINE_WIDTH = 24             # ~chars per line at FONT_SIZE on a 1080 canvas
MAX_LINES = 3


def wrap_headline(text: str, width: int = LINE_WIDTH, max_lines: int = MAX_LINES) -> str:
    """Pre-wrap for drawtext (it does no wrapping of its own). Whitespace is
    collapsed, words wrap at `width`, anything past `max_lines` is cut with an
    ellipsis — the font never shrinks, the design stays fixed."""
    text = " ".join((text or "").split())
    lines = textwrap.wrap(text, width=width, break_long_words=True)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][: width - 1].rstrip() + "…"
    return "\n".join(lines)


def _ff_path(path: str) -> str:
    """Path for use inside a filter option: ffmpeg's filter parser treats ':'
    as an option separator and '\\' as an escape, so Windows paths must use
    forward slashes with the drive colon escaped."""
    return path.replace("\\", "/").replace(":", "\\:")


def _filter_graph(font_path: str, text_path: str) -> str:
    return (
        f"[0:v]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
        f"crop={OUT_W}:{OUT_H},gblur=sigma=30[bg];"
        f"[0:v]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2[canvas];"
        f"[1:v]scale={LOGO_W}:-1[logo];"
        f"[canvas][logo]overlay=W-w-{LOGO_MARGIN}:{LOGO_MARGIN}[branded];"
        f"[branded]drawtext=fontfile='{_ff_path(font_path)}'"
        f":textfile='{_ff_path(text_path)}'"
        f":fontcolor=white:fontsize={FONT_SIZE}:line_spacing=12"
        f":box=1:boxcolor=black@{BOX_ALPHA}:boxborderw={BOX_PAD}"
        f":x=(w-text_w)/2:y=h*{TEXT_Y}"
    )


def render_branded(video_path: str, headline: str, logo_path: str, out_path: str) -> str:
    """One branded variant: `video_path` + `logo_path` + `headline` ->
    `out_path` (1080x1920 mp4, same encode profile as shorts_format). Blocking
    — async callers run it in a thread. Raises FileNotFoundError for a missing
    logo/font, RuntimeError (with ffmpeg's stderr tail) on a failed encode;
    a partial output file is removed."""
    if not os.path.isfile(logo_path):
        raise FileNotFoundError(f"logo not found: {logo_path}")
    if not os.path.isfile(FONT_PATH):
        raise FileNotFoundError(f"headline font not found: {FONT_PATH}")

    text_path = out_path + ".txt"
    with open(text_path, "w", encoding="utf-8") as fh:
        fh.write(wrap_headline(headline))
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-i", video_path, "-i", logo_path,
                "-filter_complex", _filter_graph(FONT_PATH, text_path),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                out_path,
            ],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            try:
                os.remove(out_path)
            except OSError:
                pass
            raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[-300:]}")
    finally:
        try:
            os.remove(text_path)
        except OSError:
            pass
    return out_path
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `py -m pytest tests/test_branding.py -q`
Expected: PASS (the end-to-end test runs on this machine — ffmpeg is on PATH; elsewhere it skips)

- [ ] **Step 6: Commit**

```bash
git add shared/branding.py tests/test_branding.py assets/fonts/
git commit -m "feat: branded-clip renderer (logo + lower-third headline via ffmpeg)"
```

---

### Task 3: Pure flow logic — `modules/telegram/branded.py`

**Files:**
- Create: `modules/telegram/branded.py`
- Test: `tests/test_branded.py`

**Interfaces:**
- Consumes: `config.BRANDS` dict shape from Task 1 (`name/lang/tg/yt/tw/logo` keys); `shorts_format.MAX_SHORT_S`.
- Produces (Task 4 wires these into news_bot):
  - `available_brands(brands: list) -> list` — copies with `has_logo: bool` added
  - `pairs_for(renders: list, duration_s: float) -> list` — renders are `{"brand": dict, "path": str, "headline": str}`; returns `[{"render": dict, "platform": "tg"|"yt"|"tw", "label": str}]`
  - `gate_keyboard() -> InlineKeyboardMarkup` (`b:asis` / `b:brand`)
  - `brand_keyboard(brands: list, selected: set[int]) -> InlineKeyboardMarkup` (`b:t:<i>`, `b:render`, `b:cancel`; logo-less rows are `b:noop`)
  - `publish_keyboard(pairs: list, selected: set[int]) -> InlineKeyboardMarkup` (`b:p:<i>`, `b:publish`, `b:cancel`)

- [ ] **Step 1: Write the failing tests** — create `tests/test_branded.py`:

```python
"""modules/telegram/branded.py — pure picker logic for the Brand-it flow."""

import pytest

from modules.telegram import branded


def _brand(name="mir", lang="en", tg="@mir", yt="mir", tw=""):
    return {"name": name, "lang": lang, "tg": tg, "yt": yt, "tw": tw,
            "logo": f"/nonexistent/{name}/logo.png"}


def _render(brand):
    return {"brand": brand, "path": f"/tmp/{brand['name']}.mp4", "headline": "h"}


# --- available_brands ------------------------------------------------------

def test_available_brands_flags_missing_logo(tmp_path):
    with_logo = dict(_brand("a"), logo=str(tmp_path / "logo.png"))
    (tmp_path / "logo.png").write_bytes(b"png")
    out = branded.available_brands([with_logo, _brand("b")])
    assert out[0]["has_logo"] is True
    assert out[1]["has_logo"] is False


# --- pairs_for -------------------------------------------------------------

def test_pairs_only_for_configured_platforms():
    pairs = branded.pairs_for([_render(_brand(tg="@mir", yt="", tw="mir"))], 60)
    assert [p["platform"] for p in pairs] == ["tg", "tw"]
    assert pairs[0]["label"] == "mir → TG"
    assert pairs[1]["label"] == "mir → X"


def test_pairs_hide_youtube_beyond_shorts_cap():
    renders = [_render(_brand(tg="@mir", yt="mir"))]
    assert [p["platform"] for p in branded.pairs_for(renders, 60)] == ["tg", "yt"]
    assert [p["platform"] for p in branded.pairs_for(renders, 181)] == ["tg"]


def test_pairs_carry_their_render():
    r = _render(_brand())
    assert all(p["render"] is r for p in branded.pairs_for([r], 60))


# --- keyboards -------------------------------------------------------------

def _buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


def test_gate_keyboard_has_both_choices():
    data = [b.callback_data for b in _buttons(branded.gate_keyboard())]
    assert data == ["b:asis", "b:brand"]


def test_brand_keyboard_marks_selection_and_disables_missing_logo():
    brands = [dict(_brand("a"), has_logo=True), dict(_brand("b"), has_logo=False)]
    btns = _buttons(branded.brand_keyboard(brands, {0}))
    assert btns[0].text.startswith("☑") and btns[0].callback_data == "b:t:0"
    assert btns[1].callback_data == "b:noop" and "no logo" in btns[1].text
    assert [b.callback_data for b in btns[-2:]] == ["b:render", "b:cancel"]


def test_publish_keyboard_rows_and_defaults():
    pairs = branded.pairs_for([_render(_brand(tg="@mir", yt="mir"))], 60)
    btns = _buttons(branded.publish_keyboard(pairs, set()))
    assert [b.callback_data for b in btns[:-2]] == ["b:p:0", "b:p:1"]
    assert all(b.text.startswith("☐") for b in btns[:-2])   # all OFF by default
    assert [b.callback_data for b in btns[-2:]] == ["b:publish", "b:cancel"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -m pytest tests/test_branded.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'modules.telegram.branded'`

- [ ] **Step 3: Implement** — create `modules/telegram/branded.py`:

```python
"""
modules/telegram/branded.py — the Brand-it flow's pure pieces: which
brand→platform pairs a set of renders can publish to, and the three inline
keyboards (gate, brand picker, publish picker). Lifted out of news_bot.py the
same way reactions.py was: news_bot exits at import without env, so anything
that wants an offline test has to live here. No I/O beyond one os.path check.

Callback namespace "b:" (the manual picker owns t:/y:/e:, asks own r:):
    b:asis  b:brand              the as-is / brand-it gate
    b:t:<i> b:render b:cancel    brand picker (i indexes the brands list)
    b:p:<i> b:publish            publish picker (i indexes the pairs list)
    b:noop                       disabled row (brand without a logo.png)
"""

import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from modules.youtube.shorts_format import MAX_SHORT_S

# (brand-dict key, picker label) — the order pairs appear in the picker.
PLATFORMS = (("tg", "TG"), ("yt", "YT"), ("tw", "X"))


def available_brands(brands: list) -> list:
    """Copies with has_logo added — checked once when the picker opens, so a
    logo dropped in mid-flow doesn't confuse an open keyboard."""
    return [dict(b, has_logo=os.path.isfile(b["logo"])) for b in brands]


def pairs_for(renders: list, duration_s: float) -> list:
    """Publishable (render, platform) pairs: one per configured platform of
    each rendered brand. YouTube pairs disappear past the Shorts cap — an
    upload that can't be a Short shouldn't be offered."""
    pairs = []
    for r in renders:
        b = r["brand"]
        for key, label in PLATFORMS:
            if not b.get(key):
                continue
            if key == "yt" and duration_s > MAX_SHORT_S:
                continue
            pairs.append({"render": r, "platform": key,
                          "label": f"{b['name']} → {label}"})
    return pairs


def gate_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📤 Post as-is", callback_data="b:asis"),
        InlineKeyboardButton("🎨 Brand it", callback_data="b:brand"),
    ]])


def brand_keyboard(brands: list, selected: set) -> InlineKeyboardMarkup:
    rows = []
    for i, b in enumerate(brands):
        if b["has_logo"]:
            mark = "☑" if i in selected else "☐"
            rows.append([InlineKeyboardButton(
                f"{mark} {b['name']} · {b['lang'] or 'raw'}",
                callback_data=f"b:t:{i}")])
        else:
            rows.append([InlineKeyboardButton(
                f"🚫 {b['name']} (no logo.png)", callback_data="b:noop")])
    rows.append([
        InlineKeyboardButton("🎬 Render", callback_data="b:render"),
        InlineKeyboardButton("✕ Cancel", callback_data="b:cancel"),
    ])
    return InlineKeyboardMarkup(rows)


def publish_keyboard(pairs: list, selected: set) -> InlineKeyboardMarkup:
    rows = []
    for i, p in enumerate(pairs):
        mark = "☑" if i in selected else "☐"
        rows.append([InlineKeyboardButton(f"{mark} {p['label']}",
                                          callback_data=f"b:p:{i}")])
    rows.append([
        InlineKeyboardButton("▶ Publish", callback_data="b:publish"),
        InlineKeyboardButton("✕ Cancel", callback_data="b:cancel"),
    ])
    return InlineKeyboardMarkup(rows)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -m pytest tests/test_branded.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add modules/telegram/branded.py tests/test_branded.py
git commit -m "feat: brand-it flow state helpers + keyboards"
```

---

### Task 4: Wire the flow into `news_bot.py`

**Files:**
- Modify: `modules/telegram/news_bot.py` — imports (~line 65), `_prompt`/gate (~line 206), `on_message` (~line 288), `_handle_url` (~line 239), `on_callback` (~line 485), `_sweep_orphans` (~line 591), module docstring

**Interfaces:**
- Consumes: `branding.render_branded`, `branding.FONT_PATH` (Task 2); `branded.*` (Task 3); `config.BRANDS` (Task 1); existing: `translator.translate(text, target_lang, source_lang)`, `publisher.publish(bot, text, media, dests) -> (posted, errors, links)`, `reactions.record_posts(posted, links, emojis)`, `yt_publisher.split_caption(text) -> (title, description)`, `yt uploader.upload_short(file_path, title, description, account_name) -> {"status", ...}`, `shorts_format.probe(path) -> (w, h, duration)`, `twitter poster.post_media(file_path, caption, account_name) -> {"status", ...}` (lazy import — tweepy must not be a startup requirement).
- Produces: the user-facing flow. No new public functions.

No offline unit tests — `news_bot.py` exits at import without env (all its testable logic went into Task 3's module). Verification is compile + suite + the manual checklist in Step 8.

- [ ] **Step 1: Imports and constants.** Add to the import block (after `from modules.youtube import publisher as yt_publisher`):

```python
from modules.telegram import branded, translator  # noqa: E402
from modules.youtube import shorts_format, uploader as yt_uploader  # noqa: E402
from shared import branding  # noqa: E402
```

Below `_BOT_FILE_LIMIT` add:

```python
# Bot API refuses uploads above this; a bigger render is published normally
# but can't be sent back into the group as a file.
_BOT_UPLOAD_LIMIT = 50 * 1024 * 1024
```

- [ ] **Step 2: The gate.** After `_prompt` (line ~206) add:

```python
async def _gate(msg, text: str, media: list, files: list | None = None) -> None:
    """Single-video post with brands configured: ask as-is vs brand-it before
    opening any picker. State is the same _pending dict, mode-tagged."""
    state = {"text": text, "media": media, "files": list(files or ()),
             "sel_tg": set(), "sel_yt": set(), "sel_em": set(), "mode": "gate"}
    prompt = await msg.reply_text(
        "Brand this clip, or post it as-is?" + (f"\n\n📝 {text}" if text else ""),
        reply_markup=branded.gate_keyboard())
    _pending[prompt.message_id] = state
```

In `on_message`, replace the single-message branch

```python
    gid = msg.media_group_id
    if gid is None:
        await _prompt(msg, text, [media_item] if media_item else [])
        return
```

with:

```python
    gid = msg.media_group_id
    if gid is None:
        if media_item and media_item["type"] == "video" and config.BRANDS:
            await _gate(msg, text, [media_item])
        else:
            await _prompt(msg, text, [media_item] if media_item else [])
        return
```

In `_handle_url`, after `ext = ...` the state is built and shown directly; route videos through the gate instead. Replace the block from `state = {` through `_pending[note.message_id] = state` with:

```python
    is_video = ext in _VIDEO_EXTS
    state = {
        "text": user_text or link_caption,
        "cap_src": "your text" if user_text else "from link",
        "media": [{"path": path, "type": "video" if is_video else "photo"}],
        "files": [path],
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

- [ ] **Step 3: Mode-aware caption replies.** The reply-to-edit handler in `on_message` re-renders with `_prompt_text`/`_keyboard` unconditionally. Replace its `try` block body:

```python
        state = _pending[reply.message_id]
        state["text"] = text.strip()
        state["cap_src"] = "edited"
        mode = state.get("mode")
        if mode == "gate":
            body = f"Brand this clip, or post it as-is?\n\n📝 {state['text']}"
            markup = branded.gate_keyboard()
        elif mode == "brand":
            body = _brand_prompt_text(state)
            markup = branded.brand_keyboard(state["brands"], state["sel_brands"])
        elif mode == "publish":
            return  # headline is already burned into the renders
        else:
            body = _prompt_text(state)
            markup = _keyboard(state)
        try:
            await reply.edit_text(body, reply_markup=markup)
        except Exception:  # "message is not modified" — same caption again
            pass
        return
```

- [ ] **Step 4: The brand-flow callbacks.** In `on_callback`, right after `data = q.data`, add the dispatch:

```python
    if data.startswith("b:"):
        await _on_brand_callback(q, context, state, data[2:])
        return
```

Then add the handler block (place it after `_post_to_youtube`):

```python
# --------------------------------------------------------------------------- #
# Brand-it flow (gate → brand picker → render → publish picker)               #
# --------------------------------------------------------------------------- #


def _brand_prompt_text(state: dict) -> str:
    head = state["text"].strip()
    lines = ["Brand for which brands?"]
    lines.append(f"📝 {head}" if head
                 else "⚠️ no headline yet — reply to this message with it")
    lines.append("↩️ reply to this message to replace the headline")
    return "\n\n".join(lines)


async def _ensure_local_video(bot, state: dict) -> str:
    """Local path of the source video, downloading Telegram-uploaded ones
    (≤ 20 MB — checked at the gate). URL posts already have a path."""
    video = next(m for m in state["media"] if m["type"] == "video")
    if video.get("path"):
        return video["path"]
    dl_dir = os.path.join(config.TG_DATA_DIR, "media")
    os.makedirs(dl_dir, exist_ok=True)
    path = os.path.join(dl_dir, f"brand_src_{video['file_id'][-24:]}.mp4")
    tg_file = await bot.get_file(video["file_id"])
    await tg_file.download_to_drive(path)
    video["path"] = path
    state["files"].append(path)
    return path


async def _on_brand_callback(q, context, state: dict, verb: str) -> None:
    """Taps on the gate / brand picker / publish picker ("b:<verb>")."""
    if verb == "noop":
        await q.answer("Add brands/<name>/logo.png to enable this brand.",
                       show_alert=True)
        return

    if verb == "asis":
        await q.answer()
        state["mode"] = None
        await q.edit_message_text(_prompt_text(state),
                                  reply_markup=_keyboard(state))
        return

    if verb == "brand":
        video = next((m for m in state["media"] if m["type"] == "video"), None)
        if video and not video.get("path") \
                and (video.get("file_size") or 0) > _BOT_FILE_LIMIT:
            await q.answer(
                "Video too large to brand (20 MB Bot API cap) — post the "
                "clip as a link instead.", show_alert=True)
            return
        await q.answer()
        state["mode"] = "brand"
        state["brands"] = branded.available_brands(config.BRANDS)
        state["sel_brands"] = {i for i, b in enumerate(state["brands"])
                               if b["has_logo"]}
        await q.edit_message_text(
            _brand_prompt_text(state),
            reply_markup=branded.brand_keyboard(state["brands"],
                                                state["sel_brands"]))
        return

    if verb.startswith("t:") and state.get("mode") == "brand":
        await q.answer()
        state["sel_brands"] ^= {int(verb.split(":", 1)[1])}
        await q.edit_message_reply_markup(
            branded.brand_keyboard(state["brands"], state["sel_brands"]))
        return

    if verb == "render" and state.get("mode") == "brand":
        if not state["sel_brands"]:
            await q.answer("Pick at least one brand.", show_alert=True)
            return
        if not state["text"].strip():
            await q.answer("No headline — reply to the picker message with "
                           "it first.", show_alert=True)
            return
        await q.answer()
        await _do_render(q, context, state)
        return

    if verb.startswith("p:") and state.get("mode") == "publish":
        await q.answer()
        state["sel_pairs"] ^= {int(verb.split(":", 1)[1])}
        await q.edit_message_reply_markup(
            branded.publish_keyboard(state["pairs"], state["sel_pairs"]))
        return

    if verb == "publish" and state.get("mode") == "publish":
        if not state["sel_pairs"]:
            await q.answer("Pick at least one destination.", show_alert=True)
            return
        await q.answer()
        await _do_publish(q, context, state)
        return

    if verb == "cancel":
        await q.answer()
        _pending.pop(q.message.message_id, None)
        _cleanup(state)
        await q.edit_message_text("✕ cancelled")
        return

    await q.answer()  # stale/unknown verb for this mode


async def _do_render(q, context, state: dict) -> None:
    """Render one variant per selected brand (headline translated per brand
    language, translation cached), send each back into the group, then open
    the publish picker on a fresh message. One failed brand is reported and
    skipped — never fatal for the others."""
    brands = [state["brands"][i] for i in sorted(state["sel_brands"])]
    await q.edit_message_text(
        "⏳ rendering " + ", ".join(b["name"] for b in brands) + " …")

    try:
        src = await _ensure_local_video(context.bot, state)
        _, _, duration = await asyncio.to_thread(shorts_format.probe, src)
    except Exception as exc:
        log.error("brand render setup failed: %s", exc)
        _pending.pop(q.message.message_id, None)
        _cleanup(state)
        await q.edit_message_text(f"❌ can't read the video: {str(exc)[:300]}")
        return

    media_dir = os.path.join(config.TG_DATA_DIR, "media")
    renders, failures = [], []
    cache: dict[str, str] = {}
    for b in brands:
        try:
            lang = b["lang"]
            if lang not in cache:
                cache[lang] = (await asyncio.to_thread(
                    translator.translate, state["text"], lang,
                    config.SOURCE_LANG) if lang else state["text"])
            out = os.path.join(
                media_dir, f"brand_{q.message.message_id}_{b['name']}.mp4")
            path = await asyncio.to_thread(
                branding.render_branded, src, cache[lang], b["logo"], out)
            state["files"].append(path)
            renders.append({"brand": b, "path": path, "headline": cache[lang]})
            size = os.path.getsize(path)
            if size > _BOT_UPLOAD_LIMIT:
                await q.message.chat.send_message(
                    f"🏷 {b['name']}: rendered ({size / 1024 / 1024:.0f} MB — "
                    "too big to send back; publishing still works)")
            else:
                with open(path, "rb") as fh:
                    await q.message.chat.send_video(video=fh,
                                                    caption=f"🏷 {b['name']}")
        except Exception as exc:  # translator, ffmpeg, Telegram send, ...
            log.error("brand render failed for %s: %s", b["name"], exc)
            failures.append((b["name"], str(exc)[:200]))

    if not renders:
        _pending.pop(q.message.message_id, None)
        _cleanup(state)
        await q.edit_message_text(
            "❌ all renders failed:\n"
            + "\n".join(f"{n}: {e}" for n, e in failures))
        return

    state["mode"] = "publish"
    state["renders"] = renders
    state["pairs"] = branded.pairs_for(renders, duration)
    state["sel_pairs"] = set()

    summary = "🎨 rendered: " + ", ".join(r["brand"]["name"] for r in renders)
    if failures:
        summary += "\n" + "\n".join(f"❌ {n}: {e}" for n, e in failures)
    await q.edit_message_text(summary)

    # Fresh message so the publish picker lands BELOW the delivered clips;
    # the state follows it.
    _pending.pop(q.message.message_id, None)
    if not state["pairs"]:
        _cleanup(state)
        await q.message.chat.send_message(
            "no destinations configured for the rendered brands "
            "(BRAND_<NAME>_TG/YT/TW) — files above are yours, nothing to publish")
        return
    prompt = await q.message.chat.send_message(
        "Publish which?", reply_markup=branded.publish_keyboard(state["pairs"],
                                                                set()))
    _pending[prompt.message_id] = state


async def _do_publish(q, context, state: dict) -> None:
    """Push each selected pair through its platform publisher. The headline is
    already translated per brand — Telegram destinations get lang "" so
    publisher.publish doesn't translate again."""
    pairs = [state["pairs"][i] for i in sorted(state["sel_pairs"])]
    _pending.pop(q.message.message_id, None)
    await q.edit_message_text(
        "⏳ publishing " + ", ".join(p["label"] for p in pairs) + " …")

    lines = []
    for p in pairs:
        r, b = p["render"], p["render"]["brand"]
        try:
            if p["platform"] == "tg":
                posted, errors, links = await publisher.publish(
                    context.bot, r["headline"],
                    [{"path": r["path"], "type": "video"}],
                    [{"chat_id": b["tg"], "lang": ""}])
                if posted:
                    # Same post-publish path as the manual picker (BulkFollows
                    # per-post order + durable every-5th channel counter).
                    await asyncio.to_thread(reactions.record_posts,
                                            posted, links, [])
                    lines.append(f"✅ {p['label']}")
                    for chat_id, url in links:
                        lines.append(f"🔗 {chat_id}: {url}")
                for _, err in errors:
                    lines.append(f"❌ {p['label']}: {err}")
            elif p["platform"] == "yt":
                # Renders are 1080x1920 by construction — upload directly,
                # no second ensure_short pass.
                title, description = yt_publisher.split_caption(r["headline"])
                result = await asyncio.to_thread(
                    yt_uploader.upload_short, r["path"], title, description,
                    b["yt"])
                if result.get("status") == "success":
                    lines.append(f"✅ {p['label']}")
                else:
                    lines.append(f"❌ {p['label']}: "
                                 f"{result.get('error', 'unknown error')}")
            else:  # tw — lazy import so tweepy isn't a startup requirement
                from modules.twitter import poster as tw_poster
                result = await asyncio.to_thread(
                    tw_poster.post_media, r["path"], r["headline"], b["tw"])
                if result.get("status") == "success":
                    lines.append(f"✅ {p['label']}")
                else:
                    lines.append(f"❌ {p['label']}: "
                                 f"{result.get('error', 'unknown error')}")
        except Exception as exc:
            log.error("brand publish failed for %s: %s", p["label"], exc)
            lines.append(f"❌ {p['label']}: {str(exc)[:200]}")

    _cleanup(state)
    await q.edit_message_text("\n".join(lines))
```

- [ ] **Step 5: Sweep the new temp prefixes.** In `_sweep_orphans`, replace the `if name.startswith("manual_url_"):` line with:

```python
        if name.startswith(("manual_url_", "brand_")):
```

(`brand_` covers both `brand_src_*` downloads and `brand_<msgid>_<name>.mp4` renders.)

- [ ] **Step 6: Docstring.** Add to the `news_bot.py` module docstring, after the URL-posts paragraph:

```
Brand-it: a single-video post (uploaded or URL) with BRANDS configured first
asks "Post as-is / Brand it". Brand-it renders one variant per selected brand
— brands/<name>/logo.png top-right, the caption as a translated lower-third
headline (shared/branding.py) — sends each back here, then offers a publish
picker of brand→TG/YT/X pairs (all off; YouTube hidden over 3 minutes).
Nothing publishes without a selection. Telegram-uploaded sources keep the
20 MB Bot-API download cap — bigger clips must come in as URLs.
```

- [ ] **Step 7: Verify it compiles and the suite passes**

Run: `py -m compileall modules/telegram/news_bot.py` → exit 0
Run: `py -m pytest tests -q` → all pass

- [ ] **Step 8: Manual smoke test** (needs `.env` with `BRANDS`, one logo, bot running: `py modules/telegram/news_bot.py`):

1. Send a short video with caption → gate appears; **Post as-is** → the old picker, unchanged.
2. Send it again → **Brand it** → brand picker (all logo-bearing brands pre-selected; logo-less brand shows 🚫 and alerts on tap).
3. **Render** → per-brand clips arrive labeled 🏷, logo top-right, headline banner lower third, translated per brand lang.
4. Publish picker appears below with brand→platform rows all ☐; YT row absent for a >3 min source.
5. Toggle one TG pair → **Publish** → post lands in that channel, summary shows ✅ + link; `tg_data/media/brand_*` files are gone.
6. **Cancel** at each stage → "✕ cancelled", temp files gone.
7. Send a photo → no gate, old picker directly. Send a video with no caption → gate → Brand it → picker warns "no headline"; reply to it with text → headline appears; Render works.

- [ ] **Step 9: Commit**

```bash
git add modules/telegram/news_bot.py
git commit -m "feat: brand-it flow in the news bot (gate, render, publish picker)"
```

---

### Task 5: Documentation

**Files:**
- Modify: `CLAUDE.md` (Layout tree, Files table, spec status)
- Modify: `modules/telegram/README.md` (if it documents the news bot flows — mirror the docstring addition)
- Modify: `docs/superpowers/specs/2026-07-31-branded-clips-design.md` (status line)

**Interfaces:** none — docs only.

- [ ] **Step 1: CLAUDE.md.** Add to the Layout tree: `branding.py` under `shared/`, `branded.py` under `modules/telegram/`, and a root line for `brands/` + `assets/fonts/`. Add Files-table rows:

```markdown
| `shared/branding.py` | Branded-clip renderer: one ffmpeg pass burns `brands/<name>/logo.png` (180 px, top-right, 40 px margin) and a lower-third headline (bold white 64 px on black@0.55, ≤3 wrapped lines, repo font `assets/fonts/DejaVuSans-Bold.ttf`) onto a 1080×1920 blur-fill canvas. Headline goes in via drawtext `textfile=` — never interpolated into the filter. Pure/blocking; callers use `asyncio.to_thread`. |
| `modules/telegram/branded.py` | Brand-it flow's pure half (offline-testable — news_bot exits at import without env): `pairs_for` (brand→TG/YT/X pairs, YouTube hidden past the 180 s Shorts cap) and the gate / brand-picker / publish-picker keyboards, callback namespace `b:`. |
```

and extend the `news_bot.py` row with one sentence: single-video posts with `BRANDS` configured gate through "Post as-is / Brand it"; brand-it renders per-brand variants and publishes only explicitly selected brand→platform pairs.

- [ ] **Step 2: Spec status.** In the spec, change `**Status:** approved, ready for implementation` to `**Status:** implemented`.

- [ ] **Step 3: `modules/telegram/README.md`** — if the news bot section lists its flows, add a "Brand-it" bullet mirroring the docstring paragraph from Task 4 Step 6. If the README doesn't cover flows, skip.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md modules/telegram/README.md docs/superpowers/specs/2026-07-31-branded-clips-design.md
git commit -m "docs: branded clips"
```
