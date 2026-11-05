"""
find_original.py — trace a reposted clip back to its original upload.

Reverse-image-searches frames of the clip on Yandex Images, then Google
Lens, through a real Chrome window driven by Selenium (no paid API), and
ranks every page the engines matched by how many frames pointed at it.
With --probe, the top video-platform hits are asked (yt-dlp, no download)
for resolution / upload date / uploader, so the earliest, sharpest copy
is obvious.

    py tools/find_original/find_original.py https://www.reddit.com/r/.../s/abc
    py tools/find_original/find_original.py clip.mp4 --frames 6 --probe 8
    py tools/find_original/find_original.py clip.mp4 --engines yandex

The Chrome profile is kept in tools/find_original/chrome-profile/ so a
consent screen or CAPTCHA solved once stays solved. When an engine shows
a CAPTCHA the script waits (up to --captcha-wait seconds) for you to solve
it in the window — another reason it isn't headless by default.

Output lands in tools/find_original/runs/<stamp>/: the clip, the frames
and report.json.
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlparse, urlunparse, parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(HERE, "chrome-profile")
RUNS_DIR = os.path.join(HERE, "runs")

# Where an original clip actually lives. Anything else is a blog / news
# page that embedded it — still a lead (the embed often names the source),
# just ranked below these.
VIDEO_HOSTS = {
    "x.com": "x", "twitter.com": "x",
    "tiktok.com": "tiktok",
    "instagram.com": "instagram",
    "youtube.com": "youtube", "youtu.be": "youtube",
    "facebook.com": "facebook", "fb.watch": "facebook",
    "reddit.com": "reddit", "v.redd.it": "reddit",
    "t.me": "telegram",
    "vk.com": "vk", "vkvideo.ru": "vk",
    "dailymotion.com": "dailymotion",
    "rumble.com": "rumble",
    "threads.net": "threads", "threads.com": "threads",
}

# The engines' own pages, CDNs and trackers — never a result.
NOISE = re.compile(
    r"(^|\.)(yandex\.\w+|ya\.ru|yastatic\.net|google\.\w+|googleusercontent\.com"
    r"|gstatic\.com|googleapis\.com|googleadservices\.com|doubleclick\.net|telesco\.pe"
    r"|schema\.org|w3\.org|support\.google\.com|accounts\.google\.com)$"
)

CAPTCHA_HINTS = ("showcaptcha", "/sorry/", "captcha")


# --------------------------------------------------------------------- media

def fetch_clip(url: str, run_dir: str) -> tuple[str, dict]:
    """Download the clip behind a post URL via the repo's downloader."""
    from shared import reel_downloader
    paths, _caption, source = reel_downloader.fetch(url, os.path.join(run_dir, "clip.mp4"))
    videos = [p for p in paths if p.lower().endswith((".mp4", ".mov", ".webm", ".mkv"))]
    return (videos or paths)[0], source or {}


def probe(path: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration,bit_rate",
         "-of", "json", path],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(out)
    stream = (data.get("streams") or [{}])[0]
    fmt = data.get("format") or {}
    return {
        "width": stream.get("width"), "height": stream.get("height"),
        "duration": float(fmt.get("duration") or 0),
        "kbps": int(float(fmt.get("bit_rate") or 0) / 1000),
    }


def extract_frames(path: str, n: int, out_dir: str) -> list[str]:
    """n frames spread evenly over the clip, skipping the first/last 5 %
    (intros, end cards and repost watermarks cluster there)."""
    info = probe(path)
    dur = info["duration"]
    if path.lower().endswith((".jpg", ".jpeg", ".png", ".webp")) or dur <= 0:
        return [path]
    start, span = dur * 0.05, dur * 0.90
    frames = []
    for i in range(n):
        t = start + span * (i + 0.5) / n
        dst = os.path.join(out_dir, f"frame_{i + 1:02d}_{t:06.1f}s.jpg")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", path,
             "-frames:v", "1", "-q:v", "2", dst],
            check=True,
        )
        if os.path.exists(dst):
            frames.append(dst)
    return frames


# ------------------------------------------------------------------- browser

