"""
modules/twitter/poster.py — Twitter/X posting (videos AND photos).

Tweepy in two flavours per post: the v1.1 API for media upload (chunked for
video, simple for images — v2 has no media upload endpoint) and the v2 Client
to create the tweet itself. Credentials live in the repo-root .env, five vars
per account, keyed by the account name uppercased with non-alphanumerics
mapped to "_". The names match what console.x.com shows (2026 console):

    TWITTER_<ACCOUNT>_CONSUMER_KEY     "Consumer Key" in the console
    TWITTER_<ACCOUNT>_SECRET_KEY       "Secret Key" (the consumer secret)
    TWITTER_<ACCOUNT>_BEARER_TOKEN     "Bearer Token"
    TWITTER_<ACCOUNT>_ACCESS_TOKEN     NOT shown by the console — generate
    TWITTER_<ACCOUNT>_ACCESS_SECRET    both with modules/twitter/authorize.py

(The console's "Client ID"/"Client Secret" are OAuth 2.0 and unused here —
keep them somewhere safe, the bot doesn't read them.) The older var names
CONSUMER_SECRET / ACCESS_TOKEN_SECRET keep working as fallbacks so accounts
configured before the rename don't break.

The Twitter app must have READ AND WRITE OAuth permissions — regenerate the
access token + secret after changing that (the bearer token stays the same).

Note: pulling media *from* Twitter/X is separate and already works via
shared.reel_downloader.

CLI test:
    py modules/twitter/poster.py path\\to\\clip.mp4 --caption "hello" --account myaccount
"""

import argparse
import os
import re
import sys
import time

# Repo-root bootstrap so `from shared import config` resolves when this file
# is run directly (`py modules/twitter/poster.py`).
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shared import config  # noqa: E402,F401  (loads the repo-root .env)

import tweepy  # noqa: E402

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm"}
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _env_prefix(account_name: str) -> str:
    """mirnews -> TWITTER_MIRNEWS_; victor.baxan78 -> TWITTER_VICTOR_BAXAN78_"""
    slug = re.sub(r"[^A-Z0-9]", "_", account_name.upper())
    return f"TWITTER_{slug}_"


def _get_account_env(account_name: str, field: str, *fallbacks: str) -> str:
    """Read one credential, trying `field` then any older fallback names."""
    prefix = _env_prefix(account_name)
    for name in (field, *fallbacks):
        value = os.getenv(prefix + name, "").strip()
        if value:
            return value
    raise EnvironmentError(f"Missing Twitter credential in environment: {prefix + field}")


def _get_clients(account_name: str) -> tuple:
    """Build the v1.1 API (media upload) + v2 Client (tweet creation) pair."""
    api_key = _get_account_env(account_name, "CONSUMER_KEY")
    api_secret = _get_account_env(account_name, "SECRET_KEY", "CONSUMER_SECRET")
    access_token = _get_account_env(account_name, "ACCESS_TOKEN")
    access_secret = _get_account_env(account_name, "ACCESS_SECRET", "ACCESS_TOKEN_SECRET")
    bearer_token = _get_account_env(account_name, "BEARER_TOKEN")

    auth = tweepy.OAuth1UserHandler(
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=access_token,
        access_token_secret=access_secret,
    )
    api_v1 = tweepy.API(auth, wait_on_rate_limit=True)

    client_v2 = tweepy.Client(
        bearer_token=bearer_token,
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=access_token,
        access_token_secret=access_secret,
    )
    return api_v1, client_v2


def _wait_for_processing(api_v1, media_id: str, timeout: int = 300) -> None:
    """Poll Twitter's async video processing until ready / failed / timeout.
    Photos skip this — they have no processing_info."""
    elapsed = 0
    while elapsed < timeout:
        status = api_v1.get_media_upload_status(media_id)
        info = getattr(status, "processing_info", None)
        if info is None:
            return  # no async processing — done

        state = info.get("state")
        print(
            f"[Twitter] Media processing state={state!r} "
            f"progress={info.get('progress_percent')!r} elapsed={elapsed}s",
            flush=True,
        )
        if state == "succeeded":
            return
        if state == "failed":
            error = info.get("error", {})
            raise RuntimeError(
                f"[Twitter] Media processing failed: {error.get('message', 'Unknown error')}"
            )

        wait = info.get("check_after_secs", 5)
        time.sleep(wait)
        elapsed += wait

    raise RuntimeError(f"[Twitter] Timed out waiting for media processing after {timeout}s.")


