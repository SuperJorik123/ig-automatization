"""
modules/telegram/news_bot.py — group-triggered news broadcaster.

Post a photo/video/album with a caption (or a plain text message) into the
control group (TELEGRAM_CHAT_ID). The bot replies with a channel picker
listing every TG_DESTINATIONS channel — and, when the post contains a video,
every YT_DESTINATIONS YouTube channel too. Toggle the ones you want (All /
None shortcuts included) and hit "Post to selected" — the caption is
translated into each destination's language and the post fans out: Telegram
channels get the message/media, YouTube channels get the video uploaded as a
Short (caption first line → title, full caption → description).

URL posts: drop an Instagram reel / Twitter-X status link instead of a file.
The bot downloads the media via shared/reel_downloader (yt-dlp), then shows
the same picker. Caption rule: any text you wrote around the URL wins; a
bare-URL post uses the link's own caption — the picker says which one so you
can check before posting. The file is local, so nothing has to be fetched.

Brand-it: a single-video post (uploaded or URL) with BRANDS configured first
asks "Post as-is / Brand it". Brand-it renders one variant per selected brand
— brands/<name>/logo.png top-right, the caption as a translated lower-third
headline (shared/branding.py) — sends each back here, then offers a publish
picker of brand→TG/YT/X pairs (all off; YouTube hidden over 3 minutes).
Nothing publishes without a selection.

Caption edit: reply to any open picker message with new text to replace the
caption before hitting "Post to selected".

Big files: posting to Telegram channels has never had a size limit (the bot
re-sends the file_id, so the bytes never leave Telegram). Branding and the
YouTube leg do need the file on disk, and the Bot API refuses to hand over
anything past 20 MB. Those two go through the MTProto user client instead
(modules/telegram/mtproto.py — up to 2 GB), which needs one login:

    py modules/telegram/mtproto.py --login

Without it the old 20 MB ceiling applies to those two legs, and the bot says
so instead of failing vaguely.

Weekly cleanup: every message the bot sees or sends in the control group is
recorded (queue_store.group_messages), and Mondays at 04:00 local the group is
wiped — open reaction asks closed as skipped, tracked messages deleted
(modules/telegram/cleanup.py). Give the bot the "Delete messages" admin right
or it can only remove its own recent ones. Destination channels are untouched.

Autopilot: this process also hosts the Telegram drip
(modules/telegram/autopilot.py) as a JobQueue job — every random 18-24 hours it
publishes the best story the collector+dispatcher have queued (the finalists
are compared head-to-head at post time) to the channels whose region it
matches, then asks here which reactions to buy. It shares this process because
one bot token allows exactly one poller and the ask needs callbacks. Manual
posting above is unaffected by it.

    /queue      what's queued + what the next drip would post
    /autopilot  on|off — pause or resume the drip
    /asks       re-render unanswered reaction asks
    /next       run one drip immediately

Run:  py modules/telegram/news_bot.py
"""

import asyncio
import datetime as dt
import logging
import os
import sys

# Make the repo root importable so `from shared` / `from modules` resolve when
# run directly (`py modules/telegram/news_bot.py`).
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update  # noqa: E402
from telegram.ext import (  # noqa: E402
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from shared import config, reel_downloader  # noqa: E402
from modules.telegram import (  # noqa: E402
    autopilot,
    cleanup,
    mtproto,
    publisher,
    queue_store,
    reactions,
)
from modules.youtube import publisher as yt_publisher  # noqa: E402
from modules.telegram import branded, translator  # noqa: E402
from modules.youtube import shorts_format, uploader as yt_uploader  # noqa: E402
from shared import branding  # noqa: E402
from shared.monitoring import errmail, heartbeat  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# python-telegram-bot's httpx client logs every 10 s getUpdates poll at INFO,
# which buries anything interesting. Errors still come through.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("news_bot")

if not config.NEWS_BOT_TOKEN:
    raise SystemExit("NEWS_BOT_TOKEN / TELEGRAM_BOT_TOKEN missing in .env")
if not config.TELEGRAM_CHAT_ID:
    raise SystemExit("TELEGRAM_CHAT_ID missing in .env — the control group's numeric id")
try:
    CHAT_ID = int(config.TELEGRAM_CHAT_ID)
except ValueError as exc:
    raise SystemExit(
        f"TELEGRAM_CHAT_ID must be an integer, got {config.TELEGRAM_CHAT_ID!r}"
    ) from exc
if not config.TG_DESTINATIONS and not config.YT_DESTINATIONS:
    raise SystemExit(
        "No destinations in .env — set TG_DESTINATIONS (chat_id:lang) and/or "
        "YT_DESTINATIONS (yt_account:lang)"
    )

# Bot API refuses get_file above this size. Past it we go through the MTProto
# user client (modules/telegram/mtproto.py, up to 2 GB); these two constants
# only decide WHICH transport is used, and are the hard ceiling when the user
# client isn't logged in.
_BOT_FILE_LIMIT = 20 * 1024 * 1024

# Bot API refuses uploads above this; a bigger render goes back into the group
# through the user client instead, and publishes either way.
_BOT_UPLOAD_LIMIT = 50 * 1024 * 1024


# --------------------------------------------------------------------------- #
# Pending prompts + album buffering                                           #
# --------------------------------------------------------------------------- #

# Maps the message_id of a picker prompt the bot sent → the post waiting on it:
# {"text": str, "cap_src": str|None, "media": [{"file_id"|"path","type",...}...],
#  "files": [downloaded paths to delete when the picker closes],
#  "sel_tg": set[int], "sel_yt": set[int], "sel_em": set[int]}.
# sel_tg indexes TG_DESTINATIONS, sel_yt indexes YT_DESTINATIONS (YouTube rows
# are only offered when the post contains a video), sel_em indexes EMOJI_SERVICES.
# cap_src says where the caption came from for URL posts ("your text" /
# "from link" / "edited").
# In-memory only; a bot restart expires open pickers (tapping one then says
# so) — main() sweeps the orphaned downloads on startup.
_pending = {}

# Extensions yt-dlp may hand back for a video; anything else from a URL is
# treated as a photo.
_VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".webm", ".mkv"}