def make_driver(headless: bool) -> webdriver.Chrome:
    opts = webdriver.ChromeOptions()
    opts.add_argument(f"--user-data-dir={PROFILE_DIR}")
    opts.add_argument("--window-size=1400,1000")
    opts.add_argument("--lang=en-US")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    if headless:
        opts.add_argument("--headless=new")
    # Don't sit through every result page's ads and videos: the DOM is enough.
    opts.page_load_strategy = "eager"
    driver = webdriver.Chrome(options=opts)  # Selenium Manager fetches the driver
    driver.set_page_load_timeout(30)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"},
    )
    return driver


def wait_for(cond, timeout: float, step: float = 0.5):
    end = time.time() + timeout
    while time.time() < end:
        try:
            got = cond()
        except WebDriverException:
            got = None
        if got:
            return got
        time.sleep(step)
    return None


def on_captcha(driver) -> bool:
    url = driver.current_url.lower()
    if any(h in url for h in CAPTCHA_HINTS):
        return True
    try:
        return bool(driver.find_elements(By.CSS_SELECTOR, "iframe[src*='recaptcha'], .CheckboxCaptcha, .AdvancedCaptcha"))
    except WebDriverException:
        return False


def handle_captcha(driver, engine: str, wait_s: int) -> bool:
    """True once the page is past any CAPTCHA (or never had one)."""
    if not on_captcha(driver):
        return True
    print(f"    ! {engine} shows a CAPTCHA — solve it in the Chrome window "
          f"(waiting up to {wait_s}s)…", flush=True)
    return bool(wait_for(lambda: not on_captcha(driver), wait_s, 2))


def accept_consent(driver) -> None:
    """Click through Google's / Yandex's cookie wall when it shows."""
    words = ("accept all", "i agree", "agree", "accept", "allow all",
             "acceptă tot", "accepta tot", "принять все", "принять", "alles akzeptieren")
    for el in driver.find_elements(By.CSS_SELECTOR, "button, div[role=button], input[type=submit]"):
        try:
            label = (el.text or el.get_attribute("value") or el.get_attribute("aria-label") or "").strip().lower()
            if label and label in words and el.is_displayed():
                el.click()
                time.sleep(1.5)
                return
        except WebDriverException:
            continue


def find_file_input(driver):
    els = driver.find_elements(By.CSS_SELECTOR, "input[type=file]")
    return els[0] if els else None


def upload(driver, path: str):
    """Send a file to the page's (often hidden) file input."""
    inp = find_file_input(driver)
    if inp is None:
        return False
    driver.execute_script(
        "arguments[0].style.display='block';arguments[0].style.visibility='visible';"
        "arguments[0].removeAttribute('hidden');", inp)
    inp.send_keys(os.path.abspath(path))
    return True


def harvest_links(driver, scrolls: int = 4) -> list[dict]:
    """Every outbound link on the results page, with its visible title."""
    for _ in range(scrolls):
        driver.execute_script("window.scrollBy(0, document.body.scrollHeight)")
        time.sleep(1.2)
    rows = driver.execute_script("""
        const out = [];
        for (const a of document.querySelectorAll('a[href]')) {
            const t = (a.innerText || a.getAttribute('aria-label') || a.title || '').trim();
            out.push([a.href, t.slice(0, 200)]);
        }
        return out;
    """) or []
    links, seen = [], set()
    for href, title in rows:
        href = unwrap(href)
        host = (urlparse(href).hostname or "").lower()
        if not host or NOISE.search(host) or not href.startswith("http"):
            continue
        # a thumbnail's own file is not a page anyone posted
        if re.search(r"\.(jpe?g|png|webp|gif)$", urlparse(href).path, re.I) or "videoPreview" in href:
            continue
        if href in seen:
            continue
        seen.add(href)
        links.append({"url": href, "title": title})
    return links


def unwrap(href: str) -> str:
    """Yandex / Google sometimes route results through a redirector."""
    try:
        q = parse_qs(urlparse(href).query)
    except ValueError:
        return href
    for key in ("url", "q", "imgrefurl"):
        v = q.get(key)
        if v and v[0].startswith("http"):
            return v[0]
    return href


# ------------------------------------------------------------------- engines

