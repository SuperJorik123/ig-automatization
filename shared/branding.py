"""
shared/branding.py — burn a brand onto a clip: logo top-right, headline in a
lower-third banner. Pure — no Telegram, no network. `render_branded` is one
brand per ffmpeg pass; `render_branded_multi` does every brand in a single
pass, building the shared blur-fill canvas once and splitting it per brand
(the per-brand encodes still dominate — see the speed-knob comments below).

The geometry is FIXED (same size and position every time): 1080x1920 canvas
(blur-fill, the same treatment shorts_format gives horizontal videos — the
clip is fit to the whole canvas, so a 9:16 input fills it and only wider or
narrower ones show blur above/below or beside), logo scaled to 205 px
wide sitting 82 px from the right edge and 102 px from the top, headline at
a box whose bottom edge sits at 79 % of frame height (clear of Instagram's UI
in the bottom 20 %) — semi-bold, in a banner box of fixed width (matched to
the reference renders at the repo root, `example.MP4` foremost),
tightly-spaced rows (as many as the text needs, never truncated), left-aligned
inside it, fading out smoothly after 5 s. The box does NOT resize to the
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
import logging
import os
import re
import textwrap
import unicodedata

from shared import ffrun, renderlock

log = logging.getLogger(__name__)

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
# The banner is anchored by its BOTTOM edge, not its top: Instagram draws its
# own UI (caption, buttons, the audio row) over the bottom ~20 % of a Reel, so
# the box must end above that line however many rows the headline wraps to —
# a top anchor grew a three-row banner straight into it. 0.79 leaves a few px
# of slack under the 0.80 line for drawtext's rounding of text_h.
BANNER_BOTTOM = 0.79        # box bottom edge, as a fraction of frame height
# Rows wrap to the pixel width left inside the box — a fixed box can't be
# allowed to overflow, and character counts can't promise that across fonts.
LINE_PX = BOX_X + BOX_W - TEXT_X - BOX_PAD_X
LINE_WIDTH = 37             # chars/line fallback when the font can't be measured
MAX_LINES = None            # no cap: a long headline gets more rows, never "…"
# Negative: drawtext's own line height (font ascent+descent, ~65 px at 49) is
# looser than the reference's 57 px row pitch, and headline rows are meant to
# sit tight.
LINE_SPACING = -8
FADE_START = 5              # s the banner stays fully visible
FADE_DUR = 1.5              # s of smooth fade-out after that

# ---- speed knobs, measured on the 1-vCPU VPS (2026-08-17) ------------------
# The x264 encode is ~85 % of a render's cost, which makes the preset the
# single biggest lever: superfast is ~2x veryfast at the same CRF, paying in
# file size (~1.5x) rather than visible quality. ultrafast would be another
# ~35 % faster but disables enough coding tools to soften the picture.
PRESET = os.environ.get("BRAND_PRESET") or "superfast"

# ---- quality knobs (2026-10-18) ---------------------------------------------
# Instagram re-encodes every upload, so what we hand it is a master: the
# cleaner it is, the less its encoder spends on our compression noise.
# CRF 18 is near-transparent for x264 (21 was visibly soft on faces and
# on-screen text); files come out ~1.5-2x bigger, which the Graph API's
# fetch doesn't care about.
CRF = int(os.environ.get("BRAND_CRF") or 18)
# Luma-only unsharp mask on the clip AFTER it is scaled into the canvas — most
# sources are 720p tweets upscaled to 1080 wide, and this is the "HD export"
# crispness phone editors add. 0 disables. Past ~0.8 it halos edges and
# makes the source's compression blocks stand out.
SHARPEN = float(os.environ.get("BRAND_SHARPEN") or 0.5)
# Every render is tagged BT.709 and converted to it on the way in (an SD
# source can be BT.601): untagged video is guessed at by players, and
# Instagram's guess sometimes shifts the colours.
_BT709 = ":out_color_matrix=bt709:out_range=tv"
# The blur-fill background is computed on a 1/8-scale copy and upscaled back:
# blurring 135x240 and stretching to 1080x1920 is visually identical to
# gblur sigma=30 at full resolution (the upscale is itself a low-pass) and
# skips the full-res Gaussian — ~12 % of the render.
BLUR_W, BLUR_H = 135, 240
BLUR_SIGMA = 4

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

    `writing_style` is also read out of this file, by `load_writing_style` —
    it is the account's caption voice and is deliberately not returned here.

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


# The one key in style.json that has nothing to do with the render: the
# account's WRITING voice, used by modules/instagram/caption.py to rewrite the
# shared Instagram caption in this brand's own style. It lives here because
# style.json is this module's file and a brand should have one style file, not
# two — but load_style() deliberately does not return it, so nothing in the
# ffmpeg path can be confused by a paragraph of English prose.
WRITING_STYLE_KEY = "writing_style"

# A style long enough to drown the rewrite instructions is a mistake, not a
# voice; the descriptors that ship with the repo are 300-500 characters.
MAX_WRITING_STYLE = 1200


def load_writing_style(brand_dir: str) -> str:
    """The brand's writing voice from `<brand_dir>/style.json`, or "".

    Never raises: a missing file, a broken file, a missing key or a
    non-string value all mean "this brand has no voice of its own", and the
    caption falls back to the neutral wire register every account used before
    styles existed. A caption is not worth failing a publish over — which is
    the same contract modules/instagram/caption.py holds.
    """
    path = os.path.join(brand_dir, STYLE_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        value = raw[WRITING_STYLE_KEY]
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    if not isinstance(value, str):
        log.warning("%s: %s is not a string — ignoring it",
                    path, WRITING_STYLE_KEY)
        return ""
    return " ".join(value.split())[:MAX_WRITING_STYLE]


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


def blurfill_chain(out_label: str = "[canvas]") -> str:
    """The blur-fill canvas every render shares: the source scaled small,
    cropped, blurred, upscaled as the background; the source fit on top.
    Built ONCE per ffmpeg run whatever comes after it. Ends in `out_label`
    (empty string = the graph's unlabeled final output, for callers that
    want the bare canvas, e.g. shorts_format)."""
    sar = ",setsar=1" if out_label else ""
    # The background is blurred anyway, so its scalers stay the cheap default;
    # the clip itself gets lanczos (sharper than bicubic on an upscale).
    sharpen = f",unsharp=5:5:{SHARPEN:g}:5:5:0" if SHARPEN > 0 else ""
    return (
        f"[0:v]scale={BLUR_W}:{BLUR_H}:force_original_aspect_ratio=increase,"
        f"crop={BLUR_W}:{BLUR_H},gblur=sigma={BLUR_SIGMA},"
        f"scale={OUT_W}:{OUT_H}{_BT709}[bg];"
        f"[0:v]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease"
        f":force_divisible_by=2:flags=lanczos{_BT709}{sharpen}[fg];"
        # setsar=1: the fit scales leave a fractional compensating SAR (e.g.
        # 4321:4320) in the header, and players that honor it show the frame
        # very slightly off-square. The canvas is the display shape; pin it.
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2{sar}{out_label}"
    )


def _brand_chain(i: int, font_path: str, text_path: str,
                 style: dict | None, canvas_label: str, logo_input: int,
                 out_label: str) -> str:
    """One brand's layers over a canvas copy: logo top-right, headline banner,
    fade. `logo_input` is the ffmpeg input index carrying the brand's logo;
    `out_label` names the branch's output stream ("" = unlabeled final)."""
    style = style or DEFAULT_STYLE
    font_size = style["font_size"]
    box = f"{style['background']}@{style['background_alpha']}"
    return (
        f"[{logo_input}:v]scale={LOGO_W}:-1:flags=lanczos[logo{i}];"
        f"[{canvas_label}][logo{i}]overlay="
        f"W-w-{LOGO_MARGIN_X}:{LOGO_MARGIN_Y}[branded{i}];"
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
        f":x={TEXT_X}:y=h*{BANNER_BOTTOM}-{BOX_PAD_Y}-text_h,"
        f"fade=t=out:st={FADE_START}:d={FADE_DUR}:alpha=1[banner{i}];"
        f"[branded{i}][banner{i}]overlay=0:0:shortest=1{out_label}"
    )


