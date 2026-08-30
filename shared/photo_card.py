"""
shared/photo_card.py — render a "news card" photo post from a hero photo,
up to two circular inset photos, a brand logo and a headline. Pure Pillow, no
network, no Telegram — the photo twin of `shared/branding.py`.

Layers, bottom to top (all geometry is a fraction of the canvas so a 4:5 or
1:1 target renders the same design):

  0  hero photo     cover-fit to the canvas; the crop window is steered by
                    `focus` (0..1 of the source height, where the subject's
                    eyes are) so the eyeline lands in the upper third.
  1  insets         circular crops, white stroke + soft drop shadow, top-left
                    and top-right, mirrored about the centre axis, fully
                    inside the frame with a small margin.
  1b subject        the hero's main subject (rembg alpha matte) pasted back
                    OVER the insets, so hair/shoulders overlap the circles
                    the way a cut-out does in the reference. Skipped, with a
                    log line, when rembg isn't installed or the matte is
                    empty — the card is still valid, just flat.
  2  scrim          black->transparent gradient anchored at the bottom.
  3  divider        thin white rule near 68 % height, split around a gap.
  4  logo           `brands/<name>/logo.png` centred in the gap.
  5  headline       upper-case, centred, auto-fitted (font size shrinks until
                    the text fits in MAX_LINES lines), top-anchored just
                    under the divider/logo and growing downward.

Blocking; async callers use `asyncio.to_thread`.

CLI (quick visual test):

    py shared/photo_card.py hero.jpg --headline "..." --brand mirnews \
        --inset a.jpg --inset b.jpg -o card.jpg
"""

import argparse
import logging
import os
import sys

# shared/photo_card.py -> shared/ -> <repo root>
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps  # noqa: E402

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- geometry --
# Canvas: IG portrait 4:5. Pass size=(1080, 1080) for a square post.
DEFAULT_SIZE = (1080, 1350)

# Where the subject's eyes should land, as a fraction of canvas height.
EYELINE = 0.34

# Insets: diameter as a fraction of canvas WIDTH, centre Y as a fraction of
# canvas height, margin = gap between the ring and the side edge (fraction of
# width). Measured off examplepost.png (1179x1465): circle 30..447 px on a
# centre line at y=300 -> d 0.354 W, margin 0.025 W, centre 0.205 H, ring 7 px.
INSET_DIAMETER = 0.355
INSET_CENTER_Y = 0.205
INSET_MARGIN = 0.025
INSET_STROKE = 7          # px of white ring
INSET_SHADOW_BLUR = 18    # px
INSET_SHADOW_OFFSET = (0, 10)
INSET_SHADOW_ALPHA = 150  # 0..255

# Subject cut-out over the insets. The matte is only trusted when it covers
# at least this fraction of the canvas — a near-empty matte means rembg found
# no subject (a landscape, a document) and pasting it would add noise.
SUBJECT_MIN_COVERAGE = 0.03
# rembg model. bria-rmbg (rembg's default, ~1 GB, downloaded to ~/.rembg on
# first use) gives the cleanest hair edges; "u2net" (~170 MB) is the light
# option for a small VPS. Override with CARD_CUTOUT_MODEL in .env.
CUTOUT_MODEL = os.environ.get("CARD_CUTOUT_MODEL", "bria-rmbg")
# Edge clean-up: the matte's half-transparent edge pixels still carry the
# hero's background colour, which shows as a halo over a circle. Erode the
# alpha by this many px (shaves the contaminated rim) then feather it.
CUTOUT_ERODE_PX = 2
CUTOUT_FEATHER_PX = 1.2

# Blur-fill behind a contain-fitted hero (aspect != canvas).
BLURFILL_RADIUS = 40
BLURFILL_DIM = 0.6

# Scrim: fully transparent at SCRIM_TOP, fully black from SCRIM_SOLID down.
SCRIM_TOP = 0.42
SCRIM_SOLID = 0.80

# Divider rule and logo.
DIVIDER_Y = 0.68
DIVIDER_MARGIN = 0.05     # left/right inset of the rule, fraction of width
DIVIDER_THICKNESS = 3
LOGO_HEIGHT = 0.045       # logo height as a fraction of canvas height
LOGO_GAP_PAD = 0.02       # clear space between the rule ends and the logo

# Headline block: sits between HEADLINE_TOP and HEADLINE_BOTTOM.
HEADLINE_TOP = 0.72
HEADLINE_BOTTOM = 0.97
HEADLINE_SIDE = 0.05
HEADLINE_GAP = 0.015      # space between the logo's bottom edge and the first row
MAX_LINES = 3
FONT_MAX = 0.085          # start size, fraction of canvas height
FONT_MIN = 0.04
LINE_SPACING = 1.02
HEADLINE_COLOR = (255, 255, 255)
HEADLINE_STROKE = 0       # px of dark outline, 0 = none