def search_yandex(driver, frame: str, captcha_wait: int) -> list[dict]:
    driver.get("https://yandex.com/images/")
    handle_captcha(driver, "Yandex", captcha_wait)
    accept_consent(driver)
    if find_file_input(driver) is None:
        # The camera button mounts the upload popup (and its file input).
        for sel in ("button[aria-label*='image' i]", "button[aria-label*='картинк' i]",
                    "[class*='CbirButton']", "[class*='cbir' i] button", ".input__cbir-button"):
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    if el.is_displayed():
                        el.click()
                        break
                except WebDriverException:
                    continue
            if wait_for(lambda: find_file_input(driver), 3):
                break
    if not upload(driver, frame):
        print("    ! Yandex: no upload field found (layout changed?)")
        return []
    if not wait_for(lambda: "cbir_id" in driver.current_url or on_captcha(driver), 30):
        print("    ! Yandex: results never loaded")
        return []
    if not handle_captcha(driver, "Yandex", captcha_wait):
        return []
    # The "Sites with this image" tab is the list of pages carrying it.
    url = driver.current_url
    if "cbir_page=" not in url:
        driver.get(url + ("&" if "?" in url else "?") + "cbir_page=sites")
        time.sleep(3)
        handle_captcha(driver, "Yandex", captcha_wait)
    return harvest_links(driver)


def search_lens(driver, frame: str, captcha_wait: int) -> list[dict]:
    # ?olud opens Google's "Search any image with Google Lens" drop zone.
    driver.get("https://www.google.com/?olud&hl=en")
    accept_consent(driver)
    handle_captcha(driver, "Google", captcha_wait)
    if not wait_for(lambda: find_file_input(driver), 8):
        print("    ! Lens: no upload field found (layout changed?)")
        return []
    # The page carries several file inputs and only the Lens one reacts —
    # try each until the results page opens.
    for inp in driver.find_elements(By.CSS_SELECTOR, "input[type=file]"):
        try:
            inp.send_keys(os.path.abspath(frame))
        except WebDriverException:
            continue
        if wait_for(lambda: "/search" in driver.current_url or on_captcha(driver), 12):
            break
    else:
        print("    ! Lens: results never loaded")
        return []
    if not handle_captcha(driver, "Google", captcha_wait):
        return []
    time.sleep(2)
    # "Exact matches" (udm=48) lists pages carrying this very image — the
    # default tab's grid is mostly look-alikes.
    driver.get(re.sub(r"&udm=\d+", "", driver.current_url) + "&udm=48")
    time.sleep(3)
    if not handle_captcha(driver, "Google", captcha_wait):
        return []
    for _ in range(2):
        driver.execute_script("window.scrollBy(0, document.body.scrollHeight)")
        time.sleep(1.2)
    # Each card is <a href="/goto?url=<opaque>"> — the destination exists
    # nowhere in the page until the redirect runs. The card text carries
    # what we want besides it: title, "10 Oct 2026 · 640x1.136", site.
    cards = driver.execute_script("""
        const out = [], seen = new Set();
        for (const a of document.querySelectorAll('a[href*="/goto?"], a[href^="/url?"]')) {
            if (seen.has(a.href)) continue;
            seen.add(a.href);
            out.push([a.href, (a.innerText || '').trim()]);
        }
        return out;
    """) or []
    if not cards:
        # Tell "Google has no copy" apart from "the page broke": keep a shot.
        shot = os.path.splitext(frame)[0] + "_lens.png"
        driver.save_screenshot(shot)
        print(f"    no exact matches on Lens (screenshot: {os.path.basename(shot)})")
    links = []
    for href, text in cards[:LENS_MAX_RESULTS]:
        dest = resolve_redirect(driver, href)
        if not dest or NOISE.search((urlparse(dest).hostname or "").lower()):
            continue
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        links.append({"url": dest, "title": lines[0] if lines else "",
                      "meta": " · ".join(lines[1:])[:160]})
    return links


LENS_MAX_RESULTS = 15


def resolve_redirect(driver, href: str, timeout: float = 15) -> str:
    """Follow one of Google's redirect links far enough to read where it
    lands (the destination page doesn't have to finish loading)."""
    try:
        driver.get(href)
    except WebDriverException:
        pass  # a slow destination may time out; the URL is already known
    return wait_for(
        lambda: (driver.current_url if "google." not in (urlparse(driver.current_url).hostname or "")
                 else None),
        timeout) or ""


