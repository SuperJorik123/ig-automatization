"""Hashtag post counts, and the loop that keeps a caption's tags in the band.

Instagram (2026) buries a post in a tag with millions of posts and shows it to
nobody in a tag with a few hundred, so a story tag is only published when it
has between HASHTAG_MIN_POSTS and HASHTAG_MAX_POSTS posts (5k-200k). The model
cannot know those numbers — a first live test put 0 of its 10 picks in the
band (#valencia 32M, #roadclosures 8k, #valenciafloods 0) — so they are
MEASURED, with Apify's `instagram-hashtag-stats` actor (~$0.002 a tag, ~6-10 s
for a batch of twenty).

`refine(text, candidates, suggest)` runs up to HASHTAG_ROUNDS rounds:

  1. measure the candidates (cache first — counts move slowly, so a tag seen in
     the last HASHTAG_CACHE_DAYS costs nothing and waits for nothing);
  2. keep the ones in the band, in the order given (the model orders them most
     relevant first, and `pick_hashtags` keeps the head on every account);
  3. if fewer than HASHTAG_TARGET passed, hand the MEASURED numbers back to the
     model (`suggest`) — "#valencia 32M, too big; #roadclosures 8k, ok" — plus
     the in-band related tags Apify returned alongside, and measure what it
     proposes next. Real numbers tell it which way to go, which blind guessing
     never does.

It stops at HASHTAG_TARGET or after the last round, and returns whatever is in
the band — possibly nothing, in which case the post carries only the account's
own tag (exempt from the band: it is the account's signature).

APIFY ACTOR BUG: the actor's numeric `postsCount` is 100x too big for every
count it formats with "K" ("161.1 K" -> 16,110,000; seen on #sagunto, #faa,
#blackbear). Its TEXT field `posts` is right, so counts are parsed from that
(`parse_count`) and `postsCount` is only a fallback for when it is absent.

Never raises. `lookup` returns None when Apify can't be reached (no token,
outage, out of credit, timeout) and `refine` passes that on as None, which the
caller reads as "unchecked": the model's own tags go out as before.
Blocking — async callers go through asyncio.to_thread.

CLI: py modules/instagram/hashtags.py valencia roadclosures ntsb
"""

import json
import logging
import os
import re
import sys
import tempfile
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import requests  # noqa: E402

from shared import config  # noqa: E402

log = logging.getLogger(__name__)

_API = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"

# "161.1 K", "95.41 M", "8852", "1,234", "2.15 g" — the actor's text counts.
_COUNT = re.compile(r"\s*([\d][\d.,]*)\s*([kmgb]?)\s*", re.IGNORECASE)
_UNIT = {"": 1, "k": 1e3, "m": 1e6, "g": 1e9, "b": 1e9}

# Everything Instagram does not index inside a tag — same rule as caption.py.
_TAG_CHARS = re.compile(r"[^a-z0-9]")

# The related-hashtag lists the actor returns with each result, every entry a
# {"hash": "#x", "info": "92.27 k"}. Free with the lookup, and measured.
_RELATED_KEYS = ("related", "frequent", "average", "rare",
                 "relatedFrequent", "relatedAverage", "relatedRare")


def norm(tag: str) -> str:
    """"#Valencia" -> "valencia"; "" for nothing taggable."""
    return _TAG_CHARS.sub("", (tag or "").strip().lower())


