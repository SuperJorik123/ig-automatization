"""
shared/vision.py — MEDIA NOTES: what can be read or heard in the media, and
nothing else.

DEMOTED 2026-10-18. This used to narrate the clip (`summary`, `beats`) and the
caption was written from that narration. Gemini gets ACTIONS wrong too often
for that — a woman bumping into a man came back as a hug, and on posts/005.mp4
"teenager saves friend from electrocution" came back as "two players fell
over" and overrode the operator's headline. Facts now come from people: the
operator's info, the headline, the source post the clip was lifted from (see
modules/instagram/caption.py). This module reports only what a model reads
or hears reliably — on-screen text, speech, language, setting, a head count —
and is told outright not to describe what anybody does. The headline is no
longer sent either: it was "context", and context is what talked the model
into describing the headline instead of the media.

What follows is the history of why the media is analysed at all.

THE BUG THIS EXISTS TO FIX. An Instagram caption used to be produced from the
headline alone: `modules/instagram/caption.py` sent it to an `:online` model,
and OpenRouter's `:online` suffix is a search plugin that runs BEFORE the model
sees the prompt, building its query out of that prompt. So a clip of a boxer
headlined "Boxer knocked out in first round" searched those words, returned the
most prominent story matching them, and published a caption about Floyd
Mayweather over footage of somebody else entirely. Nothing in the pipeline had
ever looked at the video.

Attaching the clip to that same call does not fix it — the search still fires
off the headline first. The order has to be inverted, which means two calls,
and this module is the first one:

    media + headline  ->  [here]  ->  what the footage actually shows
                                            |
              that description + headline  ->  [caption.expand]  ->  the caption

The second call's search query is then built from the footage, not from the
words the operator happened to type.

AUDIO IS NOT OPTIONAL. The caption this was built for reads "People nearby
began shouting and warning him to turn around" — nobody can see that. So the
clip goes to a natively multimodal model with its audio track intact rather
than to a vision model as a handful of stills.

WHY THE CLIP IS BASE64'd AND NOT HOSTED. OpenRouter takes video as a
`video_url` content part, but for Gemini a plain https URL works only for
YouTube links (AI Studio); Vertex wants a data URL. Hosting the clip on a dummy
YouTube channel is closed off too: Gemini reads PUBLIC videos only — not
unlisted, not private — so every clip would sit publicly on a channel before
publication, which is the exposure that got three YouTube accounts terminated
in 2026-08 (see YT_UPLOADS_ENABLED in shared/config.py). A data URL it is, and
`_analysis_copy` is what keeps the request body sane: a 40 MB render becomes a
2-4 MB analysis copy at 480p with 64 kbps mono audio.

NEVER RAISES, the contract this shares with modules/telegram/translator.py and
modules/instagram/caption.py. A missing key, a missing ffmpeg, a dead gateway,
prose where JSON was asked for, an oversized clip — every one of them returns
{} and logs. An empty dict is what the caption pipeline already handles as "no
footage", and that path is exactly what shipped before this module existed.

Blocking; async callers go through asyncio.to_thread.
"""

import base64
import json
import logging
import mimetypes
import os
import re
import subprocess
import sys
import tempfile

# Repo-root bootstrap for direct runs (`py shared/vision.py clip.mp4`).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from openai import OpenAI  # noqa: E402

from shared import config  # noqa: E402

log = logging.getLogger(__name__)

_client = (
    OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=config.OPENROUTER_API_KEY,
    )
    if config.OPENROUTER_API_KEY
    else None
)

# The analysis is a page of JSON, not a caption — but a model that reasons
# before answering spends tokens getting there, and a truncated object parses
# as nothing at all. Roomy enough that the model is never the one that stops.
MAX_TOKENS = 4000

_VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi"}

_SYSTEM = (
    "You are shown one video or photo a news account will publish. Do NOT "
    "describe or interpret what people do; that comes from other sources. "
    "Report only what you can read or hear with certainty.\n\n"
    "Reply with ONE JSON object and nothing else:\n"
    "{\n"
    '  "on_screen_text": "all text visible in the media, verbatim: captions, '
    'chyrons, signs, CCTV timestamps, or \\"\\"",\n'
    '  "speech": "what is said, transcribed or closely summarised, or '
    '\\"\\"",\n'
    '  "language": "spoken or written language if identifiable, or \\"\\"",\n'
    '  "setting": "indoor/outdoor, type of place, day/night, or \\"\\"",\n'
    '  "people_count": "approximate number of people clearly involved, or '
    '\\"\\""\n'
    "}\n\n"
    "If you are not certain, leave the field empty. An empty field is better "
    "than a guess. Never name a person unless the media itself names them."
)

_USER = "Report what can be read and heard in this media."

# A model told to emit bare JSON still wraps it in a fence or a sentence often
# enough to be worth recovering rather than losing the analysis over.
_FENCE = re.compile(r"```[a-zA-Z]*\s*(.*?)\s*```", re.DOTALL)

