"""
modules/facebook/poster.py — Facebook Page publishing through the Graph API,
plain `requests`, no SDK.

Host: graph.facebook.com (v23.0), NEVER graph.instagram.com — this is the
other half of Meta's split. modules/instagram/graph.py speaks the "Instagram
API with Instagram Login" flow, whose IGAA… tokens only work on the Instagram
host and never touch a Facebook Page; this module speaks the Pages API with
an EAA… PAGE access token. The two share a vendor and nothing else.

Credentials per brand in credentials/brands/<name>.json:

    "facebook": {"page_id": "…", "access_token": "EAA…"}

expanded by shared/credentials.py into the env vars this module reads (account
name uppercased, non-alphanumerics -> "_", the same rule the Twitter and
Instagram posters apply):

    FACEBOOK_<ACCOUNT>_PAGE_ID
    FACEBOOK_<ACCOUNT>_ACCESS_TOKEN

THERE IS NO TOKEN_REFRESHED TWIN, on purpose. A Page token derived from a
Business-portfolio SYSTEM USER does not expire, so the daily refresh job the
Instagram side needs (graph.refresh_stale) has no work to do here. A Page
token minted from a one-hour Explorer user token DOES expire — that is a
testing artefact, not a deployment; mint the real one from a system user.

One permission covers everything below: pages_manage_posts (plus
pages_read_engagement, which Reels refuse to publish without). There is no
separate video scope — publish_video/publish_actions have been dead since
2018 and the dashboard no longer offers them.

MEDIA CAN BE A LOCAL PATH OR A PUBLIC URL, unlike Instagram. The Pages API
accepts a multipart upload as well as a URL it fetches itself, so a caller
holding a freshly rendered file does NOT need shared/public_media.expose()
the way the Instagram leg does. `_is_local` picks the mode: an existing path
is uploaded, anything else is handed over as a URL. Preferring the upload is
also more robust — Meta's fetcher gives up on some perfectly public URLs
(error 389 / subcode 1363057, "Unable to fetch video file from URL": observed
against a Google Cloud Storage sample that curl serves fine), and that
failure looks identical to a broken URL on our side.

REELS ARE THE ODD ONE OUT: a three-phase resumable upload that wants the
BYTES, never a URL — start (returns a one-off rupload.facebook.com endpoint),
upload, finish. See publish_reel.

Every public function returns the shape the Twitter and Instagram posters
return — {"account", "status": "success", "id"} or {"account", "status":
"failed", "error"} — and never raises for API trouble, so a caller fanning
out over several platforms reports one failed leg instead of losing the rest.
Blocking; async callers use asyncio.to_thread.

CLI:
    py modules/facebook/poster.py --account frontiva24 --whoami
    py modules/facebook/poster.py --account frontiva24 --text "hello"
    py modules/facebook/poster.py card.jpg --caption "…" --account frontiva24
    py modules/facebook/poster.py clip.mp4 --caption "…" --account frontiva24
    py modules/facebook/poster.py clip.mp4 --caption "…" --account frontiva24 --feed-video
    # --draft keeps a photo/video off the feed (Reels ignore it — see publish_reel)
"""

import argparse
import json
import logging
import os
import re
import sys
import time

# Repo-root bootstrap so `from shared import config` resolves when this file
# is run directly (`py modules/facebook/poster.py`).
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import requests  # noqa: E402

from shared import config  # noqa: E402,F401  (loads the repo-root .env)

log = logging.getLogger(__name__)

HOST = "https://graph.facebook.com"
API_VERSION = "v23.0"          # pinned to match modules/instagram/graph.py
API = f"{HOST}/{API_VERSION}"
MESSAGE_MAX = 63206            # Facebook's post-body ceiling
POLL_S = 5                     # seconds between Reel status checks
POLL_TIMEOUT_S = 600
HTTP_TIMEOUT_S = 60
UPLOAD_TIMEOUT_S = 600         # the rupload leg carries the whole file

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm"}
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