def _filter_graph(font_path: str, text_path: str, style: dict | None = None) -> str:
    return (blurfill_chain() + ";"
            + _brand_chain(0, font_path, text_path, style, "canvas", 1, ""))


def _filter_graph_multi(jobs: list[dict]) -> str:
    """One canvas, N brand branches. Each job dict carries `font`, `text_path`
    and `style` (see `render_branded_multi`, which prepares them); job i reads
    its logo from ffmpeg input i+1 and writes its frames to [v<i>]."""
    n = len(jobs)
    split = f"[canvas]split={n}" + "".join(f"[c{i}]" for i in range(n))
    chains = ";".join(
        _brand_chain(i, j["font"], j["text_path"], j["style"],
                     f"c{i}", i + 1, f"[v{i}]")
        for i, j in enumerate(jobs))
    return blurfill_chain() + ";" + split + ";" + chains


# Brands per ffmpeg pass. Every extra output in a pass is another x264 encoder
# living inside the SAME process — measured at 1080x1920: ~0.5 GB for the first
# brand and ~390 MB for each one after it (13 brands in one pass peaked at
# 5.0 GB, which is how a 2 GB VPS reaches 99% RAM and the OOM killer takes the
# render). Batching caps the peak at roughly 0.5 + 0.39*(BATCH-1) GB while
# keeping the shared decode/blur inside each pass, so the saving the single
# pass exists for is only lost BETWEEN batches. 4 fits a 4 GB box; drop it to 2
# on a 2 GB one. 0 or less = one pass for everything, the old behaviour.
RENDER_BATCH = int(os.environ.get("BRAND_RENDER_BATCH") or 4)


