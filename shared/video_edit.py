"""
shared/video_edit.py — the operator's `edit:` reply: a hook, a crop, cuts.

One reply line, comma- or semicolon-separated, any order:

    edit: 23-26, crop bottom 10%, cut end 2

* ``23-26`` (or ``hook 23-26``) — the HOOK: those seconds are copied to the
  front, then the clip plays from its start as usual (so they play twice).
* ``crop bottom 10%`` / ``crop top 8% left 5%`` — cut that strip off the
  PICTURE for the whole clip (a source's banner, a watermark). Percent of the
  frame as the viewer sees it, rotation applied.
* ``cut start 2`` / ``cut end 10%`` / ``cut 40-45`` — remove TIME: the first
  or last seconds (or percent of the clip), or a stretch from the middle.

Times are ``23``, ``23.5``, ``0:23`` or ``1:02:03``, always on the ORIGINAL
clip — what the operator reads off their phone — so a hook may even come from
a stretch that is cut. ``edit:`` alone takes the edit back off.

Three steps, kept apart so the first two are offline-testable:
``parse(text) -> spec`` (JSON-safe dict, no clip needed), ``resolve(spec,
duration) -> plan`` (concrete segments, every limit checked) and
``render(src, spec, out)`` — one ffmpeg pass that runs BEFORE branding, so the
branded renders, bundles and the publish step only ever see a different input
file. ``parse``/``resolve`` raise ``ValueError`` with a message meant for the
operator; ``render`` raises ``RuntimeError`` on ffmpeg failure.

Each segment is its own ``-ss/-t`` input rather than ``trim`` on one input:
concat drains its inputs in order, so with one shared decoder every frame
before the hook's end would queue up raw in memory (≈2 GB for 23 s of 1080p —
the VPS has 2). Input-side seeking with a re-encode is still frame-accurate.
"""

import hashlib
import json
import logging
import os
import re
import subprocess
import sys

# Repo root on sys.path so `from shared import …` resolves when run directly.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shared import renderlock  # noqa: E402

log = logging.getLogger(__name__)

HOOK_MIN_S = 0.5      # shorter isn't a hook, it's a flicker
HOOK_MAX_S = 10.0     # a typo like 23-260 must not prepend four minutes
MIN_KEEP_S = 1.0      # what the cuts must leave of the clip
MAX_CROP_SIDE = 0.40  # per side
MAX_CROP_AXIS = 0.60  # top+bottom, left+right: at least 40 % of the frame stays
MAX_CUTS = 4          # middle cuts — each one is another input/segment
FADE_S = 0.03         # audio fade at every join, so a jump cut doesn't click
# An intermediate: branding re-encodes it once more, so it is kept visually
# lossless (CRF 12) rather than paying a second lossy generation before
# Instagram's own re-encode — a bigger file, but it lives minutes.
CRF = 12

SIDES = ("top", "bottom", "left", "right")

SYNTAX = ("edit: 23-26 (hook — those seconds first, then the whole clip), "
          "crop bottom 10% (also top/left/right), "
          "cut start 2 / cut end 10% / cut 40-45")

_TIME = r"\d+(?::\d{1,2}){0,2}(?:[.,]\d+)?"
_RANGE_RE = re.compile(rf"^({_TIME})\s*(?:-|–|—|to)\s*({_TIME})$")
_AMOUNT_RE = re.compile(rf"^({_TIME})\s*(%|s|sec|secs|seconds?)?$")


def _seconds(token: str) -> float:
    token = token.replace(",", ".")
    total = 0.0
    for part in token.split(":"):
        total = total * 60 + float(part)
    return total


def _amount(token: str, what: str) -> dict:
    """``2`` / ``2s`` → seconds, ``10%`` → percent of the clip."""
    m = _AMOUNT_RE.match(token.strip())
    if not m:
        raise ValueError(f"couldn't read {token!r} as {what} — e.g. 2, 0:02 or 10%")
    if m.group(2) == "%":
        return {"pct": float(m.group(1).replace(",", "."))}
    return {"s": _seconds(m.group(1))}