# Impact is the closest stock Windows face to the condensed bold in
# examplepost.png; Arial Bold and the shipped DejaVu are the fallbacks.
FONT_CANDIDATES = ("impact.ttf", "arialbd.ttf")
SHIPPED_FONT = os.path.join(_ROOT, "assets", "fonts", "DejaVuSans-Bold.ttf")
_SYSTEM_FONT_DIRS = [
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts"),
    "/usr/share/fonts", "/usr/local/share/fonts",
]


def default_font() -> str:
    for name in FONT_CANDIDATES:
        for d in _SYSTEM_FONT_DIRS:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
    return SHIPPED_FONT


def brand_logo(brand: str) -> str:
    p = os.path.join(_ROOT, "brands", brand, "logo.png")
    if not os.path.isfile(p):
        raise FileNotFoundError(f"no logo for brand {brand!r}: {p}")
    return p


# ------------------------------------------------------------------ layers --
def cover_fit(img: Image.Image, size: tuple[int, int], focus: float = EYELINE) -> Image.Image:
    """Scale-to-cover and crop. `focus` is where, in the SOURCE (0..1 of its
    height), the subject's eyes are; that row is placed at EYELINE of the
    canvas when the crop has vertical slack. Horizontally always centred."""
    W, H = size
    img = ImageOps.exif_transpose(img).convert("RGB")
    sw, sh = img.size
    scale = max(W / sw, H / sh)
    nw, nh = round(sw * scale), round(sh * scale)
    img = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - W) // 2
    # Put the focus row at EYELINE, clamped to the available slack.
    top = round(focus * nh - EYELINE * H)
    top = max(0, min(top, nh - H))
    return img.crop((left, top, left + W, top + H))


