"""
modules/newsroom/publish.py — send one article to its channel.

The sending core (_ref / _fit / the single-media send) is lifted from
modules/telegram/publisher.py; what is dropped is dropped because this bot's
input shape is narrower, not because it was wrong — see the DORMANT blocks.

Two things here are load-bearing:

  MEDIA BY URL   Telegram accepts a remote URL for photo= and video=, so the
                 featured image is handed over as a link and never downloaded.
                 That is what spares this bot a media directory, a disk quota,
                 and the whole MTProto big-file path the news bot needs.

  LINK LAST      The article link is appended AFTER the rewrite and only then
                 trimmed to Telegram's limit. The other order silently
                 truncates the link off the end of a long post, which fails
                 the client's step 6 while looking like a successful post.
"""

import logging

from shared import config

log = logging.getLogger(__name__)

# Telegram's hard caps. A media caption is a quarter of a plain message, and
# nearly every post here carries the article's featured image.
TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024

# What separates the post from its link.
LINK_PREFIX = "\n\n🔗 "


def _fit(text: str, has_media: bool) -> str:
    """Trim to what Telegram will accept.

    Truncating beats the alternative: an over-long caption is rejected outright
    and, for an unattended bot, that means the article simply never ships.
    Every trim is logged."""
    limit = CAPTION_LIMIT if has_media else TEXT_LIMIT
    if len(text or "") <= limit:
        return text
    log.warning("caption trimmed from %d to %d chars (%s limit)",
                len(text), limit, "caption" if has_media else "message")
    return text[: limit - 1].rstrip() + "…"


def compose(text: str, url: str, has_media: bool) -> str:
    """The final message: post body, then the article link, then trimmed.

    The link is protected from the trim by reserving its length first. Appending
    after trimming would push the result back over the limit; trimming after
    appending without the reservation would eat the link's tail — and a post
    whose link lost its last characters is a broken link that still looks like
    a successful post."""
    text = (text or "").strip()
    if not url:
        return _fit(text, has_media)
    limit = CAPTION_LIMIT if has_media else TEXT_LIMIT
    tail = f"{LINK_PREFIX}{url}"
    room = limit - len(tail)
    if room <= 0:
        # Pathological: a URL longer than the whole caption budget. The link is
        # the point of the post, so it is what survives.
        log.warning("link alone fills the %d-char limit — posting the link only", limit)
        return tail.strip()
    if len(text) > room:
        log.warning("body trimmed from %d to %d chars to protect the article link",
                    len(text), room)
        text = text[: room - 1].rstrip() + "…"
    return f"{text}{tail}"


def _media_kwargs(article: dict) -> dict:
    """photo=/video= for a remote URL, or {} for a text-only post."""
    url, kind = article.get("media_url"), article.get("media_type")
    if not url:
        return {}
    return {kind: url} if kind in ("photo", "video") else {}


async def publish(bot, site: dict, article: dict, text: str):
    """Send `text` plus the article's featured media to the site's channel.

    Returns (message_id, link) — link is the public t.me URL, which is what
    every BulkFollows order is placed against, and is None for a channel that
    yields none. Returns (None, None) in dry-run and on failure.

    Never raises: one article failing to send must not stop the tick."""
    chat_id = site["chat_id"]
    has_media = bool(article.get("media_url"))
    body = compose(text, article.get("url") or "", has_media)

    if config.NR_DRY_RUN:
        log.info("DRY RUN [%s] would post to %s (%s, %d chars):\n%s",
                 site["name"], chat_id,
                 article.get("media_type") or "text only", len(body), body)
        return None, None

    media = _media_kwargs(article)
    try:
        sent = await _send(bot, chat_id, body, media)
    except Exception as exc:
        # Telegram rejects a remote media URL more often than you would like:
        # a slow origin, a file over its 5 MB photo / 20 MB video URL ceiling,
        # a hotlink rule. The article is still news, so it ships without the
        # picture rather than not at all.
        if media:
            log.warning("[%s] media send failed (%s) — retrying as text", chat_id, exc)
            try:
                sent = await _send(bot, chat_id, compose(text, article.get("url") or "",
                                                         has_media=False), {})
            except Exception as exc2:
                log.error("[%s] publish failed: %s", chat_id, exc2)
                return None, None
        else:
            log.error("[%s] publish failed: %s", chat_id, exc)
            return None, None

    return getattr(sent, "message_id", None), getattr(sent, "link", None)


async def _send(bot, chat_id: str, text: str, media: dict):
    """One Telegram call. `media` is {} , {"photo": url} or {"video": url}."""
    if not media:
        return await bot.send_message(chat_id=chat_id, text=text)
    if "video" in media:
        return await bot.send_video(chat_id=chat_id, video=media["video"],
                                    caption=text or None)
    return await bot.send_photo(chat_id=chat_id, photo=media["photo"],
                                caption=text or None)


# --------------------------------------------------------------------------- #
# DORMANT                                                                     #
# --------------------------------------------------------------------------- #
#
# DORMANT: album send. WordPress featured media is a single item, so this flow
# never builds a media group. Re-enable together with a gallery-post feature —
# note that the caption rides on the FIRST item only, and that send_media_group
# returns a list whose first message is the one to record and order against.
#
# from telegram import InputMediaPhoto, InputMediaVideo
#
# async def _send_album(bot, chat_id, caption, media: list):
#     group = []
#     for i, m in enumerate(media):
#         cap = caption if i == 0 else None
#         if m["type"] == "video":
#             group.append(InputMediaVideo(media=m["url"], caption=cap))
#         else:
#             group.append(InputMediaPhoto(media=m["url"], caption=cap))
#     msgs = await bot.send_media_group(chat_id=chat_id, media=group)
#     return msgs[0] if msgs else None
#
# DORMANT: video dimensions. Without width/height/duration, Telegram clients
# lay the inline player out from defaults and play the video visibly squashed —
# the file is fine, only the playback box is wrong. modules/telegram/publisher
# probes local files with ffprobe (see shorts_format.probe), which this bot
# cannot do while it passes media by URL: probing would mean downloading the
# file first, and that drags in the disk and big-file handling this design
# avoids. Re-enable ONLY if the client's sites turn out to publish video and
# the squashed player is visible in practice — the cost is an ffmpeg
# dependency and a download step per video post.
#
# async def _vid_kwargs(local_path: str) -> dict:
#     w, h, dur = await asyncio.to_thread(shorts_format.probe, local_path)
#     return {"width": w, "height": h, "duration": int(dur)}
