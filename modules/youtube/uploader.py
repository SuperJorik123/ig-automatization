"""
modules/youtube/uploader.py — upload a video (Short) to a YouTube account.

Uses the YouTube Data API v3 (google-api-python-client) with per-account
OAuth 2.0. Credentials are FILE-based, one folder per account:

    credentials/youtube/
    └── <account_name>/
        ├── client_secrets.json   # downloaded from Google Cloud Console
        └── token.pickle          # created automatically after first login

`token.pickle` is written on the first successful OAuth consent and refreshed
automatically afterwards. If it is missing or unrefreshable the Google client
opens a BROWSER for consent — that needs a display, so generate the pickle
once interactively per account before running headless / from the server:

    py modules/youtube/uploader.py path\\to\\video.mp4 --title "Test" --account myaccount

Shorts note: YouTube auto-classifies any vertical video up to 3 minutes as a
Short — there is no separate API endpoint. Uploading a 9:16 reel through this
function is enough; no "#Shorts" tag is required anymore.
"""

import argparse
import os
import pickle
import re
import sys

# Repo-root bootstrap so `from shared import config` resolves when this file
# is run directly (`py modules/youtube/uploader.py`).
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shared import config  # noqa: E402

from google.auth.transport.requests import Request  # noqa: E402
from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402
from googleapiclient.discovery import build  # noqa: E402
from googleapiclient.errors import HttpError  # noqa: E402
from googleapiclient.http import MediaFileUpload  # noqa: E402

# OAuth scope required to upload videos.
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def _strip_emoji(text: str) -> str:
    """Keep only letters (any language), numbers, and basic punctuation —
    YouTube rejects or mangles emoji in some fields."""
    cleaned = re.sub(
        r'[^\w\s\?\!\.\,\:\;\"\'\-\(\)\@\#\/\\]',
        "",
        text or "",
        flags=re.UNICODE,
    )
    cleaned = re.sub(r" {2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _resolve_account_dir(account_name: str) -> str:
    """Return credentials/youtube/<account>/, validating that it exists and
    holds a client_secrets.json."""
    account_dir = os.path.join(config.YOUTUBE_CREDS_DIR, account_name)
    if not os.path.isdir(account_dir):
        raise FileNotFoundError(f"[YouTube] Account folder not found: {account_dir}")

    secrets_file = os.path.join(account_dir, "client_secrets.json")
    if not os.path.exists(secrets_file):
        raise FileNotFoundError(f"[YouTube] client_secrets.json not found: {secrets_file}")

    return account_dir


def _get_youtube_service(account_dir: str):
    """Authenticate for one account dir and return an authorised YouTube API
    service. Loads/refreshes token.pickle; falls back to the interactive
    browser consent flow when there is no usable token."""
    secrets_file = os.path.join(account_dir, "client_secrets.json")
    token_pickle = os.path.join(account_dir, "token.pickle")
    account = os.path.basename(account_dir)

    creds = None
    if os.path.exists(token_pickle):
        with open(token_pickle, "rb") as fh:
            creds = pickle.load(fh)
        print(
            f"[YouTube:{account}] token.pickle loaded — valid={creds.valid}, "
            f"expired={creds.expired}, has_refresh_token={bool(creds.refresh_token)}",
            flush=True,
        )

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print(f"[YouTube:{account}] Refreshing expired access token …", flush=True)
            creds.refresh(Request())
        else:
            # Interactive: opens a browser. Will HANG headless — see module doc.
            print(f"[YouTube:{account}] Opening browser for OAuth consent …", flush=True)
            flow = InstalledAppFlow.from_client_secrets_file(secrets_file, YOUTUBE_SCOPES)
            creds = flow.run_local_server(port=0)

        with open(token_pickle, "wb") as fh:
            pickle.dump(creds, fh)
        print(f"[YouTube:{account}] token.pickle saved", flush=True)

    return build("youtube", "v3", credentials=creds)


def _upload(youtube, account_name, file_path, title, description, privacy_status):
    """Resumable chunked upload; returns the new video id."""
    body = {
        "snippet": {
            "title": title[:100],  # YouTube enforces a 100-char title limit
            "description": description[:5000],
            "categoryId": "22",  # "People & Blogs"
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(file_path, mimetype="video/mp4", resumable=True)

    print(f"[YouTube:{account_name}] Uploading: {file_path}", flush=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"[YouTube:{account_name}] Upload progress: {int(status.progress() * 100)}%", flush=True)

    video_id = response.get("id")
    print(f"[YouTube:{account_name}] Done — https://youtu.be/{video_id}", flush=True)
    return video_id


def upload_short(
    file_path: str,
    title: str,
    description: str = "",
    account_name: str = "",
    privacy_status: str = "public",
) -> dict:
    """
    Upload a local video file to one YouTube account.

    Args:
        file_path:      Absolute path to the .mp4 file. Vertical + ≤3 min
                        is auto-classified as a Short by YouTube.
        title:          Video title (truncated to 100 chars, emoji stripped).
        description:    Description (truncated to 5000 chars, emoji stripped).
        account_name:   Folder name under credentials/youtube/.
        privacy_status: "public" | "unlisted" | "private".

    Returns:
        {"account": ..., "video_id": "...", "status": "success"} or
        {"account": ..., "video_id": None, "status": "failed", "error": "..."}
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"[YouTube] Video file not found: {file_path}")

    clean_title = _strip_emoji(title)[:100]
    clean_description = _strip_emoji(description)[:5000]
    print(f"\n[YouTube:{account_name}] Title: {clean_title!r}", flush=True)

    try:
        account_dir = _resolve_account_dir(account_name)
        youtube = _get_youtube_service(account_dir)
        video_id = _upload(
            youtube, account_name, file_path, clean_title, clean_description, privacy_status
        )
        return {"account": account_name, "video_id": video_id, "status": "success"}

    except HttpError as exc:
        print(f"[YouTube:{account_name}] HTTP error: {exc}", flush=True)
        return {"account": account_name, "video_id": None, "status": "failed", "error": str(exc)}

    except Exception as exc:
        print(f"[YouTube:{account_name}] Failed: {exc}", flush=True)
        return {"account": account_name, "video_id": None, "status": "failed", "error": str(exc)}


def main():
    parser = argparse.ArgumentParser(
        description="Upload a video (Short) to YouTube. Also use this to run the "
        "one-time interactive OAuth flow that creates token.pickle per account."
    )
    parser.add_argument("video", help="Path to the .mp4 file")
    parser.add_argument("--title", required=True, help="Video title")
    parser.add_argument("--description", default="", help="Video description")
    parser.add_argument("--account", required=True, help="Folder name under credentials/youtube/")
    parser.add_argument(
        "--privacy", default="public", choices=["public", "unlisted", "private"]
    )
    args = parser.parse_args()

    result = upload_short(
        args.video,
        title=args.title,
        description=args.description,
        account_name=args.account,
        privacy_status=args.privacy,
    )
    sys.exit(0 if result["status"] == "success" else 1)


if __name__ == "__main__":
    main()