# Every key the callers read, with what an absent one means.
_SHAPE = {
    "on_screen_text": "",
    "speech": "",
    "language": "",
    "setting": "",
    "people_count": "",
}


def is_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in _VIDEO_EXTS


def _mime(path: str, video: bool) -> str:
    guess, _ = mimetypes.guess_type(path)
    if guess and guess.startswith("video/" if video else "image/"):
        return guess
    return "video/mp4" if video else "image/jpeg"


# The two encoder passes, cheapest first. Each is (short-edge pixels, CRF) —
# the second is only reached when the first still lands over IG_VISION_MAX_MB,
# which on a two-minute clip means something pathological (a screen recording
# of noise, say) rather than an ordinary phone video.
# 720p first (2026-10-07): Gemini bills per FRAME, not per pixel, and squeezes
# each frame into roughly a 768-px budget — a 720x1280 frame fills it, a
# 480p one at CRF 30 left faces, plates and on-screen text blurred for nothing.
# So 720p costs the same tokens and only a bigger upload (~2-4 MB a minute,
# under the ceiling up to ~4-5 min). The smaller passes are for longer clips:
# a smaller picture of all of it beats a skipped analysis.
_PASSES = ((720, 28), (480, 30), (360, 34), (240, 38))


def _analysis_copy(path: str):
    """A small copy of `path` for analysis: (copy_path, cleanup).

    Scaled to `short edge` pixels, low-bitrate H.264, 64 kbps MONO audio KEPT —
    the shouting in the clip is half of what the caption is written from — and
    the WHOLE clip by default; trimmed to the first IG_VISION_MAX_S seconds
    only when that is set above 0.

    `-t` before `-i` would seek the input; it goes AFTER, so the trim is on the
    output and the clip starts where the clip starts. Raises RuntimeError with
    ffmpeg's stderr tail when the encode fails, which `describe` turns into {}.
    Always returns a cleanup that never raises — call it in a `finally`.

    Passes are tried cheapest first and stop as soon as one lands under the
    ceiling, but the LAST pass is returned whatever it weighs: refusing to send
    an oversized body is `describe`'s call, since that is the one that knows
    what a request costs. This function's job ends at making the file small.
    """
    fd, out = tempfile.mkstemp(prefix="vision_", suffix=".mp4")
    os.close(fd)

    def _drop():
        try:
            os.remove(out)
        except OSError:
            pass

    limit = float(config.IG_VISION_MAX_MB) * 1024 * 1024
    last_err = ""
    made = False
    try:
        for edge, crf in _PASSES:
            # IG_VISION_MAX_S = 0 sends the whole clip; a positive value trims.
            trim = (["-t", str(int(config.IG_VISION_MAX_S))]
                    if int(config.IG_VISION_MAX_S) > 0 else [])
            proc = subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-i", path, *trim,
                 # The SHORT edge goes to `edge`, whichever way round the clip
                 # is (scaling the width alone made a 1920x1080 clip 480x270);
                 # -2 keeps the long edge even (x264 needs it), and min()
                 # never upscales a clip that is already smaller.
                 "-vf", (f"scale='if(gt(iw,ih),-2,min(iw,{edge}))'"
                         f":'if(gt(iw,ih),min(ih,{edge}),-2)'"),
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                 "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-ac", "1", "-b:a", "64k",
                 "-movflags", "+faststart", out],
                capture_output=True, text=True)
            if proc.returncode != 0:
                last_err = proc.stderr.strip()[-300:]
                continue
            made = True
            if os.path.getsize(out) <= limit:
                return out, _drop
            log.info("vision: analysis copy is %.1f MB at %dp — trying harder",
                     os.path.getsize(out) / 1048576, edge)
    except FileNotFoundError:
        _drop()
        raise RuntimeError("ffmpeg is not on PATH")
    except Exception:
        _drop()
        raise
    if made:
        return out, _drop                     # describe() weighs it
    _drop()
    raise RuntimeError(last_err or "ffmpeg produced nothing")


def _data_url(path: str, mime: str) -> str:
    with open(path, "rb") as fh:
        return f"data:{mime};base64," + base64.b64encode(fh.read()).decode()


