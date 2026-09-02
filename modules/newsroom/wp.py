"""
modules/newsroom/wp.py — reads new articles out of a WordPress site.

Every WordPress install ships the REST API at /wp-json/wp/v2, so this is one
GET per site per tick and no scraping:

    GET <wp_base>/posts?per_page=20&orderby=date&order=desc&status=publish&_embed

`_embed` inlines the featured media in the same response, which is what keeps
it to one request rather than one per article.

**No date watermark.** The obvious optimisation — ?after=<last seen date> — has
a hole that loses articles permanently: WordPress lets an editor backdate a
post, and a scheduled post appears at its scheduled time. Either can land
BEHIND a watermark that has already moved past it, and it is then never seen
again. Refetching the last 20 every tick and filtering against the ids already
in the store cannot miss anything, and the overlap costs nothing because
store.add_article ignores a duplicate.

Blocking (uses `requests`) — async callers wrap it in asyncio.to_thread.
"""

import html
import logging
import re

import requests

log = logging.getLogger(__name__)

# A site that has not answered in this long is having a bad day; the tick skips
# it and tries again next time rather than holding up the other six.
TIMEOUT = 20

# How many recent posts to examine per tick. Twenty covers any realistic
# publishing burst between two five-minute polls, and the ones already stored
# cost nothing to re-see.
PER_PAGE = 20

# Tags whose content is NOT part of the prose. Dropped with their inner text
# before the rest of the markup is stripped, otherwise a stylesheet or a
# caption ends up in the article body handed to the model.
_DROP_BLOCKS = re.compile(
    r"<(script|style|figcaption)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
# Block-level boundaries become paragraph breaks so the body keeps its shape.
_BLOCK_BREAK = re.compile(r"</(p|div|li|h[1-6]|blockquote)\s*>", re.IGNORECASE)
_LINE_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_BLANK_RUN = re.compile(r"\n{3,}")


def clean_html(raw: str) -> str:
    """WordPress `rendered` fields into plain text.

    They arrive as HTML with entities (`&#8217;`, `&nbsp;`, `<p>`, shortcode
    leftovers). Both the model and Telegram want prose: unstripped markup
    wastes prompt tokens and, sent as-is, either shows literally or trips
    Telegram's HTML parser on an unclosed tag."""
    if not raw:
        return ""
    text = _DROP_BLOCKS.sub(" ", raw)
    text = _LINE_BREAK.sub("\n", text)
    text = _BLOCK_BREAK.sub("\n\n", text)
    text = _TAG.sub("", text)
    # Unescape AFTER stripping tags: doing it first would turn a literal
    # `&lt;script&gt;` in the copy into markup the stripper then eats.
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANK_RUN.sub("\n\n", text).strip()


def _media_from(post: dict) -> tuple[str | None, str | None]:
    """(url, "photo"|"video") for the post's featured image, or (None, None).

    Every level of the _embedded path is optional — an article with no featured
    media is ordinary, not an error, and simply becomes a text-only Telegram
    post."""
    try:
        media = (post.get("_embedded") or {}).get("wp:featuredmedia") or []
        first = media[0] if media else None
        if not isinstance(first, dict):
            return None, None
        # An embed that failed to resolve comes back as {"code": "...", ...}.
        url = first.get("source_url")
        if not url:
            return None, None
        mime = (first.get("mime_type") or "").lower()
        kind = first.get("media_type") or ""
        if mime.startswith("video/") or kind == "video":
            return url, "video"
        if mime.startswith("image/") or kind == "image":
            return url, "photo"
        return None, None
    except (AttributeError, IndexError, TypeError):
        return None, None


def normalise(post: dict) -> dict:
    """One REST post into the shape store.add_article expects."""
    media_url, media_type = _media_from(post)
    title = clean_html((post.get("title") or {}).get("rendered", ""))
    body = clean_html((post.get("content") or {}).get("rendered", ""))
    if not body:
        # Some sites publish excerpt-only posts, and a few block content
        # behind a plugin that empties `content.rendered` for anonymous
        # readers. The excerpt is thin but it is real copy.
        body = clean_html((post.get("excerpt") or {}).get("rendered", ""))
    # date_gmt is unambiguous; `date` is site-local with no offset attached,
    # which makes ordering across sites in different timezones wrong.
    published = (post.get("date_gmt") or "").strip()
    if published and not published.endswith("Z") and "+" not in published:
        published += "+00:00"
    return {
        "wp_id": int(post["id"]),
        "url": post.get("link") or "",
        "title": title,
        "body": body,
        "media_url": media_url,
        "media_type": media_type,
        "published_at": published,
    }


def fetch_recent(site: dict, limit: int = PER_PAGE) -> list:
    """The site's most recent published posts, normalised, newest first.

    Raises RuntimeError on anything that means "no usable answer" — the caller
    logs it and skips this site for this tick. One site being down, moved
    behind Cloudflare, or having its REST API disabled must never stop the
    other six from posting."""
    url = f"{site['wp_base']}/posts"
    params = {
        "per_page": limit,
        "orderby": "date",
        "order": "desc",
        "status": "publish",
        "_embed": "1",
    }
    try:
        resp = requests.get(url, params=params, timeout=TIMEOUT,
                            headers={"User-Agent": "newsroom-bot/1.0"})
    except requests.RequestException as exc:
        raise RuntimeError(f"{site['name']}: request failed: {exc}") from exc

    if resp.status_code != 200:
        # A REST API disabled by a plugin or a security rule answers 401/403;
        # the body is the useful part, so a slice of it goes into the message.
        raise RuntimeError(
            f"{site['name']}: HTTP {resp.status_code} from {url} — "
            f"{resp.text.strip()[:200]}"
        )

    try:
        payload = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"{site['name']}: non-JSON response from {url}") from exc

    if not isinstance(payload, list):
        # WordPress reports its own errors as an object, e.g.
        # {"code": "rest_no_route", ...} — a shape, not an exception.
        raise RuntimeError(f"{site['name']}: unexpected payload {str(payload)[:200]}")

    out = []
    for post in payload:
        try:
            out.append(normalise(post))
        except (KeyError, TypeError, ValueError) as exc:
            # One malformed entry is not worth losing the other nineteen.
            log.warning("%s: skipping unparseable post: %s", site["name"], exc)
    return out