def _range(token: str):
    m = _RANGE_RE.match(token.strip())
    if not m:
        return None
    a, b = _seconds(m.group(1)), _seconds(m.group(2))
    if b <= a:
        raise ValueError(f"{token!r}: the end must come after the start")
    return [a, b]


def _parse_crop(rest: str, crop: dict) -> None:
    words = rest.replace("%", "% ").split()
    if not words:
        raise ValueError("crop what? e.g. crop bottom 10%")
    i = 0
    while i < len(words):
        side = words[i].lower()
        if side not in SIDES:
            raise ValueError(f"crop: {words[i]!r} isn't a side — "
                             "use top, bottom, left or right")
        if i + 1 >= len(words):
            raise ValueError(f"crop {side}: how much? e.g. crop {side} 10%")
        amount = words[i + 1]
        if amount != "%" and i + 2 < len(words) and words[i + 2] == "%":
            amount, i = amount + "%", i + 1
        if not amount.endswith("%"):
            raise ValueError(f"crop {side}: give it in percent, e.g. "
                             f"crop {side} 10%")
        crop[side] = float(amount[:-1].replace(",", ".")) / 100
        i += 2


def _parse_cut(rest: str, spec: dict) -> None:
    rest = rest.strip()
    head, _, tail = rest.partition(" ")
    where = head.lower()
    if where in ("start", "beginning", "begin", "first"):
        spec["cut_start"] = _amount(tail, "the length to cut from the start")
    elif where in ("end", "last"):
        spec["cut_end"] = _amount(tail, "the length to cut from the end")
    else:
        rng = _range(rest)
        if rng is None:
            raise ValueError(f"cut {rest!r}: use cut start 2, cut end 10% or "
                             "cut 40-45")
        spec["cuts"].append(rng)


def parse(text: str) -> dict:
    """The text after ``edit:`` as a spec. Empty text → ``{}`` (no edit).
    Raises ValueError naming the clause it couldn't read."""
    spec = {"hook": None, "crop": {}, "cut_start": None, "cut_end": None,
            "cuts": []}
    clauses = [c.strip() for c in re.split(r"[,;\n]+", text or "") if c.strip()]
    if not clauses:
        return {}
    for clause in clauses:
        word, _, rest = clause.partition(" ")
        word = word.lower()
        if word == "crop":
            _parse_crop(rest, spec["crop"])
        elif word in ("cut", "trim"):
            _parse_cut(rest, spec)
        else:
            body = rest if word == "hook" else clause
            rng = _range(body)
            if rng is None:
                raise ValueError(f"couldn't read {clause!r} — " + SYNTAX)
            if spec["hook"] is not None:
                raise ValueError("only one hook per clip")
            spec["hook"] = rng
    if len(spec["cuts"]) > MAX_CUTS:
        raise ValueError(f"at most {MAX_CUTS} middle cuts")
    _check_crop(spec["crop"])
    if spec["hook"]:
        length = spec["hook"][1] - spec["hook"][0]
        if not HOOK_MIN_S <= length <= HOOK_MAX_S:
            raise ValueError(f"the hook is {length:g}s — keep it between "
                             f"{HOOK_MIN_S:g} and {HOOK_MAX_S:g}s")
    return spec


def _check_crop(crop: dict) -> None:
    for side, frac in crop.items():
        if not 0 < frac <= MAX_CROP_SIDE:
            raise ValueError(f"crop {side}: between 1% and "
                             f"{MAX_CROP_SIDE:.0%} per side")
    for a, b in (("top", "bottom"), ("left", "right")):
        if crop.get(a, 0) + crop.get(b, 0) > MAX_CROP_AXIS:
            raise ValueError(f"crop {a}+{b} would leave less than "
                             f"{1 - MAX_CROP_AXIS:.0%} of the picture")


