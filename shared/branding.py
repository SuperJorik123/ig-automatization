"""
shared/branding.py — burn a brand onto a clip: logo top-right, headline in a
lower-third banner. One ffmpeg pass per call; pure — no Telegram, no network.

The geometry is FIXED (same size and position every time): 1080x1920 canvas
(blur-fill, the same treatment shorts_format gives horizontal videos — for an
exact 9:16 input the background is simply invisible), logo scaled to 205 px
wide sitting 82 px from the right edge and 102 px from the top, headline at
77.5 % frame height — semi-bold, in a banner box of fixed width (matched to
the reference renders at the repo root, `example.MP4` foremost),
tightly-spaced rows (as many as the text needs, never truncated), left-aligned
inside it, fading out smoothly after 10 s. The box does NOT resize to the
headline — every post's banner starts and ends at the same x — so rows wrap to
the pixel width that fits inside it (`wrap_to_px`), not to a character count.

Colors, font and font size are per brand: drop a `style.json` next to the
brand's logo (see `load_style`) and it overrides the defaults below. Without
one, every brand renders white-on-black@0.55 in the default font.

The headline reaches ffmpeg through drawtext's textfile= (a UTF-8 temp file):
that keeps quotes/colons in real headlines out of the filter string entirely.
Two things about that file: rows are separated by a bare CR (ffmpeg 8's
drawtext breaks the row on a LF but also DRAWS it, as a missing-glyph box),
and characters the font has no glyph for are stripped first
(`strip_unrenderable`) so emoji don't come out as black rectangles. Requires
ffmpeg built with libfreetype (all standard builds are).
"""

import json
import os
import re
import subprocess
import textwrap
import unicodedata

# shared/branding.py -> shared/ -> <repo root>
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The repo-shipped fallback. DejaVu Sans Bold is a much heavier, wider face
# than the reference render's — it is only used where no system font matches
# (Linux/CI), so a render there still succeeds, just fatter.
SHIPPED_FONT = os.path.join(_ROOT, "assets", "fonts", "DejaVuSans-Bold.ttf")

# Headline face, in preference order. Segoe UI Bold is what `example.MP4`'s
# banner matches (measured width-to-cap-height ratio ~22 vs DejaVu's 23.5, and
# a visibly thinner stem); Arial Bold is the near-identical stand-in on a
# Windows box without it. First hit wins.
FONT_CANDIDATES = ("segoeuib.ttf", "arialbd.ttf")

# Where a bare font name in style.json ("arialbd.ttf", "Verdana") — and the
# FONT_CANDIDATES above — are looked up.
_SYSTEM_FONT_DIRS = [
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts"),
    "/usr/share/fonts", "/usr/local/share/fonts",
    os.path.expanduser("~/.fonts"),
]


def _default_font() -> str:
    """First FONT_CANDIDATES hit in the system font dirs, else the shipped
    DejaVu. Resolved once at import — the font set of a machine doesn't
    change under a running bot."""
    for name in FONT_CANDIDATES:
        for directory in _SYSTEM_FONT_DIRS:
            if directory and os.path.isfile(os.path.join(directory, name)):
                return os.path.join(directory, name)
    return SHIPPED_FONT


FONT_PATH = _default_font()

OUT_W, OUT_H = 1080, 1920   # Shorts-safe vertical canvas
# Logo box, measured off the reference renders at the repo root and converted
# to this canvas. The margins are NOT equal
# and are measured to the FILE's edges, not the ink: brand logos ship with
# transparent padding (mirnews/logo.png has ~4 % on the sides, 8 % on top), so
# equal file margins still look right-heavy on screen if you tune them by eye.
LOGO_W = 205                # logo width, aspect kept
LOGO_MARGIN_X = 82          # px from the right edge
LOGO_MARGIN_Y = 102         # px from the top edge
# Banner metrics, measured off `example.MP4` on this canvas: box x 110..966
# (fixed — it does NOT shrink to the text, which is why the reference's short
# second row still has 160 px of banner to its right), top edge at 76.9 % of
# frame height, text starting 7 px inside the box with 12 px above it, rows
# ~23 px/char wide (= 49 px Segoe UI Bold).
FONT_SIZE = 49
BOX_ALPHA = 0.55
BOX_X = 110                 # box left edge
BOX_W = 856                 # box width, independent of the headline
BOX_PAD_X = 7               # box border left/right of the text
BOX_PAD_Y = 12              # box border above/below the text block
TEXT_X = BOX_X + BOX_PAD_X
TEXT_Y = 0.775              # TEXT top (box top = this - BOX_PAD_Y/OUT_H)
# Rows wrap to the pixel width left inside the box — a fixed box can't be
# allowed to overflow, and character counts can't promise that across fonts.
LINE_PX = BOX_X + BOX_W - TEXT_X - BOX_PAD_X
LINE_WIDTH = 37             # chars/line fallback when the font can't be measured
MAX_LINES = None            # no cap: a long headline gets more rows, never "…"
# Negative: drawtext's own line height (font ascent+descent, ~65 px at 49) is
# looser than the reference's 57 px row pitch, and headline rows are meant to
# sit tight.
LINE_SPACING = -8
FADE_START = 10             # s the banner stays fully visible
FADE_DUR = 1.5              # s of smooth fade-out after that

