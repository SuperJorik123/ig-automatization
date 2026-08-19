"""
modules/youtube/shorts_format.py — make sure a video will be classified as a
YouTube Short before uploading.

YouTube auto-detects Shorts — there is no API flag, the file itself decides.
In practice only strictly vertical videos (display height > width) reliably
classify as Shorts (a square 1:1 upload has been observed landing as a
regular video), so only those pass through untouched; square and horizontal
ones are re-rendered to 1080x1920 with the clip centered over a blurred,
zoomed copy of itself (the standard Shorts fill). Videos over the 3-minute
cap raise — better to fail loudly than to silently publish a regular video.

Requires ffmpeg + ffprobe on PATH.
"""

import json
import logging
import os
import subprocess
import sys

# Repo root on sys.path so `from shared import …` resolves when run directly.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shared import branding, renderlock  # noqa: E402

log = logging.getLogger(__name__)

MAX_SHORT_S = 180          # YouTube's Shorts length cap (3 minutes)
OUT_W, OUT_H = 1080, 1920  # target vertical canvas


def probe(path: str) -> tuple[int, int, float]:
    """Display width, height and duration (seconds) of the first video stream.
    Rotation metadata is applied so a phone clip stored 1920x1080 with a 90°
    display-matrix reads as 1080x1920."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,side_data_list",
        "-show_entries", "format=duration",
        "-of", "json", path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {proc.stderr.strip()[-300:]}")
    info = json.loads(proc.stdout)
    if not info.get("streams"):
        raise ValueError("no video stream found")
    stream = info["streams"][0]
    w, h = stream["width"], stream["height"]
    rotation = 0
    for sd in stream.get("side_data_list", []):
        if "rotation" in sd:
            rotation = int(sd["rotation"])
    if abs(rotation) % 180 == 90:
        w, h = h, w
    duration = float(info.get("format", {}).get("duration") or 0)
    return w, h, duration


def ensure_short(path: str) -> tuple[str, bool]:
    """Return (upload_path, converted). Vertical/square videos come back
    unchanged; horizontal ones are re-rendered next to the source as
    <name>_short.mp4 — the caller deletes it after uploading. Raises
    ValueError when the video can't be a Short (too long)."""
    w, h, duration = probe(path)
    if duration > MAX_SHORT_S:
        raise ValueError(
            f"video is {duration:.0f}s — over the {MAX_SHORT_S}s Shorts cap"
        )
    if h > w:
        return path, False

    out = os.path.splitext(path)[0] + "_short.mp4"
    # The same blur-fill canvas and encode profile branding uses (cheap-blur
    # background, speed preset) — one place tunes both renderers.
    filters = branding.blurfill_chain(out_label="")
    log.info("re-rendering %s (%dx%d horizontal) to %dx%d vertical", path, w, h, OUT_W, OUT_H)
    with renderlock.render_slot():
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error", "-i", path,
                "-filter_complex", filters,
                *branding.ENCODE_ARGS,
                out,
            ],
            capture_output=True, text=True,
        )
    if proc.returncode != 0:
        try:
            os.remove(out)
        except OSError:
            pass
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[-300:]}")
    return out, True