# The per-output half of the encode command, identical for every render here
# and in shorts_format: x264 at the preset/CRF above, BT.709 tags matching the
# conversion in blurfill_chain, 192k/48 kHz audio, faststart for streaming
# players.
ENCODE_ARGS = [
    "-c:v", "libx264", "-preset", PRESET, "-crf", str(CRF),
    "-pix_fmt", "yuv420p",
    "-colorspace", "bt709", "-color_primaries", "bt709",
    "-color_trc", "bt709", "-color_range", "tv",
    "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
    "-movflags", "+faststart",
]

# The retry after a hang. ffmpeg's default AAC method looped for ever on a clip
# in the 176-208k band (2026-11-04); twoloop finished the same audio.
SAFE_AUDIO_ARGS = ["-aac_coder", "twoloop"]


def _encode_args(safe_audio: bool) -> list:
    if not safe_audio:
        return ENCODE_ARGS
    at = ENCODE_ARGS.index("-b:a")
    return ENCODE_ARGS[:at] + SAFE_AUDIO_ARGS + ENCODE_ARGS[at:]


def run_encode(build, source: str, outputs: int = 1,
               safe_audio: bool = False) -> tuple:
    """Run the ffmpeg command `build(encode_args)` under a time limit; on a
    hang, once more with the safe audio method. Returns (process, whether the
    safe method was used). `safe_audio=True` starts there, so a later batch of
    the same clip doesn't wait out the limit again. Raises ffrun.Timeout when
    the retry hangs too."""
    limit = ffrun.limit_s(ffrun.duration_of(source), outputs)
    if not safe_audio:
        try:
            return ffrun.run(build(ENCODE_ARGS), limit), False
        except ffrun.Timeout:
            log.warning("render of %s hung — retrying with %s", source,
                        " ".join(SAFE_AUDIO_ARGS))
    return ffrun.run(build(_encode_args(True)), limit), True


def _prepare_job(headline: str, logo_path: str, out_path: str,
                 style: dict | None) -> dict:
    """Validate one brand's inputs and write its drawtext textfile. Returns
    the dict `_filter_graph_multi` consumes. Raises before any ffmpeg runs —
    a bad brand must fail on the tap, not a minute into a render."""
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
    return {"logo_path": logo_path, "out_path": out_path, "style": style,
            "font": font_path, "text_path": text_path}


