"""
modules/twitter/publisher.py — translate a caption per Twitter account language
and post one media file to every selected account.

The Twitter twin of modules/youtube/publisher.py, shaped the same way:
destinations are {"chat_id": <account>, "lang": <lang>} dicts (TW_DESTINATIONS
parsed by shared.config — "chat_id" holds the account name keying the
TWITTER_<ACCOUNT>_* env vars). Translation reuses modules/telegram/translator
with the same per-language cache, so N same-language accounts cost one call.

The only Twitter-specific text rule lives here: the 280-char ceiling.
`trim_tweet` counts every URL as 23 chars (X wraps all links in t.co
regardless of their real length) and cuts the rest on a word boundary with an
ellipsis. NOTE the money angle: pay-per-use bills a tweet CONTAINING A LINK
at ~13x a plain one, so captions are passed through as-is — nothing here adds
links.

Posting is a blocking network call — async callers (the news bot) run
publish_tweets in a thread (asyncio.to_thread).
"""

import logging
import os
import re
import sys

# Repo-root bootstrap for direct runs / imports from anywhere.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shared import config  # noqa: E402
from modules.telegram import translator  # noqa: E402
from modules.twitter import poster  # noqa: E402

log = logging.getLogger(__name__)

TWEET_MAX = 280
URL_LEN = 23  # every URL counts as one t.co link, regardless of real length
_URL_RE = re.compile(r"https?://\S+")


def tweet_len(text: str) -> int:
    """Length as X counts it: URLs are 23 chars each, the rest is itself."""
    plain = _URL_RE.sub("", text)
    return len(plain) + URL_LEN * len(_URL_RE.findall(text))


def trim_tweet(text: str, limit: int = TWEET_MAX) -> str:
    """Fit `text` into `limit` as X counts it, cutting on a word boundary with
    an ellipsis. URLs are never split — a truncated URL is worse than a
    dropped one, so the cut always lands before the URL that would overflow."""
    text = (text or "").strip()
    if tweet_len(text) <= limit:
        return text

    # Walk tokens (URLs are atomic) until the budget is spent.
    out: list[str] = []
    used = 0
    budget = limit - 1  # reserve the ellipsis
    for token in text.split():
        cost = URL_LEN if _URL_RE.fullmatch(token) else len(token)
        sep = 1 if out else 0
        if used + sep + cost > budget:
            break
        out.append(token)
        used += sep + cost
    if not out:  # one giant unbreakable first token — hard cut
        return text[:budget] + "…"
    return " ".join(out) + "…"


def publish_tweets(media_path: str, caption: str, dests: list | None = None):
    """Post `media_path` (photo or video) to each Twitter destination (default:
    all of TW_DESTINATIONS), translating the caption into each account's
    language first. A failure on one account is logged and skipped — it does
    not abort the rest.

    Returns (posted, errors): posted is the list of account names that
    succeeded, errors is a list of (account, message)."""
    if dests is None:
        dests = config.TW_DESTINATIONS

    cache: dict[str, str] = {}
    posted, errors = [], []
    for dest in dests:
        account, lang = dest["chat_id"], dest["lang"]
        if lang not in cache:
            cache[lang] = (
                translator.translate(caption, lang, config.SOURCE_LANG) if lang else caption
            )
        text = trim_tweet(cache[lang])
        try:
            result = poster.post_media(media_path, text, account)
        except Exception as exc:  # missing creds, unreadable file, bad ext ...
            result = {"status": "failed", "error": str(exc)}
        if result["status"] == "success":
            posted.append(account)
        else:
            err = result.get("error", "unknown error")
            log.error("Twitter post to %s failed: %s", account, err)
            errors.append((account, err))
    return posted, errors
