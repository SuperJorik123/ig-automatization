"""
scripts/caption_trace.py — run the whole caption pipeline on real clips and
put every brand's output side by side in ONE HTML report, for the operator to
review before the design goes live.

Per clip: download (shared/reel_downloader.fetch — the source post comes with
it) or take a local file, the media notes (shared/vision), one branded render
through the same treatment the bot uses (shared/branding) with a frame saved
from it, then every Instagram brand's caption (modules/instagram/caption.
captions_for: one expansion, the per-brand rewrites, ONE hashtag measurement).
Each caption is checked against the acceptance criteria of the 2026-10-18
redesign, and the hashtag log (count + why each tag was or wasn't picked) is
captured per clip.

Costs real money: OpenRouter (vision ~1¢/min of clip, the captions well under
a cent each) and Apify (~$0.002 per uncached tag).

    py scripts/caption_trace.py --out trace/ \\
        "https://www.reddit.com/r/.../comments/..." \\
        "posts/005.mp4::Teenager saves friend from electrocution::info text"

An item is SOURCE[::HEADLINE[::INFO]]. SOURCE is a URL or a local file. With
no HEADLINE a URL's own post text stands in (its first line), as it does in
the bot when the operator types nothing.
"""

import argparse
import base64
import datetime
import html
import json
import logging
import os
import re
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shared import branding, config, reel_downloader, vision  # noqa: E402
from modules.instagram import caption  # noqa: E402

log = logging.getLogger("caption_trace")

# The brand whose banner the sample render carries.
RENDER_BRAND = "wswire"

# Acceptance checks. Words that mean the caption is describing a video, or
# asking for an action Instagram treats as engagement bait.
_VIDEO_TALK = re.compile(r"\b(the video shows|footage|is seen|are seen|the clip "
                         r"ends|video ends|caught on (?:camera|video) shows)\b",
                         re.IGNORECASE)
_CTA = re.compile(r"\b(share this|send this|save this|follow (?:us|for)|"
                  r"comment below|tag a friend|like if|drop a comment)\b",
                  re.IGNORECASE)
_COLOURS = re.compile(r"\b(?:in (?:a |the )?|wearing (?:a |the )?)(?:red|blue|"
                      r"green|black|white|yellow|grey|gray|orange|purple|pink|"
                      r"brown)\b", re.IGNORECASE)
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
_LATIN = re.compile(r"[A-Za-z]")