def render_branded(video_path: str, headline: str, logo_path: str, out_path: str,
                   style: dict | None = None) -> str:
    """One branded variant: `video_path` + `logo_path` + `headline` ->
    `out_path` (1080x1920 mp4, same encode profile as shorts_format). `style`
    defaults to the `style.json` sitting next to the logo (see `load_style`),
    so callers get per-brand colors without passing anything. Blocking
    — async callers run it in a thread. Raises FileNotFoundError for a missing
    logo/font, ValueError for a broken style.json, RuntimeError (with ffmpeg's
    stderr tail) on a failed encode; a partial output file is removed."""
    job = _prepare_job(headline, logo_path, out_path, style)
    try:
        with renderlock.render_slot():
            proc, _ = run_encode(
                lambda encode: [
                    "ffmpeg", "-y", "-v", "error",
                    "-i", video_path, "-i", logo_path,
                    "-filter_complex",
                    _filter_graph(job["font"], job["text_path"], job["style"]),
                    *encode,
                    out_path,
                ],
                video_path,
            )
        if proc.returncode != 0:
            try:
                os.remove(out_path)
            except OSError:
                pass
            raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[-300:]}")
    except ffrun.Timeout:
        try:
            os.remove(out_path)
        except OSError:
            pass
        raise
    finally:
        try:
            os.remove(job["text_path"])
        except OSError:
            pass
    return out_path


def _render_pass(video_path: str, prepared: list[dict],
                 safe_audio: bool = False) -> bool:
    """One ffmpeg invocation for `prepared` (already validated by
    `_prepare_job`): the source decoded and the blur-fill canvas built once,
    split N ways, each branch adding its own logo + banner and encoding its
    own output. Raises RuntimeError with ffmpeg's stderr tail on a failure;
    the caller owns the cleanup of the outputs. Returns whether the pass
    needed the safe audio method (see `run_encode`)."""
    def build(encode: list) -> list:
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", video_path]
        for p in prepared:
            cmd += ["-i", p["logo_path"]]
        cmd += ["-filter_complex", _filter_graph_multi(prepared)]
        for i, p in enumerate(prepared):
            # "0:a?" — map the source audio when there is one, silently skip
            # when there isn't (screen recordings often ship without).
            cmd += ["-map", f"[v{i}]", "-map", "0:a?", *encode, p["out_path"]]
        return cmd

    # The slot is taken per batch, not once around the whole set: between two
    # batches is exactly where another process's render should get its turn.
    with renderlock.render_slot():
        proc, used_safe = run_encode(build, video_path, len(prepared), safe_audio)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[-300:]}")
    return used_safe


def render_branded_multi(video_path: str, jobs: list[dict],
                         batch: int | None = None) -> list[str]:
    """Every brand rendered, `batch` of them per ffmpeg pass (default
    `RENDER_BATCH`; 0 or less puts them all in one pass). Within a pass the
    source is decoded and the blur-fill canvas built once and split N ways —
    the encodes still happen N times and are what dominates, which is why the
    pass is capped at all: N simultaneous x264 encoders is N times the encoder
    memory, and that is what pegs a small VPS. Each job dict: `headline`,
    `logo_path`, `out_path`, optional `style`.

    Every job is validated up front, so a bad logo or a broken style.json
    fails before the first ffmpeg starts rather than a batch and a half in.

    All-or-nothing across ALL batches: any failure removes every output
    written so far — including finished earlier batches — and raises. A caller
    that needs per-brand isolation (news_bot does) catches this and falls back
    to one `render_branded` per job. Returns out paths in job order."""
    if not jobs:
        return []
    size = batch if batch is not None else RENDER_BATCH
    if size <= 0:
        size = len(jobs)
    prepared = []
    try:
        for job in jobs:
            prepared.append(_prepare_job(job["headline"], job["logo_path"],
                                         job["out_path"], job.get("style")))
        safe_audio = False
        for start in range(0, len(prepared), size):
            try:
                safe_audio = _render_pass(video_path, prepared[start:start + size],
                                          safe_audio)
            except Exception:
                for p in prepared:
                    try:
                        os.remove(p["out_path"])
                    except OSError:
                        pass
                raise
        return [p["out_path"] for p in prepared]
    finally:
        for p in prepared:
            try:
                os.remove(p["text_path"])
            except OSError:
                pass