class GraphError(RuntimeError):
    """An error body from the API, or a transport failure."""


# --------------------------------------------------------------------------- #
# credentials                                                                  #
# --------------------------------------------------------------------------- #


def _env_prefix(account: str) -> str:
    """frontiva24 -> FACEBOOK_FRONTIVA24_ ; my.brand-2 -> FACEBOOK_MY_BRAND_2_"""
    slug = re.sub(r"[^A-Z0-9]", "_", account.upper())
    return f"FACEBOOK_{slug}_"


def _creds(account: str) -> tuple[str, str]:
    prefix = _env_prefix(account)
    token = os.getenv(prefix + "ACCESS_TOKEN", "").strip()
    page_id = os.getenv(prefix + "PAGE_ID", "").strip()
    if not token:
        raise EnvironmentError(f"Missing Facebook credential: {prefix}ACCESS_TOKEN")
    if not page_id:
        raise EnvironmentError(f"Missing Facebook credential: {prefix}PAGE_ID")
    return token, page_id


# --------------------------------------------------------------------------- #
# text                                                                         #
# --------------------------------------------------------------------------- #


def trim_message(text: str, limit: int = MESSAGE_MAX) -> str:
    """Cut to Facebook's cap on a word boundary, ending in an ellipsis. A
    single token longer than the cap is hard-cut (nothing else to do). The
    cap is so high that nothing this repo writes will reach it — it exists so
    a runaway caption fails as a trim rather than as an API error."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space > 0:
        cut = cut[:space]
    return cut.rstrip() + "…"


# --------------------------------------------------------------------------- #
# HTTP                                                                         #
# --------------------------------------------------------------------------- #


def _raise_for_error(resp) -> dict:
    """Parsed body, or GraphError carrying the API's own wording."""
    try:
        body = resp.json()
    except ValueError:
        raise GraphError(f"HTTP {resp.status_code}: non-JSON reply")
    if not isinstance(body, dict):
        raise GraphError(f"HTTP {resp.status_code}: unexpected reply {body!r}")
    err = body.get("error")
    if err or resp.status_code >= 400:
        if isinstance(err, dict):
            msg = err.get("error_user_msg") or err.get("message") or json.dumps(err)
            code = err.get("code")
            sub = err.get("error_subcode")
            detail = f"{msg} (code {code}" + (f"/{sub}" if sub else "") + ")"
        else:
            detail = f"HTTP {resp.status_code}: {json.dumps(body)[:300]}"
        raise GraphError(detail)
    return body


def _call(method: str, url: str, params: dict | None = None,
          data: dict | None = None, files: dict | None = None,
          timeout: int = HTTP_TIMEOUT_S) -> dict:
    """One API call -> parsed JSON. Raises GraphError on an error body, a
    non-JSON reply or a transport failure."""
    try:
        resp = requests.request(method, url, params=params, data=data,
                                files=files, timeout=timeout)
    except requests.RequestException as exc:
        raise GraphError(f"request failed: {exc}") from exc
    return _raise_for_error(resp)


def _is_local(ref: str) -> bool:
    """A media reference we can upload rather than ask Meta to fetch. A URL
    never exists on disk, so os.path.exists is the whole rule."""
    try:
        return bool(ref) and os.path.exists(ref)
    except (OSError, ValueError):     # absurdly long strings on Windows
        return False


# --------------------------------------------------------------------------- #
# publish — text, photo, feed video                                            #
# --------------------------------------------------------------------------- #


def _post_id(body: dict) -> str:
    """The id a caller should keep. /photos answers with both the photo id and
    the feed post's `post_id`; the post id is the one that addresses the
    story (and the one a DELETE takes)."""
    return str(body.get("post_id") or body.get("id") or "")