# Telegram delivers an album as separate messages sharing a media_group_id.
# Buffer the parts and flush after a short quiet period so one album produces
# one picker instead of N.
_albums = {}
_ALBUM_SETTLE_S = 1.5


def _extract_media(msg):
    """file_id + kind for a photo/video message, else None (text-only).

    chat_id/msg_id ride along because a Bot-API file_id is useless to the
    MTProto user client that fetches oversized videos — it re-reads the
    original message instead (see modules/telegram/mtproto.py)."""
    src = {"chat_id": msg.chat_id, "msg_id": msg.message_id}
    if msg.photo:
        return {"file_id": msg.photo[-1].file_id, "type": "photo", **src}
    if msg.video:
        return {
            "file_id": msg.video.file_id,
            "type": "video",
            "file_size": msg.video.file_size or 0,
            **src,
        }
    return None


def _track(msg):
    """Record a control-group message for the weekly cleanup (incoming AND
    outgoing — the Bot API can't list history, so anything not recorded here
    survives the wipe). Returns `msg` so send sites can wrap in place."""
    if msg is not None and msg.chat.id == CHAT_ID:
        queue_store.track_group_message(CHAT_ID, msg.message_id)
    return msg


def _has_video(media: list) -> bool:
    return any(m["type"] == "video" for m in media)


# The emoji catalogue and the BulkFollows ordering rules live in
# modules/telegram/reactions.py — shared with the autopilot's reaction ask so
# the two flows can't drift apart.
EMOJI_SERVICES = reactions.EMOJI_SERVICES