def _tweet(client_v2, account_name: str, caption: str, media_id: str):
    response = client_v2.create_tweet(text=caption[:280], media_ids=[media_id])
    tweet_id = response.data.get("id") if response.data else None
    print(f"[Twitter:{account_name}] Tweet posted — tweet_id: {tweet_id}", flush=True)
    return tweet_id


def post_media(file_path: str, caption: str, account_name: str) -> dict:
    """
    Upload a local media file (video or photo, by extension) and post it as a
    tweet from one account.

    Returns:
        {"account": ..., "tweet_id": "...", "status": "success"} or
        {"account": ..., "tweet_id": None, "status": "failed", "error": "..."}
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"[Twitter] Media file not found: {file_path}")

    ext = os.path.splitext(file_path)[1].lower()
    is_video = ext in VIDEO_EXTS
    if not is_video and ext not in PHOTO_EXTS:
        raise ValueError(f"[Twitter] Unsupported media extension: {ext!r} ({file_path})")

    print(f"\n[Twitter:{account_name}] Posting {'video' if is_video else 'photo'}: {file_path}", flush=True)

    try:
        api_v1, client_v2 = _get_clients(account_name)

        if is_video:
            media = api_v1.chunked_upload(filename=file_path, media_category="tweet_video")
            media_id = media.media_id_string
            print(f"[Twitter:{account_name}] Media uploaded — media_id: {media_id}", flush=True)
            _wait_for_processing(api_v1, media_id)
        else:
            media = api_v1.media_upload(filename=file_path)
            media_id = media.media_id_string
            print(f"[Twitter:{account_name}] Photo uploaded — media_id: {media_id}", flush=True)

        tweet_id = _tweet(client_v2, account_name, caption, media_id)
        return {"account": account_name, "tweet_id": tweet_id, "status": "success"}

    except Exception as exc:
        print(f"[Twitter:{account_name}] Failed: {exc}", flush=True)
        return {"account": account_name, "tweet_id": None, "status": "failed", "error": str(exc)}


def post_video(file_path: str, caption: str, account_name: str) -> dict:
    """Upload a video file and tweet it. Thin alias over post_media."""
    return post_media(file_path, caption, account_name)


def post_photo(file_path: str, caption: str, account_name: str) -> dict:
    """Upload a photo and tweet it. Thin alias over post_media."""
    return post_media(file_path, caption, account_name)


def upload_post(d, media_path, caption_body, hashtags, kind="post", target_account=None):
    """Back-compat wrapper mirroring modules.instagram.upload_post.upload_post
    so existing call sites can slot Twitter in. `d` (the phone device) and
    `kind` are ignored — posting goes through the API, and video-vs-photo is
    detected from the file extension."""
    caption = (caption_body or "").strip()
    tags = " ".join("#" + t.lstrip("#") for t in (hashtags or []) if t.strip())
    if tags:
        caption = f"{caption}\n\n{tags}" if caption else tags

    result = post_media(media_path, caption, target_account or "")
    if result["status"] != "success":
        raise RuntimeError(f"[Twitter] Post failed: {result.get('error')}")
    return result


def main():
    parser = argparse.ArgumentParser(description="Post a video or photo to Twitter/X.")
    parser.add_argument("media", help="Path to the video/photo file")
    parser.add_argument("--caption", default="", help="Tweet text (max 280 chars)")
    parser.add_argument("--account", required=True, help="Account name keying the TWITTER_<ACCOUNT>_* env vars")
    args = parser.parse_args()

    result = post_media(args.media, args.caption, args.account)
    sys.exit(0 if result["status"] == "success" else 1)


if __name__ == "__main__":
    main()