ENGINES = {"yandex": search_yandex, "lens": search_lens}


# ------------------------------------------------------------------- ranking

def platform_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.").removeprefix("m.")
    for dom, name in VIDEO_HOSTS.items():
        if host == dom or host.endswith("." + dom):
            return name
    return ""


def canonical(url: str) -> str:
    """Collapse tracking params so the same page from two frames counts once.
    YouTube keeps ?v= — it IS the page."""
    p = urlparse(url)
    host = (p.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    query = ""
    if "youtube.com" in host and p.path == "/watch":
        v = parse_qs(p.query).get("v")
        query = f"v={v[0]}" if v else ""
    host = {"twitter.com": "x.com", "mobile.twitter.com": "x.com"}.get(host, host)
    return urlunparse(("https", host, p.path.rstrip("/"), "", query, ""))


def is_post(url: str) -> bool:
    """A link to one video/post, not a profile, hashtag or search page."""
    path = urlparse(url).path
    return bool(re.search(
        r"/status/\d+|/video/\d+|/(reel|reels|p|tv)/|/watch|/shorts/|/comments/"
        r"|/videos?/|^/[A-Za-z0-9_]+/\d+$|/posts?/|/v/", path))


def rank(hits: dict) -> list[dict]:
    rows = []
    for url, h in hits.items():
        plat = platform_of(url)
        rows.append({
            "url": url,
            "fetch_url": h.get("raw") or url,
            "platform": plat,
            "is_post": is_post(url),
            "frames": len(h["frames"]),
            "engines": sorted(h["engines"]),
            "title": h["title"],
            "meta": h["meta"],  # Lens card line: date · resolution · site
            "score": len(h["frames"]) * 2 + len(h["engines"]) + (3 if plat else 0)
                     + (2 if plat and is_post(url) else 0),
        })
    rows.sort(key=lambda r: (-r["score"], r["url"]))
    return rows


# ------------------------------------------------------------- clean check
#
# "Original" here means NOTHING ADDED: no logo, handle, caption, sticker,
# blur-fill frame or end card, and no music / sound effect / voice-over laid
# over the recording. Whether it is the first upload doesn't matter — a clean
# repost is as good as the source. Marks that were IN the recording (a CCTV
# timestamp, the camera's own name, signs in the scene) don't count.

CLEAN_SYSTEM = (
    "You inspect a video clip that was downloaded from social media. Decide "
    "whether anything was ADDED to it in editing after it was recorded.\n\n"
    "Added visuals (count these): channel/page logos or watermarks, @handles, "
    "platform watermarks (TikTok, CapCut, Instagram), captions, headlines, "
    "subtitles, emoji, stickers, arrows, circles, zoom/slow-motion effects, "
    "banners, borders, a blurred or coloured fill around a smaller picture, "
    "letterboxing with text, picture-in-picture, intro or end cards, a "
    "reaction video around the clip.\n"
    "NOT added (do not count): what was in front of the camera (signs, shop "
    "names, screens), a CCTV/dashcam/bodycam timestamp or camera name burned "
    "in by the recording device, the phone camera's own UI.\n\n"
    "Audio: 'natural' = only the sound of the scene; 'music', 'sfx', "
    "'voiceover' = laid over in editing; 'silent' = no audio track or "
    "silence; 'mixed' = natural plus something added.\n\n"
    "Reply with ONE JSON object and nothing else:\n"
    '{"visual_overlays": [{"what": "short description", "where": "e.g. '
    'bottom-right", "when": "whole clip | 0-3s | end card"}], '
    '"audio": "natural|music|sfx|voiceover|silent|mixed", '
    '"audio_notes": "what was added, or \\"\\"", '
    '"clean": true if there are no added visuals and audio is natural or '
    'silent}'
)


def clean_check(path: str) -> dict:
    """Gemini's verdict on added overlays / audio in `path`.
    {} when the check couldn't run (no key, ffmpeg or API trouble)."""
    from shared import vision
    if vision._client is None:
        return {"error": "OPENROUTER_API_KEY not set"}
    drop = None
    try:
        send, drop = vision._analysis_copy(path)
        url = vision._data_url(send, "video/mp4")
        resp = vision._client.chat.completions.create(
            model=os.getenv("FIND_ORIGINAL_MODEL") or vision.config.IG_VISION_MODEL,
            max_tokens=vision.MAX_TOKENS,
            messages=[
                {"role": "system", "content": CLEAN_SYSTEM},
                {"role": "user", "content": [
                    {"type": "text", "text": "Was anything added to this clip?"},
                    {"type": "video_url", "video_url": {"url": url}},
                ]},
            ],
        )
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as e:  # never let one candidate stop the run
        return {"error": str(e).splitlines()[0][:160]}
    finally:
        if drop:
            drop()
    fenced = vision._FENCE.search(raw)
    if fenced:
        raw = fenced.group(1)
    start, end = raw.find("{"), raw.rfind("}")
    try:
        data = json.loads(raw[start:end + 1])
    except ValueError:
        return {"error": "unparseable reply: " + raw[:120]}
    overlays = data.get("visual_overlays") or []
    audio = str(data.get("audio") or "").lower()
    return {
        "overlays": overlays,
        "audio": audio,
        "audio_notes": data.get("audio_notes") or "",
        # Recomputed rather than trusted: the model's own flag drifts from
        # the lists it just wrote often enough to matter.
        "clean": not overlays and audio in ("natural", "silent"),
    }


def download(url: str, dest_stub: str) -> str:
    """Best video+audio of `url` as <dest_stub>.mp4 ("" on failure)."""
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "format": "bv*+ba/b",
            "merge_output_format": "mp4", "outtmpl": dest_stub + ".%(ext)s",
            "socket_timeout": 30}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info)
    except Exception as e:
        print(f"    ! download failed: {str(e).splitlines()[0][:140]}")
        return ""
    mp4 = os.path.splitext(path)[0] + ".mp4"
    return mp4 if os.path.exists(mp4) else (path if os.path.exists(path) else "")


