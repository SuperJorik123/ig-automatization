"""
shared/vision.py — what the media actually shows, before anything is written
about it.

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
    "You are shown one video or photo that a news account is about to "
    "publish, together with the headline its operator typed for it. Report "
    "what the media ACTUALLY SHOWS. Somebody downstream writes the caption "
    "from your report and cannot see the media themselves, so your report is "
    "the only thing standing between the footage and a caption about a "
    "different event.\n\n"
    "OBSERVE, DO NOT IDENTIFY. Describe what is visible and audible. Do not "
    "name a person unless the media itself names them — a caption on screen, "
    "a chyron, a jersey, somebody addressing them by name. Never guess an "
    "identity from resemblance, and never assume the headline's people are "
    "the people on screen: checking exactly that is why you are here. The "
    "same goes for place and date — report the city only if a sign, a plate, "
    "a uniform or the speech says so, and say what is inferable as inferable "
    "(\"an English-speaking country\", \"a residential street\").\n\n"
    "LISTEN. The audio is half the story: shouting, warnings, what bystanders "
    "say, a commentator, an announcement, a language you can identify. A "
    "caption that says people were shouting can only come from you.\n\n"
    "SEQUENCE. Give the beats in the order they happen, as separate short "
    "sentences — what leads up to it, what happens, how it ends. That order is "
    "what the caption is written from, so a report that only says what the "
    "scene contains is a failed report.\n\n"
    "CHECK THE HEADLINE. Set headline_ok to false when the media plainly "
    "contradicts the headline, shows something materially different, or shows "
    "nothing that supports it. Say why in headline_note and give a better one "
    "in headline_suggestion — a plain factual headline for what you actually "
    "saw, under about 90 characters, no clickbait and no exclamation marks. "
    "When the headline is a fair description of the media, set headline_ok "
    "true and leave the other two empty. A headline that is merely shorter "
    "or plainer than what you saw is still fair.\n\n"
    "Reply with ONE JSON object and nothing else — no prose before it, no "
    "explanation after it, no markdown fence:\n"
    "{\n"
    '  "summary": "one sentence: what happens in this media",\n'
    '  "beats": ["what happens first", "then this", "how it ends"],\n'
    '  "audible": "speech, shouting, commentary, or \\"\\" if there is none",\n'
    '  "setting": "where and when it appears to be, or \\"\\"",\n'
    '  "headline_ok": true,\n'
    '  "headline_note": "",\n'
    '  "headline_suggestion": ""\n'
    "}"
)

_USER_WITH_HEADLINE = (
    "The operator's headline for this post:\n\n{headline}\n\n"
    "Report what the media shows, and whether that headline matches it."
)
_USER_NO_HEADLINE = (
    "No headline has been written for this post yet. Report what the media "
    "shows; leave headline_ok true and the two headline fields empty."
)

# A model told to emit bare JSON still wraps it in a fence or a sentence often
# enough to be worth recovering rather than losing the analysis over.
_FENCE = re.compile(r"```[a-zA-Z]*\s*(.*?)\s*```", re.DOTALL)

# Every key the callers read, with what an absent one means. headline_ok
# defaults to True: no complaint is not a complaint, and a warning raised on a
# perfectly good headline trains the operator to ignore the warning.
_SHAPE = {
    "summary": "",
    "beats": [],
    "audible": "",
    "setting": "",
    "headline_ok": True,
    "headline_note": "",
    "headline_suggestion": "",
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
_PASSES = ((480, 30), (360, 34))


def _analysis_copy(path: str):
    """A small copy of `path` for analysis: (copy_path, cleanup).

    Scaled to `short edge` pixels, low-bitrate H.264, 64 kbps MONO audio KEPT —
    the shouting in the clip is half of what the caption is written from — and
    trimmed to the first IG_VISION_MAX_S seconds.

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
            proc = subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-i", path,
                 "-t", str(int(config.IG_VISION_MAX_S)),
                 # -2 keeps the long edge even (x264 needs it) whatever the
                 # source aspect is, and `min(iw,edge)` never upscales a clip
                 # that is already smaller than the target.
                 "-vf", f"scale='min(iw,{edge})':-2",
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
    forces every value to the type its reader expects: a model that answers
    `"beats": "a then b"` must not reach a caller iterating it.
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
    for key, default in _SHAPE.items():
        value = data.get(key, default)
        if key == "beats":
            out[key] = [str(b).strip() for b in value
                        if isinstance(value, list) and str(b).strip()]
        elif key == "headline_ok":
            # Anything that isn't literally true is treated as a complaint
            # EXCEPT an absent key, which _SHAPE already defaulted to True.
            out[key] = value is True if key in data else True
        else:
            out[key] = value.strip() if isinstance(value, str) else ""

    # A summary is the one field every caller reads; without it there is
    # nothing to put in front of the operator or into the caption prompt.
    if not out["summary"]:
        return {}
    # A suggestion beside an approving verdict is not a complaint — the picker
    # branches on headline_ok, so don't let a stray field raise a warning on a
    # headline the model just approved.
    if out["headline_ok"]:
        out["headline_note"] = ""
        out["headline_suggestion"] = ""
    return out


def describe(path: str, headline: str = "", model: str | None = None) -> dict:
    """What `path` shows, as a dict — or {} when that can't be established.

    `headline` is what the operator typed; it is sent along so the model can
    say whether the media matches it, and may be empty (a post can reach the
    gate before a headline is written).

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

    headline = (headline or "").strip()
    prompt = (_USER_WITH_HEADLINE.format(headline=headline) if headline
              else _USER_NO_HEADLINE)
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
    log.info("vision: %s — %s", os.path.basename(path), out["summary"][:160])
    return out


def as_prompt(footage: dict) -> str:
    """The analysis as the block that goes into the caption prompt.

    Kept here rather than in caption.py because the shape of the dict is this
    module's business: a key added here reaches the caption without the caption
    module having to know it exists.
    """
    if not footage:
        return ""
    lines = ["WHAT THE MEDIA SHOWS: " + footage["summary"]]
    if footage.get("beats"):
        lines.append("IN ORDER:")
        lines += [f"  - {b}" for b in footage["beats"]]
    if footage.get("audible"):
        lines.append("AUDIBLE: " + footage["audible"])
    if footage.get("setting"):
        lines.append("SETTING: " + footage["setting"])
    return "\n".join(lines)


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Describe a video or photo the way the caption pipeline "
                    "sees it.")
    parser.add_argument("path")
    parser.add_argument("--headline", default="",
                        help="check this headline against the media")
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
