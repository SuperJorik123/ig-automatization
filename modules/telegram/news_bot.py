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
can check before posting. Because the file is local, URL posts skip the
20 MB Bot-API limit on the YouTube leg.

Brand-it: a single-video post (uploaded or URL) with BRANDS configured first
asks "Post as-is / Brand it". Brand-it renders one variant per selected brand
— brands/<name>/logo.png top-right, the caption as a translated lower-third
headline (shared/branding.py) — sends each back here, then offers a publish
picker of brand→TG/YT/X pairs (all off; YouTube hidden over 3 minutes).
Nothing publishes without a selection. Telegram-uploaded sources keep the
20 MB Bot-API download cap — bigger clips must come in as URLs.

Caption edit: reply to any open picker message with new text to replace the
caption before hitting "Post to selected".

YouTube limitation: the Bot API cannot download files over 20 MB, so manual
YouTube posting of Telegram-uploaded videos fails with a clear error for
bigger ones. (URL posts and the collector → dispatcher path are unaffected.)

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
    publisher,
    queue_store,
    reactions,
)
from modules.youtube import publisher as yt_publisher  # noqa: E402
from modules.telegram import branded, translator  # noqa: E402
from modules.youtube import shorts_format, uploader as yt_uploader  # noqa: E402
from shared import branding  # noqa: E402

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

# Bot API refuses get_file above this size; bigger videos can't be posted to
# YouTube through the manual flow.
_BOT_FILE_LIMIT = 20 * 1024 * 1024

# Bot API refuses uploads above this; a bigger render is published normally
# but can't be sent back into the group as a file.
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
    """file_id + kind for a photo/video message, else None (text-only)."""
    if msg.photo:
        return {"file_id": msg.photo[-1].file_id, "type": "photo"}
    if msg.video:
        return {
            "file_id": msg.video.file_id,
            "type": "video",
            "file_size": msg.video.file_size or 0,
        }
    return None


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
    prompt = await msg.reply_text(_prompt_text(state), reply_markup=_keyboard(state))
    _pending[prompt.message_id] = state


async def _gate(msg, text: str, media: list, files: list | None = None) -> None:
    """Single-video post with brands configured: ask as-is vs brand-it before
    opening any picker. State is the same _pending dict, mode-tagged."""
    state = {"text": text, "media": media, "files": list(files or ()),
             "sel_tg": set(), "sel_yt": set(), "sel_em": set(), "mode": "gate"}
    prompt = await msg.reply_text(
        "Brand this clip, or post it as-is?" + (f"\n\n📝 {text}" if text else ""),
        reply_markup=branded.gate_keyboard())
    _pending[prompt.message_id] = state


def _cleanup(state: dict) -> None:
    """Remove files downloaded for this picker (URL posts). file_id-based
    media has nothing on disk."""
    for path in state.get("files", ()):
        try:
            os.remove(path)
        except OSError:
            pass