def _keyboard(state: dict) -> InlineKeyboardMarkup:
    """Channel picker: one row per Telegram destination ("t:<i>" indexes
    TG_DESTINATIONS) plus, for posts that contain a video, one row per YouTube
    channel ("y:<i>" indexes YT_DESTINATIONS). Indices keep callback_data tiny
    — Telegram caps it at 64 bytes."""
    rows = []
    for i, dest in enumerate(config.TG_DESTINATIONS):
        mark = "☑" if i in state["sel_tg"] else "☐"
        label = f"{mark} {dest['chat_id']}"
        if dest["lang"]:
            label += f" · {dest['lang']}"
        rows.append([InlineKeyboardButton(label, callback_data=f"t:{i}")])
    if _has_video(state["media"]):
        for i, dest in enumerate(config.YT_DESTINATIONS):
            mark = "☑" if i in state["sel_yt"] else "☐"
            label = f"{mark} ▶️ YT {dest['chat_id']}"
            if dest["lang"]:
                label += f" · {dest['lang']}"
            rows.append([InlineKeyboardButton(label, callback_data=f"y:{i}")])
    # Reactions ("e:<i>" indexes EMOJI_SERVICES), two per row so the labels stay
    # readable. Optional: submitting with none selected just skips them.
    for i in range(0, len(EMOJI_SERVICES), 2):
        row = []
        for j, em in enumerate(EMOJI_SERVICES[i:i + 2], start=i):
            mark = "☑" if j in state["sel_em"] else "☐"
            row.append(InlineKeyboardButton(f"{mark} {reactions.face(em)}",
                                            callback_data=f"e:{j}"))
        rows.append(row)
    rows.append([
        InlineKeyboardButton("All", callback_data="all"),
        InlineKeyboardButton("None", callback_data="none"),
    ])
    rows.append([InlineKeyboardButton("▶ Post to selected", callback_data="submit")])
    rows.append([InlineKeyboardButton("✕ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def _prompt_text(state: dict) -> str:
    """Picker message body: what's being posted, the caption that will go out
    (with its origin when the operator didn't type it), and the reply-to-edit
    hint."""
    n_media = len(state["media"])
    what = f"{n_media} media" if n_media else "text post"
    lines = [f"Post this ({what}) to which channels?"]
    if state["text"]:
        preview = state["text"]
        # Show the caption in full — the preview is how the operator verifies
        # what goes out. Trim only to fit Telegram's 4096-char message cap.
        if len(preview) > 3800:
            preview = preview[:3800] + "…"
        src = state.get("cap_src")
        lines.append(f"📝{f' ({src})' if src else ''} {preview}")
    lines.append("↩️ reply to this message to replace the caption")
    return "\n\n".join(lines)


async def _prompt(msg, text: str, media: list) -> None:
    """Reply to the news message with the channel picker."""
    state = {"text": text, "media": media, "files": [],
             "sel_tg": set(), "sel_yt": set(), "sel_em": set()}
    prompt = _track(await msg.reply_text(_prompt_text(state),
                                         reply_markup=_keyboard(state)))
    _pending[prompt.message_id] = state


async def _gate(msg, text: str, media: list, files: list | None = None) -> None:
    """Single-video post with brands configured: ask as-is vs brand-it before
    opening any picker. State is the same _pending dict, mode-tagged."""
    state = {"text": text, "media": media, "files": list(files or ()),
             "sel_tg": set(), "sel_yt": set(), "sel_em": set(), "mode": "gate"}
    prompt = _track(await msg.reply_text(
        "Brand this clip, or post it as-is?" + (f"\n\n📝 {text}" if text else ""),
        reply_markup=branded.gate_keyboard()))
    _pending[prompt.message_id] = state


def _cleanup(state: dict) -> None:
    """Remove files downloaded for this picker (URL posts). file_id-based
    media has nothing on disk."""
    for path in state.get("files", ()):
        try:
            os.remove(path)
        except OSError:
            pass


async def _handle_url(msg, text: str) -> None:
    """URL post: download the media, then show the regular picker. Caption
    rule: the operator's text around the URL wins; a bare URL falls back to
    the link's own caption."""
    m = reel_downloader.SUPPORTED_URL_RE.search(text)
    url = m.group(0)
    # Strip the WHOLE pasted token from the operator text — the regex stops at
    # the shortcode, but pastes usually carry a ?igsh=…/&s=… tracking tail
    # that must not survive as "your text".
    start, end = m.start(), m.end()
    while end < len(text) and not text[end].isspace():
        end += 1
    user_text = (text[:start] + text[end:]).strip()

    note = _track(await msg.reply_text("⏳ downloading …"))
    dest = os.path.join(config.TG_DATA_DIR, "media", f"manual_url_{msg.message_id}.mp4")
    try:
        # yt-dlp / gallery-dl are blocking — keep the event loop free.
        paths, link_caption = await asyncio.to_thread(
            reel_downloader.download_any, url, dest)
    except Exception as exc:
        log.error("URL download failed for %s: %s", url, exc)
        await note.edit_text(f"❌ download failed: {str(exc)[:300]}")
        return

    is_video = (len(paths) == 1
                and os.path.splitext(paths[0])[1].lower() in _VIDEO_EXTS)
    state = {
        "text": user_text or link_caption,
        "cap_src": "your text" if user_text else "from link",
        # One media item per file: a photo carousel becomes a Telegram album
        # via the existing publisher album path.
        "media": [{"path": p, "type": "video" if is_video else "photo"}
                  for p in paths],
        "files": list(paths),
        # Twitter clips carry hairline edge artifacts; the brand render reads
        # this to decide the edge-trim.
        "origin": "twitter" if reel_downloader.is_twitter_url(url) else "instagram",
        "sel_tg": set(),
        "sel_yt": set(),
        "sel_em": set(),
    }
    if is_video and config.BRANDS:
        state["mode"] = "gate"
        cap = state["text"]
        await note.edit_text(
            "Brand this clip, or post it as-is?" + (f"\n\n📝 {cap}" if cap else ""),
            reply_markup=branded.gate_keyboard())
    else:
        await note.edit_text(_prompt_text(state), reply_markup=_keyboard(state))
    _pending[note.message_id] = state


async def _flush_album(gid) -> None:
    """Fires after the album has been quiet for _ALBUM_SETTLE_S."""
    try:
        await asyncio.sleep(_ALBUM_SETTLE_S)
    except asyncio.CancelledError:
        return  # another part arrived; its timer took over
    entry = _albums.pop(gid, None)
    if entry:
        await _prompt(entry["msg"], entry["text"], entry["media"])


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A message in the control group = a news candidate. Show the picker."""
    msg = update.effective_message
    if msg is None:
        return
    _track(msg)  # the operator's own messages get wiped weekly too
    text = msg.caption or msg.text or ""

    # Reply to an open picker = replace that post's caption (any post type).
    reply = msg.reply_to_message
    if reply is not None and reply.message_id in _pending and text.strip():
        state = _pending[reply.message_id]
        state["text"] = text.strip()
        state["cap_src"] = "edited"
        mode = state.get("mode")
        if mode == "gate":
            body = f"Brand this clip, or post it as-is?\n\n📝 {state['text']}"
            markup = branded.gate_keyboard()
        elif mode == "brand":
            body = _brand_prompt_text(state)
            markup = branded.brand_keyboard(state["brands"], state["sel_brands"])
        elif mode == "publish":
            return  # headline is already burned into the renders
        else:
            body = _prompt_text(state)
            markup = _keyboard(state)
        try:
            await reply.edit_text(body, reply_markup=markup)
        except Exception:  # "message is not modified" — same caption again
            pass
        return

    media_item = _extract_media(msg)
    if not text.strip() and media_item is None:
        return  # stickers, joins, etc.

    # An IG / Twitter-X link with no attached media = URL post: download it,
    # then run the normal picker flow on the local file.
    if media_item is None and reel_downloader.is_supported_url(text):
        await _handle_url(msg, text)
        return

    gid = msg.media_group_id
    if gid is None:
        if media_item and media_item["type"] == "video" and config.BRANDS:
            await _gate(msg, text, [media_item])
        else:
            await _prompt(msg, text, [media_item] if media_item else [])
        return

    # Album part: merge into the buffer and restart the settle timer.
    entry = _albums.get(gid)
    if entry is None:
        _albums[gid] = entry = {"msg": msg, "text": text, "media": []}
    else:
        entry["task"].cancel()
        if text and not entry["text"]:
            entry["text"] = text  # caption can ride on any part
    if media_item:
        entry["media"].append(media_item)
    entry["task"] = asyncio.create_task(_flush_album(gid))


def _too_big_error(size: int) -> str:
    """Message for a video neither transport can fetch."""
    return (f"video is {mtproto.human_size(size)} and the Bot API caps "
            f"downloads at 20 MB — {mtproto.unavailable_reason()}. "
            f"Or post the clip as a URL instead.")


async def _fetch_video(bot, video: dict, dest_stem: str) -> str:
    """Local path of a post's video, downloading it if it only exists on
    Telegram. Returns the real path (the extension comes from the source).

    Transport order: a URL post is already on disk; anything the user client
    can reach comes down through MTProto at up to 2 GB; the Bot API's 20 MB
    `get_file` is the fallback for when that client isn't logged in."""
    if video.get("path"):  # URL post — nothing to fetch
        return video["path"]

    size = video.get("file_size") or 0
    if video.get("msg_id") and await mtproto.ensure_ready():
        try:
            return await mtproto.download(video["chat_id"], video["msg_id"],
                                          dest_stem)
        except Exception as exc:
            # Small files can still go the Bot-API way; big ones are stuck, and
            # the raise below names the real reason.
            log.warning("MTProto download failed (%s) — falling back to the "
                        "Bot API", exc)
            if size > _BOT_FILE_LIMIT:
                raise RuntimeError(f"big-file download failed: {exc}") from exc

    if size > _BOT_FILE_LIMIT:
        raise RuntimeError(_too_big_error(size))
    path = dest_stem + ".mp4"
    tg_file = await bot.get_file(video["file_id"])
    await tg_file.download_to_drive(path)
    return path


async def _post_to_youtube(bot, text: str, media: list, dests: list):
    """Upload the post's video as a Short to each selected YouTube channel
    (translated per channel). URL posts already have the file on disk; a
    Telegram-uploaded video is fetched first — see _fetch_video.
    Returns the same (posted, errors) shape as the Telegram publisher."""
    video = next((m for m in media if m["type"] == "video"), None)
    if video is None:
        return [], [(d["chat_id"], "post has no video") for d in dests]

    dl_dir = os.path.join(config.TG_DATA_DIR, "media")
    os.makedirs(dl_dir, exist_ok=True)
    # file_id chars are filename-safe (base64url alphabet); last chars suffice.
    stem = os.path.join(dl_dir, f"manual_{video['file_id'][-24:]}")
    path = None
    try:
        path = await _fetch_video(bot, video, stem)
        # Uploads are blocking network calls — keep the bot's event loop free.
        return await asyncio.to_thread(yt_publisher.publish_shorts, path, text, dests)
    except Exception as exc:  # download refusal, network, ...
        log.error("manual YouTube post failed: %s", exc)
        return [], [(d["chat_id"], str(exc)) for d in dests]
    finally:
        # Only a file WE downloaded gets removed — a URL post's file belongs to
        # the picker state and is cleaned up with it.
        if path and path != video.get("path"):
            try:
                os.remove(path)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Brand-it flow (gate → brand picker → render → publish picker)               #
# --------------------------------------------------------------------------- #


def _brand_prompt_text(state: dict) -> str:
    head = state["text"].strip()
    lines = ["Brand for which brands?"]
    lines.append(f"📝 {head}" if head
                 else "⚠️ no headline yet — reply to this message with it")
    lines.append("↩️ reply to this message to replace the headline")
    return "\n\n".join(lines)


async def _ensure_local_video(bot, state: dict) -> str:
    """Local path of the source video, downloading Telegram-uploaded ones (the
    gate already checked a transport can carry it). URL posts have a path
    already. The download is memoised on the media dict so a second render
    doesn't re-fetch a gigabyte."""
    video = next(m for m in state["media"] if m["type"] == "video")
    if video.get("path"):
        return video["path"]
    dl_dir = os.path.join(config.TG_DATA_DIR, "media")
    os.makedirs(dl_dir, exist_ok=True)
    stem = os.path.join(dl_dir, f"brand_src_{video['file_id'][-24:]}")
    path = await _fetch_video(bot, video, stem)
    video["path"] = path
    state["files"].append(path)
    return path


async def _on_brand_callback(q, context, state: dict, verb: str) -> None:
    """Taps on the gate / brand picker / publish picker ("b:<verb>")."""
    if verb == "noop":
        await q.answer("Add brands/<name>/logo.png to enable this brand.",
                       show_alert=True)
        return

    if verb == "asis":
        await q.answer()
        state["mode"] = None
        await q.edit_message_text(_prompt_text(state),
                                  reply_markup=_keyboard(state))
        return

    if verb == "brand":
        # Fail here, on the tap, rather than after a minute of rendering setup.
        video = next((m for m in state["media"] if m["type"] == "video"), None)
        size = (video or {}).get("file_size") or 0
        if video and not video.get("path") and size > _BOT_FILE_LIMIT \
                and not await mtproto.ensure_ready():
            # answerCallbackQuery caps its text at 200 chars.
            await q.answer(("Too large to brand: " + _too_big_error(size))[:200],
                           show_alert=True)
            return
        await q.answer()
        state["mode"] = "brand"
        state["brands"] = branded.available_brands(config.BRANDS)
        state["sel_brands"] = {i for i, b in enumerate(state["brands"])
                               if b["has_logo"]}
        await q.edit_message_text(
            _brand_prompt_text(state),
            reply_markup=branded.brand_keyboard(state["brands"],
                                                state["sel_brands"]))
        return

    if verb.startswith("t:") and state.get("mode") == "brand":
        await q.answer()
        state["sel_brands"] ^= {int(verb.split(":", 1)[1])}
        await q.edit_message_reply_markup(
            branded.brand_keyboard(state["brands"], state["sel_brands"]))
        return

    if verb == "render" and state.get("mode") == "brand":
        if not state["sel_brands"]:
            await q.answer("Pick at least one brand.", show_alert=True)
            return
        if not state["text"].strip():
            await q.answer("No headline — reply to the picker message with "
                           "it first.", show_alert=True)
            return
        await q.answer()
        await _do_render(q, context, state)
        return

    if verb.startswith("p:") and state.get("mode") == "publish":
        await q.answer()
        state["sel_pairs"] ^= {int(verb.split(":", 1)[1])}
        await q.edit_message_reply_markup(
            branded.publish_keyboard(state["pairs"], state["sel_pairs"]))
        return

    if verb == "publish" and state.get("mode") == "publish":
        if not state["sel_pairs"]:
            await q.answer("Pick at least one destination.", show_alert=True)
            return
        await q.answer()
        await _do_publish(q, context, state)
        return

    if verb == "cancel":
        await q.answer()
        _pending.pop(q.message.message_id, None)
        _cleanup(state)
        await q.edit_message_text("✕ cancelled")
        return

    await q.answer()  # stale/unknown verb for this mode


async def _do_render(q, context, state: dict) -> None:
    """Render one variant per selected brand (headline translated per brand
    language, translation cached), send each back into the group, then open
    the publish picker on a fresh message. All brands render in ONE ffmpeg
    pass (the blur-fill canvas is built once and split per brand); if that
    combined pass fails it falls back to one render per brand, so a single
    failed brand is still reported and skipped — never fatal for the others."""
    brands = [state["brands"][i] for i in sorted(state["sel_brands"])]
    await q.edit_message_text(
        "⏳ rendering " + ", ".join(b["name"] for b in brands) + " …")

    try:
        src = await _ensure_local_video(context.bot, state)
        _, _, duration = await asyncio.to_thread(shorts_format.probe, src)
    except Exception as exc:
        log.error("brand render setup failed: %s", exc)
        _pending.pop(q.message.message_id, None)
        _cleanup(state)
        await q.edit_message_text(f"❌ can't read the video: {str(exc)[:300]}")
        return

    media_dir = os.path.join(config.TG_DATA_DIR, "media")
    renders, failures, warnings = [], [], []
    cache: dict[str, str] = {}

    # Translate first: a brand whose headline can't be produced is reported
    # and dropped here, before it can sink the combined render below.
    jobs = []
    for b in brands:
        try:
            lang = b["lang"]
            if lang not in cache:
                cache[lang] = (await asyncio.to_thread(
                    translator.translate, state["text"], lang,
                    config.SOURCE_LANG) if lang else state["text"])
            out = os.path.join(
                media_dir, f"brand_{q.message.message_id}_{b['name']}.mp4")
            jobs.append({"brand": b, "headline": cache[lang],
                         "logo_path": b["logo"], "out_path": out})
        except Exception as exc:
            log.error("brand render failed for %s: %s", b["name"], exc)
            failures.append((b["name"], str(exc)[:200]))

    # One ffmpeg pass for every brand. All-or-nothing by design, so a failure
    # (one bad logo, one broken style.json) retries brand-by-brand — the slow
    # path, but the one where the healthy brands still come out.
    if jobs:
        try:
            paths = await asyncio.to_thread(
                branding.render_branded_multi, src,
                [{k: j[k] for k in ("headline", "logo_path", "out_path")}
                 for j in jobs])
            for j, path in zip(jobs, paths):
                state["files"].append(path)
                renders.append({"brand": j["brand"], "path": path,
                                "headline": j["headline"]})
        except Exception as exc:
            log.warning("combined brand render failed (%s) — retrying "
                        "brand-by-brand", str(exc)[:200])
            for j in jobs:
                try:
                    path = await asyncio.to_thread(
                        branding.render_branded, src, j["headline"],
                        j["logo_path"], j["out_path"])
                except Exception as exc2:  # translator, ffmpeg — render failed
                    log.error("brand render failed for %s: %s",
                              j["brand"]["name"], exc2)
                    failures.append((j["brand"]["name"], str(exc2)[:200]))
                    continue
                state["files"].append(path)
                renders.append({"brand": j["brand"], "path": path,
                                "headline": j["headline"]})

    # A render that succeeded is publishable regardless of whether the
    # preview send below works — a failed send must not knock it out of
    # `renders` (that would make the summary and the publish picker
    # disagree about what's available).
    for r in renders:
        b, path = r["brand"], r["path"]
        try:
            size = os.path.getsize(path)
            if size > _BOT_UPLOAD_LIMIT and await mtproto.ensure_ready():
                # Past the Bot API's 50 MB upload cap the preview comes from
                # your own account instead — the clip still lands in the group.
                # Not _track()ed: it's a Telethon message, and the weekly wipe
                # deletes through the BOT, which can't remove your own posts.
                await mtproto.send_video(q.message.chat.id, path,
                                         f"🏷 {b['name']}",
                                         branding.OUT_W, branding.OUT_H,
                                         int(duration))
            elif size > _BOT_UPLOAD_LIMIT:
                _track(await q.message.chat.send_message(
                    f"🏷 {b['name']}: rendered ({mtproto.human_size(size)} — "
                    f"too big to send back, {mtproto.unavailable_reason()}; "
                    "publishing still works)"))
            else:
                with open(path, "rb") as fh:
                    # width/height matter: without them Telegram sizes the
                    # inline player from defaults and plays the clip squashed.
                    _track(await q.message.chat.send_video(
                        video=fh, caption=f"🏷 {b['name']}",
                        width=branding.OUT_W, height=branding.OUT_H,
                        duration=int(duration)))
        except Exception as exc:  # Telegram send only — the render already succeeded
            log.error("brand preview send failed for %s: %s", b["name"], exc)
            warnings.append((b["name"], str(exc)[:200]))

    if not renders:
        _pending.pop(q.message.message_id, None)
        _cleanup(state)
        await q.edit_message_text(
            "❌ all renders failed:\n"
            + "\n".join(f"{n}: {e}" for n, e in failures))
        return

    state["mode"] = "publish"
    state["renders"] = renders
    state["pairs"] = branded.pairs_for(renders, duration)
    state["sel_pairs"] = set()

    summary = "🎨 rendered: " + ", ".join(r["brand"]["name"] for r in renders)
    if warnings:
        summary += "\n" + "\n".join(
            f"⚠️ {n}: rendered, preview send failed: {e}" for n, e in warnings)
    if failures:
        summary += "\n" + "\n".join(f"❌ {n}: {e}" for n, e in failures)

    # Everything below can fail on a network blip. If it does, the rendered
    # files (and the downloaded source) must not be orphaned with no picker
    # and no owner — clean up and surface an error instead of leaving them
    # for the next restart's sweep to eventually find.
    try:
        await q.edit_message_text(summary)
        if not state["pairs"]:
            _track(await q.message.chat.send_message(
                "no destinations configured for the rendered brands "
                "(BRAND_<NAME>_TG/YT/TW) — files above are yours, nothing to publish"))
            _pending.pop(q.message.message_id, None)
            _cleanup(state)
            return
        # Fresh message so the publish picker lands BELOW the delivered clips.
        # Sent BEFORE the old _pending entry is popped/reassigned, so a failed
        # send here is caught below with the state (and its files) still
        # intact to clean up, instead of orphaning everything silently.
        prompt = _track(await q.message.chat.send_message(
            "Publish which?", reply_markup=branded.publish_keyboard(
                state["pairs"], set())))
    except Exception as exc:
        log.error("brand publish-picker handoff failed: %s", exc)
        _pending.pop(q.message.message_id, None)
        _cleanup(state)
        try:
            _track(await q.message.chat.send_message(
                f"❌ rendered but couldn't open the publish picker "
                f"({str(exc)[:200]}) — temp files removed"))
        except Exception:
            pass
        return

    _pending.pop(q.message.message_id, None)
    _pending[prompt.message_id] = state


async def _do_publish(q, context, state: dict) -> None:
    """Push each selected pair through its platform publisher. The headline is
    already translated per brand — Telegram destinations get lang "" so
    publisher.publish doesn't translate again."""
    pairs = [state["pairs"][i] for i in sorted(state["sel_pairs"])]
    _pending.pop(q.message.message_id, None)
    await q.edit_message_text(
        "⏳ publishing " + ", ".join(p["label"] for p in pairs) + " …")

    lines = []
    for p in pairs:
        r, b = p["render"], p["render"]["brand"]
        try:
            if p["platform"] == "tg":
                posted, errors, links = await publisher.publish(
                    context.bot, r["headline"],
                    [{"path": r["path"], "type": "video"}],
                    [{"chat_id": b["tg"], "lang": ""}])
                if posted:
                    # Same post-publish path as the manual picker (BulkFollows
                    # per-post order + durable every-5th channel counter).
                    await asyncio.to_thread(reactions.record_posts,
                                            posted, links, [])
                    lines.append(f"✅ {p['label']}")
                    for chat_id, url in links:
                        lines.append(f"🔗 {chat_id}: {url}")
                for _, err in errors:
                    lines.append(f"❌ {p['label']}: {err}")
            elif p["platform"] == "yt":
                # Renders are 1080x1920 by construction — upload directly,
                # no second ensure_short pass.
                title, description = yt_publisher.split_caption(r["headline"])
                result = await asyncio.to_thread(
                    yt_uploader.upload_short, r["path"], title, description,
                    b["yt"])
                if result.get("status") == "success":
                    lines.append(f"✅ {p['label']}")
                else:
                    lines.append(f"❌ {p['label']}: "
                                 f"{result.get('error', 'unknown error')}")
            else:  # tw — lazy import so tweepy isn't a startup requirement
                from modules.twitter import poster as tw_poster
                result = await asyncio.to_thread(
                    tw_poster.post_media, r["path"], r["headline"], b["tw"])
                if result.get("status") == "success":
                    lines.append(f"✅ {p['label']}")
                else:
                    lines.append(f"❌ {p['label']}: "
                                 f"{result.get('error', 'unknown error')}")
        except Exception as exc:
            log.error("brand publish failed for %s: %s", p["label"], exc)
            lines.append(f"❌ {p['label']}: {str(exc)[:200]}")

    _cleanup(state)
    await q.edit_message_text("\n".join(lines))


# --------------------------------------------------------------------------- #
# Reaction asks (autopilot posts)                                             #
# --------------------------------------------------------------------------- #


async def _on_ask_callback(q, context) -> None:
    """Taps on an autopilot reaction ask ("r:<ask_id>:<verb>").

    All the state is in SQLite and the ask id rides in the callback data, so
    these keep working across a bot restart — unlike the manual picker, whose
    state is deliberately in-memory and short-lived."""
    try:
        _, raw_id, verb = q.data.split(":", 2)
        ask_id = int(raw_id)
    except ValueError:
        await q.answer()
        return

    ask = queue_store.get_ask(ask_id)
    if ask is None or ask["status"] != "open":
        await q.answer("That ask is already closed.", show_alert=True)
        return
    await q.answer()

    state, action = reactions.reduce(ask["state"], verb)
    queue_store.save_ask_state(ask_id, state)

    if action is None:
        text, keyboard = reactions.render(ask_id, state)
        try:
            await q.edit_message_text(text, reply_markup=keyboard,
                                      disable_web_page_preview=True)
        except Exception:  # "message is not modified" — same tap twice
            pass
        return

    if action == "apply":
        # Ordering is blocking HTTP — keep the bot's event loop free.
        n = await asyncio.to_thread(reactions.apply_orders, state)
        queue_store.close_ask(ask_id, "applied")
        await q.edit_message_text(f"{reactions.summary(state, True)}\n({n} order(s) placed)")
    else:
        queue_store.close_ask(ask_id, "skipped")
        await q.edit_message_text(reactions.summary(state, False))


# --------------------------------------------------------------------------- #
# Commands                                                                    #
# --------------------------------------------------------------------------- #


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/queue — what the smart filter is holding and what goes out next."""
    # Commands bypass on_message (~filters.COMMAND), so each one tracks its own
    # trigger message for the weekly wipe.
    _track(update.effective_message)
    counts = queue_store.counts()
    # compare=False: a status command shouldn't burn a model call. The real
    # pick is made head-to-head at post time, so this is the front-runner.
    item, targets = autopilot.pick(compare=False)
    lines = [
        "📊 " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "empty"),
        f"autopilot: {'on' if autopilot.is_enabled() else 'paused'} · "
        f"every {config.TG_DRIP_MIN_S // 3600}-{config.TG_DRIP_MAX_S // 3600} h · "
        f"score ≥ {config.TG_AUTO_MIN_SCORE} · max age {config.TG_MAX_AGE_H}h",
        "",
        "front-runner: " + (autopilot.describe(item, targets) if item else "nothing eligible"),
    ]
    if item:
        lines.append(f"(final pick compared across the top {config.TG_COMPARE_TOP} at post time)")
    per_channel = queue_store.channel_post_counts()
    if per_channel:
        lines.append(
            f"\n📣 posts per channel (channel order every {reactions.CHANNEL_POST_THRESHOLD}): "
            + ", ".join(f"{k} {v}" for k, v in per_channel.items())
        )
    open_n = len(queue_store.open_asks())
    if open_n:
        lines.append(f"\n🎛 {open_n} reaction ask(s) waiting — /asks")
    _track(await update.effective_message.reply_text("\n".join(lines)))


async def cmd_autopilot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/autopilot [on|off] — pause or resume the drip without a restart."""
    _track(update.effective_message)
    arg = (context.args[0].lower() if context.args else "")
    if arg in ("on", "off"):
        autopilot.set_enabled(arg == "on")
    _track(await update.effective_message.reply_text(
        f"autopilot is {'on' if autopilot.is_enabled() else 'paused'}"
        + ("" if arg in ("on", "off") else "  (use /autopilot on|off to change)")
    ))


async def cmd_asks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/asks — re-render every unanswered reaction ask, including any whose
    message never made it out because the bot died mid-tick."""
    _track(update.effective_message)
    asks = queue_store.open_asks()
    if not asks:
        _track(await update.effective_message.reply_text("no open reaction asks"))
        return
    for ask in asks:
        await autopilot.refresh_ask(context.bot, ask)
    _track(await update.effective_message.reply_text(f"re-sent {len(asks)} open ask(s)"))


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/next — run one drip right now. The scheduled tick is untouched."""
    _track(update.effective_message)
    msg = _track(await update.effective_message.reply_text("⏳ running one drip …"))
    await msg.edit_text(await autopilot.tick(context.bot))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle taps on the channel picker and on autopilot reaction asks."""
    q = update.callback_query
    if q.message is None:
        await q.answer()
        return

    # Asks may live in a different chat (TG_ASK_CHAT_ID) than the control
    # group, so they're routed before the control-group check below.
    if (q.data or "").startswith("r:"):
        await _on_ask_callback(q, context)
        return

    if q.message.chat.id != CHAT_ID:
        await q.answer()
        return

    state = _pending.get(q.message.message_id)
    if state is None:
        await q.answer()
        await q.edit_message_text("(prompt expired — post the news again)")
        return

    data = q.data

    if data.startswith("b:"):
        await _on_brand_callback(q, context, state, data[2:])
        return

    if data == "submit" and not (state["sel_tg"] or state["sel_yt"]):
        await q.answer("Pick at least one channel.", show_alert=True)
        return

    await q.answer()

    if data.startswith("t:"):
        state["sel_tg"] ^= {int(data.split(":", 1)[1])}  # toggle
        await q.edit_message_reply_markup(_keyboard(state))
        return

    if data.startswith("y:"):
        state["sel_yt"] ^= {int(data.split(":", 1)[1])}  # toggle
        await q.edit_message_reply_markup(_keyboard(state))
        return

    if data.startswith("e:"):
        state["sel_em"] ^= {int(data.split(":", 1)[1])}  # toggle
        await q.edit_message_reply_markup(_keyboard(state))
        return

    if data == "all":
        state["sel_tg"] = set(range(len(config.TG_DESTINATIONS)))
        if _has_video(state["media"]):
            state["sel_yt"] = set(range(len(config.YT_DESTINATIONS)))
        await q.edit_message_reply_markup(_keyboard(state))
        return

    # "All" is about channels only — nobody wants all eight reactions at once.
    # "None" is a full reset, reactions included.
    if data == "none":
        state["sel_tg"] = set()
        state["sel_yt"] = set()
        state["sel_em"] = set()
        await q.edit_message_reply_markup(_keyboard(state))
        return

    if data == "cancel":
        _pending.pop(q.message.message_id, None)
        _cleanup(state)
        await q.edit_message_text("✕ cancelled")
        return

    if data == "submit":
        dests_tg = [config.TG_DESTINATIONS[i] for i in sorted(state["sel_tg"])]
        dests_yt = [config.YT_DESTINATIONS[i] for i in sorted(state["sel_yt"])]
        emojis = [EMOJI_SERVICES[i] for i in sorted(state["sel_em"])]
        _pending.pop(q.message.message_id, None)
        names = [d["chat_id"] for d in dests_tg] + [f"YT {d['chat_id']}" for d in dests_yt]
        note = "⏳ posting to " + ", ".join(names)
        if emojis:
            note += "  |  " + " ".join(e.get("label") or e["emoji"] for e in emojis)
        await q.edit_message_text(note + " …")

        lines = []
        if dests_tg:
            posted, errors, links = await publisher.publish(
                context.bot, state["text"], state["media"], dests_tg
            )
            if posted:
                # Ordering is blocking HTTP — keep the bot's event loop free.
                await asyncio.to_thread(reactions.record_posts, posted, links, emojis)
                lines.append("✅ posted to " + ", ".join(posted))
                for chat_id, url in links:
                    lines.append(f"🔗 {chat_id}: {url}")
            for chat_id, err in errors:
                lines.append(f"❌ {chat_id}: {err}")

        if dests_yt:
            posted, errors = await _post_to_youtube(
                context.bot, state["text"], state["media"], dests_yt
            )
            if posted:
                lines.append("✅ YouTube: " + ", ".join(posted))
            for account, err in errors:
                lines.append(f"❌ YT {account}: {err}")

        _cleanup(state)
        await q.edit_message_text("\n".join(lines))


def _sweep_orphans() -> None:
    """Picker state is memory-only, so URL downloads left behind by a restart
    have no owner — remove them."""
    dl_dir = os.path.join(config.TG_DATA_DIR, "media")
    if not os.path.isdir(dl_dir):
        return
    for name in os.listdir(dl_dir):
        if name.startswith(("manual_", "brand_")):
            try:
                os.remove(os.path.join(dl_dir, name))
            except OSError:
                pass


async def _weekly_cleanup_job(context) -> None:
    """Mondays 04:00 local: wipe the control group (tracked messages + open
    asks). Needs the "Delete messages" admin right for the operator's own
    messages."""
    try:
        deleted, failed = await cleanup.wipe_chat(context.bot, CHAT_ID)
        log.info("weekly cleanup: %d deleted, %d failed", deleted, failed)
    except Exception:
        log.exception("weekly cleanup crashed — next Monday retries")


async def _heartbeat_job(context) -> None:
    """Dead-man's switch: prove the event loop is alive to healthchecks.io.
    Running as a JobQueue job is the point — a wedged loop stops the pings."""
    await asyncio.to_thread(heartbeat.ping, config.HEALTHCHECK_URL_NEWSBOT)


async def _on_start(app) -> None:
    """Queue the autopilot's first tick once the event loop is running, register
    the weekly cleanup, and connect the big-file client so its state is known
    (and logged) before the first post rather than discovered mid-upload."""
    autopilot.schedule(app)
    if app.job_queue is not None and config.HEALTHCHECK_URL_NEWSBOT:
        app.job_queue.run_repeating(_heartbeat_job, interval=heartbeat.INTERVAL_S,
                                    first=10, name="heartbeat")
        log.info("heartbeat: pinging healthchecks every %ds", heartbeat.INTERVAL_S)
    if app.job_queue is not None:
        # A tz-aware time is required for "local" — a naive one is read as UTC.
        # See cleanup.MONDAY for the day-index trap.
        local_tz = dt.datetime.now().astimezone().tzinfo
        app.job_queue.run_daily(_weekly_cleanup_job,
                                time=dt.time(4, 0, tzinfo=local_tz),
                                days=(cleanup.MONDAY,), name="weekly-cleanup")
        log.info("weekly control-group cleanup scheduled: Mondays 04:00 %s", local_tz)
    else:
        log.warning("no job queue — weekly cleanup NOT scheduled "
                    "(install python-telegram-bot[job-queue])")
    if await mtproto.ensure_ready():
        log.info("big files: on — videos up to 2 GB can be branded and sent to YouTube")
    else:
        log.info("big files: off (%s) — videos over 20 MB can only be posted "
                 "to Telegram channels", mtproto.unavailable_reason())


async def _on_shutdown(app) -> None:
    """Release the Telethon session file on the way out."""
    await mtproto.close()


def main() -> None:
    errmail.install("news_bot")  # every logged ERROR -> one email to the operator
    _sweep_orphans()
    queue_store.init()  # the autopilot reads/writes the same DB as the collector
    app = (Application.builder().token(config.NEWS_BOT_TOKEN)
           .post_init(_on_start).post_shutdown(_on_shutdown).build())
    control = filters.Chat(CHAT_ID)
    app.add_handler(CommandHandler("queue", cmd_queue, filters=control))
    app.add_handler(CommandHandler("autopilot", cmd_autopilot, filters=control))
    app.add_handler(CommandHandler("asks", cmd_asks, filters=control))
    app.add_handler(CommandHandler("next", cmd_next, filters=control))
    # Chat filter limits the bot to the control group; ~COMMAND skips /commands.
    app.add_handler(MessageHandler(control & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(on_callback))
    log.info(
        "news bot up: listening in chat %s, %d telegram channel(s): %s | %d youtube channel(s): %s",
        CHAT_ID,
        len(config.TG_DESTINATIONS),
        ", ".join(
            f"{d['chat_id']}({d['lang'] or 'raw'}/{'+'.join(sorted(d['regions'])) or 'all'})"
            for d in config.TG_DESTINATIONS
        ) or "—",
        len(config.YT_DESTINATIONS),
        ", ".join(f"{d['chat_id']}({d['lang'] or 'raw'})" for d in config.YT_DESTINATIONS) or "—",
    )
    log.info(
        "autopilot %s: score >= %d, every %.1f-%.1f h, top %d compared at post time, asks in chat %s",
        "on" if autopilot.is_enabled() else "paused",
        config.TG_AUTO_MIN_SCORE, config.TG_DRIP_MIN_S / 3600, config.TG_DRIP_MAX_S / 3600,
        config.TG_COMPARE_TOP, autopilot.ask_chat_id(),
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
