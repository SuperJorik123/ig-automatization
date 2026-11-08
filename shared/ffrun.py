"""
shared/ffrun.py — run ffmpeg/ffprobe with a time limit.

Why: on 2026-11-04 ffmpeg's AAC encoder looped forever on one 18-second clip
(a mono track opening on digital silence, encoded at 192k by a build whose
retry loop has no cap). subprocess.run without a timeout waits for ever, and
the news bot handles one update at a time, so a single render froze the bot
for 30 hours while its heartbeat job kept reporting healthy.

Every ffmpeg in the bots goes through `run`, so a hang costs one bounded wait.
A hang is logged at ERROR, which reaches the operator through errmail.
"""

import logging
import os
import subprocess

log = logging.getLogger("ffrun")

# Measured on the 2-vCPU VPS (2026-11-08): a worst-case 60 s clip (1080x1920
# with heavy noise, 1.3 GB per output) took 236 s for two brands, i.e. about
# 2 s per clip-second per output. The defaults allow three times that.
BASE_S = int(os.environ.get("FFMPEG_TIMEOUT_BASE_S") or 180)
PER_CLIP_S = int(os.environ.get("FFMPEG_TIMEOUT_PER_CLIP_S") or 6)
UNKNOWN_DURATION_S = int(os.environ.get("FFMPEG_TIMEOUT_UNKNOWN_S") or 3600)
PROBE_S = 60


class Timeout(RuntimeError):
    """ffmpeg/ffprobe was still running at its limit and has been killed."""


def limit_s(duration: float | None, outputs: int = 1) -> int:
    """Seconds one encode of a `duration`-long clip into `outputs` files may
    take. An unknown duration gets a flat, generous limit."""
    if not duration or duration <= 0:
        return UNKNOWN_DURATION_S
    return int(BASE_S + PER_CLIP_S * max(outputs, 1) * duration)


def run(cmd: list, limit: int) -> subprocess.CompletedProcess:
    """subprocess.run with a limit: past it the process is SIGKILLed (a hung
    encoder doesn't react to SIGTERM) and `Timeout` is raised."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=limit)
    except subprocess.TimeoutExpired as exc:
        log.error("%s killed after %d s: %s", cmd[0], limit, cmd[-1])
        raise Timeout(f"{cmd[0]} hung and was stopped after {limit} s") from exc


def duration_of(path: str) -> float | None:
    """The file's duration in seconds, None when it can't be read. Never
    raises: it only sizes a time limit."""
    try:
        proc = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "csv=p=0", path], PROBE_S)
        return float(proc.stdout.strip())
    except Exception:
        return None