def publish_text(message: str, account: str) -> dict:
    """Publish a text-only story to the Page feed."""
    try:
        token, page_id = _creds(account)
        body = _call("POST", f"{API}/{page_id}/feed",
                     data={"message": trim_message(message),
                           "access_token": token})
        pid = _post_id(body)
        if not pid:
            raise GraphError(f"feed returned no id: {body!r}")
        log.info("[FB:%s] published text — post %s", account, pid)
        return {"account": account, "id": pid, "status": "success"}
    except Exception as exc:
        log.warning("[FB:%s] text publish failed: %s", account, exc)
        return {"account": account, "status": "failed", "error": str(exc)}


def _publish_simple(kind: str, ref: str, caption: str, account: str,
                    published: bool) -> dict:
    """/photos and /videos differ only in the endpoint and which field carries
    the caption and the URL, so they share one body."""
    edge, text_field, url_field, file_field = (
        ("photos", "caption", "url", "source") if kind == "photo"
        else ("videos", "description", "file_url", "source"))
    try:
        token, page_id = _creds(account)
        data = {text_field: trim_message(caption), "access_token": token}
        if not published:
            # Uploads the media without putting a story on the feed — what a
            # test run wants, and what the CLI's --draft passes.
            data["published"] = "false"
        url = f"{API}/{page_id}/{edge}"
        if _is_local(ref):
            log.info("[FB:%s] uploading %s %s", account, kind, ref)
            with open(ref, "rb") as fh:
                body = _call("POST", url, data=data,
                             files={file_field: fh}, timeout=UPLOAD_TIMEOUT_S)
        else:
            log.info("[FB:%s] publishing %s from %s", account, kind, ref)
            body = _call("POST", url, data={**data, url_field: ref})
        pid = _post_id(body)
        if not pid:
            raise GraphError(f"{edge} returned no id: {body!r}")
        log.info("[FB:%s] published %s — post %s", account, kind, pid)
        return {"account": account, "id": pid, "status": "success"}
    except Exception as exc:
        log.warning("[FB:%s] %s publish failed: %s", account, kind, exc)
        return {"account": account, "status": "failed", "error": str(exc)}


def publish_photo(ref: str, caption: str, account: str,
                  published: bool = True) -> dict:
    """Publish one photo — `ref` is a local path (uploaded) or a public URL
    (fetched by Meta)."""
    return _publish_simple("photo", ref, caption, account, published)


def publish_video(ref: str, caption: str, account: str,
                  published: bool = True) -> dict:
    """Publish one video to the Page FEED (not as a Reel) — `ref` is a local
    path (uploaded) or a public URL (fetched by Meta)."""
    return _publish_simple("video", ref, caption, account, published)


# --------------------------------------------------------------------------- #
# publish — Reels (three-phase resumable upload)                               #
# --------------------------------------------------------------------------- #


def _reel_start(page_id: str, token: str) -> tuple[str, str]:
    body = _call("POST", f"{API}/{page_id}/video_reels",
                 data={"upload_phase": "start", "access_token": token})
    video_id = str(body.get("video_id") or "")
    upload_url = str(body.get("upload_url") or "")
    if not video_id or not upload_url:
        raise GraphError(f"reel start returned no upload target: {body!r}")
    return video_id, upload_url


def _reel_upload(upload_url: str, token: str, path: str) -> None:
    """Phase 2 — the bytes, to the one-off rupload host the start phase named.

    Not a Graph call: the credential travels as `Authorization: OAuth <token>`
    and the offset/file_size headers are what make it resumable. We always
    send the whole file in one shot (offset 0); these renders are single-digit
    megabytes, and a resume path we never exercise is a resume path that does
    not work when it is finally needed."""
    size = os.path.getsize(path)
    headers = {"Authorization": f"OAuth {token}",
               "offset": "0",
               "file_size": str(size),
               "Content-Type": "application/octet-stream"}
    try:
        with open(path, "rb") as fh:
            resp = requests.post(upload_url, headers=headers, data=fh,
                                 timeout=UPLOAD_TIMEOUT_S)
    except requests.RequestException as exc:
        raise GraphError(f"reel upload failed: {exc}") from exc
    body = _raise_for_error(resp)
    if not body.get("success", True):
        raise GraphError(f"reel upload rejected: {body!r}")