def contain_fit(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Scale-to-FIT (never crop, never zoom past the canvas): the whole photo,
    centred, over a blurred + darkened copy of itself that fills the bands
    left over when the aspect doesn't match. A 4:5 source fills the canvas
    exactly and looks identical to cover_fit."""
    W, H = size
    img = ImageOps.exif_transpose(img).convert("RGB")
    sw, sh = img.size
    scale = min(W / sw, H / sh)
    nw, nh = round(sw * scale), round(sh * scale)
    fg = img.resize((nw, nh), Image.LANCZOS)
    if (nw, nh) == (W, H):
        return fg
    bg = ImageOps.fit(img, size, Image.LANCZOS)
    bg = bg.filter(ImageFilter.GaussianBlur(BLURFILL_RADIUS))
    bg = Image.eval(bg, lambda v: int(v * BLURFILL_DIM))
    bg.paste(fg, ((W - nw) // 2, (H - nh) // 2))
    return bg


def circle_inset(img: Image.Image, diameter: int) -> Image.Image:
    """Circular crop with a white stroke and drop shadow, RGBA, sized so the
    shadow has room: (diameter + pad) square, circle centred."""
    pad = INSET_SHADOW_BLUR * 2 + max(abs(INSET_SHADOW_OFFSET[0]), abs(INSET_SHADOW_OFFSET[1]))
    S = diameter + 2 * pad
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # Shadow.
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    ox, oy = INSET_SHADOW_OFFSET
    sd.ellipse((pad + ox, pad + oy, pad + ox + diameter, pad + oy + diameter),
               fill=(0, 0, 0, INSET_SHADOW_ALPHA))
    shadow = shadow.filter(ImageFilter.GaussianBlur(INSET_SHADOW_BLUR))
    out.alpha_composite(shadow)

    # White ring.
    ring = ImageDraw.Draw(out)
    ring.ellipse((pad, pad, pad + diameter, pad + diameter), fill=(255, 255, 255, 255))

    # Photo, inside the ring.
    inner = diameter - 2 * INSET_STROKE
    photo = ImageOps.fit(ImageOps.exif_transpose(img).convert("RGB"), (inner, inner),
                         Image.LANCZOS, centering=(0.5, 0.4))
    mask = Image.new("L", (inner * 4, inner * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, inner * 4 - 1, inner * 4 - 1), fill=255)
    mask = mask.resize((inner, inner), Image.LANCZOS)      # anti-aliased edge
    out.paste(photo, (pad + INSET_STROKE, pad + INSET_STROKE), mask)
    return out


def subject_cutout(hero: Image.Image):
    """RGBA copy of `hero` with the background removed (rembg, u2net),
    or None when rembg is unavailable / the matte covers too little."""
    try:
        from rembg import remove
    except ImportError:
        log.info("photo_card: rembg not installed, no subject cut-out")
        return None
    try:
        from rembg import new_session
        cut = remove(hero.convert("RGB"), session=new_session(CUTOUT_MODEL)).convert("RGBA")
    except Exception as exc:  # model download failed, onnxruntime missing ...
        log.warning("photo_card: rembg failed (%s), no subject cut-out", exc)
        return None
    alpha = cut.getchannel("A")
    coverage = sum(alpha.histogram()[128:]) / (alpha.width * alpha.height)
    if coverage < SUBJECT_MIN_COVERAGE:
        log.info("photo_card: matte covers %.1f%%, no subject cut-out", coverage * 100)
        return None
    return cut


def _restrict_to_circles(subject: Image.Image, circles: list) -> Image.Image:
    """Keep the subject only over the circle badges (disc + shadow reach) so
    nothing outside them changes by even one pixel, and clean the matte edge
    (erode + feather) so the background-tinted rim doesn't halo the circle."""
    alpha = subject.getchannel("A")
    if CUTOUT_ERODE_PX:
        alpha = alpha.filter(ImageFilter.MinFilter(2 * CUTOUT_ERODE_PX + 1))
    if CUTOUT_FEATHER_PX:
        alpha = alpha.filter(ImageFilter.GaussianBlur(CUTOUT_FEATHER_PX))
    region = Image.new("L", subject.size, 0)
    rd = ImageDraw.Draw(region)
    reach = INSET_SHADOW_BLUR * 2 + max(abs(INSET_SHADOW_OFFSET[0]), abs(INSET_SHADOW_OFFSET[1]))
    for cx, cy, d in circles:
        r = d / 2 + reach
        rd.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
    alpha = Image.fromarray(
        (__import__("numpy").asarray(alpha, dtype="uint16")
         * __import__("numpy").asarray(region, dtype="uint16") // 255).astype("uint8"))
    out = subject.copy()
    out.putalpha(alpha)
    return out


def scrim(size: tuple[int, int]) -> Image.Image:
    W, H = size
    top, solid = round(SCRIM_TOP * H), round(SCRIM_SOLID * H)
    alpha = Image.new("L", (1, H), 0)
    px = alpha.load()
    for y in range(H):
        if y <= top:
            a = 0
        elif y >= solid:
            a = 255
        else:
            t = (y - top) / (solid - top)
            a = round(255 * t * t * (3 - 2 * t))          # smoothstep
        px[0, y] = a
    alpha = alpha.resize((W, H))
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    layer.putalpha(alpha)
    return layer


def _fit_font(draw: ImageDraw.ImageDraw, text: str, font_path: str, max_w: int,
              max_h: int, size_max: int, size_min: int):
    """Largest font size at which `text` wraps into <= MAX_LINES lines that fit
    max_w x max_h. Returns (font, lines). Below size_min we accept overflow in
    line count rather than dropping words."""
    words = text.split()
    size = size_max
    while True:
        font = ImageFont.truetype(font_path, size)
        lines, cur = [], []
        for w in words:
            trial = " ".join(cur + [w])
            if cur and draw.textlength(trial, font=font) > max_w:
                lines.append(" ".join(cur))
                cur = [w]
            else:
                cur = [w]  if not cur else cur + [w]
        if cur:
            lines.append(" ".join(cur))
        line_h = size * LINE_SPACING
        widest = max((draw.textlength(l, font=font) for l in lines), default=0)
        if (len(lines) <= MAX_LINES and widest <= max_w
                and line_h * len(lines) <= max_h) or size <= size_min:
            return font, lines
        size -= 2


# ---------------------------------------------------------------- renderer --
def render_card(hero_path: str, headline: str, logo_path: str, out_path: str,
                insets: list[str] | None = None, size: tuple[int, int] = DEFAULT_SIZE,
                focus: float = EYELINE, font_path: str | None = None,
                quality: int = 92, cutout: bool = True,
                fit: str = "contain") -> str:
    """Compose the card and write it to `out_path` (JPEG). Returns out_path.
    `cutout=False` skips the rembg subject layer (tests, quick previews).
    `fit`: "contain" (default) shows the whole photo with blur-filled bands;
    "cover" zooms/crops it to fill the canvas (steered by `focus`)."""
    W, H = size
    font_path = font_path or default_font()
    insets = [p for p in (insets or []) if p][:2]

    # 0 — hero
    if fit == "cover":
        hero = cover_fit(Image.open(hero_path), size, focus)
    else:
        hero = contain_fit(Image.open(hero_path), size)
    canvas = hero.convert("RGBA")

    # 1 — insets
    d = round(INSET_DIAMETER * W)
    cy = round(INSET_CENTER_Y * H)
    circles = []                                    # (cx, cy, d) for the cut-out mask
    for i, path in enumerate(insets):
        badge = circle_inset(Image.open(path), d)
        pad = (badge.width - d) // 2
        m = round(INSET_MARGIN * W)
        if i == 0:                                  # left, fully inside the frame
            cx = m + d // 2
        else:                                       # right, mirrored
            cx = W - m - d // 2
        canvas.alpha_composite(badge, (cx - d // 2 - pad, cy - d // 2 - pad))
        circles.append((cx, cy, d))

    # 1b — subject back on top of the insets, ONLY where a circle sits
    if insets and cutout:
        subject = subject_cutout(hero)
        if subject is not None:
            canvas.alpha_composite(_restrict_to_circles(subject, circles))

    # 2 — scrim
    canvas.alpha_composite(scrim(size))

    draw = ImageDraw.Draw(canvas)

    # 4 (measured first) — logo
    logo = Image.open(logo_path).convert("RGBA")
    lh = round(LOGO_HEIGHT * H)
    lw = round(logo.width * lh / logo.height)
    logo = logo.resize((lw, lh), Image.LANCZOS)
    ly = round(DIVIDER_Y * H)
    lx = (W - lw) // 2
    gap = round(LOGO_GAP_PAD * W)

    # 3 — divider, two segments around the logo
    m = round(DIVIDER_MARGIN * W)
    t = DIVIDER_THICKNESS
    draw.rectangle((m, ly - t // 2, lx - gap, ly + t // 2), fill=(255, 255, 255, 255))
    draw.rectangle((lx + lw + gap, ly - t // 2, W - m, ly + t // 2), fill=(255, 255, 255, 255))
    canvas.alpha_composite(logo, (lx, ly - lh // 2))

    # 5 — headline
    text = " ".join(headline.split()).upper()
    box_top, box_bot = round(HEADLINE_TOP * H), round(HEADLINE_BOTTOM * H)
    max_w = W - 2 * round(HEADLINE_SIDE * W)
    font, lines = _fit_font(draw, text, font_path, max_w, box_bot - box_top,
                            round(FONT_MAX * H), round(FONT_MIN * H))
    line_h = round(font.size * LINE_SPACING)
    # Top-anchored: the first row sits right under the divider/logo (plus a
    # small gap) and extra rows grow downward — a short headline hugs the
    # logo, a long one fills the block towards the bottom.
    y = max(box_top, ly + lh // 2 + round(HEADLINE_GAP * H))
    for line in lines:
        lw_ = draw.textlength(line, font=font)
        x = (W - lw_) / 2
        kw = {}
        if HEADLINE_STROKE:
            kw = dict(stroke_width=HEADLINE_STROKE, stroke_fill=(0, 0, 0, 255))
        draw.text((x, y), line, font=font, fill=HEADLINE_COLOR, anchor="la", **kw)
        y += line_h

    canvas.convert("RGB").save(out_path, "JPEG", quality=quality, subsampling=0)
    return out_path


# --------------------------------------------------------------------- CLI --
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Render a news-card photo post.")
    ap.add_argument("hero")
    ap.add_argument("--headline", required=True)
    ap.add_argument("--brand", help="brand name under brands/ (uses its logo.png)")
    ap.add_argument("--logo", help="explicit logo path (overrides --brand)")
    ap.add_argument("--inset", action="append", default=[], help="inset photo (max 2)")
    ap.add_argument("--focus", type=float, default=EYELINE,
                    help="0..1: where the eyes are in the source (default %(default)s)")
    ap.add_argument("--square", action="store_true", help="1080x1080 instead of 4:5")
    ap.add_argument("--fit", choices=("contain", "cover"), default="contain",
                    help="contain = whole photo, blur-filled bands (default); "
                         "cover = zoom/crop to fill")
    ap.add_argument("--font", help="TTF path override")
    ap.add_argument("-o", "--out", default="card.jpg")
    a = ap.parse_args(argv)

    logo = a.logo or brand_logo(a.brand or "mirnews")
    size = (1080, 1080) if a.square else DEFAULT_SIZE
    out = render_card(a.hero, a.headline, logo, a.out, insets=a.inset,
                      size=size, focus=a.focus, font_path=a.font, fit=a.fit)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