def _length(amount, duration: float) -> float:
    if not amount:
        return 0.0
    if "pct" in amount:
        return duration * amount["pct"] / 100
    return amount["s"]


def resolve(spec: dict, duration: float) -> dict:
    """``spec`` against a clip of ``duration`` seconds: ``{"segments":
    [[start, end], …], "crop": {…}, "duration": total}`` — segments in play
    order on the ORIGINAL clip, the hook first. Raises ValueError when the
    edit doesn't fit this clip."""
    if duration <= 0:
        raise ValueError("couldn't read the clip's length")
    start = _length(spec.get("cut_start"), duration)
    end = duration - _length(spec.get("cut_end"), duration)
    if end - start < MIN_KEEP_S:
        raise ValueError(f"the cuts leave {max(end - start, 0):.1f}s of a "
                         f"{duration:.1f}s clip")

    keep = [[start, end]]
    for a, b in sorted(spec.get("cuts") or []):
        if a >= duration:
            raise ValueError(f"cut {_clock(a)}-{_clock(b)}: the clip is only "
                             f"{_clock(duration)} long")
        nxt = []
        for s, e in keep:
            if b <= s or a >= e:
                nxt.append([s, e])
                continue
            if a > s:
                nxt.append([s, a])
            if b < e:
                nxt.append([b, e])
        keep = nxt
    keep = [[s, e] for s, e in keep if e - s >= 0.05]
    if sum(e - s for s, e in keep) < MIN_KEEP_S:
        raise ValueError(f"the cuts leave less than {MIN_KEEP_S:g}s of the clip")

    segments = []
    hook = spec.get("hook")
    if hook:
        a, b = hook
        if a >= duration - HOOK_MIN_S:
            raise ValueError(f"hook {_clock(a)}-{_clock(b)}: the clip is only "
                             f"{_clock(duration)} long")
        segments.append([a, min(b, duration)])
    segments += keep
    return {"segments": [[round(s, 3), round(e, 3)] for s, e in segments],
            "crop": dict(spec.get("crop") or {}),
            "duration": round(sum(e - s for s, e in segments), 3)}


def is_noop(spec: dict) -> bool:
    return not spec or not (spec.get("hook") or spec.get("crop")
                            or spec.get("cut_start") or spec.get("cut_end")
                            or spec.get("cuts"))


def _clock(s: float) -> str:
    s = max(s, 0)
    whole = int(s)
    frac = s - whole
    text = f"{whole // 60}:{whole % 60:02d}"
    return text + (f"{frac:.1f}"[1:] if frac >= 0.05 else "")


def _amount_text(amount) -> str:
    return f"{amount['pct']:g}%" if "pct" in amount else f"{amount['s']:g}s"


def describe(spec: dict, duration: float | None = None) -> str:
    """The ✂️ line on the plan card. With the clip's length it adds the result
    (or what is wrong with the edit for this clip)."""
    if is_noop(spec):
        return ""
    parts = []
    if spec.get("hook"):
        a, b = spec["hook"]
        parts.append(f"hook {_clock(a)}–{_clock(b)} ({b - a:g}s) first")
    if spec.get("crop"):
        parts.append("crop " + " ".join(
            f"{side} {spec['crop'][side]:.0%}" for side in SIDES
            if side in spec["crop"]))
    if spec.get("cut_start"):
        parts.append("cut start " + _amount_text(spec["cut_start"]))
    if spec.get("cut_end"):
        parts.append("cut end " + _amount_text(spec["cut_end"]))
    for a, b in spec.get("cuts") or ():
        parts.append(f"cut {_clock(a)}–{_clock(b)}")
    line = "✂️ edit: " + " · ".join(parts)
    if duration:
        try:
            plan = resolve(spec, duration)
            line += f" → {_clock(plan['duration'])} (was {_clock(duration)})"
        except ValueError as exc:
            line += f"\n⚠️ {exc}"
    return line