def _reel_finish(page_id: str, token: str, video_id: str, caption: str) -> None:
    _call("POST", f"{API}/{page_id}/video_reels",
          data={"upload_phase": "finish",
                "video_id": video_id,
                "video_state": "PUBLISHED",
                "description": trim_message(caption),
                "access_token": token})


def _wait_reel_published(video_id: str, token: str, account: str) -> None:
    """Poll the Reel's publishing status.

    BEST-EFFORT ON PURPOSE. By the time finish returns, the upload is
    committed and Facebook will publish on its own schedule, so a timeout
    here does NOT mean the post failed — reporting a failure for a Reel that
    is about to appear is worse than reporting success a minute early. Only
    an explicit error status raises."""
    deadline = time.monotonic() + POLL_TIMEOUT_S
    while True:
        try:
            body = _call("GET", f"{API}/{video_id}",
                         params={"fields": "status", "access_token": token})
        except GraphError as exc:
            log.info("[FB:%s] reel %s status unreadable (%s) — not failing",
                     account, video_id, exc)
            return
        status = body.get("status") or {}
        phase = (status.get("video_status") or "").lower()
        if phase in ("ready", "published"):
            return
        if phase == "error":
            raise GraphError(f"reel processing failed: {json.dumps(status)[:300]}")
        if time.monotonic() >= deadline:
            log.warning("[FB:%s] reel %s still %s after %ss — assuming it "
                        "lands", account, video_id, phase or "?", POLL_TIMEOUT_S)
            return
        log.info("[FB:%s] reel %s status=%s — waiting", account, video_id,
                 phase or "?")
        time.sleep(POLL_S)


def publish_reel(path: str, caption: str, account: str) -> dict:
    """Publish one local MP4 as a Reel.

    A LOCAL PATH ONLY — the Reels endpoint takes the bytes, so there is no
    URL mode to fall back on. `published=False` has no equivalent either:
    the API's video_state offers PUBLISHED, DRAFT and SCHEDULED, and a DRAFT
    reel is invisible outside the Page's own composer, which makes it useless
    for verifying a publish."""
    try:
        token, page_id = _creds(account)
        if not _is_local(path):
            raise GraphError(f"reel needs a local file, got {path!r} "
                             f"(the Reels API uploads bytes, it cannot fetch "
                             f"a URL — use publish_video for a URL)")
        log.info("[FB:%s] starting reel upload for %s", account, path)
        video_id, upload_url = _reel_start(page_id, token)
        _reel_upload(upload_url, token, path)
        _reel_finish(page_id, token, video_id, caption)
        _wait_reel_published(video_id, token, account)
        log.info("[FB:%s] published reel — video %s", account, video_id)
        return {"account": account, "id": video_id, "status": "success"}
    except Exception as exc:
        log.warning("[FB:%s] reel publish failed: %s", account, exc)
        return {"account": account, "status": "failed", "error": str(exc)}


# --------------------------------------------------------------------------- #
# dispatch                                                                     #
# --------------------------------------------------------------------------- #


def post_media(file_path: str, caption: str, account_name: str,
               as_reel: bool = True) -> dict:
    """Publish a local file, picking the edge from its extension — the entry
    point that matches modules/twitter/poster.post_media.

    Video defaults to a REEL rather than a feed video: everything this repo
    renders is the 1080x1920 vertical clip shared/branding.py produces, which
    is what Reels is for. `as_reel=False` puts it on the feed instead."""
    ext = os.path.splitext(file_path or "")[1].lower()
    if ext in PHOTO_EXTS:
        return publish_photo(file_path, caption, account_name)
    if ext in VIDEO_EXTS:
        return (publish_reel(file_path, caption, account_name) if as_reel
                else publish_video(file_path, caption, account_name))
    return {"account": account_name, "status": "failed",
            "error": f"unsupported media type {ext or '(none)'} for {file_path}"}