def check_candidates(rows: list[dict], n: int, run_dir: str) -> list[dict]:
    """Download the n best-looking probed video posts and clean-check each.
    Sharpest first (a 4x-pixel copy is worth checking before an earlier
    720p one), earliest breaking ties."""
    pool = [r for r in rows if (r.get("probe") or {}).get("height")]
    pool.sort(key=lambda r: (-(r["probe"]["height"] or 0), r["probe"].get("timestamp") or 0))
    cand_dir = os.path.join(run_dir, "candidates")
    os.makedirs(cand_dir, exist_ok=True)
    checked = []
    for i, r in enumerate(pool[:n], 1):
        print(f"[check {i}/{min(n, len(pool))}] {r['url']}", flush=True)
        path = download(r.get("fetch_url") or r["url"], os.path.join(cand_dir, f"{i:02d}_{r['platform']}"))
        if not path:
            continue
        r["file"] = path
        r["file_meta"] = probe(path)
        r["check"] = clean_check(path)
        c = r["check"]
        if "error" in c:
            print(f"    ? check failed: {c['error']}")
        else:
            bits = [f"{o.get('what')} ({o.get('where', '')}, {o.get('when', '')})" for o in c["overlays"]]
            if c["audio"] not in ("natural", "silent"):
                bits.append(f"audio: {c['audio']} {c['audio_notes']}".strip())
            m = r["file_meta"]
            print(f"    {m['width']}x{m['height']} {m['kbps']} kbps — "
                  + ("CLEAN" + (" (silent)" if c["audio"] == "silent" else "")
                     if c["clean"] else "added: " + "; ".join(bits)))
        checked.append(r)
    return checked


def best_clean(checked: list[dict]) -> dict | None:
    clean = [r for r in checked if (r.get("check") or {}).get("clean")]
    if not clean:
        return None
    # Sound beats silence (a muted repost lost something), then pixels,
    # then bitrate, then the earlier upload.
    clean.sort(key=lambda r: (r["check"]["audio"] == "silent",
                              -(r["file_meta"]["width"] or 0) * (r["file_meta"]["height"] or 0),
                              -r["file_meta"]["kbps"],
                              r["probe"].get("timestamp") or 0))
    return clean[0]


