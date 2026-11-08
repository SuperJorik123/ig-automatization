"""
modules/instagram/graph.py — Instagram publishing through the Graph API
("Instagram API with Instagram Login" flow, no Facebook Page), plain
`requests`, no SDK.

Host: graph.instagram.com (v23.0), NEVER graph.facebook.com — the tokens this
flow hands out (prefix "IGAA") only work on the Instagram host.

Credentials per account in the repo-root .env, keyed like the Twitter poster
(account name uppercased, non-alphanumerics -> "_"). The IG_ prefix alone
belongs to the phone/ADB accounts (IG_ACCOUNTS); this module owns IG_GRAPH_:

    IG_GRAPH_<ACCOUNT>_ACCESS_TOKEN     long-lived (60 d) user token
    IG_GRAPH_<ACCOUNT>_USER_ID          the account's Instagram user id
    IG_GRAPH_<ACCOUNT>_TOKEN_REFRESHED  YYYY-MM-DD, written by refresh_token
                                        (drives the daily "older than 7 days"
                                        refresh in the news bot)

Publishing is two calls: POST /{user_id}/media with image_url (JPEG only) or
video_url + media_type=REELS plus the caption -> a container id; poll
GET /{container}?fields=status_code until FINISHED (video transcodes, photos
are usually instant); POST /{user_id}/media_publish with creation_id. The
media must sit at a PUBLIC URL — the API fetches it, there is no upload
(shared/public_media.py provides the URL). Limit: 100 API-published posts per
account per rolling 24 h.

Every public function returns the Twitter poster's dict shape —
{"account", "status": "success", "id"} or {"account", "status": "failed",
"error"} — and never raises for API trouble, so a caller fanning out over
several platforms reports one failed leg instead of losing the rest.
Blocking; the news bot calls it via asyncio.to_thread.

CLI:
    py modules/instagram/graph.py --account wswiremedia --whoami
    py modules/instagram/graph.py --account wswiremedia --refresh
    py modules/instagram/graph.py --account wswiremedia --photo URL --caption "..."
    py modules/instagram/graph.py --account wswiremedia --reel URL --caption "..."
"""

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
import time

# Repo-root bootstrap so `from shared import config` resolves when run directly.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import requests  # noqa: E402

from shared import config  # noqa: E402  (loads the repo-root .env)
from shared import credentials  # noqa: E402

log = logging.getLogger(__name__)

HOST = "https://graph.instagram.com"
API = f"{HOST}/v23.0"
CAPTION_MAX = 2200          # Instagram's caption ceiling
POLL_S = 5                  # seconds between container status checks
POLL_TIMEOUT_S = 600        # a reel transcode can take a few minutes
HTTP_TIMEOUT_S = 60
ENV_PATH = os.path.join(config.ROOT_DIR, ".env")


class GraphError(RuntimeError):
    """An error body from the API, or a transport failure."""


# --------------------------------------------------------------------------- #
# credentials                                                                  #
# --------------------------------------------------------------------------- #


def _env_prefix(account: str) -> str:
    """wswiremedia -> IG_GRAPH_WSWIREMEDIA_ ; my.brand-2 -> IG_GRAPH_MY_BRAND_2_"""
    slug = re.sub(r"[^A-Z0-9]", "_", account.upper())
    return f"IG_GRAPH_{slug}_"


def _creds(account: str) -> tuple[str, str]:
    prefix = _env_prefix(account)
    token = os.getenv(prefix + "ACCESS_TOKEN", "").strip()
    user_id = os.getenv(prefix + "USER_ID", "").strip()
    if not token:
        raise EnvironmentError(f"Missing Instagram credential: {prefix}ACCESS_TOKEN")
    if not user_id:
        raise EnvironmentError(f"Missing Instagram credential: {prefix}USER_ID")
    return token, user_id


# --------------------------------------------------------------------------- #
# caption                                                                      #
# --------------------------------------------------------------------------- #


def trim_caption(text: str, limit: int = CAPTION_MAX) -> str:
    """Cut to Instagram's cap on a word boundary, ending in an ellipsis. A
    single token longer than the cap is hard-cut (nothing else to do)."""
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


