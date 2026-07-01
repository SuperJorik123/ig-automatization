"""
server.py — FastAPI backend for the IG queue manager UI.

Endpoints:
  POST /api/posts  multipart form (image file + caption text + hashtags).
                   Auto-numbers the next NNN, writes posts/NNN.<ext> +
                   posts/NNN.json in the format upload_post.py reads.
  GET  /api/posts  list queued posts (everything in posts/ minus posted/).

Run with:
  py -m uvicorn server:app --reload --port 8000

CORS is open to http://localhost:4200 so `ng serve` can call the API directly.
"""

import json
import os
import re
import shutil
import sys
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
import uiautomator2 as u2

HERE = os.path.dirname(os.path.abspath(__file__))
POSTS_DIR = os.path.join(HERE, "posts")
ALLOWED_EXT_BY_TYPE = {
    "post": {".jpg", ".jpeg", ".png"},
    "reel": {".mp4", ".mov"},
}
# Combined set kept around so the queue listing still recognises every
# media file regardless of which type queued it.
ALLOWED_EXT = {ext for exts in ALLOWED_EXT_BY_TYPE.values() for ext in exts}

# Make upload_post.py / reel_downloader.py importable so /api/posts can
# fetch + publish inline. Otherwise we'd have to fork a subprocess and
# stream stdout.
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import upload_post as ig
import reel_downloader

app = FastAPI(title="ig-automatization UI backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:4200"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _next_index(posts_dir: str) -> int:
    """Scan queue + posted/ for existing NNN.* files, return max+1. We never
    reuse a number even after archive, so the running queue stays unique."""
    used = set()
    pattern = re.compile(r"^(\d+)\.(?:jpg|jpeg|png|json)$", re.IGNORECASE)
    for root, _dirs, files in os.walk(posts_dir):
        for f in files:
            m = pattern.match(f)
            if m:
                used.add(int(m.group(1)))
    return (max(used) + 1) if used else 1


def _parse_hashtags(raw: str):
    """Accept either a JSON array string ('["a","b"]') or a free-form
    comma/space-separated list ('#a #b, c'). Returns a clean list of
    bare tag strings (no leading `#`)."""
    raw = (raw or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            arr = json.loads(raw)
            if isinstance(arr, list):
                return [str(t).strip().lstrip("#") for t in arr if str(t).strip()]
        except json.JSONDecodeError:
            pass
    return [t.strip().lstrip("#") for t in re.split(r"[\s,]+", raw) if t.strip()]


@app.post("/api/posts")
async def create_post(
    image: Optional[UploadFile] = File(None),
    url: str = Form(""),
    caption: str = Form(""),
    hashtags: str = Form("[]"),
    post_type: str = Form("post"),
    publish: bool = Form(False),
):
    allowed_for_type = ALLOWED_EXT_BY_TYPE.get(post_type)
    if allowed_for_type is None:
        raise HTTPException(
            status_code=400,
            detail=f"{post_type!r} not supported. Allowed: {sorted(ALLOWED_EXT_BY_TYPE)}",
        )

    # The URL path takes priority over an uploaded file: if the user pasted
    # an IG link we trust that over any leftover file selection in the UI.
    from_url = reel_downloader.is_instagram_url(url)
    if not from_url and (image is None or not image.filename):
        raise HTTPException(
            status_code=400,
            detail="Provide either a media file or an Instagram URL.",
        )

    os.makedirs(POSTS_DIR, exist_ok=True)
    idx = _next_index(POSTS_DIR)
    name = f"{idx:03d}"

    if from_url:
        # yt-dlp picks the actual extension; the stub only seeds the
        # output filename stem. For reels we expect .mp4, for posts .jpg.
        stub_ext = ".jpg" if post_type == "post" else ".mp4"
        stub = os.path.join(POSTS_DIR, f"{name}{stub_ext}")
        try:
            # download_media also returns the source caption; the UI
            # supplies its own caption form field so we ignore it here.
            image_path, _src_caption = reel_downloader.download_media(
                url, stub, kind=post_type
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=f"Download failed: {exc}")
        ext = os.path.splitext(image_path)[1].lower()
        if ext not in allowed_for_type:
            try:
                os.remove(image_path)
            except OSError:
                pass
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Downloaded media has extension {ext!r} which isn't allowed "
                    f"for post_type {post_type!r}. Allowed: {sorted(allowed_for_type)}"
                ),
            )
    else:
        ext = os.path.splitext(image.filename or "")[1].lower()
        if ext not in allowed_for_type:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unsupported extension {ext!r} for post_type {post_type!r}. "
                    f"Allowed: {sorted(allowed_for_type)}"
                ),
            )
        image_path = os.path.join(POSTS_DIR, f"{name}{ext}")
        with open(image_path, "wb") as fp:
            shutil.copyfileobj(image.file, fp)

    json_path = os.path.join(POSTS_DIR, f"{name}.json")

    # Caption is stored as an array of lines so upload_post.py reads the
    # multi-paragraph form. splitlines() handles both \n and \r\n.
    meta = {
        "caption": (caption or "").splitlines(),
        "hashtags": _parse_hashtags(hashtags),
    }
    with open(json_path, "w", encoding="utf-8") as fp:
        json.dump(meta, fp, ensure_ascii=False, indent=2)

    response = {
        "id": name,
        "image": os.path.basename(image_path),
        "json": os.path.basename(json_path),
        "source": "url" if from_url else "upload",
        "hashtags": meta["hashtags"],
        "caption_lines": len(meta["caption"]),
        "status": "queued",
    }

    if publish:
        # File is already on disk. If the IG upload fails we leave it there
        # so the user can retry from `posts/` without re-queueing the image.
        try:
            d = u2.connect(ig.DEVICE_ID)
            caption_str = "\n".join(meta["caption"])
            ig.upload_post(
                d, image_path, caption_str, meta["hashtags"], kind=post_type
            )
            ig.archive_post(POSTS_DIR, image_path)
            response["status"] = "queued_and_posted"
        except Exception as exc:
            response["status"] = "queued_but_post_failed"
            response["post_error"] = str(exc)

    return response


@app.get("/api/posts")
async def list_posts():
    """List queued posts only (excludes posts/posted/)."""
    os.makedirs(POSTS_DIR, exist_ok=True)
    posts = []
    for f in sorted(os.listdir(POSTS_DIR)):
        full = os.path.join(POSTS_DIR, f)
        if not os.path.isfile(full):
            continue
        ext = os.path.splitext(f)[1].lower()
        if ext not in ALLOWED_EXT:
            continue
        name = os.path.splitext(f)[0]
        json_path = os.path.join(POSTS_DIR, f"{name}.json")
        meta = None
        if os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as fp:
                    meta = json.load(fp)
            except json.JSONDecodeError:
                meta = {"error": "invalid JSON"}
        posts.append({"id": name, "image": f, "meta": meta})
    return {"queued": posts}