def _parse(raw: str) -> dict:
    """The model's reply as the analysis dict, or {} if it isn't one.

    Recovers a fenced or preamble-wrapped object rather than losing an
    otherwise good analysis to a wrapper the prompt already forbade, then
    forces every value to a string: a model that answers `"people_count": 3`
    or a list of signs must not reach a caller expecting text. Keys outside
    the shape (an old-style "summary" a model still volunteers) are dropped —
    narration is exactly what this module no longer passes on.
    """
    text = (raw or "").strip()
    if not text:
        return {}
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return {}
        text = text[start:end + 1]
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}

    out = dict(_SHAPE)
    for key in _SHAPE:
        value = data.get(key)
        if isinstance(value, list):
            value = "; ".join(str(v).strip() for v in value if str(v).strip())
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        # One line per field: a sign or a chyron comes back with its own line
        # breaks, and the notes travel as ONE line of the caption prompt.
        out[key] = (" / ".join(part.strip() for part in value.splitlines()
                               if part.strip())
                    if isinstance(value, str) else "")
    # Every field empty is a model that saw nothing it was sure of — the
    # prompt's own instruction — and is the same as no notes at all.
    if not any(out.values()):
        return {}
    return out


def describe(path: str, headline: str = "", model: str | None = None) -> dict:
    """What `path` shows, as a dict — or {} when that can't be established.

    `headline` is accepted for the callers' sake and NOT sent: the notes are
    what the media itself says, and a headline in the prompt is what used to
    talk the model into reporting the headline.

    Returns {} — never raises — for: analysis switched off, no API key, a
    missing file, a failed or oversized ffmpeg pre-pass, a failed call, or a
    reply that isn't the JSON object it was asked for. Every caller treats {}
    as "no footage" and behaves exactly as it did before this existed.
    """
    if not config.IG_VISION_ENABLED:
        return {}
    if _client is None:
        log.warning("OPENROUTER_API_KEY not set — publishing without a "
                    "footage analysis")
        return {}
    if not path or not os.path.isfile(path):
        log.warning("vision: %r is not a file — no footage analysis", path)
        return {}

    video = is_video(path)
    drop = None
    try:
        if video:
            send_path, drop = _analysis_copy(path)
        else:
            send_path = path
        # The last word on what goes over the wire. A clip the pre-pass could
        # not get under the ceiling is skipped rather than sent: base64 adds a
        # third on top of it, and a caption with no footage costs the post far
        # less than a request body that times out mid-publish.
        size = os.path.getsize(send_path)
        if size > float(config.IG_VISION_MAX_MB) * 1024 * 1024:
            raise RuntimeError(
                f"{size / 1048576:.1f} MB after the pre-pass, over the "
                f"{config.IG_VISION_MAX_MB} MB ceiling")
        part_kind = "video_url" if video else "image_url"
        url = _data_url(send_path, _mime(send_path, video))
    except Exception as exc:
        if drop:
            drop()
        log.error("vision: could not prepare %s for analysis: %s — "
                  "publishing without one", os.path.basename(path), exc)
        return {}

    prompt = _USER
    try:
        resp = _client.chat.completions.create(
            model=model or config.IG_VISION_MODEL,
            max_tokens=MAX_TOKENS,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": part_kind, part_kind: {"url": url}},
                ]},
            ],
        )
    # Broader than an `except APIError`: a gateway fails outside it too (DNS,
    # TLS, a 502 HTML body), and this function's whole job is that none of it
    # reaches the publish loop.
    except Exception as exc:
        log.error("vision: analysis of %s failed: %s — publishing without one",
                  os.path.basename(path), exc)
        return {}
    finally:
        if drop:
            drop()

    out = _parse(resp.choices[0].message.content or "")
    if not out:
        log.warning("vision: analysis of %s came back unusable — publishing "
                    "without one", os.path.basename(path))
        return {}
    log.info("vision: %s — %s", os.path.basename(path), as_prompt(out)[:200])
    return out


def as_prompt(footage: dict) -> str:
    """The notes as the one MEDIA NOTES line the caption prompt carries:
    "MEDIA NOTES: on-screen text: ...; speech: ...; language: ...; setting:
    ...". An empty field is left out rather than printed as "" — the model
    reads a field that is there as a fact. "" for no notes.

    Kept here rather than in caption.py because the shape of the dict is this
    module's business. A dict from before 2026-10-18 (a scheduled post that
    stored `summary`/`beats`) has none of these keys and gives "" — the old
    narration is deliberately not passed on.
    """
    if not footage:
        return ""
    parts = [f"{label}: {footage[key].strip()}"
             for key, label in (("on_screen_text", "on-screen text"),
                                ("speech", "speech"),
                                ("language", "language"),
                                ("setting", "setting"),
                                ("people_count", "people"))
             if isinstance(footage.get(key), str) and footage[key].strip()]
    return "MEDIA NOTES: " + "; ".join(parts) if parts else ""


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Describe a video or photo the way the caption pipeline "
                    "sees it.")
    parser.add_argument("path")
    parser.add_argument("--headline", default="",
                        help="ignored — kept so old invocations still run")
    parser.add_argument("--model", default="")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    out = describe(args.path, args.headline, args.model or None)
    if not out:
        print("no analysis (see the log above)")
        return 1
    print(json.dumps(out, indent=2, ensure_ascii=False))
    print("\n--- as the caption prompt sees it ---\n")
    print(as_prompt(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