def key(spec: dict) -> str:
    """Stable id of an edit — the rendered copy is reused while it holds."""
    return hashlib.sha1(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:10]


def _has_audio(path: str) -> bool:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=index", "-of", "csv=p=0", path],
        capture_output=True, text=True)
    return proc.returncode == 0 and bool(proc.stdout.strip())


def _crop_filter(crop: dict) -> str:
    if not crop:
        return ""
    t, b = crop.get("top", 0), crop.get("bottom", 0)
    l, r = crop.get("left", 0), crop.get("right", 0)
    # Even width/height/offsets: yuv420p can't carry an odd dimension.
    return (f",crop=w=trunc(iw*{1 - l - r:.4f}/2)*2:h=trunc(ih*{1 - t - b:.4f}/2)*2"
            f":x=trunc(iw*{l:.4f}/2)*2:y=trunc(ih*{t:.4f}/2)*2")


def build_command(src: str, plan: dict, out: str, audio: bool) -> list:
    """The ffmpeg argv for a resolved plan (pure — tests check it)."""
    segs = plan["segments"]
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for s, e in segs:
        cmd += ["-ss", f"{s:.3f}", "-t", f"{e - s:.3f}", "-i", src]
    graph, pads = [], []
    for i, (s, e) in enumerate(segs):
        graph.append(f"[{i}:v:0]setpts=PTS-STARTPTS,setsar=1[v{i}]")
        pads.append(f"[v{i}]")
        if audio:
            length = e - s
            fades = []
            if i > 0:
                fades.append(f"afade=t=in:d={FADE_S}")
            if i < len(segs) - 1 and length > 4 * FADE_S:
                fades.append(f"afade=t=out:st={length - FADE_S:.3f}:d={FADE_S}")
            # A fixed sample format/rate/layout: concat refuses mismatched
            # audio, and a mono/stereo flip mid-file is not unheard of.
            chain = ",".join(["asetpts=PTS-STARTPTS",
                              "aresample=48000,aformat=sample_fmts=fltp:"
                              "channel_layouts=stereo", *fades])
            graph.append(f"[{i}:a:0]{chain}[a{i}]")
            pads.append(f"[a{i}]")
    graph.append("".join(pads) + f"concat=n={len(segs)}:v=1:a={int(audio)}"
                 + ("[vc][a]" if audio else "[vc]"))
    graph.append("[vc]" + (_crop_filter(plan["crop"])[1:] or "null") + "[v]")
    cmd += ["-filter_complex", ";".join(graph), "-map", "[v]"]
    if audio:
        cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(CRF),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]
    return cmd


def render(src: str, spec: dict, out: str, duration: float) -> str:
    """Write the edited clip to ``out`` and return it. Blocking (callers use
    ``asyncio.to_thread``). ValueError for an edit that doesn't fit the clip,
    RuntimeError when ffmpeg fails (the partial output is removed)."""
    plan = resolve(spec, duration)
    cmd = build_command(src, plan, out, _has_audio(src))
    log.info("editing %s: %d segment(s), crop %s → %.1fs", src,
             len(plan["segments"]), plan["crop"] or "none", plan["duration"])
    with renderlock.render_slot():
        proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.exists(out):
        try:
            os.remove(out)
        except OSError:
            pass
        raise RuntimeError("video edit failed: "
                           + (proc.stderr.strip()[-300:] or "ffmpeg error"))
    return out


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Apply an edit: line to a clip.")
    ap.add_argument("src")
    ap.add_argument("edit", help='e.g. "23-26, crop bottom 10%%, cut end 2"')
    ap.add_argument("-o", "--out", default=None)
    args = ap.parse_args()
    from modules.youtube import shorts_format
    spec = parse(args.edit)
    _, _, duration = shorts_format.probe(args.src)
    print(describe(spec, duration))
    out = args.out or os.path.splitext(args.src)[0] + "_edit.mp4"
    render(args.src, spec, out, duration)
    print(out)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