def parse_count(text) -> int | None:
    """The actor's text count as an int; None when it is not one."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return int(text)
    m = _COUNT.fullmatch(str(text))
    if not m:
        return None
    try:
        return int(round(float(m.group(1).replace(",", ""))
                         * _UNIT[m.group(2).lower()]))
    except ValueError:
        return None


def in_band(count) -> bool:
    return count is not None and \
        config.HASHTAG_MIN_POSTS <= count <= config.HASHTAG_MAX_POSTS


def verdict(count) -> str:
    """How the feedback prompt describes a measured count."""
    if not count:
        return "no posts — nobody uses it"
    if count < config.HASHTAG_MIN_POSTS:
        return "too small"
    if count > config.HASHTAG_MAX_POSTS:
        return "too big"
    return "in the band"


# --------------------------------------------------------------------------- #
# cache                                                                        #
# --------------------------------------------------------------------------- #


def _cache_path() -> str:
    return os.path.join(config.TG_DATA_DIR, "hashtag_counts.json")


def _load() -> dict:
    """{tag: [count, unix_ts]} with expired entries dropped. {} on any trouble."""
    try:
        with open(_cache_path(), encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    cutoff = time.time() - config.HASHTAG_CACHE_DAYS * 86400
    return {t: v for t, v in raw.items()
            if isinstance(v, list) and len(v) == 2 and v[1] >= cutoff}


def _save(cache: dict) -> None:
    """Atomic write — a crash mid-save must not leave half a JSON file."""
    path = _cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("hashtag cache not saved: %s", exc)


# --------------------------------------------------------------------------- #
# Apify                                                                        #
# --------------------------------------------------------------------------- #


def _fetch(tags: list) -> list | None:
    """One actor run over `tags`: the dataset items, or None on any failure."""
    timeout = int(config.HASHTAG_LOOKUP_TIMEOUT_S)
    try:
        resp = requests.post(
            _API.format(actor=config.HASHTAG_ACTOR),
            params={"timeout": timeout},
            headers={"Authorization": f"Bearer {config.APIFY_TOKEN}"},
            json={"hashtags": tags, "includeLatestPosts": False,
                  "includeTopPosts": False},
            timeout=timeout + 15)
    except requests.RequestException as exc:
        log.error("hashtag lookup failed: %s", exc)
        return None
    if resp.status_code >= 400:
        log.error("hashtag lookup failed: HTTP %s %s", resp.status_code,
                  resp.text[:200])
        return None
    try:
        items = resp.json()
    except ValueError:
        log.error("hashtag lookup returned no JSON: %s", resp.text[:200])
        return None
    return items if isinstance(items, list) else None


def lookup(tags) -> tuple[dict, dict] | None:
    """Post counts for `tags`: (counts, related) or None when Apify failed.

    `counts` has every requested tag (normalised) — a tag the actor returned
    nothing for counts as 0, since that is what an unused tag looks like.
    `related` is the in-band related tags the actor reported alongside, with
    their counts: suggestions the next round may use, already paid for.
    Cached tags are answered without a run; everything measured is cached.
    """
    want = [t for t in dict.fromkeys(norm(t) for t in tags) if t]
    cache = _load()
    counts = {t: cache[t][0] for t in want if t in cache}
    missing = [t for t in want if t not in counts]
    related: dict = {}
    if missing:
        if not config.APIFY_TOKEN:
            return None
        items = _fetch(missing)
        if items is None:
            return None
        now = int(time.time())
        for item in items:
            name = norm(item.get("name") or item.get("id") or "")
            if not name:
                continue
            n = parse_count(item.get("posts"))
            if n is None:
                # Text count absent: the numeric one, despite its "K" bug.
                n = parse_count(item.get("postsCount")) or 0
            counts[name] = n
            cache[name] = [n, now]
            for key in _RELATED_KEYS:
                for rel in item.get(key) or ():
                    tag = norm((rel or {}).get("hash", ""))
                    rn = parse_count((rel or {}).get("info"))
                    if tag and rn is not None:
                        cache.setdefault(tag, [rn, now])
                        if in_band(rn):
                            related[tag] = rn
        for t in missing:
            if t not in counts:
                counts[t] = 0
                cache[t] = [0, now]
        _save(cache)
    return {t: counts[t] for t in want}, related


def refine(text: str, candidates, suggest) -> list[str] | None:
    """The in-band tags for a caption, after up to HASHTAG_ROUNDS rounds.

    `candidates` is the model's first list, most relevant first. `suggest(text,
    measured, related, need)` is the model call that proposes the next round
    from the measured numbers (caption.suggest_hashtags); it returns a list of
    tags, empty to give up. Returns the in-band tags in order (at most
    HASHTAG_TARGET, possibly none), or None when the FIRST lookup failed —
    the caller then publishes the unchecked tags. A later round failing keeps
    what earlier rounds found.
    """
    target = max(int(config.HASHTAG_TARGET), 1)
    found: list = []
    measured: dict = {}
    related: dict = {}
    batch = [t for t in dict.fromkeys(norm(c) for c in candidates) if t]
    for round_no in range(1, max(int(config.HASHTAG_ROUNDS), 1) + 1):
        batch = [t for t in batch if t not in measured]
        if not batch:
            break
        res = lookup(batch)
        if res is None:
            return None if round_no == 1 else found
        counts, rel = res
        measured.update(counts)
        related.update(rel)
        found += [t for t in batch if in_band(counts[t]) and t not in found]
        log.info("hashtags round %d: %d/%d in band, %d found so far",
                 round_no, sum(in_band(counts[t]) for t in batch), len(batch),
                 len(found))
        if len(found) >= target or round_no >= config.HASHTAG_ROUNDS:
            break
        offer = {t: n for t, n in related.items() if t not in measured}
        try:
            batch = list(suggest(text, measured, offer, target - len(found)) or ())
        except Exception as exc:          # a model failure ends the search
            log.error("hashtag suggestion failed: %s", exc)
            break
        batch = [t for t in dict.fromkeys(norm(c) for c in batch) if t]
    return found[:target]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) < 2:
        sys.exit("usage: py modules/instagram/hashtags.py TAG [TAG ...]")
    res = lookup(sys.argv[1:])
    if res is None:
        sys.exit("lookup failed (see the log above; is APIFY_TOKEN set?)")
    counts, related = res
    for t, n in sorted(counts.items(), key=lambda kv: kv[1]):
        print(f"{n:>13,}  #{t}  ({verdict(n)})")
    if related:
        print("\nin-band related tags:")
        for t, n in sorted(related.items(), key=lambda kv: kv[1]):
            print(f"{n:>13,}  #{t}")