STYLE_FILE = "style.json"   # optional, next to the brand's logo.png

# What a brand renders as when it ships no style.json — the look every brand
# had before per-brand styling existed.
DEFAULT_STYLE = {
    "background": "black",
    "background_alpha": BOX_ALPHA,
    "text": "white",
    "font": FONT_PATH,
    "font_size": FONT_SIZE,
}

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def _color(value: str, field: str) -> str:
    """style.json color -> ffmpeg color. `#RRGGBB`/`RRGGBB` becomes 0xRRGGBB
    (ffmpeg reads bare `#…` too, but 0x survives filter-string escaping
    unambiguously); anything else is passed through as an ffmpeg color name."""
    value = str(value).strip()
    m = _HEX_RE.match(value)
    if m:
        return "0x" + m.group(1).upper()
    if value.startswith("#") or not value:
        raise ValueError(f"{field}: not a #RRGGBB color: {value!r}")
    return value


def _resolve_font(value: str, brand_dir: str) -> str:
    """A style.json `font` may be an absolute path, a file next to the brand's
    logo, or a bare system-font name/filename (looked up in the OS font dirs,
    `.ttf`/`.otf` appended when the name has no extension)."""
    value = str(value).strip()
    if os.path.isabs(value):
        candidates = [value]
    else:
        candidates = [os.path.join(brand_dir, value)]
        names = [value] if os.path.splitext(value)[1] else [value + ext
                                                            for ext in (".ttf", ".otf")]
        candidates += [os.path.join(d, n) for d in _SYSTEM_FONT_DIRS if d
                       for n in names]
    for path in candidates:
        if os.path.isfile(path):
            return path
    raise FileNotFoundError(f"font not found: {value!r} (brand dir: {brand_dir})")


def load_style(brand_dir: str) -> dict:
    """Read `<brand_dir>/style.json` over DEFAULT_STYLE. Missing file -> the
    defaults. Recognised keys (all optional):

        background        "#C90A0A" — the headline banner's box color
        background_alpha  0..1; defaults to 1.0 once `background` is set
                          (the 0.55 default only makes sense for the black box)
        text              "#122e44" — headline color
        font              path (absolute / next to the logo) or system font name
        font_size         px, default 41 (wrapping width scales with it)

    Raises ValueError for malformed JSON or values, FileNotFoundError for a
    font that can't be resolved — a typo must be loud, not silently ignored."""
    style = dict(DEFAULT_STYLE)
    path = os.path.join(brand_dir, STYLE_FILE)
    if not os.path.isfile(path):
        return style
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a JSON object")

    if "background" in raw:
        style["background"] = _color(raw["background"], "background")
        style["background_alpha"] = 1.0        # explicit color -> show it as-is
    if "text" in raw:
        style["text"] = _color(raw["text"], "text")
    if "background_alpha" in raw:
        try:
            alpha = float(raw["background_alpha"])
        except (TypeError, ValueError):
            alpha = -1.0
        if not 0.0 <= alpha <= 1.0:
            raise ValueError(f"{path}: background_alpha must be 0..1")
        style["background_alpha"] = alpha
    if "font" in raw:
        style["font"] = _resolve_font(raw["font"], brand_dir)
    if "font_size" in raw:
        try:
            size = int(raw["font_size"])
        except (TypeError, ValueError):
            size = 0
        if not 8 <= size <= 200:
            raise ValueError(f"{path}: font_size must be 8..200")
        style["font_size"] = size
    return style


_charset_cache: dict[str, frozenset | None] = {}