def upload_post(d, media_path, caption_body, hashtags, kind="post",
                target_account=None):
    """Back-compat wrapper mirroring modules.instagram.upload_post.upload_post
    so existing call sites keep their shape. `d` (the ADB device) is ignored —
    this is an API poster, there is no phone."""
    caption = "\n\n".join(p for p in (caption_body, hashtags) if p)
    return post_media(media_path, caption, target_account or "",
                      as_reel=(kind == "reel"))


# --------------------------------------------------------------------------- #
# whoami / delete                                                              #
# --------------------------------------------------------------------------- #


def whoami(account: str) -> dict:
    """GET /{page_id} — raises GraphError / EnvironmentError (a CLI check,
    not a publish leg)."""
    token, page_id = _creds(account)
    return _call("GET", f"{API}/{page_id}",
                 params={"fields": "id,name,category,fan_count",
                         "access_token": token})


def delete_post(post_id: str, account: str) -> dict:
    """Remove a post. Used to clear test posts off a live Page; not part of
    any publish path."""
    try:
        token, _ = _creds(account)
        _call("DELETE", f"{API}/{post_id}", params={"access_token": token})
        log.info("[FB:%s] deleted post %s", account, post_id)
        return {"account": account, "id": post_id, "status": "success"}
    except Exception as exc:
        return {"account": account, "status": "failed", "error": str(exc)}


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def main() -> None:
    ap = argparse.ArgumentParser(description="Facebook Page publisher.")
    ap.add_argument("media", nargs="?", help="local photo/video to publish")
    ap.add_argument("--account", required=True,
                    help="account name keying the FACEBOOK_<ACCOUNT>_* env vars")
    ap.add_argument("--caption", default="", help="post text")
    ap.add_argument("--whoami", action="store_true", help="GET /{page_id}")
    ap.add_argument("--text", help="publish a text-only post")
    ap.add_argument("--photo", metavar="URL", help="publish a photo at this public URL")
    ap.add_argument("--video", metavar="URL", help="publish a video at this public URL")
    ap.add_argument("--delete", metavar="POST_ID", help="delete a post")
    ap.add_argument("--feed-video", action="store_true",
                    help="publish a local video to the feed instead of as a Reel")
    ap.add_argument("--draft", action="store_true",
                    help="upload without putting a story on the feed "
                         "(photo/video only — Reels have no such mode)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.whoami:
        print(json.dumps(whoami(args.account), indent=2, ensure_ascii=False))
        return
    if args.delete:
        out = delete_post(args.delete, args.account)
    elif args.text:
        out = publish_text(args.text, args.account)
    elif args.photo:
        out = publish_photo(args.photo, args.caption, args.account,
                            published=not args.draft)
    elif args.video:
        out = publish_video(args.video, args.caption, args.account,
                            published=not args.draft)
    elif args.media:
        ext = os.path.splitext(args.media)[1].lower()
        if args.draft and ext in VIDEO_EXTS and not args.feed_video:
            # A drafted Reel is invisible; say so rather than silently
            # publishing one to a live Page.
            ap.error("--draft needs --feed-video for a video "
                     "(Reels cannot be drafted)")
        if args.draft:
            out = (publish_photo if ext in PHOTO_EXTS else publish_video)(
                args.media, args.caption, args.account, published=False)
        else:
            out = post_media(args.media, args.caption, args.account,
                             as_reel=not args.feed_video)
    else:
        ap.error("give a media path, or one of --text/--photo/--video/"
                 "--whoami/--delete")
    print(json.dumps(out, indent=2, ensure_ascii=False))
    sys.exit(0 if out.get("status") == "success" else 1)


if __name__ == "__main__":
    main()
