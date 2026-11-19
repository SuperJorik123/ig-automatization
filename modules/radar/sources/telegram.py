"""Telegram channels via a USER account (bots can't read channels they don't run).

The pure half: which channels, and a Telethon message -> Item. The live half
(the client, the event handler, joining) is in `modules/radar/main.py`.
"""
import json
import time

from modules.radar import settings as S
from modules.radar.items import Item


def load_channels(path: str | None = None) -> list:
    with open(path or S.CHANNELS_FILE, encoding="utf-8") as fh:
        data = json.load(fh)
    return [c["handle"].lstrip("@") for c in data.get("channels", []) if c.get("handle")]


def _video_info(msg):
    """(is_video, size, duration) — a video upload, a round video or a
    video sent as a file all count; a GIF doesn't."""
    doc = getattr(msg, "document", None) or getattr(msg, "video", None)
    if doc is None:
        return False, 0, 0.0
    mime = (getattr(doc, "mime_type", "") or "").lower()
    duration = 0.0
    is_gif = False
    for attr in getattr(doc, "attributes", []) or []:
        name = type(attr).__name__
        if name == "DocumentAttributeVideo":
            duration = float(getattr(attr, "duration", 0) or 0)
        elif name == "DocumentAttributeAnimated":
            is_gif = True
    if not mime.startswith("video/") or is_gif:
        return False, 0, 0.0
    return True, int(getattr(doc, "size", 0) or 0), duration


def to_item(msg, username: str, chat_id: int) -> Item:
    has_video, size, duration = _video_info(msg)
    grouped = getattr(msg, "grouped_id", None)
    date = getattr(msg, "date", None)
    return Item(
        source=f"tg:@{username}",
        # One album = one item (each part arrives as its own message).
        ext_id=f"g{grouped}" if grouped else str(msg.id),
        time=date.timestamp() if date else time.time(),
        text=getattr(msg, "message", "") or "",
        link=f"https://t.me/{username}/{msg.id}",
        has_video=has_video, media_size=size, duration=duration,
        ref={"chat_id": chat_id, "msg_id": msg.id, "username": username},
    )