def _call(method: str, url: str, params: dict | None = None,
          data: dict | None = None) -> dict:
    """One API call -> parsed JSON. Raises GraphError on an error body, a
    non-JSON reply or a transport failure."""
    try:
        resp = requests.request(method, url, params=params, data=data,
                                timeout=HTTP_TIMEOUT_S)
    except requests.RequestException as exc:
        raise GraphError(f"request failed: {exc}") from exc
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


# --------------------------------------------------------------------------- #
# publish                                                                      #
# --------------------------------------------------------------------------- #


def _create_container(user_id: str, token: str, fields: dict) -> str:
    body = _call("POST", f"{API}/{user_id}/media",
                 data={**fields, "access_token": token})
    cid = body.get("id")
    if not cid:
        raise GraphError(f"container creation returned no id: {body!r}")
    return str(cid)


def _wait_finished(container_id: str, token: str) -> None:
    """Poll the container until FINISHED. ERROR/EXPIRED raise with the API's
    own status text; anything else keeps polling until POLL_TIMEOUT_S."""
    deadline = time.monotonic() + POLL_TIMEOUT_S
    while True:
        body = _call("GET", f"{API}/{container_id}",
                     params={"fields": "status_code,status",
                             "access_token": token})
        status = (body.get("status_code") or "").upper()
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise GraphError(f"container {status}: "
                             f"{body.get('status') or 'no detail from the API'}")
        log.info("[IG] container %s status=%s — waiting", container_id, status or "?")
        if time.monotonic() >= deadline:
            raise GraphError(f"timed out after {POLL_TIMEOUT_S}s waiting for "
                             f"container {container_id} (last status {status or '?'})")
        time.sleep(POLL_S)


def _publish(user_id: str, token: str, container_id: str) -> str:
    body = _call("POST", f"{API}/{user_id}/media_publish",
                 data={"creation_id": container_id, "access_token": token})
    mid = body.get("id")
    if not mid:
        raise GraphError(f"media_publish returned no id: {body!r}")
    return str(mid)


def _publish_media(kind: str, url: str, caption: str, account: str) -> dict:
    try:
        token, user_id = _creds(account)
        fields = {"caption": trim_caption(caption)}
        if kind == "reel":
            fields.update(video_url=url, media_type="REELS")
        else:
            fields["image_url"] = url
        log.info("[IG:%s] creating %s container for %s", account, kind, url)
        cid = _create_container(user_id, token, fields)
        _wait_finished(cid, token)
        mid = _publish(user_id, token, cid)
        log.info("[IG:%s] published %s — media id %s", account, kind, mid)
        return {"account": account, "id": mid, "status": "success"}
    except Exception as exc:  # EnvironmentError, GraphError, anything else
        log.warning("[IG:%s] %s publish failed: %s", account, kind, exc)
        return {"account": account, "status": "failed", "error": str(exc)}


def publish_photo(url: str, caption: str, account: str) -> dict:
    """Publish one JPEG sitting at a public URL as a feed post."""
    return _publish_media("photo", url, caption, account)


def publish_reel(url: str, caption: str, account: str) -> dict:
    """Publish one MP4 sitting at a public URL as a Reel (create container,
    poll until transcoded, publish)."""
    return _publish_media("reel", url, caption, account)


# --------------------------------------------------------------------------- #
# whoami / token refresh                                                       #
# --------------------------------------------------------------------------- #


def whoami(account: str) -> dict:
    """GET /me — raises GraphError / EnvironmentError (a CLI check, not a
    publish leg)."""
    token, _ = _creds(account)
    return _call("GET", f"{API}/me",
                 params={"fields": "user_id,username,account_type",
                         "access_token": token})


# --------------------------------------------------------------------------- #
# numbers: followers, Reel insights, publishing quota (bundle mode)           #
# --------------------------------------------------------------------------- #

# What a Reel's insights are read as. `follows` is deliberately NOT here: the
# Media Insights API rejects it for Reels ("does not support the follows
# metric for this media product type", checked 2026-10-18), so follows are
# measured as the account's follower delta instead (`followers`).
REEL_METRICS = ("views", "reach", "shares", "saved", "comments")


