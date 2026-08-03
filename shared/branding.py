"""
shared/branding.py — burn a brand onto a clip: logo top-right, headline in a
lower-third banner. One ffmpeg pass per call; pure — no Telegram, no network.

The design is FIXED (same font, size, position every time): 1080x1920 canvas
(blur-fill, the same treatment shorts_format gives horizontal videos — for an
exact 9:16 input the background is simply invisible), logo scaled to 180 px
wide with a 40 px top-right margin, headline centered at 72 % frame height —
white bold 42 px, at most two tightly-spaced rows, on a translucent black
box. The font ships in the repo
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
FONT_SIZE = 42
BOX_ALPHA = 0.55
BOX_PAD = 20                # boxborderw
TEXT_Y = 0.72               # banner anchor, fraction of frame height
LINE_WIDTH = 40             # ~chars per line at FONT_SIZE on a 1080 canvas
MAX_LINES = 2
LINE_SPACING = 10           # px between the two rows — tight, headline-style


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
        # setsar=1: the fit scales leave a fractional compensating SAR (e.g.
        # 4321:4320) in the header, and players that honor it show the frame
        # very slightly off-square. The canvas is the display shape; pin it.
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[canvas];"
        f"[1:v]scale={LOGO_W}:-1[logo];"
        f"[canvas][logo]overlay=W-w-{LOGO_MARGIN}:{LOGO_MARGIN}[branded];"
        f"[branded]drawtext=fontfile='{_ff_path(font_path)}'"
        f":textfile='{_ff_path(text_path)}'"
        f":fontcolor=white:fontsize={FONT_SIZE}:line_spacing={LINE_SPACING}"
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
    # newline="\n": Windows text mode would write \r\n, and drawtext renders
    # the \r as a whole blank row between the lines.
    with open(text_path, "w", encoding="utf-8", newline="\n") as fh:
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
