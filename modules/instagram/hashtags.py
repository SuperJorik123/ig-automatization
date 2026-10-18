"""Hashtag post counts, and the choice of a caption's tags from them.

Instagram (2026) buries a post in a tag with millions of posts and shows it to
nobody in a tag with a few hundred, so a story tag is only published when it
has between HASHTAG_MIN_POSTS and HASHTAG_MAX_POSTS posts (5k-200k). The model
cannot know those numbers — a first live test put 0 of its 10 picks in the
band (#valencia 32M, #roadclosures 8k, #valenciafloods 0) — so they are
MEASURED, with Apify's `instagram-hashtag-stats` actor (~$0.002 a tag, ~6-10 s
for a batch of twenty).

ONE MEASUREMENT PASS (2026-10-18). The caption model returns eight candidates
per caption, most relevant first (a non-English account's rewrite brings its
own eight), and `caption.choose_hashtags` sends every caption's candidates
through ONE `lookup`. `select` then picks per caption:

  1. drop tags under HASHTAG_FLOOR_POSTS (500) — a tag with no posts is
     usually a compound the model coined, and nobody browses one with fifty;
  2. keep the in-band ones (5k-200k), in the model's relevance order;
  3. fewer than three in the band → fill up to three with the most relevant
     REMAINING candidates, whatever their size;
  4. never anything from Apify's "related" lists — that is where a misspelled
     #futsall and a run of unrelated Indonesian futsal tags came from, back
     when a feedback round fed them to the model.

Each candidate's count and verdict is logged, so a selection can be reviewed.
The account's own tag is exempt — it is the account's signature.

APIFY ACTOR BUG: the actor's numeric `postsCount` is 100x too big for every
count it formats with "K" ("161.1 K" -> 16,110,000; seen on #sagunto, #faa,
#blackbear). Its TEXT field `posts` is right, so counts are parsed from that
(`parse_count`) and `postsCount` is only a fallback for when it is absent.

Never raises. `lookup` returns None when Apify can't be reached (no token,
outage, out of credit, timeout), which the caller reads as "unchecked": the
model's top candidates go out unmeasured.
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

# Everything Instagram does not index inside a tag: anything but a letter or a
# digit. Unicode letters ARE tag characters — a Russian account's #новости is
# one tag — which is why this is not caption.py's ASCII-only brand-tag rule.
_TAG_CHARS = re.compile(r"[\W_]+")

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


def measurable(tag: str) -> bool:
    """Can the actor count this tag? Only a Latin-script one: it URL-encodes
    anything else and answers a bare `postsCount: 0` with no `posts` text —
    seen live on #спасение and #новости (2026-10-18), both huge. A tag it
    can't count is left UNMEASURED, never recorded as 0: a 0 would drop it
    from the caption and sit in the cache for HASHTAG_CACHE_DAYS."""
    return bool(tag) and tag.isascii()


def lookup(tags) -> tuple[dict, dict] | None:
    """Post counts for `tags`: (counts, related) or None when Apify failed.

    `counts` has every requested MEASURABLE tag (normalised) — a tag the
    actor returned nothing for counts as 0, since that is what an unused tag
    looks like. A tag `measurable` rejects is absent from it: unmeasured.
    `related` is the in-band related tags the actor reported alongside, with
    their counts: suggestions the next round may use, already paid for.
    Cached tags are answered without a run; everything measured is cached.
    """
    want = [t for t in dict.fromkeys(norm(t) for t in tags) if measurable(t)]
    if not want:
        return {}, {}
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


def select(candidates, counts: dict, limit: int = 4, fill: int = 3,
           label: str = "") -> list:
    """Up to `limit` tags out of `candidates` (most relevant first), by their
    measured `counts`: the in-band ones first in relevance order; when fewer
    than `fill` are in the band, the most relevant remaining candidates fill
    up to `fill`, whatever their size. Never a tag measured under
    HASHTAG_FLOOR_POSTS (0 = nobody uses it); a tag absent from `counts` is
    UNMEASURED (`measurable`) and may fill like any other. Returned in the
    candidates' relevance order.

    Logs one line per candidate — its count and whether it was picked in band,
    picked as a relevance fallback, dropped, or not needed. Pure otherwise."""
    tags = [t for t in dict.fromkeys(norm(c) for c in candidates) if t]
    usable = [t for t in tags if t not in counts
              or (counts[t] or 0) >= config.HASHTAG_FLOOR_POSTS]
    band = [t for t in usable if in_band(counts.get(t))][:max(int(limit), 0)]
    picked = set(band)
    fallback = set()
    for t in usable:
        if len(picked) >= min(int(fill), int(limit)):
            break
        if t not in picked:
            picked.add(t)
            fallback.add(t)
    for t in tags:
        n = counts.get(t)
        size = "unmeasured" if n is None else f"{int(n or 0):,} posts"
        why = ("picked: in band" if t in band
               else "picked: relevance fallback" if t in fallback
               else "not picked" if t in usable
               else f"dropped: under {config.HASHTAG_FLOOR_POSTS:,} posts")
        log.info("hashtag%s #%s — %s — %s",
                 f" [{label}]" if label else "", t, size, why)
    return [t for t in tags if t in picked]


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