def followers(account: str) -> int | None:
    """The account's follower count, or None when it can't be read. Never
    raises — a number for a report is not worth failing a publish over."""
    try:
        token, _ = _creds(account)
        body = _call("GET", f"{API}/me",
                     params={"fields": "followers_count", "access_token": token})
        return int(body["followers_count"])
    except Exception as exc:
        log.warning("[IG:%s] follower count unavailable: %s", account, exc)
        return None


def reel_insights(media_id: str, account: str) -> tuple[dict | None, str]:
    """({metric: value} for REEL_METRICS, "") — or (None, why) when the token
    can't read them (a missing permission names itself in `why`). Never
    raises."""
    try:
        token, _ = _creds(account)
        body = _call("GET", f"{API}/{media_id}/insights",
                     params={"metric": ",".join(REEL_METRICS),
                             "access_token": token})
        out = {}
        for item in body.get("data") or ():
            values = item.get("values") or [{}]
            out[item.get("name")] = values[0].get("value")
        return out, ""
    except Exception as exc:
        return None, str(exc)[:300]


def latest_reel(account: str) -> tuple[dict | None, str]:
    """The account's most recent Reel with its numbers, for the /report
    command: ({url, posted_at, caption, views, likes, comments, shares,
    saves, reach}, "") — (None, "no reels yet") or (None, why). Read from
    the account itself, not from the bot's reel_log, so a reel posted by hand
    counts too. A missing insights permission still returns likes/comments
    (those come with the media) with views None. Never raises."""
    try:
        token, user_id = _creds(account)
        body = _call("GET", f"{API}/{user_id}/media",
                     params={"fields": "id,media_type,media_product_type,"
                                       "timestamp,permalink,caption,"
                                       "like_count,comments_count",
                             "limit": 25, "access_token": token})
    except Exception as exc:
        return None, str(exc)[:300]
    items = body.get("data") or []
    media = (next((m for m in items if m.get("media_product_type") == "REELS"), None)
             or next((m for m in items if m.get("media_type") == "VIDEO"), None))
    if media is None:
        return None, "no reels yet"
    insights, _ = reel_insights(media["id"], account)
    insights = insights or {}
    return {
        "url": media.get("permalink") or "",
        "posted_at": media.get("timestamp") or "",
        "caption": media.get("caption") or "",
        "views": insights.get("views"),
        "likes": media.get("like_count"),
        "comments": media.get("comments_count", insights.get("comments")),
        "shares": insights.get("shares"),
        "saves": insights.get("saved"),
        "reach": insights.get("reach"),
    }, ""


def publishing_quota(account: str) -> tuple[int, int] | None:
    """(posts used, posts allowed) in the rolling 24 h — Instagram's own
    count (100 a day per account) — or None when it can't be read."""
    try:
        token, user_id = _creds(account)
        body = _call("GET", f"{API}/{user_id}/content_publishing_limit",
                     params={"fields": "config,quota_usage",
                             "access_token": token})
        row = (body.get("data") or [{}])[0]
        return int(row.get("quota_usage") or 0), \
            int((row.get("config") or {}).get("quota_total") or 100)
    except Exception as exc:
        log.warning("[IG:%s] publishing quota unavailable: %s", account, exc)
        return None


def _rewrite_env(path: str, key: str, value: str, stamp_key: str,
                 stamp_value: str) -> None:
    """Replace `key=` in place (keeping every other line byte-for-byte) and
    upsert `stamp_key=` right after it. Written via a temp file + rename so a
    crash mid-write can't leave a half .env."""
    with open(path, encoding="utf-8", newline="") as fh:
        lines = fh.read().splitlines(keepends=True)
    # Match the file's own line ending (the Windows .env is CRLF).
    nl = "\r\n" if any(l.endswith("\r\n") for l in lines) else "\n"
    out, replaced = [], False
    for line in lines:
        bare = line.rstrip("\r\n")
        if bare.startswith(key + "="):
            out.append(f"{key}={value}{nl}")
            replaced = True
            continue
        if bare.startswith(stamp_key + "="):
            continue  # re-inserted below, next to the token line
        out.append(line)
    if not replaced:
        if out and not out[-1].endswith("\n"):
            out[-1] += nl
        out.append(f"{key}={value}{nl}")
    idx = next(i for i, l in enumerate(out) if l.startswith(key + "="))
    out.insert(idx + 1, f"{stamp_key}={stamp_value}{nl}")

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write("".join(out))
    os.replace(tmp, path)