def probe_candidates(rows: list[dict], n: int) -> None:
    """yt-dlp metadata (no download) for the top n video-post hits."""
    import yt_dlp
    picked = [r for r in rows if r["platform"] and r["is_post"]][:n]
    if not picked:
        return
    print(f"\nProbing {len(picked)} candidate(s) with yt-dlp…")
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "socket_timeout": 20, "extractor_retries": 1}
    with yt_dlp.YoutubeDL(opts) as ydl:
        for r in picked:
            try:
                info = ydl.extract_info(r.get("fetch_url") or r["url"], download=False) or {}
                if info.get("entries"):
                    info = next((e for e in info["entries"] if e), {})
                if not info.get("formats") and not info.get("upload_date"):
                    r["probe"] = {"error": "no video at this URL"}
                    continue
            except Exception as e:  # yt-dlp raises many types; a miss is fine
                r["probe"] = {"error": str(e).splitlines()[0][:160]}
                continue
            heights = [f.get("height") or 0 for f in info.get("formats") or []]
            r["probe"] = {
                "height": max(heights or [info.get("height") or 0]),
                "width": info.get("width"),
                "duration": info.get("duration"),
                "upload_date": info.get("upload_date"),
                "timestamp": info.get("timestamp"),
                "uploader": info.get("uploader_id") or info.get("uploader") or info.get("channel"),
            }


# ---------------------------------------------------------------------- main