class _Capture(logging.Handler):
    """Collects the hashtag selection log for one clip."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.lines = []

    def emit(self, record):
        msg = record.getMessage()
        if msg.startswith("hashtag") or "measurement pass" in msg \
                or "unavailable" in msg:
            self.lines.append(msg)


def _brands() -> list:
    """Every brand that gets an Instagram caption: those with a tone line."""
    out = []
    for b in config.BRANDS:
        style = branding.load_writing_style(config.brand_dir(b["name"]))
        if style:
            out.append({"name": b["name"], "lang": b.get("lang", ""),
                        "style": style})
    return out


def _probe(path: str) -> str:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=60).stdout.strip()
        return out.replace(",", "×")
    except Exception as exc:
        return f"? ({exc})"


def _frame(video: str, png: str, at: float = 2.0) -> str:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(at), "-i", video,
                    "-frames:v", "1", "-vf", "scale=540:-2", "-q:v", "3", png],
                   check=True, capture_output=True)
    return png


def _parse_item(raw: str) -> dict:
    parts = raw.split("::")
    return {"src": parts[0].strip(),
            "headline": parts[1].strip() if len(parts) > 1 else "",
            "info": parts[2].strip() if len(parts) > 2 else ""}


def check(text: str, lang: str) -> list:
    """(label, ok, detail) per acceptance criterion for one finished caption."""
    prose, tags = caption.split_tags(text)
    paras = [p for p in prose.split("\n\n") if p.strip()]
    hook = paras[0] if paras else ""
    question = next((p for p in paras[1:] if p.rstrip().endswith("?")), "")
    story_tags = tags[1:] if tags else []
    out = [
        ("hook ≤ 90 chars", len(hook) <= 90, f"{len(hook)}"),
        ("150-400 chars before tags", 150 <= len(prose) <= 400, f"{len(prose)}"),
        ("question present", bool(question), question[:60] or "none"),
        ("no video talk", not _VIDEO_TALK.search(text),
         (_VIDEO_TALK.search(text) or [""])[0]),
        ("no call to action", not _CTA.search(text),
         (_CTA.search(text) or [""])[0]),
        ("no clothing colour", not _COLOURS.search(text),
         (_COLOURS.search(text) or [""])[0]),
        ("3-4 story tags + brand tag", 3 <= len(story_tags) <= 4,
         f"{len(story_tags)}"),
    ]
    if not caption.is_base_lang(lang):
        cyr, lat = len(_CYRILLIC.findall(prose)), len(_LATIN.findall(prose))
        out.append((f"written in {caption.lang_name(lang)}",
                    cyr > 5 * max(lat, 1) if lang == "ru" else True,
                    f"cyrillic {cyr} / latin {lat}"))
    return out


def run_item(item: dict, out_dir: str, brands: list, n: int) -> dict:
    stub = os.path.join(out_dir, f"clip{n}")
    res = {"item": item, "error": "", "source": {}, "headline": item["headline"]}
    if re.match(r"https?://", item["src"]):
        paths, link_caption, source = reel_downloader.fetch(item["src"],
                                                            stub + ".mp4")
        res["source"] = source
        if not res["headline"]:
            res["headline"] = (link_caption or "").strip().split("\n")[0]
        path = paths[0]
    else:
        path = item["src"]
    res["path"] = path
    res["original_res"] = _probe(path)

    capture = _Capture()
    logging.getLogger().addHandler(capture)
    try:
        res["footage"] = vision.describe(path)
        render = branding.render_branded(
            path, res["headline"], os.path.join(
                config.brand_dir(RENDER_BRAND), "logo.png"),
            stub + "_render.mp4")
        res["render_res"] = _probe(render)
        res["frame"] = _frame(render, stub + "_frame.jpg")
        res["base"] = caption.expand(res["headline"], res["footage"], None,
                                     item["info"], res["source"])
        res["captions"] = caption.captions_for(
            res["headline"], brands, res["footage"], item["info"],
            res["source"], base=res["base"],
            others={b["name"] for b in config.BRANDS})
    finally:
        logging.getLogger().removeHandler(capture)
    res["hashtag_log"] = capture.lines
    return res


def _cross_checks(res: dict) -> list:
    caps = res.get("captions") or {}
    hooks, questions = {}, {}
    for name, text in caps.items():
        paras = [p for p in caption.split_tags(text)[0].split("\n\n") if p]
        hooks[name] = paras[0] if paras else ""
        questions[name] = next((p for p in paras[1:] if p.endswith("?")), "")
    dup_h = len(set(hooks.values())) < len(hooks)
    q = [x for x in questions.values() if x]
    dup_q = len(set(q)) < len(q)
    passes = sum(1 for line in res.get("hashtag_log", ())
                 if "measurement pass" in line)
    return [("every brand has a different hook", not dup_h, ""),
            ("every brand has a different question", not dup_q, ""),
            ("exactly one Apify pass", passes == 1, f"{passes} pass(es)")]


def _img(path: str) -> str:
    with open(path, "rb") as fh:
        return "data:image/jpeg;base64," + base64.b64encode(fh.read()).decode()


def _checks_html(rows) -> str:
    return "<ul class=checks>" + "".join(
        f"<li class={'ok' if ok else 'bad'}>{'✓' if ok else '✗'} "
        f"{html.escape(label)}"
        f"{' — ' + html.escape(str(detail)) if detail else ''}</li>"
        for label, ok, detail in rows) + "</ul>"


def report(results: list, brands: list, path: str) -> None:
    langs = {b["name"]: b["lang"] for b in brands}
    parts = [
        "<!doctype html><meta charset=utf-8><title>Caption trace</title>",
        "<style>body{font:14px/1.45 system-ui,sans-serif;margin:24px;"
        "background:#fafafa;color:#111}h2{margin-top:48px;border-top:2px solid "
        "#ccc;padding-top:16px}.top{display:flex;gap:24px;align-items:flex-start;"
        "flex-wrap:wrap}.top img{width:270px;border:1px solid #ccc}.meta{flex:1;"
        "min-width:320px}pre{white-space:pre-wrap;background:#fff;border:1px "
        "solid #ddd;padding:10px;margin:0}.grid{display:grid;grid-template-"
        "columns:repeat(auto-fill,minmax(320px,1fr));gap:14px;margin-top:16px}"
        ".card{background:#fff;border:1px solid #ddd;padding:10px}.card h3"
        "{margin:0 0 6px}.checks{list-style:none;padding:0;margin:8px 0 0;"
        "font-size:12px}.ok{color:#1a7f37}.bad{color:#c62828;font-weight:600}"
        "details{margin-top:10px}code{font-size:12px}</style>",
        f"<h1>Caption trace — {datetime.date.today()}</h1>",
        f"<p>{len(results)} clip(s) × {len(brands)} brand(s): "
        + ", ".join(f"{b['name']} ({b['lang'] or 'en'})" for b in brands)
        + ".</p>",
    ]
    for n, res in enumerate(results, 1):
        item = res["item"]
        parts.append(f"<h2>{n}. {html.escape(res.get('headline') or item['src'])}</h2>")
        if res.get("error"):
            parts.append(f"<p class=bad>FAILED: {html.escape(res['error'])}</p>")
            continue
        src = res.get("source") or {}
        parts.append("<div class=top>")
        if res.get("frame"):
            parts.append(f"<img src='{_img(res['frame'])}' alt='frame'>")
        parts.append("<div class=meta>")
        parts.append(
            f"<p><b>Source:</b> {html.escape(item['src'])}<br>"
            f"<b>Platform / author:</b> {html.escape(src.get('platform') or 'local file')}"
            f"{html.escape(caption.credit(src))}<br>"
            f"<b>Original:</b> {html.escape(res.get('original_res', ''))} → "
            f"<b>rendered:</b> {html.escape(res.get('render_res', ''))}<br>"
            f"<b>Headline:</b> {html.escape(res.get('headline', ''))}<br>"
            f"<b>Operator info:</b> {html.escape(item['info'] or '(none)')}</p>")
        parts.append("<b>Prompt sources</b><pre>" + html.escape(caption.user_message(
            res["headline"], item["info"], res.get("footage"), src)) + "</pre>")
        parts.append("<b>Base caption</b><pre>"
                     + html.escape(res.get("base") or "(failed)") + "</pre>")
        parts.append("<b>Clip-level checks</b>" + _checks_html(_cross_checks(res)))
        parts.append("</div></div><div class=grid>")
        for name, text in (res.get("captions") or {}).items():
            parts.append(f"<div class=card><h3>{html.escape(name)} "
                         f"<small>({html.escape(langs.get(name) or 'en')})</small></h3>"
                         f"<pre>{html.escape(text)}</pre>"
                         + _checks_html(check(text, langs.get(name, "")))
                         + "</div>")
        parts.append("</div><details><summary>Hashtag selection log</summary><pre>"
                     + html.escape("\n".join(res.get("hashtag_log") or ["(none)"]))
                     + "</pre></details>")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("items", nargs="+", help="SOURCE[::HEADLINE[::INFO]]")
    ap.add_argument("--out", default="trace", help="output directory")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    os.makedirs(args.out, exist_ok=True)
    brands = _brands()
    results = []
    for n, raw in enumerate(args.items, 1):
        item = _parse_item(raw)
        log.info("clip %d: %s", n, item["src"])
        try:
            results.append(run_item(item, args.out, brands, n))
        except Exception as exc:
            log.exception("clip %d failed", n)
            results.append({"item": item, "error": str(exc)[:500],
                            "headline": item["headline"]})
    path = os.path.join(args.out, "caption_trace.html")
    report(results, brands, path)
    with open(os.path.join(args.out, "caption_trace.json"), "w",
              encoding="utf-8") as fh:
        json.dump([{k: v for k, v in r.items() if k != "frame"}
                   for r in results], fh, ensure_ascii=False, indent=2,
                  default=str)
    print(os.path.abspath(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