def refresh_token(account: str, env_path: str | None = None) -> dict:
    """Exchange the current long-lived token for a fresh 60-day one, rewrite
    the IG_GRAPH_<ACCOUNT>_ACCESS_TOKEN line in .env in place, stamp the
    refresh date next to it, and update os.environ so the running process
    uses the new token without a restart. The API refuses tokens younger
    than 24 h ("not old enough"), which comes back as a failed result."""
    env_path = env_path or ENV_PATH
    prefix = _env_prefix(account)
    try:
        token = os.getenv(prefix + "ACCESS_TOKEN", "").strip()
        if not token:
            raise EnvironmentError(f"Missing Instagram credential: {prefix}ACCESS_TOKEN")
        body = _call("GET", f"{HOST}/refresh_access_token",
                     params={"grant_type": "ig_refresh_token",
                             "access_token": token})
        new = body.get("access_token")
        if not new:
            raise GraphError(f"refresh returned no access_token: {body!r}")
        today = dt.date.today().isoformat()
        # credentials/accounts.json is the source of truth for the accounts
        # it lists; only a .env-configured account gets its line rewritten.
        if not credentials.update_instagram_token(account, new, today):
            _rewrite_env(env_path, prefix + "ACCESS_TOKEN", new,
                         prefix + "TOKEN_REFRESHED", today)
        os.environ[prefix + "ACCESS_TOKEN"] = new
        os.environ[prefix + "TOKEN_REFRESHED"] = today
        expires = int(body.get("expires_in") or 0)
        log.info("[IG:%s] token refreshed — expires in %.1f days", account,
                 expires / 86400)
        return {"account": account, "status": "success", "expires_in": expires}
    except Exception as exc:
        return {"account": account, "status": "failed", "error": str(exc)}


def token_age_days(account: str) -> int | None:
    """Days since the token was last refreshed per its .env stamp; None when
    there is no (parseable) stamp."""
    raw = os.getenv(_env_prefix(account) + "TOKEN_REFRESHED", "").strip()
    try:
        return (dt.date.today() - dt.date.fromisoformat(raw)).days
    except ValueError:
        return None


def refresh_stale(accounts: list, max_age_days: int = 7,
                  env_path: str | None = None) -> list:
    """Refresh every account whose token is older than `max_age_days` or has
    no stamp at all (unknown age = assume old). Returns [(account, result)]
    for the accounts it touched — the caller decides what to log."""
    out = []
    for account in accounts:
        age = token_age_days(account)
        if age is not None and age < max_age_days:
            continue
        out.append((account, refresh_token(account, env_path=env_path)))
    return out


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def main() -> None:
    ap = argparse.ArgumentParser(description="Instagram Graph API publisher.")
    ap.add_argument("--account", required=True,
                    help="account name keying the IG_GRAPH_<ACCOUNT>_* env vars")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--whoami", action="store_true", help="GET /me")
    g.add_argument("--refresh", action="store_true",
                   help="refresh the long-lived token and rewrite .env")
    g.add_argument("--photo", metavar="URL", help="publish a JPEG at this public URL")
    g.add_argument("--reel", metavar="URL", help="publish an MP4 at this public URL as a Reel")
    ap.add_argument("--caption", default="")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.whoami:
        print(json.dumps(whoami(args.account), indent=2))
        return
    if args.refresh:
        result = refresh_token(args.account)
    elif args.photo:
        result = publish_photo(args.photo, args.caption, args.account)
    else:
        result = publish_reel(args.reel, args.caption, args.account)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["status"] == "success" else 1)


if __name__ == "__main__":
    main()