# Fallback ranges used when fontTools isn't importable and the real cmap is
# unknown: emoji blocks, regional-indicator flags, the combining keycap, and
# the misc-symbols/dingbat range no text font covers.
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U0001F1E6-\U0001F1FF☀-➿⃣️︎]"
)

# Categories that may be dropped when the font has no glyph: symbols and
# combining marks. Letters and digits are NEVER dropped — a Chinese headline in
# a Latin-only font must show boxes, not silently lose its words.
_DROPPABLE_CATEGORIES = {"So", "Sk", "Sc", "Me", "Mn", "Cs", "Co"}


def font_charset(font_path: str) -> frozenset | None:
    """Codepoints the font can actually draw, or None when that can't be
    determined (fontTools missing / unreadable font). Cached per path."""
    if font_path not in _charset_cache:
        try:
            from fontTools.ttLib import TTFont
            with TTFont(font_path, fontNumber=0, lazy=True) as font:
                chars = frozenset(font.getBestCmap())
        except Exception:
            chars = None
        _charset_cache[font_path] = chars
    return _charset_cache[font_path]


def strip_unrenderable(text: str, font_path: str) -> str:
    """Remove characters that would render as ffmpeg's missing-glyph box.

    Telegram captions carry emoji, flags and keycaps ("2️⃣" = digit + U+20E3),
    and no text font has glyphs for them: drawtext draws a black rectangle
    instead, which is what showed up mid-headline in newlinebug.mp4. Invisible
    formatting characters (zero-widths, BOM, direction marks) go too — they can
    only ever be a box or nothing."""
    chars = font_charset(font_path)
    out = []
    for ch in text or "":
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf") and ch not in "\n\t":
            continue
        if chars is None:
            if _EMOJI_RE.match(ch):
                continue
        elif ord(ch) not in chars and cat in _DROPPABLE_CATEGORIES:
            continue
        out.append(ch)
    return "".join(out)


_metrics_cache: dict[str, tuple | None] = {}


def _font_metrics(font_path: str):
    """(units_per_em, {codepoint: advance width}) for the font, or None when
    fontTools can't read it. Cached per path."""
    if font_path not in _metrics_cache:
        try:
            from fontTools.ttLib import TTFont
            with TTFont(font_path, fontNumber=0, lazy=True) as font:
                upem = font["head"].unitsPerEm
                hmtx = font["hmtx"].metrics
                advances = {cp: hmtx[name][0]
                            for cp, name in font.getBestCmap().items()
                            if name in hmtx}
            _metrics_cache[font_path] = (upem, advances)
        except Exception:
            _metrics_cache[font_path] = None
    return _metrics_cache[font_path]


def text_width(text: str, font_path: str, font_size: int) -> float | None:
    """Rendered width of `text` in px, or None when the font can't be measured.
    Sums advance widths — kerning is ignored, which is worth a px or two on a
    banner and keeps this dependency-light."""
    metrics = _font_metrics(font_path)
    if metrics is None:
        return None
    upem, advances = metrics
    fallback = advances.get(ord(" "), upem // 2)
    total = sum(advances.get(ord(ch), fallback) for ch in text)
    return total * font_size / upem


def wrap_to_px(text: str, font_path: str, font_size: int,
               max_px: int = LINE_PX) -> str:
    """Word-wrap so no row renders wider than `max_px`. Falls back to
    `wrap_headline`'s character counting when the font can't be measured. A
    single word longer than the box is left long rather than broken mid-word —
    it would only overflow by a little, and hyphen-free breaks read worse."""
    text = " ".join((text or "").split())
    if not text:
        return ""
    if text_width("x", font_path, font_size) is None:
        width = max(8, round(LINE_WIDTH * FONT_SIZE / font_size))
        return wrap_headline(text, width=width)
    lines, row = [], ""
    for word in text.split(" "):
        candidate = f"{row} {word}" if row else word
        if row and text_width(candidate, font_path, font_size) > max_px:
            lines.append(row)
            row = word
        else:
            row = candidate
    if row:
        lines.append(row)
    return "\n".join(lines)


def wrap_headline(text: str, width: int = LINE_WIDTH,
                  max_lines: int | None = MAX_LINES) -> str:
    """Pre-wrap for drawtext (it does no wrapping of its own). Whitespace is
    collapsed, words wrap at `width`. By default every word is kept — a long
    headline just gets more rows; pass `max_lines` to cut with an ellipsis
    instead."""
    text = " ".join((text or "").split())
    lines = textwrap.wrap(text, width=width, break_long_words=True)
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][: width - 1].rstrip() + "…"
    return "\n".join(lines)