def recheck(args) -> int:
    """--from-report: the search is the slow, CAPTCHA-prone half, so a
    second look at the same candidates (more of them, a better model)
    starts from the hits an earlier run already found."""
    with open(args.from_report, encoding="utf-8") as f:
        report = json.load(f)
    run_dir = os.path.dirname(os.path.abspath(args.from_report))
    rows = report["results"]
    for r in rows:
        r.pop("probe", None)
    probe_candidates(rows, args.probe or 10)
    print()
    best = best_clean(check_candidates(rows, args.check or 5, run_dir))
    report["best_clean"] = best and {k: best[k] for k in ("url", "file")}
    with open(args.from_report, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, ensure_ascii=False)
    if best:
        m = best["file_meta"]
        print(f"\nBEST CLEAN COPY: {best['url']}\n  {m['width']}x{m['height']}, "
              f"{m['kbps']} kbps, audio {best['check']['audio']}\n  file: {best['file']}")
    else:
        print("\nNo clean copy among the checked candidates.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", nargs="?", help="post URL (Reddit / X / IG / …) or a local video/image file")
    ap.add_argument("--from-report", metavar="REPORT_JSON",
                    help="skip the search: re-probe and clean-check the results of an earlier run")
    ap.add_argument("--frames", type=int, default=5, help="frames to search (default 5)")
    ap.add_argument("--engines", default="yandex,lens", help="comma list: yandex,lens")
    ap.add_argument("--probe", type=int, default=10, metavar="N",
                    help="yt-dlp-probe the top N video hits (0 = off, default 10)")
    ap.add_argument("--check", type=int, default=5, metavar="N",
                    help="download the N sharpest probed copies and have the vision model "
                         "flag added logos/text/music (~0.1-0.5 cent each; 0 = off, default 5)")
    ap.add_argument("--headless", action="store_true", help="no window (CAPTCHAs then just fail)")
    ap.add_argument("--captcha-wait", type=int, default=180, help="seconds to wait for a manual CAPTCHA solve")
    ap.add_argument("--top", type=int, default=25, help="rows to print (default 25)")
    args = ap.parse_args(argv)

    if args.from_report:
        return recheck(args)
    if not args.source:
        ap.error("give a post URL / file, or --from-report")

    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    for e in engines:
        if e not in ENGINES:
            ap.error(f"unknown engine {e!r} (have: {', '.join(ENGINES)})")

    run_dir = os.path.join(RUNS_DIR, datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(run_dir, exist_ok=True)

    source = {}
    if os.path.exists(args.source):
        clip = os.path.abspath(args.source)
    else:
        print(f"Downloading {args.source} …")
        clip, source = fetch_clip(args.source, run_dir)
        if source.get("text"):
            print("Source post:\n  " + source["text"].replace("\n", "\n  "))
        if source.get("author"):
            print(f"  author: {source['author']}")

    is_image = clip.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
    meta = {} if is_image else probe(clip)
    if meta:
        print(f"Clip: {meta['width']}x{meta['height']}, {meta['duration']:.0f}s, {meta['kbps']} kbps")
    frames = extract_frames(clip, args.frames, run_dir)
    print(f"{len(frames)} frame(s) in {run_dir}\n")

    hits = defaultdict(lambda: {"frames": set(), "engines": set(), "title": "", "meta": "", "raw": ""})
    errors = []
    driver = make_driver(args.headless)
    try:
        for engine in engines:
            for frame in frames:
                name = os.path.basename(frame)
                print(f"[{engine}] {name}", flush=True)
                try:
                    links = ENGINES[engine](driver, frame, args.captcha_wait)
                except WebDriverException as e:
                    msg = str(e).splitlines()[0][:160]
                    print(f"    ! {msg}")
                    errors.append({"engine": engine, "frame": name, "error": msg})
                    links = []
                    if "invalid session" in msg or "no such window" in msg:
                        # The window was closed or Chrome crashed: one dead
                        # session would otherwise fail every search after it.
                        print("    restarting Chrome…")
                        try:
                            driver.quit()
                        except WebDriverException:
                            pass
                        driver = make_driver(args.headless)
                for link in links:
                    key = canonical(link["url"])
                    h = hits[key]
                    h["frames"].add(name)
                    h["engines"].add(engine)
                    # canonical() is for counting; extractors want the URL as
                    # the engine gave it (yt-dlp rejects tiktok.com without www.)
                    h["raw"] = h["raw"] or link["url"]
                    if link["title"] and len(link["title"]) > len(h["title"]):
                        h["title"] = link["title"]
                    if link.get("meta"):
                        h["meta"] = link["meta"]
                print(f"    {len(links)} link(s)")
                time.sleep(random.uniform(3, 7))  # don't look like a bot
    finally:
        driver.quit()

    rows = rank(hits)
    if args.probe:
        probe_candidates(rows, args.probe)
    best = None
    if args.check and args.probe:
        print()
        best = best_clean(check_candidates(rows, args.check, run_dir))

    report = {"source": args.source, "clip": clip, "clip_meta": meta, "source_post": source,
              "frames": [os.path.basename(f) for f in frames], "engines": engines,
              "errors": errors, "best_clean": best and {k: best[k] for k in ("url", "file")},
              "results": rows}
    with open(os.path.join(run_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, ensure_ascii=False)

    print(f"\n{len(rows)} distinct page(s). Top {min(args.top, len(rows))}:")
    for r in rows[:args.top]:
        tag = r["platform"] or "web"
        line = f"  [{r['frames']}f {'+'.join(r['engines'])}] {tag:<10} {r['url']}"
        if r["meta"] or r["title"]:
            line += f"\n      {r['meta']}  {r['title'][:90]}".rstrip()
        p = r.get("probe")
        if p and "error" not in p:
            line += f"\n      → {p.get('height')}p, uploaded {p.get('upload_date')} by {p.get('uploader')}"
        elif p:
            line += f"\n      → probe failed: {p['error']}"
        print(line)

    dated = [r for r in rows if (r.get("probe") or {}).get("upload_date")]
    if dated:
        dated.sort(key=lambda r: (r["probe"].get("timestamp") or 0, r["probe"]["upload_date"]))
        best = dated[0]
        print(f"\nEarliest probed upload: {best['url']} "
              f"({best['probe']['upload_date']}, {best['probe']['height']}p)")
    if args.check and args.probe:
        if best:
            m = best["file_meta"]
            print(f"\nBEST CLEAN COPY: {best['url']}\n  {m['width']}x{m['height']}, "
                  f"{m['kbps']} kbps, {m['duration']:.0f}s, audio {best['check']['audio']}"
                  f"\n  file: {best['file']}")
        else:
            print("\nNo clean copy among the checked candidates — try --check with a "
                  "bigger N, or follow the news articles' source links by hand.")
    print(f"\nReport: {os.path.join(run_dir, 'report.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