def _download_any(url: str, dest_path: str) -> tuple[str, str]:
    """download_media, video-first: a URL doesn't say whether it holds a video
    or a photo, so try the mp4-steered "reel" mode and fall back to "post"
    (best format, typically a jpg). When both fail the FIRST error is
    re-raised — it names the real problem (login gate, dead link); the
    fallback's usually just repeats it."""
    try:
        return reel_downloader.download_media(url, dest_path, kind="reel")
    except RuntimeError as exc:
        try:
            return reel_downloader.download_media(url, dest_path, kind="post")
        except RuntimeError:
            raise exc


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

    note = await msg.reply_text("⏳ downloading …")
    dest = os.path.join(config.TG_DATA_DIR, "media", f"manual_url_{msg.message_id}.mp4")
    try:
        # yt-dlp is blocking — keep the event loop free.
        path, link_caption = await asyncio.to_thread(_download_any, url, dest)
    except Exception as exc:
        log.error("URL download failed for %s: %s", url, exc)
        await note.edit_text(f"❌ download failed: {str(exc)[:300]}")
        return

    ext = os.path.splitext(path)[1].lower()
    is_video = ext in _VIDEO_EXTS
    state = {
        "text": user_text or link_caption,
        "cap_src": "your text" if user_text else "from link",
        "media": [{"path": path, "type": "video" if is_video else "photo"}],
        "files": [path],
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


async def _post_to_youtube(bot, text: str, media: list, dests: list):
    """Upload the post's video as a Short to each selected YouTube channel
    (translated per channel). URL posts already have the file on disk; a
    Telegram-uploaded video is downloaded first (≤20 MB — Bot API cap).
    Returns the same (posted, errors) shape as the Telegram publisher."""
    video = next((m for m in media if m["type"] == "video"), None)
    if video is None:
        return [], [(d["chat_id"], "post has no video") for d in dests]

    if video.get("path"):  # URL post — local file, no Bot-API size cap
        try:
            return await asyncio.to_thread(
                yt_publisher.publish_shorts, video["path"], text, dests
            )
        except Exception as exc:  # translator/network failure surfacing raw
            log.error("manual YouTube post failed: %s", exc)
            return [], [(d["chat_id"], str(exc)) for d in dests]

    size = video.get("file_size") or 0
    if size > _BOT_FILE_LIMIT:
        err = f"video too large for manual posting ({size / 1024 / 1024:.0f} MB > 20 MB limit)"
        return [], [(d["chat_id"], err) for d in dests]

    dl_dir = os.path.join(config.TG_DATA_DIR, "media")
    os.makedirs(dl_dir, exist_ok=True)
    # file_id chars are filename-safe (base64url alphabet); last chars suffice.
    path = os.path.join(dl_dir, f"manual_{video['file_id'][-24:]}.mp4")
    try:
        tg_file = await bot.get_file(video["file_id"])
        await tg_file.download_to_drive(path)
        # Uploads are blocking network calls — keep the bot's event loop free.
        return await asyncio.to_thread(yt_publisher.publish_shorts, path, text, dests)
    except Exception as exc:  # get_file size refusal, network, ...
        log.error("manual YouTube post failed: %s", exc)
        return [], [(d["chat_id"], str(exc)) for d in dests]
    finally:
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
    """Local path of the source video, downloading Telegram-uploaded ones
    (≤ 20 MB — checked at the gate). URL posts already have a path."""
    video = next(m for m in state["media"] if m["type"] == "video")
    if video.get("path"):
        return video["path"]
    dl_dir = os.path.join(config.TG_DATA_DIR, "media")
    os.makedirs(dl_dir, exist_ok=True)
    path = os.path.join(dl_dir, f"brand_src_{video['file_id'][-24:]}.mp4")
    tg_file = await bot.get_file(video["file_id"])
    await tg_file.download_to_drive(path)
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
        video = next((m for m in state["media"] if m["type"] == "video"), None)
        if video and not video.get("path") \
                and (video.get("file_size") or 0) > _BOT_FILE_LIMIT:
            await q.answer(
                "Video too large to brand (20 MB Bot API cap) — post the "
                "clip as a link instead.", show_alert=True)
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
    the publish picker on a fresh message. One failed brand is reported and
    skipped — never fatal for the others."""
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
    renders, failures = [], []
    cache: dict[str, str] = {}
    for b in brands:
        try:
            lang = b["lang"]
            if lang not in cache:
                cache[lang] = (await asyncio.to_thread(
                    translator.translate, state["text"], lang,
                    config.SOURCE_LANG) if lang else state["text"])
            out = os.path.join(
                media_dir, f"brand_{q.message.message_id}_{b['name']}.mp4")
            path = await asyncio.to_thread(
                branding.render_branded, src, cache[lang], b["logo"], out)
            state["files"].append(path)
            renders.append({"brand": b, "path": path, "headline": cache[lang]})
            size = os.path.getsize(path)
            if size > _BOT_UPLOAD_LIMIT:
                await q.message.chat.send_message(
                    f"🏷 {b['name']}: rendered ({size / 1024 / 1024:.0f} MB — "
                    "too big to send back; publishing still works)")
            else:
                with open(path, "rb") as fh:
                    await q.message.chat.send_video(video=fh,
                                                    caption=f"🏷 {b['name']}")
        except Exception as exc:  # translator, ffmpeg, Telegram send, ...
            log.error("brand render failed for %s: %s", b["name"], exc)
            failures.append((b["name"], str(exc)[:200]))

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
    if failures:
        summary += "\n" + "\n".join(f"❌ {n}: {e}" for n, e in failures)
    await q.edit_message_text(summary)

    # Fresh message so the publish picker lands BELOW the delivered clips;
    # the state follows it.
    _pending.pop(q.message.message_id, None)
    if not state["pairs"]:
        _cleanup(state)
        await q.message.chat.send_message(
            "no destinations configured for the rendered brands "
            "(BRAND_<NAME>_TG/YT/TW) — files above are yours, nothing to publish")
        return
    prompt = await q.message.chat.send_message(
        "Publish which?", reply_markup=branded.publish_keyboard(state["pairs"],
                                                                set()))
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
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_autopilot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/autopilot [on|off] — pause or resume the drip without a restart."""
    arg = (context.args[0].lower() if context.args else "")
    if arg in ("on", "off"):
        autopilot.set_enabled(arg == "on")
    await update.effective_message.reply_text(
        f"autopilot is {'on' if autopilot.is_enabled() else 'paused'}"
        + ("" if arg in ("on", "off") else "  (use /autopilot on|off to change)")
    )


async def cmd_asks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/asks — re-render every unanswered reaction ask, including any whose
    message never made it out because the bot died mid-tick."""
    asks = queue_store.open_asks()
    if not asks:
        await update.effective_message.reply_text("no open reaction asks")
        return
    for ask in asks:
        await autopilot.refresh_ask(context.bot, ask)
    await update.effective_message.reply_text(f"re-sent {len(asks)} open ask(s)")


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/next — run one drip right now. The scheduled tick is untouched."""
    msg = await update.effective_message.reply_text("⏳ running one drip …")
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
        if name.startswith(("manual_url_", "brand_")):
            try:
                os.remove(os.path.join(dl_dir, name))
            except OSError:
                pass


async def _on_start(app) -> None:
    """Queue the autopilot's first tick once the event loop is running."""
    autopilot.schedule(app)


def main() -> None:
    _sweep_orphans()
    queue_store.init()  # the autopilot reads/writes the same DB as the collector
    app = Application.builder().token(config.NEWS_BOT_TOKEN).post_init(_on_start).build()
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