def _ff_path(path: str) -> str:
    """Path for use inside a filter option: ffmpeg's filter parser treats ':'
    as an option separator and '\\' as an escape, so Windows paths must use
    forward slashes with the drive colon escaped."""
    return path.replace("\\", "/").replace(":", "\\:")


def _filter_graph(font_path: str, text_path: str, style: dict | None = None) -> str:
    style = style or DEFAULT_STYLE
    font_size = style["font_size"]
    box = f"{style['background']}@{style['background_alpha']}"
    return (
        f"[0:v]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
        f"crop={OUT_W}:{OUT_H},gblur=sigma=30[bg];"
        f"[0:v]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease[fg];"
        # setsar=1: the fit scales leave a fractional compensating SAR (e.g.
        # 4321:4320) in the header, and players that honor it show the frame
        # very slightly off-square. The canvas is the display shape; pin it.
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[canvas];"
        f"[1:v]scale={LOGO_W}:-1[logo];"
        f"[canvas][logo]overlay=W-w-{LOGO_MARGIN_X}:{LOGO_MARGIN_Y}[branded];"
        # The banner lives on its own transparent layer: drawtext's alpha=
        # fades the glyphs but not the box, so the layer is faded as a whole
        # and text + box vanish together. The color source is unbounded —
        # shortest=1 on the final overlay is what ends the render with the
        # video; without it ffmpeg keeps producing frames forever.
        f"color=black@0:size={OUT_W}x{OUT_H},format=rgba,"
        f"drawtext=fontfile='{_ff_path(font_path)}'"
        f":textfile='{_ff_path(text_path)}'"
        f":fontcolor={style['text']}:fontsize={font_size}:line_spacing={LINE_SPACING}"
        # boxw pins the banner's width: without it drawtext sizes the box to
        # the widest row, so a short headline gets a stubby box and no two
        # posts line up. boxborderw is top|right|bottom|left (ffmpeg 7.1+).
        f":box=1:boxcolor={box}"
        # boxw is the TEXT area — the borders sit outside it, so the drawn box
        # comes out BOX_W wide.
        f":boxborderw={BOX_PAD_Y}|{BOX_PAD_X}|{BOX_PAD_Y}|{BOX_PAD_X}"
        f":boxw={BOX_W - 2 * BOX_PAD_X}"
        f":x={TEXT_X}:y=h*{TEXT_Y},"
        f"fade=t=out:st={FADE_START}:d={FADE_DUR}:alpha=1[banner];"
        f"[branded][banner]overlay=0:0:shortest=1"
    )


def render_branded(video_path: str, headline: str, logo_path: str, out_path: str,
                   style: dict | None = None) -> str:
    """One branded variant: `video_path` + `logo_path` + `headline` ->
    `out_path` (1080x1920 mp4, same encode profile as shorts_format). `style`
    defaults to the `style.json` sitting next to the logo (see `load_style`),
    so callers get per-brand colors without passing anything. Blocking
    — async callers run it in a thread. Raises FileNotFoundError for a missing
    logo/font, ValueError for a broken style.json, RuntimeError (with ffmpeg's
    stderr tail) on a failed encode; a partial output file is removed."""
    if not os.path.isfile(logo_path):
        raise FileNotFoundError(f"logo not found: {logo_path}")
    if style is None:
        style = load_style(os.path.dirname(os.path.abspath(logo_path)))
    font_path = style["font"]
    if not os.path.isfile(font_path):
        raise FileNotFoundError(f"headline font not found: {font_path}")

    text_path = out_path + ".txt"
    # Rows are separated by a bare CR, and the file is written with newline=""
    # so Python doesn't translate it. ffmpeg 8's drawtext breaks the line on
    # either CR or LF, but it also DRAWS the LF — as the font has no glyph for
    # it, every row but the last ended in a black .notdef box (newlinebug.mp4).
    # A CR breaks the row and draws nothing.
    rows = wrap_to_px(strip_unrenderable(headline, font_path),
                      font_path, style["font_size"])
    with open(text_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(rows.replace("\n", "\r"))
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-i", video_path, "-i", logo_path,
                "-filter_complex", _filter_graph(font_path, text_path, style),
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
