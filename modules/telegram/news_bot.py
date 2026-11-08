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
— brands/<group>/<name>/logo.png top-right, the caption as a translated lower-third
headline (shared/branding.py) — sends each back here, then offers a publish
picker of PLATFORMS (TG/YT/X/IG, all off; YouTube hidden over 3 minutes),
each fanning out to every rendered brand configured for it.
IG is the Graph API (modules/instagram/graph.py): the render is exposed at a
public URL (shared/public_media.py, nginx on the VPS) just long enough for
Instagram to fetch it — a video render becomes a Reel, a photo card a post.
IG is also the one platform that posts more than the headline: the caption is
the brand's headline, a blank line, then paragraphs plus hashtags written by a
web-searching model (modules/instagram/caption.py) once per publish and
reworded per brand. Nothing publishes without a selection.

Caption edit: reply to any open picker message with new text to replace the
caption before hitting "Post to selected". On the brand-it flow, a reply
starting `info:` instead gives the Instagram caption its source and facts.

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
import contextlib
import datetime as dt
import html
import itertools
import logging
import os
import sys

# Make the repo root importable so `from shared` / `from modules` resolve when
# run directly (`py modules/telegram/news_bot.py`).
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from telegram import (BotCommand, BotCommandScopeChat, InlineKeyboardButton,  # noqa: E402
                      InlineKeyboardMarkup, Update)
from telegram.error import BadRequest  # noqa: E402
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
from modules.twitter import publisher as tw_publisher  # noqa: E402
from modules.telegram import branded, bundle, groups, headlines, schedule, spellfix, translator  # noqa: E402
from modules.telegram import report as reach_report  # noqa: E402
from modules.youtube import shorts_format, uploader as yt_uploader  # noqa: E402
from modules.instagram import caption as ig_caption, graph as ig_graph  # noqa: E402
from modules.facebook import poster as fb_poster  # noqa: E402
from shared import branding, ffrun, photo_card, public_media, video_edit, vision  # noqa: E402
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
if not (config.TG_DESTINATIONS or config.YT_DESTINATIONS or config.TW_DESTINATIONS):
    raise SystemExit(
        "No destinations in .env — set TG_DESTINATIONS (chat_id:lang), "
        "YT_DESTINATIONS (yt_account:lang) and/or TW_DESTINATIONS (x_account:lang)"
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
#  "sel_tg": set[int], "sel_yt": set[int], "sel_tw": set[int], "sel_em": set[int]}.
# sel_tg indexes TG_DESTINATIONS, sel_yt indexes YT_DESTINATIONS (YouTube rows
# are only offered when the post contains a video), sel_tw indexes
# TW_DESTINATIONS (Twitter rows only for single-photo / single-video posts —
# albums need X's multi-image path, not built), sel_em indexes EMOJI_SERVICES.
# cap_src says where the caption came from for URL posts ("your text" /
# "from link" / "edited").
# In-memory only; a bot restart expires open pickers (tapping one then says
# so) — main() sweeps the orphaned downloads on startup.
_pending = {}

# The busy marker: a file that exists exactly while this process is rendering,
# sending a download back, or publishing (scheduled posts included). A
# restart in the middle of any of those loses it — half the platforms posted,
# the rest never — so a deploy waits for the file to go away before it
# restarts the unit (deploy/restart_when_idle.sh). Its content names what is
# running, for the deploy's log. Removed on startup: a crash leaves it behind.
_BUSY_PATH = os.path.join(config.TG_DATA_DIR, "busy")
_busy_ops: dict = {}
_busy_ids = itertools.count()


def _write_busy() -> None:
    try:
        if _busy_ops:
            os.makedirs(config.TG_DATA_DIR, exist_ok=True)
            with open(_BUSY_PATH, "w", encoding="utf-8") as fh:
                fh.write(", ".join(sorted(_busy_ops.values())) + "\n")
        elif os.path.exists(_BUSY_PATH):
            os.remove(_BUSY_PATH)
    except OSError as exc:
        log.warning("busy marker not updated: %s", exc)


@contextlib.contextmanager
def _busy(what: str):
    key = next(_busy_ids)
    _busy_ops[key] = what
    _write_busy()
    try:
        yield
    finally:
        _busy_ops.pop(key, None)
        _write_busy()


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
            # Stable across bots and re-forwards — the bundle history's key
            # for a video that came in as a Telegram upload.
            "file_unique_id": msg.video.file_unique_id,
            "type": "video",
            "file_size": msg.video.file_size or 0,
            **src,
        }
    return None


# Caption every branded preview carries. Also the marker that tells a preview
# sent from the user account apart from a real post by that same account — the
# operator usually IS the MTProto account, so the sender alone proves nothing.
_PREVIEW_MARK = "🏷 "


def _is_own_preview(msg) -> bool:
    """True for a branded preview this process sent through the user client.

    Past the Bot API's 50 MB upload cap the preview comes from your own
    account (see mtproto.send_video), which means it lands in the control
    group as an ordinary member message: the bot sees it, and without this it
    opens a fresh picker for the render it just made. The recorded id is the
    real check; the sender + caption pair covers the race where getUpdates
    delivers the message before send_file() has returned that id.
    """
    if mtproto.sent_by_us(msg.chat.id, msg.message_id):
        return True
    sender = msg.from_user.id if msg.from_user else None
    return (sender is not None and sender == mtproto.self_id()
            and (msg.caption or "").startswith(_PREVIEW_MARK))


def _track(msg):
    """Record a control-group message for the weekly cleanup (incoming AND
    outgoing — the Bot API can't list history, so anything not recorded here
    survives the wipe). Returns `msg` so send sites can wrap in place."""
    if msg is not None and msg.chat.id == CHAT_ID:
        queue_store.track_group_message(CHAT_ID, msg.message_id)
    return msg


def _has_video(media: list) -> bool:
    return any(m["type"] == "video" for m in media)


def _tweetable(media: list) -> bool:
    """Twitter rows are offered for exactly one photo or one video. Albums are
    excluded — X takes up to 4 images but through a different upload path the
    poster doesn't implement."""
    return len(media) == 1 and media[0]["type"] in ("photo", "video")


def _all_photos(media: list) -> bool:
    return bool(media) and all(m["type"] == "photo" for m in media)


# Shown wherever the operator can still act on it. Instagram is the only
# platform that publishes more than the headline, so this is the only platform
# the reply affects — and what the operator already knows about the story (the
# source, the names) is the one input the web search cannot find for itself,
# which is why it is advertised rather than left as a known incantation.
_INFO_HINT = ("↩️ reply `info: …` with the source and facts for the "
              "Instagram caption")


def _info_lines(state: dict) -> list:
    """The operator's info for the Instagram caption, echoed back so it is
    never invisible. Shown on every picker that has one, whether or not that
    picker is where it was typed."""
    text = (state.get("info") or "").strip()
    if not text:
        return []
    preview = text if len(text) <= 900 else text[:900] + "…"
    return ["ℹ️ your IG info:\n" + preview]


def _source_text(state: dict) -> str:
    return ((state.get("source") or {}).get("text") or "").strip()


# How much of the source post the card shows — it is a reminder of what the
# caption will draw on, not the post itself.
_SOURCE_PREVIEW = 300


def _source_lines(state: dict) -> list:
    """The 📎 line: where the clip came from and what that post says."""
    source = state.get("source") or {}
    platform = ig_caption.PLATFORM_NAMES.get(source.get("platform") or "")
    if not platform:
        return []
    who = ig_caption.credit(source).strip()
    text = _source_text(state)
    if len(text) > _SOURCE_PREVIEW:
        text = text[:_SOURCE_PREVIEW].rstrip() + "…"
    return [f"📎 source post ({who or platform}): {text or '(no text)'}"]


def _photo_count(media: list) -> int:
    return sum(1 for m in media if m["type"] == "photo")


# --------------------------------------------------------------------------- #
# The plan card (branded posts)                                               #
# --------------------------------------------------------------------------- #
#
# One message that already holds every answer — brands, design, platforms —
# from the house default (branded.DEFAULT_GROUP / DEFAULT_PLATFORMS), with
# 🎬 Render to accept it and ✏️ Change to edit all of it on one screen. The
# pure half (defaults, summary lines, keyboards) is in branded.py.

_PLAN_RULE = "━━━━━━━━━━━━━━"


def _init_plan(state: dict, kind: str) -> None:
    """Turn a post's state into an open plan card ("video" = brand the clip,
    "photo" = compose news cards), pre-filled with the default plan."""
    brands = branded.available_brands(config.BRANDS)
    plan = branded.default_plan(brands, kind, _photo_count(state["media"]))
    state.update(mode="plan", gate_kind=kind, card=kind == "photo",
                 brands=brands, sel_brands=plan["sel_brands"],
                 layout=plan["layout"], plan_platforms=plan["platforms"],
                 custom_brands=False, editing=False,
                 # The analysis starts the moment the card is up; until it
                 # lands the card says so. Photos are never analysed — see
                 # _start_watching.
                 watching=config.IG_VISION_ENABLED and kind != "photo")
    state["video_key"] = _video_key(state)


def _plan_available(state: dict) -> list:
    """Platform keys the currently selected brands could publish to."""
    return branded.plan_platform_keys(state["brands"], state["sel_brands"],
                                      state["gate_kind"])


def _plan_keys(state: dict) -> list:
    """The platforms the plan will actually offer pre-ticked: the chosen ones
    the selected brands still have (untick every IG brand and IG drops off
    the card, but comes back with them)."""
    return [k for k in _plan_available(state) if k in state["plan_platforms"]]


def _plan_text(state: dict) -> str:
    """The card: headline, what the clip shows, the operator's info, the plan,
    and the two things a reply can do."""
    head = state["text"].strip()
    lines = [f"📰 {head}" if head
             else "⚠️ no headline yet — reply to this message with it"]
    if state.get("watching") and "footage" not in state:
        lines.append("👁 watching the clip…")
    else:
        lines += branded.footage_lines(state.get("footage") or {})
    lines += _source_lines(state)
    lines += _info_lines(state)
    lines += _edit_lines(state)
    lines += _headline_lines(state)
    lines.append("\n".join([
        _PLAN_RULE,
        *branded.plan_lines(state["brands"], state["sel_brands"],
                            _plan_keys(state), state.get("layout"),
                            _photo_count(state["media"])),
        _PLAN_RULE]))
    hint = ("↩️ reply to this message to replace the headline\n"
            + _INFO_HINT + " — and get 5 headline options")
    if state["gate_kind"] == "video":
        hint += "\n" + _EDIT_HINT
    lines.append(hint)
    return "\n\n".join(lines)


_EDIT_HINT = ("↩️ reply `edit: 23-26, crop bottom 10%, cut end 2` to put a "
              "hook first, crop a banner off or cut time")


def _edit_lines(state: dict) -> list:
    """The ✂️ line: the clip edit the render will apply, with its result
    length once the clip's length is known."""
    line = video_edit.describe(state.get("edit") or {},
                               state.get("src_duration"))
    return [line] if line else []


async def _on_edit_reply(state: dict, value: str, reply, msg) -> None:
    """An `edit:` reply: parse it, check it against the clip when the clip is
    already here, and redraw the card or the bundle line. Only before the
    render — after that the edit is burned into nothing and would mislead."""
    mode = state.get("mode")
    if not _has_video(state.get("media") or []):
        _track(await msg.reply_text("✂️ edit works on videos only"))
        return
    if mode not in ("plan", "bundle"):
        _track(await msg.reply_text(
            "✂️ too late for an edit — it is applied when 🎬 Render runs"
            if mode in ("rendering", "publish", "countdown", "schedule")
            else "✂️ an edit applies to branded renders — use 🎬 Render"))
        return
    try:
        spec = video_edit.parse(value)
        video = next(m for m in state["media"] if m["type"] == "video")
        if video.get("path") and not state.get("src_duration"):
            _, _, state["src_duration"] = await asyncio.to_thread(
                shorts_format.probe, video["path"])
        if spec and state.get("src_duration"):
            video_edit.resolve(spec, state["src_duration"])
    except ValueError as exc:
        _track(await msg.reply_text(f"✂️ {exc}\n\n{video_edit.SYNTAX}"))
        return
    except Exception as exc:  # ffprobe — the render will check it again
        log.warning("edit: couldn't probe the clip (%s)", exc)
    state["edit"] = spec if not video_edit.is_noop(spec) else None
    if mode == "bundle":
        await asyncio.to_thread(_save_video, state)
        await _redraw_line(state, reply)
        return
    try:
        await reply.edit_text(_plan_text(state), reply_markup=_plan_markup(state))
    except Exception:  # "message is not modified" — same edit again
        pass


async def _preview_edit(q, context, state: dict) -> None:
    """▶ Preview edit: the edited clip on its own — no branding, nothing
    published — as a reply to the card. Made by _edited_source, so the
    🎬 Render after it reuses the same file instead of editing twice."""
    if not state.get("edit"):
        await q.answer("No edit on this video — reply `edit: …` first.",
                       show_alert=True)
        return
    if state.get("previewing"):
        await q.answer("Already making the preview…")
        return
    if await _video_too_big(state):
        await q.answer("Too large to fetch for a preview.", show_alert=True)
        return
    await q.answer("✂️ making the preview…")
    state["previewing"] = True
    chat = q.message.chat
    try:
        with _busy("previewing an edit"):
            src = await _ensure_local_video(context.bot, state)
            _, _, duration = await asyncio.to_thread(shorts_format.probe, src)
            path, duration = await _edited_source(state, src, duration)
            w, h, _ = await asyncio.to_thread(shorts_format.probe, path)
            caption = "✂️ edit preview — " + video_edit.describe(
                state["edit"]).removeprefix("✂️ edit: ")
            size = os.path.getsize(path)
            if size > _BOT_UPLOAD_LIMIT and await mtproto.ensure_ready():
                # Not _track()ed: a Telethon message the bot can't delete.
                await mtproto.send_video(chat.id, path, _PREVIEW_MARK + caption,
                                         w, h, int(duration))
            elif size > _BOT_UPLOAD_LIMIT:
                _track(await q.message.reply_text(
                    f"✂️ the edited clip is {mtproto.human_size(size)} — too "
                    f"big to send back ({mtproto.unavailable_reason()}); "
                    "🎬 Render still works"))
            else:
                with open(path, "rb") as fh:
                    _track(await q.message.reply_video(
                        video=fh, caption=caption, width=w, height=h,
                        duration=int(duration)))
    except _EditError as exc:
        _track(await q.message.reply_text(
            f"✂️ the edit failed: {str(exc)[:300]}\n\n{video_edit.SYNTAX}"))
    except Exception as exc:
        log.error("edit preview failed: %s", exc)
        _track(await q.message.reply_text(
            f"✂️ couldn't make the preview: {str(exc)[:300]}"))
    finally:
        state["previewing"] = False


async def _edited_source(state: dict, src: str, duration: float) -> tuple:
    """(path, duration) of the clip the brands render from: `src` itself, or
    its edited copy when the post has an `edit:`. The copy is made once per
    edit and reused by a second render of the same post. Per-post lock: a
    🎬 Render tapped while ▶ Preview edit is still making the copy waits for
    it and reuses it, rather than writing the same file twice at once."""
    spec = state.get("edit")
    if video_edit.is_noop(spec or {}):
        return src, duration
    lock = state.setdefault("edit_lock", asyncio.Lock())
    async with lock:
        k = video_edit.key(spec)
        done = state.get("edited") or {}
        if done.get("key") == k and os.path.exists(done.get("path") or ""):
            return done["path"], done["duration"]
        out = os.path.splitext(src)[0] + f"_edit_{k}.mp4"
        try:
            await asyncio.to_thread(video_edit.render, src, spec, out, duration)
            _, _, new_duration = await asyncio.to_thread(shorts_format.probe, out)
        except (ValueError, RuntimeError) as exc:
            raise _EditError(str(exc)) from exc
        state["files"].append(out)
        state["edited"] = {"key": k, "path": out, "duration": new_duration}
        return out, new_duration


def _headline_lines(state: dict) -> list:
    """The AI headline options on the card (see _start_headlines): a
    placeholder while they are being written, then the numbered list."""
    if _task_running(state.get("headline_task")):
        return ["✨ writing headline options…"]
    if state.get("headline_failed"):
        return ["⚠️ headline options failed — reply with your own"]
    return headlines.card_lines(state.get("headline_opts"),
                                state.get("headline_pick"))


def _task_running(task) -> bool:
    return task is not None and not task.done()


def _plan_markup(state: dict) -> InlineKeyboardMarkup:
    """🎬 Render / ✏️ Change, or — while editing — every choice on one screen."""
    layouts = (branded.card_layouts(_photo_count(state["media"]))
               if state["gate_kind"] == "photo" else [])
    if not state.get("editing"):
        opts = state.get("headline_opts") or {}
        return branded.plan_keyboard(layouts, state.get("layout"),
                                     len(opts.get("headlines", ())),
                                     state.get("headline_pick"),
                                     bundle=state["gate_kind"] == "video",
                                     preview=bool(state.get("edit")))
    return branded.plan_edit_keyboard(
        state["brands"], state["sel_brands"], state.get("custom_brands", False),
        layouts, state.get("layout"), _plan_available(state),
        state["plan_platforms"])


# The emoji catalogue and the BulkFollows ordering rules live in
# modules/telegram/reactions.py — shared with the autopilot's reaction ask so
# the two flows can't drift apart.
EMOJI_SERVICES = reactions.EMOJI_SERVICES


def _dest_lists(state: dict) -> tuple:
    """The three destination lists AS OFFERED for this post: YouTube rows only
    for a post with a video, X rows only for a single photo/video. One place
    decides it, so the keyboard, the group rows and "All" can't disagree."""
    return (config.TG_DESTINATIONS,
            config.YT_DESTINATIONS if _has_video(state["media"]) else [],
            config.TW_DESTINATIONS if _tweetable(state["media"]) else [])


def _sel_map(state: dict) -> dict:
    """The picker's three selections keyed the way groups.py wants them."""
    return {"tg": state["sel_tg"], "yt": state["sel_yt"], "tw": state["sel_tw"]}


def _dest_groups(state: dict) -> list:
    return groups.dest_groups(config.BRANDS, *_dest_lists(state))


def _keyboard(state: dict) -> InlineKeyboardMarkup:
    """Channel picker. It OPENS COLLAPSED: one row per account group ("g:<i>"
    indexes _dest_groups) with the per-channel list behind "⚙ Custom…", since
    a post normally goes to a whole family of accounts. Expanded it is the
    keyboard it always was — one row per Telegram destination ("t:<i>" indexes
    TG_DESTINATIONS), plus one per YouTube channel for a post with a video
    ("y:<i>") and one per X account for a single photo/video ("x:<i>"). A
    destination no brand claims is grouped by nothing and only appears there
    (and under "All"), and with no group at all the picker never collapses.
    Indices keep callback_data tiny — Telegram caps it at 64 bytes."""
    rows = []
    tg_dests, yt_dests, tw_dests = _dest_lists(state)
    gs = _dest_groups(state)
    if gs and not state.get("custom"):
        for i, g in enumerate(gs):
            n = groups.dest_count(g)
            rows.append([InlineKeyboardButton(
                f"{groups.dest_mark(g, _sel_map(state))} {g['name']} · "
                f"{n} channel{'' if n == 1 else 's'}",
                callback_data=f"g:{i}")])
        rows.append([InlineKeyboardButton("⚙ Custom…", callback_data="custom")])
    else:
        for i, dest in enumerate(tg_dests):
            mark = "☑" if i in state["sel_tg"] else "☐"
            label = f"{mark} {dest['chat_id']}"
            if dest["lang"]:
                label += f" · {dest['lang']}"
            rows.append([InlineKeyboardButton(label, callback_data=f"t:{i}")])
        for i, dest in enumerate(yt_dests):
            mark = "☑" if i in state["sel_yt"] else "☐"
            label = f"{mark} ▶️ YT {dest['chat_id']}"
            if dest["lang"]:
                label += f" · {dest['lang']}"
            rows.append([InlineKeyboardButton(label, callback_data=f"y:{i}")])
        for i, dest in enumerate(tw_dests):
            mark = "☑" if i in state["sel_tw"] else "☐"
            label = f"{mark} 𝕏 {dest['chat_id']}"
            if dest["lang"]:
                label += f" · {dest['lang']}"
            rows.append([InlineKeyboardButton(label, callback_data=f"x:{i}")])
        if gs:
            rows.append([InlineKeyboardButton("⬅ Groups", callback_data="groups")])
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
             "sel_tg": set(), "sel_yt": set(), "sel_tw": set(), "sel_em": set()}
    prompt = _track(await msg.reply_text(_prompt_text(state),
                                         reply_markup=_keyboard(state)))
    _pending[prompt.message_id] = state


async def _gate(msg, text: str, media: list, files: list | None = None,
                kind: str = "video") -> None:
    """Post with brands configured (single video, or photos → news card):
    open the plan card instead of the channel picker — "📤 Post as-is" on it
    leads there. State is the same _pending dict, mode-tagged."""
    state = {"text": text, "media": media, "files": list(files or ()),
             "sel_tg": set(), "sel_yt": set(), "sel_tw": set(), "sel_em": set()}
    if kind == "video":
        bndl = await asyncio.to_thread(queue_store.current_bundle, CHAT_ID)
        if bndl is not None:
            line = _track(await msg.reply_text("📦 joining the bundle …"))
            await _join_bundle(msg.get_bot(), bndl, state, line)
            return
    _init_plan(state, kind)
    prompt = _track(await msg.reply_text(_plan_text(state),
                                         reply_markup=_plan_markup(state)))
    _pending[prompt.message_id] = state
    _start_watching(msg.get_bot(), state, prompt)


def _cleanup(state: dict) -> None:
    """Remove files downloaded for this picker (URL posts). file_id-based
    media has nothing on disk.

    Cancels a footage analysis still in flight first: its download would land
    in a directory this is about to empty, and its edit would land on a picker
    that no longer exists."""
    for key in ("vision_task", "headline_task"):
        task = state.get(key)
        if task is not None and not task.done():
            task.cancel()
    _disarm_auto_publish(state)
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
        paths, link_caption, source = await asyncio.to_thread(
            reel_downloader.fetch, url, dest)
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
        "origin": ("twitter" if reel_downloader.is_twitter_url(url)
                   else "reddit" if reel_downloader.is_reddit_url(url)
                   else "instagram"),
        # The post the clip came from — its text and author — kept whether or
        # not the operator typed a headline: it is the caption's third source
        # (after the operator's info and headline) and the pinned comment's
        # credit. Text "" when the platform gave none; never blocks a post.
        "source": source,
        "sel_tg": set(),
        "sel_yt": set(),
        "sel_tw": set(),
        "sel_em": set(),
    }
    if config.BRANDS and is_video:
        bndl = await asyncio.to_thread(queue_store.current_bundle, CHAT_ID)
        if bndl is not None:
            await _join_bundle(msg.get_bot(), bndl, state, note)
            return
    if config.BRANDS and (is_video or _all_photos(state["media"])):
        _init_plan(state, "video" if is_video else "photo")
        await note.edit_text(_plan_text(state), reply_markup=_plan_markup(state))
        _pending[note.message_id] = state
        _start_watching(msg.get_bot(), state, note)
        if _source_text(state):
            # The source post is enough to write headline options from.
            _start_headlines(state, note)
        return
    await note.edit_text(_prompt_text(state), reply_markup=_keyboard(state))
    _pending[note.message_id] = state


async def _flush_album(gid) -> None:
    """Fires after the album has been quiet for _ALBUM_SETTLE_S."""
    try:
        await asyncio.sleep(_ALBUM_SETTLE_S)
    except asyncio.CancelledError:
        return  # another part arrived; its timer took over
    entry = _albums.pop(gid, None)
    if entry and config.BRANDS and _all_photos(entry["media"]):
        await _gate(entry["msg"], entry["text"], entry["media"], kind="photo")
    elif entry:
        await _prompt(entry["msg"], entry["text"], entry["media"])


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A message in the control group = a news candidate. Show the picker."""
    msg = update.effective_message
    if msg is None:
        return
    if _is_own_preview(msg):
        return  # our own big-file preview echoing back — not a news candidate
    _track(msg)  # the operator's own messages get wiped weekly too
    text = msg.caption or msg.text or ""

    # Reply to an open picker = replace that post's headline, or — behind an
    # "info:" prefix — give the Instagram caption its source and facts.
    reply = msg.reply_to_message
    if reply is not None and reply.message_id in _pending and text.strip():
        state = _pending[reply.message_id]
        field, value = branded.parse_reply(text)
        if field == "edit":
            await _on_edit_reply(state, value, reply, msg)
            return
        if state.get("mode") == "bundle":
            # A bundle video's line: info starts its headlines (the model's
            # pick is used unless another is tapped); anything else is the
            # operator's own headline.
            if field == "info":
                state["info"] = value
                _start_headlines(state, reply)
            else:
                state.update(text=value, cap_src="edited", headline_pick=None)
            await asyncio.to_thread(_save_video, state)
            await _redraw_line(state, reply)
            return
        # On the publish picker (or its time picker) a reply that reads as a
        # time schedules the post — before it could be taken as a headline.
        if field == "text" and state.get("mode") in ("publish", "schedule"):
            when = schedule.parse_when(value)
            if when is not None:
                await _schedule_post(context, state, reply, when, msg)
                return
            if state["mode"] == "schedule":
                _track(await msg.reply_text(
                    "🕒 couldn't read that as a time — try 18:30, tomorrow "
                    "9:00, 14.10 18:30 or +2h"))
                return
        if field == "info":
            state["info"] = value
        else:
            state["text"] = value
            state["cap_src"] = "edited"
            state["headline_pick"] = None  # option 6: the operator's own
        mode = state.get("mode")
        if field == "info" and mode == "plan":
            # The headline is still open on the card — write options from it.
            _start_headlines(state, reply)
        if mode in ("rendering", "countdown", "schedule"):
            # The headline is being burned in, or the publish is counting
            # down; an info reply was kept above and still reaches the
            # Instagram caption, and the message keeps showing what it shows.
            return
        if mode == "plan":
            body = _plan_text(state)
            markup = _plan_markup(state)
        elif mode == "publish":
            # The headline is burned into the renders and can't be changed
            # now — but the Instagram caption is written at publish time, so an
            # info reply is still worth taking this late.
            # Any reply means the operator is here — restart the idle clock.
            _arm_auto_publish(msg.get_bot(), state, reply)
            if field != "info":
                return
            body = _publish_prompt_text(state)
            markup = branded.platform_keyboard(state["platforms"],
                                               state["sel_platforms"])
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
        elif media_item and media_item["type"] == "photo" and config.BRANDS:
            await _gate(msg, text, [media_item], kind="photo")
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


async def _post_to_twitter(bot, text: str, media: list, dests: list):
    """Tweet the post's single media item from each selected Twitter account
    (translated + 280-trimmed per account by the publisher). URL posts already
    have the file on disk; a Telegram-uploaded video goes through _fetch_video
    (MTProto past 20 MB); a Telegram photo is always within the Bot API limit.
    Returns the same (posted, errors) shape as the other publishers."""
    if not _tweetable(media):
        return [], [(d["chat_id"], "not a single photo/video post") for d in dests]
    item = media[0]

    dl_dir = os.path.join(config.TG_DATA_DIR, "media")
    os.makedirs(dl_dir, exist_ok=True)
    path = None
    try:
        if item.get("path"):
            path = item["path"]
        elif item["type"] == "video":
            stem = os.path.join(dl_dir, f"manual_{item['file_id'][-24:]}")
            path = await _fetch_video(bot, item, stem)
        else:  # photo — Telegram caps these way under the 20 MB get_file limit
            path = os.path.join(dl_dir, f"manual_{item['file_id'][-24:]}.jpg")
            tg_file = await bot.get_file(item["file_id"])
            await tg_file.download_to_drive(path)
        # Posting is blocking network I/O — keep the bot's event loop free.
        return await asyncio.to_thread(tw_publisher.publish_tweets, path, text, dests)
    except Exception as exc:  # download refusal, network, ...
        log.error("manual Twitter post failed: %s", exc)
        return [], [(d["chat_id"], str(exc)) for d in dests]
    finally:
        # Only a file WE downloaded gets removed — a URL post's file belongs to
        # the picker state and is cleaned up with it.
        if path and path != item.get("path"):
            try:
                os.remove(path)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Brand-it flow (plan card → render → publish picker)                         #
# --------------------------------------------------------------------------- #


def _publish_prompt_text(state: dict) -> str:
    """Publish-picker body. The headline is burned into the renders by now, but
    the Instagram caption is still written at publish time — so this is the
    last place an `info:` reply can land, and it says so."""
    lines = ["Publish to which platforms?"]
    lines += _info_lines(state)
    if any(p["platform"] in ("ig", "fb") for p in state.get("platforms", ())):
        lines.append(_INFO_HINT)
    if _AUTO_PUBLISH_S:
        lines.append(f"⏱ publishes the ticked platforms by itself after "
                     f"{_AUTO_PUBLISH_S // 60} min untouched — any tap or "
                     "reply restarts the clock")
    return "\n\n".join(lines)


# The publish picker ships what it has ticked once it has sat this long with
# nobody touching it — the renders are made and the plan is already approved,
# so a post must not die in the group because the operator walked away.
# 0 turns it off.
_AUTO_PUBLISH_S = 5 * 60


def _arm_auto_publish(bot, state: dict, message) -> None:
    """(Re)start the idle clock on an open publish picker. Called when the
    picker opens and on every tap or reply it takes, so it measures idleness,
    not age."""
    _disarm_auto_publish(state)
    if _AUTO_PUBLISH_S:
        state["auto_task"] = asyncio.create_task(
            _auto_publish(bot, state, message))


def _disarm_auto_publish(state: dict) -> None:
    """Stop the idle clock — never the task running this very call, which is
    the auto-publish itself on its way through _do_publish."""
    task = state.pop("auto_task", None)
    if task is not None and not task.done() and task is not asyncio.current_task():
        task.cancel()


async def _auto_publish(bot, state: dict, message) -> None:
    try:
        await asyncio.sleep(_AUTO_PUBLISH_S)
    except asyncio.CancelledError:
        return  # touched, published or cancelled in the meantime
    # Same guards as the button: still this picker's state, still publishing,
    # and something to publish to.
    if _pending.get(message.message_id) is not state or state.get("mode") != "publish":
        return
    if not state["sel_platforms"]:
        log.info("auto-publish skipped: no platform ticked")
        return
    log.info("publish picker idle for %ss — publishing", _AUTO_PUBLISH_S)
    state["auto_published"] = True
    _start_countdown(bot, state, message)


# Publishing never starts on the tap itself: the picker turns into a countdown
# with a ✕ Cancel button for this long first, and Cancel stops every post.
# It is the last chance to catch a wrong headline or a wrong clip before it
# lands on a dozen accounts at once. 0 publishes straight away.
_PUBLISH_GRACE_S = 60
# How often the countdown message is redrawn — often enough to read as a
# timer, rarely enough to stay far under Telegram's edit rate limit.
_COUNTDOWN_TICK_S = 10


def _countdown_text(state: dict, left: int) -> str:
    ticked = [p["label"] for i, p in enumerate(state["platforms"])
              if i in state["sel_platforms"]]
    lines = []
    if state.get("auto_published"):
        lines.append(f"⏱ picker untouched for {_AUTO_PUBLISH_S // 60} min")
    lines.append(f"🚀 publishing to {' · '.join(ticked)} in {int(left)} s")
    lines.append("✕ Cancel stops every post — nothing has gone out yet")
    return "\n\n".join(lines)


def _start_countdown(bot, state: dict, message) -> None:
    """Publish (the 🚀 tap or the idle clock) lands here: the picker becomes a
    countdown, and _do_publish runs only if nobody cancels it. The task sits
    in the same slot as the idle clock, so ✕ Cancel → _cleanup stops it."""
    _disarm_auto_publish(state)
    state["mode"] = "countdown"
    state["auto_task"] = asyncio.create_task(_countdown(bot, state, message))


async def _countdown(bot, state: dict, message) -> None:
    # Busy from the first second: a restart during the grace period would
    # drop a post the operator already sent off.
    with _busy("publish countdown"):
        await _countdown_then_publish(bot, state, message)


async def _countdown_then_publish(bot, state: dict, message) -> None:
    try:
        left = _PUBLISH_GRACE_S
        while left > 0:
            try:
                await message.edit_text(_countdown_text(state, left),
                                        reply_markup=branded.countdown_keyboard())
            except Exception:  # "not modified", a network blip — the clock runs on
                pass
            step = min(_COUNTDOWN_TICK_S, left)
            await asyncio.sleep(step)
            left -= step
    except asyncio.CancelledError:
        return  # ✕ Cancel
    if _pending.get(message.message_id) is not state or state.get("mode") != "countdown":
        return
    try:
        await _do_publish(message, bot, state)
    except Exception:
        log.exception("publish failed")


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


# How long a render will wait for an analysis that is still going. The call
# starts when the plan card opens and the operator then has to read it, so in
# practice it has long since landed; this is the ceiling for
# a gateway that has stopped answering, not an expected wait.
_VISION_WAIT_S = 90


async def _video_too_big(state: dict) -> str:
    """Why this post's video can't be fetched for branding, or "" when it can:
    past the Bot API's 20 MB get_file cap only the MTProto client can carry
    it. Checked on Render (fail on the tap, not after a minute of setup) and by
    the analysis, which would otherwise mail an error for every big clip."""
    video = next((m for m in state["media"] if m["type"] == "video"), None)
    size = (video or {}).get("file_size") or 0
    if video and not video.get("path") and size > _BOT_FILE_LIMIT \
            and not await mtproto.ensure_ready():
        return _too_big_error(size)
    return ""


async def _watch_media(bot, state: dict, message) -> None:
    """Analyse this post's media in the background and put what it saw on the
    plan card.

    Started when the card opens, so what the clip shows is on it while the
    headline and info can still be replied to. That is also why this does the
    download itself rather than waiting for the render to: the file has to be
    fetched anyway, and fetching it here buys the operator the analysis while
    they are still reading the card.

    Never raises and never blocks anything: a failure leaves state["footage"]
    empty, which every consumer already treats as "no analysis".
    """
    try:
        if await _video_too_big(state):
            footage = {}  # Render will say why; nothing to analyse without the file
        else:
            path = await _ensure_local_video(bot, state)
            footage = await asyncio.to_thread(vision.describe, path, state["text"])
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.error("footage analysis failed: %s — continuing without one", exc)
        footage = {}

    state["footage"] = footage
    if state.get("mode") == "bundle":
        await asyncio.to_thread(_save_video, state)
        await _redraw_line(state, message)
        return
    # The operator may have moved on (rendered, cancelled) while this ran —
    # only refresh a card that is still open.
    if state.get("mode") != "plan":
        return
    try:
        await message.edit_text(_plan_text(state),
                                reply_markup=_plan_markup(state))
    except Exception:  # message deleted, or unchanged text
        pass


def _start_watching(bot, state: dict, message) -> None:
    """Kick off _watch_media once per post, tracked so the render can wait.

    Video only. A photo post's caption is written from the headline and the
    operator's `info:` alone — the search runs on those, exactly as it did
    before shared/vision existed — so the photos never go to the model."""
    if (state.get("vision_task") is not None or not config.IG_VISION_ENABLED
            or state.get("gate_kind") == "photo" or state.get("card")):
        return
    state["vision_task"] = asyncio.create_task(
        _watch_media(bot, state, message))


def _start_headlines(state: dict, message) -> None:
    """(Re)write the AI headline options from the operator's info, in the
    background — a newer info reply replaces a call still in flight, and an
    empty `info:` (taking the info back) clears the options."""
    task = state.get("headline_task")
    if task is not None and not task.done():
        task.cancel()
    state.update(headline_opts=None, headline_pick=None, headline_failed=False)
    state["headline_task"] = (asyncio.create_task(_write_headlines(state, message))
                              if state.get("info") or _source_text(state)
                              else None)


async def _write_headlines(state: dict, message) -> None:
    """Never raises. Uses whatever footage analysis has landed by now rather
    than waiting for it — the options are about the operator's info."""
    try:
        opts = await asyncio.to_thread(
            headlines.propose, state.get("info", ""),
            state.get("footage") or {}, state["text"], _source_text(state))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.error("headline options failed: %s", exc)
        opts = None
    state["headline_opts"] = opts
    state["headline_failed"] = opts is None
    state["headline_task"] = None
    if state.get("mode") == "bundle":
        # No step waits for a tap in a bundle: the model's pick IS the
        # headline unless the operator taps another (or typed their own).
        if opts and state.get("cap_src") != "edited":
            pick = opts.get("pick", 0)
            state.update(text=opts["headlines"][pick], cap_src="AI",
                         headline_pick=pick)
        await asyncio.to_thread(_save_video, state)
        await _redraw_line(state, message)
        return
    if state.get("mode") != "plan":
        return
    try:
        await message.edit_text(_plan_text(state),
                                reply_markup=_plan_markup(state))
    except Exception:  # message deleted, or unchanged text
        pass


def _stop_watching(state: dict) -> None:
    """Posting as-is renders nothing, so nothing will read the analysis."""
    task = state.get("vision_task")
    if task is not None and not task.done():
        task.cancel()


async def _await_footage(state: dict) -> dict:
    """The analysis, waiting for it only if it is somehow still running.

    Bounded by _VISION_WAIT_S: a caption without footage is the behaviour that
    shipped for months, while a render that never starts is a post the operator
    has to chase. The task is cancelled on a timeout so it can't later edit a
    picker that has moved on.
    """
    task = state.get("vision_task")
    if task is not None and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), _VISION_WAIT_S)
        except Exception:
            log.warning("footage analysis did not finish in %ss — rendering "
                        "without it", _VISION_WAIT_S)
            task.cancel()
    return state.get("footage") or {}


async def _spellfix_headline(state: dict, chat) -> None:
    """Fix typos in the headline right before it is burned in
    (modules/telegram/spellfix.py). Rewrites state["text"], so the
    translations, the banner and the IG caption all get the fixed one, and
    tells the group when it changed a word. Checked once per headline: a
    re-render of the same text costs nothing."""
    text = state.get("text") or ""
    if not text.strip() or state.get("spellchecked") == text:
        return
    fixed = await asyncio.to_thread(spellfix.correct, text)
    state["spellchecked"] = fixed
    if fixed == text:
        return
    state["text"] = fixed
    log.info("headline spell-fixed: %r -> %r", text, fixed)
    with contextlib.suppress(Exception):  # a note only — the render goes on
        _track(await chat.send_message(
            f"✏️ spelling fixed: {text}\n→ {fixed}"))


async def _on_brand_callback(q, context, state: dict, verb: str) -> None:
    """Taps on the plan card / its editor / the publish picker ("b:<verb>")."""
    if state.get("mode") == "bundle":
        await _on_bundle_line(q, context, state, verb)
        return
    if verb == "noop":
        await q.answer("Add brands/<group>/<name>/logo.png to enable this brand.",
                       show_alert=True)
        return

    if verb == "asis" and state.get("mode") == "plan":
        await q.answer()
        _stop_watching(state)
        state["mode"] = None
        await q.edit_message_text(_prompt_text(state),
                                  reply_markup=_keyboard(state))
        return

    if state.get("mode") == "plan" and verb != "cancel":
        await _on_plan_callback(q, context, state, verb)
        return

    if verb.startswith("p:") and state.get("mode") == "publish":
        await q.answer()
        state["sel_platforms"] ^= {int(verb.split(":", 1)[1])}
        _arm_auto_publish(context.bot, state, q.message)
        await q.edit_message_reply_markup(
            branded.platform_keyboard(state["platforms"],
                                      state["sel_platforms"]))
        return

    if verb == "publish" and state.get("mode") == "publish":
        if not state["sel_platforms"]:
            await q.answer("Pick at least one platform.", show_alert=True)
            return
        await q.answer()
        _start_countdown(context.bot, state, q.message)
        return

    if verb == "sched" and state.get("mode") == "publish":
        if not state["sel_platforms"]:
            await q.answer("Pick at least one platform.", show_alert=True)
            return
        await q.answer()
        # The idle clock would publish from under the time picker; Back
        # re-arms it.
        _disarm_auto_publish(state)
        state["mode"] = "schedule"
        await q.edit_message_text(
            schedule.picker_text(schedule.ticked_labels(
                state["platforms"], state["sel_platforms"])),
            reply_markup=schedule.picker_keyboard())
        return

    if verb == "back" and state.get("mode") == "schedule":
        await q.answer()
        state["mode"] = "publish"
        _arm_auto_publish(context.bot, state, q.message)
        await q.edit_message_text(
            _publish_prompt_text(state),
            reply_markup=branded.platform_keyboard(state["platforms"],
                                                   state["sel_platforms"]))
        return

    if verb.startswith("at:") and state.get("mode") == "schedule":
        when = dt.datetime.fromtimestamp(int(verb.split(":", 1)[1]),
                                         schedule.TZ)
        why = schedule.check(when)
        if why:
            await q.answer(f"Can't schedule: {why}.", show_alert=True)
            await q.edit_message_reply_markup(schedule.picker_keyboard())
            return
        await q.answer()
        await _schedule_post(context, state, q.message, when)
        return

    if verb == "cancel":
        await q.answer()
        counting = state.get("mode") == "countdown"
        _pending.pop(q.message.message_id, None)
        _cleanup(state)  # also stops a running publish countdown
        await q.edit_message_text("✕ cancelled — nothing was published"
                                  if counting else "✕ cancelled")
        return

    await q.answer()  # stale/unknown verb for this mode


async def _on_plan_callback(q, context, state: dict, verb: str) -> None:
    """Taps on an open plan card and its editor. Every edit redraws the whole
    card, so the plan block always shows what Render will do."""
    async def redraw():
        try:
            await q.edit_message_text(_plan_text(state),
                                      reply_markup=_plan_markup(state))
        except BadRequest:  # "message is not modified"
            pass

    if verb in ("edit", "done"):
        await q.answer()
        state["editing"] = verb == "edit"
        await redraw()
        return

    if verb.startswith("t:"):
        await q.answer()
        state["sel_brands"] ^= {int(verb.split(":", 1)[1])}
        await redraw()
        return

    if verb.startswith("g:"):
        await q.answer()
        i = int(verb.split(":", 1)[1])
        if i < len(groups.brand_groups(state["brands"])):
            state["sel_brands"] = branded.toggle_brand_group(
                state["brands"], state["sel_brands"], i)
        await redraw()
        return

    if verb in ("custom", "groups"):
        await q.answer()
        state["custom_brands"] = verb == "custom"
        await redraw()
        return

    if verb.startswith("lay:"):
        layout = verb.split(":", 1)[1]
        if layout not in dict(branded.card_layouts(_photo_count(state["media"]))):
            await q.answer("Unknown card design.", show_alert=True)
            return
        await q.answer()
        state["layout"] = layout
        await redraw()
        return

    if verb.startswith("pk:"):
        await q.answer()
        key = verb.split(":", 1)[1]
        if key in _plan_available(state):
            state["plan_platforms"] ^= {key}
        await redraw()
        return

    if verb.startswith("h:"):
        opts = (state.get("headline_opts") or {}).get("headlines", [])
        i = int(verb.split(":", 1)[1])
        if i >= len(opts):
            await q.answer("Those options were replaced — pick again.",
                           show_alert=True)
            await redraw()
            return
        await q.answer()
        state["text"] = opts[i]
        state["cap_src"] = "AI"
        state["headline_pick"] = i
        await redraw()
        return

    if verb == "bundle":
        # 📦 Bundle: open one (or, should one have opened meanwhile, join it)
        # with this video in it.
        if state["gate_kind"] != "video":
            await q.answer("Bundles are for single videos.", show_alert=True)
            return
        await q.answer()
        bndl = await _open_bundle_for(state)
        _pending.pop(q.message.message_id, None)
        await _join_bundle(context.bot, bndl, state, q.message)
        return

    if verb == "dl":
        with _busy("sending a download"):
            await _do_download(q, context, state)
        return

    if verb == "pv":
        await _preview_edit(q, context, state)
        return

    if verb == "go":
        # "b:go" is the 🎬 Render button. Validate BEFORE answering: a query
        # can only be answered once, so an alert sent after a bare q.answer()
        # never reaches the operator.
        if not state["sel_brands"]:
            await q.answer("No brands selected — tap ✏️ Change.",
                           show_alert=True)
            return
        if not state["text"].strip():
            await q.answer("No headline — reply to this message with it "
                           "first.", show_alert=True)
            return
        # No platform check: Render renders the selected brands, whatever
        # accounts they have. Platforms only matter at publish, and a brand
        # with none still gets its render back in the group.
        if not state["card"]:
            too_big = await _video_too_big(state)
            if too_big:
                # answerCallbackQuery caps its text at 200 chars.
                await q.answer(("Too large to brand: " + too_big)[:200],
                               show_alert=True)
                return
        await q.answer()
        state["mode"] = "rendering"  # a second tap on Render must not render twice
        state["plan_platforms"] = set(_plan_keys(state))
        with _busy("rendering"):
            if state["card"]:
                await _do_render_card(q, context, state)
            else:
                await _do_render(q, context, state)
        return

    await q.answer()  # stale/unknown verb for a plan card


async def _send_original(chat, item: dict) -> None:
    """One media item of a post back into the group, untouched. Telegram-held
    media is re-sent by file_id (no size limit, nothing downloaded); a URL
    post's file goes up from disk — through the user client past the Bot
    API's 50 MB upload cap, like a big render preview."""
    caption = "⬇ original"
    if not item.get("path"):
        if item["type"] == "video":
            _track(await chat.send_video(video=item["file_id"], caption=caption))
        else:
            _track(await chat.send_photo(photo=item["file_id"], caption=caption))
        return

    path = item["path"]
    if item["type"] != "video":
        with open(path, "rb") as fh:
            try:
                _track(await chat.send_photo(photo=fh, caption=caption))
            except BadRequest:  # too big / odd dimensions for a photo
                fh.seek(0)
                _track(await chat.send_document(document=fh, caption=caption))
        return

    try:
        w, h, duration = await asyncio.to_thread(shorts_format.probe, path)
    except Exception:  # no ffprobe — Telegram guesses the player size
        w = h = duration = 0
    size = os.path.getsize(path)
    if size > _BOT_UPLOAD_LIMIT:
        if not await mtproto.ensure_ready():
            raise RuntimeError(f"video is {mtproto.human_size(size)} — over the "
                               f"Bot API's 50 MB, and {mtproto.unavailable_reason()}")
        # Sent from the user account, so on_message sees it: the caption must
        # carry _PREVIEW_MARK or it opens a fresh card for its own download.
        await mtproto.send_video(chat.id, path, _PREVIEW_MARK + "original",
                                 w, h, int(duration))
        return
    with open(path, "rb") as fh:
        _track(await chat.send_video(video=fh, caption=caption,
                                     width=w or None, height=h or None,
                                     duration=int(duration) or None,
                                     supports_streaming=True))


async def _do_download(q, context, state: dict) -> None:
    """⬇ Download: send the post's media back as it came — no render, no
    caption work, nothing published — and close the card."""
    await q.answer()
    state["mode"] = "downloading"  # a second tap must not send twice
    _pending.pop(q.message.message_id, None)
    await q.edit_message_text("⬇ sending the original …")
    sent, errors = 0, []
    try:
        for item in state["media"]:
            try:
                await _send_original(q.message.chat, item)
                sent += 1
            except Exception as exc:
                log.error("download send failed: %s", exc)
                errors.append(str(exc)[:200])
    finally:
        _cleanup(state)  # the files are in the chat now
    lines = [f"⬇ sent {sent} file{'' if sent == 1 else 's'} — nothing was "
             "rendered or published"]
    lines += [f"❌ {e}" for e in errors]
    await q.edit_message_text("\n".join(lines))


def _preselect(state: dict) -> set:
    """Publish-picker rows to tick up front: the platforms the plan promised."""
    keys = state.get("plan_platforms") or set()
    return {i for i, p in enumerate(state["platforms"]) if p["platform"] in keys}


class _RenderSetupError(Exception):
    """The source video couldn't be fetched or probed — nothing rendered."""


class _RenderHung(_RenderSetupError):
    """ffmpeg hung twice and was killed. A single post goes back to its plan
    card; a bundle reports it like any other unreadable video."""


class _EditError(_RenderSetupError):
    """The operator's `edit:` couldn't be applied to this clip. A single post
    goes back to its plan card (the edit is the operator's to fix, the post
    isn't lost); a bundle reports it like any other unreadable video."""


async def _render_video(bot, chat, state: dict, brands: list, tag: str,
                        label: str = "") -> tuple[list, list, list, float]:
    """Render `state`'s video once per brand in `brands` (headline translated
    per brand language), send each back into `chat` as a preview. Returns
    (renders, failures, warnings, duration). All brands render in ONE ffmpeg
    pass (batched — see branding.RENDER_BATCH); if that fails it falls back to
    one render per brand, so a single bad brand is reported and skipped, never
    fatal for the others. `tag` makes the output filenames unique; `label`
    goes on each preview after the brand name ("video 2" in a bundle).

    Raises _RenderSetupError when the source can't be read at all. Shared by
    the single-post Render and the bundle Render, which calls it once per
    video with that video's accounts."""
    # Settle the analysis before the headline is burned in — normally already
    # done, since it started when the card opened.
    await _await_footage(state)
    await _spellfix_headline(state, chat)
    try:
        src = await _ensure_local_video(bot, state)
        _, _, duration = await asyncio.to_thread(shorts_format.probe, src)
    except Exception as exc:
        raise _RenderSetupError(str(exc)) from exc
    # The operator's `edit:` (hook / crop / cuts) — one pass, before the
    # brands, so everything after this only sees a different input file.
    src, duration = await _edited_source(state, src, duration)

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
            out = os.path.join(media_dir, f"brand_{tag}_{b['name']}.mp4")
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
        except ffrun.Timeout as exc:
            # A hang isn't one brand's fault: going brand-by-brand would sit
            # out the limit once per brand.
            raise _RenderHung(str(exc)) from exc
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
        name = b["name"] + (f" · {label}" if label else "")
        try:
            size = os.path.getsize(path)
            if size > _BOT_UPLOAD_LIMIT and await mtproto.ensure_ready():
                # Past the Bot API's 50 MB upload cap the preview comes from
                # your own account instead — the clip still lands in the group.
                # Not _track()ed: it's a Telethon message, and the weekly wipe
                # deletes through the BOT, which can't remove your own posts.
                # The caption must keep _PREVIEW_MARK: on_message recognises
                # this message by it while send_video is still returning.
                await mtproto.send_video(chat.id, path, _PREVIEW_MARK + name,
                                         branding.OUT_W, branding.OUT_H,
                                         int(duration))
            elif size > _BOT_UPLOAD_LIMIT:
                _track(await chat.send_message(
                    f"🏷 {name}: rendered ({mtproto.human_size(size)} — "
                    f"too big to send back, {mtproto.unavailable_reason()}; "
                    "publishing still works)"))
            else:
                with open(path, "rb") as fh:
                    # width/height matter: without them Telegram sizes the
                    # inline player from defaults and plays the clip squashed.
                    sent = _track(await chat.send_video(
                        video=fh, caption=f"🏷 {name}",
                        width=branding.OUT_W, height=branding.OUT_H,
                        duration=int(duration)))
                    r["preview_id"] = sent.message_id
        except Exception as exc:  # Telegram send only — the render already succeeded
            log.error("brand preview send failed for %s: %s", b["name"], exc)
            warnings.append((b["name"], str(exc)[:200]))
    return renders, failures, warnings, duration


async def _do_render(q, context, state: dict) -> None:
    """Render one variant per selected brand (`_render_video`), send each back
    into the group, then open the publish picker on a fresh message."""
    brands = [state["brands"][i] for i in sorted(state["sel_brands"])]
    await q.edit_message_text(
        "⏳ rendering " + ", ".join(b["name"] for b in brands) + " …")
    try:
        renders, failures, warnings, duration = await _render_video(
            context.bot, q.message.chat, state, brands,
            str(q.message.message_id))
    except _EditError as exc:
        state["mode"] = "plan"
        await q.edit_message_text(
            _plan_text(state) + f"\n\n❌ the edit failed: {str(exc)[:300]}",
            reply_markup=_plan_markup(state))
        return
    except _RenderHung as exc:
        state["mode"] = "plan"
        await q.edit_message_text(
            _plan_text(state) + f"\n\n❌ the render failed: {str(exc)[:300]}. "
            "Nothing was published.",
            reply_markup=_plan_markup(state))
        return
    except _RenderSetupError as exc:
        log.error("brand render setup failed: %s", exc)
        _pending.pop(q.message.message_id, None)
        _cleanup(state)
        await q.edit_message_text(f"❌ can't read the video: {str(exc)[:300]}")
        return

    if not renders:
        _pending.pop(q.message.message_id, None)
        _cleanup(state)
        await q.edit_message_text(
            "❌ all renders failed:\n"
            + "\n".join(f"{n}: {e}" for n, e in failures))
        return
    _start_manual_captions(q.message.chat, state, renders)

    state["mode"] = "publish"
    state["renders"] = renders
    state["platforms"] = branded.platforms_for(renders, duration)
    state["sel_platforms"] = _preselect(state)

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
        if not state["platforms"]:
            _track(await q.message.chat.send_message(
                "✅ rendered — these brands have no accounts set up "
                "(credentials/brands/<name>.json), so there is nothing to "
                "publish to; the files above are yours"))
            _pending.pop(q.message.message_id, None)
            _cleanup(state)
            return
        # Fresh message so the publish picker lands BELOW the delivered clips.
        # Sent BEFORE the old _pending entry is popped/reassigned, so a failed
        # send here is caught below with the state (and its files) still
        # intact to clean up, instead of orphaning everything silently.
        prompt = _track(await q.message.chat.send_message(
            _publish_prompt_text(state),
            reply_markup=branded.platform_keyboard(state["platforms"],
                                                  state["sel_platforms"])))
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
    _arm_auto_publish(context.bot, state, prompt)


async def _ensure_local_photos(bot, state: dict) -> list[str]:
    """Local paths of every photo in the post, in message order. Telegram
    photos are always far under the 20 MB get_file cap. Memoised on the
    media dicts like _ensure_local_video."""
    dl_dir = os.path.join(config.TG_DATA_DIR, "media")
    os.makedirs(dl_dir, exist_ok=True)
    paths = []
    for i, m in enumerate(state["media"]):
        if m["type"] != "photo":
            continue
        if not m.get("path"):
            path = os.path.join(dl_dir, f"card_src_{m['file_id'][-24:]}_{i}.jpg")
            tg_file = await bot.get_file(m["file_id"])
            await tg_file.download_to_drive(path)
            m["path"] = path
            state["files"].append(path)
        paths.append(m["path"])
    return paths


async def _do_render_card(q, context, state: dict) -> None:
    """Photo twin of _do_render: first photo = hero, the next two are the
    other photos the chosen design uses (circular insets, split panels, or
    nothing at all), headline translated per brand, one card per selected
    brand via shared/photo_card.py. Each card is sent back as a photo, then
    the publish picker opens (TG + X + IG — YouTube can't take a photo)."""
    brands = [state["brands"][i] for i in sorted(state["sel_brands"])]
    await q.edit_message_text(
        "⏳ composing " + ", ".join(b["name"] for b in brands) + " …")
    await _await_footage(state)
    await _spellfix_headline(state, q.message.chat)

    try:
        photos = await _ensure_local_photos(context.bot, state)
        if not photos:
            raise ValueError("no photos in this post")
    except Exception as exc:
        log.error("card render setup failed: %s", exc)
        _pending.pop(q.message.message_id, None)
        _cleanup(state)
        await q.edit_message_text(f"❌ can't fetch the photos: {str(exc)[:300]}")
        return
    hero, layout, insets = branded.render_args(state.get("layout", "solo"),
                                               photos)

    media_dir = os.path.join(config.TG_DATA_DIR, "media")
    renders, failures, warnings = [], [], []
    cache: dict[str, str] = {}
    for b in brands:
        try:
            lang = b["lang"]
            if lang not in cache:
                cache[lang] = (await asyncio.to_thread(
                    translator.translate, state["text"], lang,
                    config.SOURCE_LANG) if lang else state["text"])
            out = os.path.join(
                media_dir, f"card_{q.message.message_id}_{b['name']}.jpg")
            await asyncio.to_thread(photo_card.render_card, hero, cache[lang],
                                    b["logo"], out, insets, layout=layout)
        except Exception as exc:
            log.error("card render failed for %s: %s", b["name"], exc)
            failures.append((b["name"], str(exc)[:200]))
            continue
        state["files"].append(out)
        renders.append({"brand": b, "path": out, "headline": cache[lang],
                        "kind": "photo"})

    for r in renders:
        try:
            with open(r["path"], "rb") as fh:
                sent = _track(await q.message.chat.send_photo(
                    photo=fh, caption=f"🖼 {r['brand']['name']}"))
                r["preview_id"] = sent.message_id
        except Exception as exc:  # Telegram send only — the render succeeded
            log.error("card preview send failed for %s: %s", r["brand"]["name"], exc)
            warnings.append((r["brand"]["name"], str(exc)[:200]))

    if not renders:
        _pending.pop(q.message.message_id, None)
        _cleanup(state)
        await q.edit_message_text(
            "❌ all cards failed:\n"
            + "\n".join(f"{n}: {e}" for n, e in failures))
        return
    _start_manual_captions(q.message.chat, state, renders)

    state["mode"] = "publish"
    state["renders"] = renders
    state["platforms"] = branded.platforms_for(renders, 0)
    state["sel_platforms"] = _preselect(state)

    summary = "🖼 composed: " + ", ".join(r["brand"]["name"] for r in renders)
    if warnings:
        summary += "\n" + "\n".join(
            f"⚠️ {n}: composed, preview send failed: {e}" for n, e in warnings)
    if failures:
        summary += "\n" + "\n".join(f"❌ {n}: {e}" for n, e in failures)

    try:
        await q.edit_message_text(summary)
        if not state["platforms"]:
            _track(await q.message.chat.send_message(
                "no destinations configured for the composed brands "
                "(credentials/brands/<name>.json) — cards above are yours, nothing to publish"))
            _pending.pop(q.message.message_id, None)
            _cleanup(state)
            return
        prompt = _track(await q.message.chat.send_message(
            _publish_prompt_text(state),
            reply_markup=branded.platform_keyboard(state["platforms"],
                                                  state["sel_platforms"])))
    except Exception as exc:
        log.error("card publish-picker handoff failed: %s", exc)
        _pending.pop(q.message.message_id, None)
        _cleanup(state)
        try:
            _track(await q.message.chat.send_message(
                f"❌ composed but couldn't open the publish picker "
                f"({str(exc)[:200]}) — temp files removed"))
        except Exception:
            pass
        return

    _pending.pop(q.message.message_id, None)
    _pending[prompt.message_id] = state
    _arm_auto_publish(context.bot, state, prompt)


def _manual_ig(brand: dict) -> bool:
    """A brand whose Instagram is posted BY HAND: outside GMN and with no IG
    account connected (no `instagram` block in its credentials file). Its
    render preview is followed by the caption it would have published, ready
    to copy (`_send_manual_captions`)."""
    return (brand.get("group") or "").upper() != "GMN" and not brand.get("ig")


async def _send_manual_captions(chat, state: dict, renders: list) -> None:
    """The finished Instagram caption for every hand-posted brand in
    `renders`, one message each, as a reply to that brand's preview.

    Runs as a task after the render (it waits on the caption's web search);
    the expansion lands in `state["caption_cache"]`, so a publish to IG/YT
    later on the same post reuses it instead of paying for a second search.
    Never raises — a missing caption must not touch the renders."""
    manual = [r for r in renders if _manual_ig(r["brand"])]
    if not manual:
        return
    try:
        caps = await _ig_captions(
            state.get("text", ""), [{"platform": "ig", "render": r} for r in manual],
            state.get("footage") or {}, state.get("info", ""),
            cache=state.setdefault("caption_cache", {}),
            source=state.get("source"))
        texts = {r["brand"]["name"]: caps.get(r["brand"]["name"]) or
                 ig_caption.with_brand_tag(r["headline"], r["brand"]["name"])
                 for r in manual}
        # One cheap searchless call per account, all at once: the comment to
        # pin under the post, written from that account's own caption and the
        # source post, credited to the source when it is known.
        comments = dict(zip(texts, await asyncio.gather(
            *(asyncio.to_thread(ig_caption.pinned_comment, t,
                                state.get("source"))
              for t in texts.values()))))
        for r in manual:
            name = r["brand"]["name"]
            comment = comments.get(name) or ""
            pin = (f"\n\n📌 Comment to pin:\n<pre>{html.escape(comment)}</pre>"
                   if comment else "")
            # <pre>: one tap copies the whole block in Telegram.
            _track(await chat.send_message(
                f"📋 <b>{html.escape(name)}</b> — Instagram caption "
                f"(no IG account connected, post by hand):\n\n"
                f"<pre>{html.escape(texts[name])}</pre>{pin}",
                parse_mode="HTML",
                reply_to_message_id=r.get("preview_id"),
                allow_sending_without_reply=True))
    except Exception:
        log.exception("manual instagram captions failed")


def _start_manual_captions(chat, state: dict, renders: list) -> None:
    """Fire `_send_manual_captions` without holding up the picker."""
    if any(_manual_ig(r["brand"]) for r in renders):
        state["manual_task"] = asyncio.create_task(
            _send_manual_captions(chat, state, renders))


# Platforms whose post carries the expanded caption rather than the bare
# headline: Instagram and Facebook post it whole, YouTube a cut of it.
CAPTION_PLATFORMS = ("ig", "fb", "yt")


async def _ig_captions(source_text: str, pairs: list, footage: dict | None = None,
                       info: str = "", cache: dict | None = None,
                       source: dict | None = None) -> dict[str, str]:
    """The Instagram caption for each brand, keyed by brand NAME.

    Instagram, Facebook (verbatim) and YouTube (a cut, via
    `caption.youtube_description`) post more than the headline, so this runs
    only when one of those pairs is in the set.

    ONE EXPANSION, A REWRITE PER ACCOUNT. `caption.expand` writes the base
    caption — hook, context, question, eight hashtag candidates — from the
    operator's `info`, the headline (`source_text`), the `source` post the
    clip came from and the `footage` media notes, in that order of trust.
    `caption.plan` then says who rewrites: every brand with a tone line
    (`writing_style` in brands/<group>/<name>/style.json) gets a cheap
    searchless `caption.rephrase` with its own hook and question, all of them
    at once, then once more for a page whose hook or question came back too
    close to another's (`caption.rewrite_all`); a non-English brand's rewrite is wholly in its language and
    brings its own eight native candidates.

    ONE HASHTAG MEASUREMENT. Every brand's candidates go through a single
    `caption.choose_hashtags` (one Apify pass) and each brand keeps up to four
    story tags under its own tag (`caption.finish`).

    Each value is the FINISHED caption. `cache` (the post's
    `state["caption_cache"]`) holds the one base expansion per (headline, info,
    source text), so the hand-posted brands' captions sent at render time and
    the publish afterwards share it; an `info:` reply in between changes the
    key and buys a fresh one.

    Never raises: an empty dict means every pair falls back to its tagged
    headline, and a single failed rewrite falls back to the base caption.
    """
    # One caption per BRAND — a brand on several platforms is planned once.
    ig_pairs = list({p["render"]["brand"]["name"]: p for p in pairs
                     if p["platform"] in CAPTION_PLATFORMS}.values())
    brands = [dict(p["render"]["brand"],
                   style=branding.load_writing_style(
                       os.path.dirname(p["render"]["brand"]["logo"])))
              for p in ig_pairs]
    headlines = {p["render"]["brand"]["name"]: p["render"]["headline"]
                 for p in ig_pairs}
    # Every brand the bot knows, so a sibling's tag is never dealt to another
    # account — not only the ones in this publish.
    names = {b["name"] for b in config.BRANDS} | set(headlines)
    if not brands or not (source_text or "").strip():
        return {}
    try:
        key = (source_text, info or "", ((source or {}).get("text") or ""))
        if cache is not None and cache.get("key") == key:
            base = cache["base"]
        else:
            base = await asyncio.to_thread(
                ig_caption.expand, source_text, footage or {}, None,
                info or "", source)
            if base and cache is not None:
                cache.update(key=key, base=base)
        if not base:
            return {}
        prose, base_tags = ig_caption.split_tags(base)
        entries = ig_caption.plan(brands, ig_caption.seed_for(source_text))
        # In parallel, then a second pass for any page too close to another.
        texts = await asyncio.to_thread(ig_caption.rewrite_all, prose, entries)
        bodies, candidates = {}, {}
        for e, text in zip(entries, texts):
            body, own = ig_caption.split_tags(text)
            bodies[e["name"]] = body
            candidates[e["name"]] = (own if own and not ig_caption.is_base_lang(
                e["lang"]) else base_tags)
        chosen = await asyncio.to_thread(ig_caption.choose_hashtags, candidates)
        return {name: ig_caption.finish(headlines.get(name) or source_text,
                                        body, chosen.get(name, []),
                                        brand=name, others=names)
                for name, body in bodies.items()}
    except Exception:
        log.exception("instagram caption expansion failed — posting headlines")
        return {}


async def _log_reel(state: dict, brand: dict, media_id) -> None:
    """One published Reel into queue_store's reel log: bundle history (which
    account has had which video) and the weekly report's raw numbers. Never
    raises — the reel is already up."""
    try:
        meta = state.get("bundle") or {}
        key = state.get("video_key") or meta.get("video_key") or ""
        if not key:
            return
        followers = await asyncio.to_thread(ig_graph.followers, brand["ig"])
        await asyncio.to_thread(
            queue_store.log_reel, key, brand["name"], brand["ig"],
            str(media_id) if media_id else None, meta.get("id"),
            meta.get("copy_count"), followers)
    except Exception:
        log.exception("reel log failed for %s", brand.get("name"))


async def _do_publish(message, bot, state: dict) -> None:
    """_publish_pairs under the busy marker — a deploy waits for it."""
    with _busy("publishing"):
        await _publish_pairs(message, bot, state)


async def _publish_pairs(message, bot, state: dict) -> None:
    """Push each selected pair through its platform publisher. The headline is
    already translated per brand — Telegram destinations get lang "" so
    publisher.publish doesn't translate again.

    Takes the picker message rather than a callback query: the 🚀 Publish tap
    and the idle auto-publish (`_auto_publish`) both end up here."""
    pairs = branded.expand(state["platforms"], state["sel_platforms"])
    _pending.pop(message.message_id, None)
    _disarm_auto_publish(state)
    await message.edit_text(
        "⏳ publishing " + ", ".join(p["label"] for p in pairs) + " …")

    # The hand-posted brands' captions may still be searching: let them land
    # in the cache first, so this publish reuses that search, not a second one.
    manual = state.get("manual_task")
    if manual is not None and not manual.done():
        try:
            await asyncio.wait_for(asyncio.shield(manual), _VISION_WAIT_S)
        except Exception:
            pass

    # Before the loop: one footage-backed expansion shared by every IG and YT
    # pair, informed by the operator's info: reply when there is one. A
    # bundle post carries its captions already — written at render time.
    ig_caps = state.get("captions") or await _ig_captions(
        state.get("text", ""), pairs, state.get("footage") or {},
        state.get("info", ""), cache=state.setdefault("caption_cache", {}),
        source=state.get("source"))

    lines = []
    failed = set()   # platform keys with a ❌ — what a Retry re-sends
    for p in pairs:
        r, b = p["render"], p["render"]["brand"]
        before = len(lines)
        try:
            if p["platform"] == "tg":
                posted, errors, links = await publisher.publish(
                    bot, r["headline"],
                    [{"path": r["path"], "type": r.get("kind", "video")}],
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
                # Title = the headline; description = this brand's IG caption
                # cut to its lead paragraph(s) + its hashtags + #shorts, or
                # just the tags when the expansion didn't come back.
                title, _ = yt_publisher.split_caption(r["headline"])
                description = ig_caption.youtube_description(
                    ig_caps.get(b["name"]) or
                    ig_caption.with_brand_tag(r["headline"], b["name"]),
                    r["headline"])
                result = await asyncio.to_thread(
                    yt_uploader.upload_short, r["path"], title, description,
                    b["yt"])
                if result.get("status") == "success":
                    lines.append(f"✅ {p['label']}")
                else:
                    lines.append(f"❌ {p['label']}: "
                                 f"{result.get('error', 'unknown error')}")
            elif p["platform"] == "ig":
                # The Graph API fetches from a URL — expose the render for
                # exactly as long as the publish takes, then take it down.
                url, drop = await asyncio.to_thread(public_media.expose, r["path"])
                try:
                    publish = (ig_graph.publish_photo if r.get("kind") == "photo"
                               else ig_graph.publish_reel)
                    # This brand's own caption — its own wording and its own
                    # hashtags; the bare headline whenever expansion was off or
                    # didn't come back. Either way it is signed with the
                    # account's own tag, which every post carries.
                    ig_caption_text = ig_caps.get(b["name"]) or \
                        ig_caption.with_brand_tag(r["headline"], b["name"])
                    result = await asyncio.to_thread(
                        publish, url, ig_caption_text,
                        b["ig"])
                finally:
                    drop()
                if result.get("status") == "success":
                    lines.append(f"✅ {p['label']}")
                    if r.get("kind", "video") == "video":
                        await _log_reel(state, b, result.get("id"))
                else:
                    lines.append(f"❌ {p['label']}: "
                                 f"{result.get('error', 'unknown error')}")
            elif p["platform"] == "fb":
                # No public_media round trip, unlike the Instagram leg: the
                # Pages API takes a multipart upload, so the render goes
                # straight from disk. post_media picks the edge from the
                # extension — a video render becomes a Reel, a card a photo
                # post. The caption is the same one Instagram gets — this
                # brand's headline, the AI-written body and its hashtags — or
                # the tagged headline when the expansion didn't come back.
                fb_caption_text = ig_caps.get(b["name"]) or \
                    ig_caption.with_brand_tag(r["headline"], b["name"])
                result = await asyncio.to_thread(
                    fb_poster.post_media, r["path"], fb_caption_text, b["fb"])
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
        if any(line.startswith("❌") for line in lines[before:]):
            failed.add(p["platform"])
    state["failed_platforms"] = sorted(failed)

    if state.get("auto_published"):
        lines.insert(0, f"⏱ auto-published after {_AUTO_PUBLISH_S // 60} min idle")
    if state.get("note"):
        lines.insert(0, state["note"])
    if not (failed and state.get("bundle")):
        _cleanup(state)
    await message.edit_text("\n".join(lines))


# --------------------------------------------------------------------------- #
# Bundle mode (modules/telegram/bundle.py)                                    #
# --------------------------------------------------------------------------- #
#
# 📦 Bundle on a video's plan card opens a bundle; while one is open every new
# single video joins it with no tap, as a one-line message that its `info:`
# reply goes to. Each video stays exactly a single post — its own info, its
# own AI headlines (the model's pick is taken unless another is tapped), its
# own source post, renders and captions. 🎬 Render bundle spreads the videos
# across EVERY active account (bundle.assign), renders each account's version
# and writes its caption; 🚀 Publish turns the timetable into scheduled posts
# (the `scheduled` table, one row per account). Bundle and video state live in
# queue_store, so an open or rendered bundle survives a restart; callbacks on
# the bundle's own messages ("u:…") are routed by bundle id from the DB.

_BUNDLE_DIR = os.path.join(config.TG_DATA_DIR, "media", "bundles")

# The part of a video's state a bundle persists — everything its render and
# caption are made from. The rest (brands, pickers, tasks) is rebuilt.
_VIDEO_KEYS = ("text", "cap_src", "info", "source", "footage",
               "headline_opts", "headline_pick", "media", "files", "origin",
               "video_key", "edit")


def _video_key(state: dict) -> str:
    """What makes two posts "the same video" for bundle history: the source
    post's URL (query stripped), or Telegram's file_unique_id."""
    url = ((state.get("source") or {}).get("url") or "").strip()
    if url:
        return url.split("?")[0].rstrip("/")
    media = (state.get("media") or [{}])[0]
    uid = media.get("file_unique_id") or media.get("file_id") or ""
    return f"tg:{uid}" if uid else ""


def _persistable(state: dict) -> dict:
    return {k: state.get(k) for k in _VIDEO_KEYS}


def _save_video(state: dict) -> None:
    if state.get("bundle_video"):
        queue_store.save_bundle_video(state["bundle_video"],
                                      state=_persistable(state))


def _bundle_state(row: dict, bundle_id: int, platforms) -> dict:
    """A video's state rebuilt from its bundle_videos row (after a restart, or
    for a video no longer in _pending)."""
    state = dict(row["state"])
    state.setdefault("files", [])
    state.setdefault("media", [])
    state.update(sel_tg=set(), sel_yt=set(), sel_tw=set(), sel_em=set())
    _init_plan(state, "video")
    state.update(mode="bundle", bundle_id=bundle_id, bundle_video=row["id"],
                 plan_platforms=set(platforms), watching=False)
    state["text"] = state.get("text") or ""
    return state


def _move_into(state: dict, folder: str) -> None:
    """Move the video's downloaded files into `folder`, out of reach of the
    nightly 24 h media sweep (it never recurses into a subdirectory)."""
    os.makedirs(folder, exist_ok=True)
    moved = {}
    for path in list(state.get("files") or ()):
        if os.path.dirname(os.path.abspath(path)) == os.path.abspath(folder):
            continue
        new = os.path.join(folder, os.path.basename(path))
        try:
            os.replace(path, new)
            moved[path] = new
        except OSError as exc:
            log.warning("bundle: couldn't move %s: %s", path, exc)
    state["files"] = [moved.get(p, p) for p in state.get("files") or ()]
    for m in state.get("media") or ():
        if m.get("path") in moved:
            m["path"] = moved[m["path"]]


def _line_body(state: dict) -> tuple[str, InlineKeyboardMarkup]:
    """A bundle video's one-line message: the bundle's size, this video's
    headline and what it still needs, its AI headline options."""
    ids = [v["id"] for v in queue_store.bundle_videos(state["bundle_id"])]
    n = ids.index(state["bundle_video"]) + 1 if state["bundle_video"] in ids \
        else len(ids)
    view = dict(state, headline_task_running=_task_running(
        state.get("headline_task")))
    lines = [bundle.line_text(state["bundle_id"], len(ids), n, view)]
    lines += _edit_lines(state)
    if state.get("info") or _task_running(state.get("headline_task")):
        lines += _headline_lines(state)
    opts = (state.get("headline_opts") or {}).get("headlines", [])
    return "\n\n".join(lines), bundle.line_keyboard(
        len(opts), state.get("headline_pick"), preview=bool(state.get("edit")))


async def _redraw_line(state: dict, message) -> None:
    body, markup = _line_body(state)
    try:
        await message.edit_text(body, reply_markup=markup)
    except Exception:  # "message is not modified", or deleted
        pass


async def _join_bundle(bot, bndl: dict, state: dict, message) -> None:
    """Put a single-video post into the open bundle; `message` becomes its
    line (a reply to it carries the video's info)."""
    if state.get("mode") != "plan":
        _init_plan(state, "video")
    state.update(mode="bundle", bundle_id=bndl["id"],
                 plan_platforms=set(bndl["platforms"]))
    state["video_key"] = _video_key(state)
    _move_into(state, os.path.join(_BUNDLE_DIR, str(bndl["id"])))
    state["bundle_video"] = await asyncio.to_thread(
        queue_store.add_bundle_video, bndl["id"], _persistable(state),
        message.message_id)
    _pending[message.message_id] = state
    log.info("bundle #%d: video %d joined (%s)", bndl["id"],
             state["bundle_video"], state["video_key"])
    await _redraw_line(state, message)
    _start_watching(bot, state, message)


async def _open_bundle_for(state: dict) -> dict:
    """The chat's open bundle, opened now (with this card's platforms) when
    there is none."""
    bndl = await asyncio.to_thread(queue_store.current_bundle, CHAT_ID)
    if bndl is None:
        keys = sorted(_plan_keys(state)) or ["ig"]
        bid = await asyncio.to_thread(queue_store.open_bundle, CHAT_ID, keys)
        bndl = await asyncio.to_thread(queue_store.get_bundle, bid)
        log.info("bundle #%d opened on %s", bid, ", ".join(keys))
    return bndl


async def _on_bundle_line(q, context, state: dict, verb: str) -> None:
    """Taps on a bundle video's line ("b:…" — it lives in _pending)."""
    if verb.startswith("h:"):
        opts = (state.get("headline_opts") or {}).get("headlines", [])
        i = int(verb.split(":", 1)[1])
        if i >= len(opts):
            await q.answer("Those options were replaced — pick again.",
                           show_alert=True)
            return
        await q.answer()
        state.update(text=opts[i], cap_src="AI", headline_pick=i)
        await asyncio.to_thread(_save_video, state)
        await _redraw_line(state, q.message)
        return
    if verb == "bx":
        await q.answer()
        await asyncio.to_thread(queue_store.save_bundle_video,
                                state["bundle_video"], status="removed")
        _pending.pop(q.message.message_id, None)
        _cleanup(state)
        n = len(await asyncio.to_thread(queue_store.bundle_videos,
                                        state["bundle_id"]))
        await q.edit_message_text(f"🗑 removed from bundle #{state['bundle_id']}"
                                  f" — {n} video{'s' if n != 1 else ''} left")
        return
    if verb == "bgo":
        await _render_bundle(q, context, state["bundle_id"])
        return
    if verb == "pv":
        await _preview_edit(q, context, state)
        return
    await q.answer()


def _bundle_accounts() -> list:
    """The active accounts' brand dicts, in BUNDLE_ACCOUNTS order (only those
    that can render — a brand without a logo can't)."""
    brands = {b["name"]: b for b in branded.available_brands(config.BRANDS)}
    return [brands[n] for n in config.BUNDLE_ACCOUNTS
            if n in brands and brands[n]["has_logo"]]


def _history(accounts: list, keys: dict) -> dict:
    """{account: {video id it already published}} for this bundle's videos."""
    raw = queue_store.published_videos(accounts)
    return {a: {vid for vid, key in keys.items() if key and key in raw.get(a, ())}
            for a in accounts}


def _platform_labels(keys) -> list:
    names = dict(branded.PLATFORMS)
    return [names.get(k, k) for k in keys]


async def _render_pairs(bot, chat, bid: int, states: dict, assignment: dict,
                        plan: dict, platforms) -> None:
    """Render and caption every (video, account) pair of `assignment` that
    `plan["renders"]` doesn't have yet — per video, exactly the single-post
    pipeline (`_render_video`, `_ig_captions`) for that video's accounts."""
    accounts = {b["name"]: b for b in _bundle_accounts()}
    folder = os.path.join(_BUNDLE_DIR, str(bid))
    for vid, state in states.items():
        have = plan["renders"].setdefault(vid, {})
        want = [accounts[a] for a, v in assignment.items()
                if v == vid and a not in have and a in accounts]
        if not want:
            continue
        n = plan["videos"][vid]["n"]
        try:
            renders, failures, warnings, duration = await _render_video(
                bot, chat, state, want, f"b{bid}v{vid}", f"video {n}")
        except _RenderSetupError as exc:
            plan["failures"].append(f"❌ video {n}: can't read the video — "
                                    f"{str(exc)[:200]}")
            continue
        plan["failures"] += [f"❌ {name} (video {n}): {err}"
                             for name, err in failures]
        offered = [p for p in branded.platforms_for(renders, duration)
                   if p["platform"] in platforms]
        pairs = branded.expand(offered, set(range(len(offered))))
        caps = await _ig_captions(
            state["text"], pairs, state.get("footage") or {},
            state.get("info", ""), cache=state.setdefault("caption_cache", {}),
            source=state.get("source"))
        os.makedirs(folder, exist_ok=True)
        for r in renders:
            new = os.path.join(folder, os.path.basename(r["path"]))
            try:
                os.replace(r["path"], new)
            except OSError:
                new = r["path"]
            have[r["brand"]["name"]] = {
                "path": new, "headline": r["headline"], "duration": duration,
                "caption": caps.get(r["brand"]["name"], "")}


def _slots_for(plan: dict) -> list:
    rendered = {a: v for a, v in plan["assignment"].items()
                if a in plan["renders"].get(v, {})}
    return bundle.timetable(
        rendered, gap=(config.BUNDLE_GAP_MIN_S, config.BUNDLE_GAP_MAX_S),
        max_copies=config.BUNDLE_MAX_COPIES,
        window=config.BUNDLE_COPY_WINDOW_S)


def _summary(bid: int, plan: dict, platforms, start=None,
             scheduled: bool = False) -> str:
    start = start or (dt.datetime.now().astimezone()
                      + dt.timedelta(seconds=config.BUNDLE_FIRST_POST_S))
    heads = {v: (d["state"].get("text") or "") for v, d in plan["videos"].items()}
    return bundle.summary_text(bid, heads, plan["slots"], start,
                               _platform_labels(platforms),
                               plan["notes"] + plan["failures"], scheduled)


async def _render_bundle(q, context, bid: int) -> None:
    """🎬 Render bundle: check every video is ready, assign, render, caption,
    and post the one summary with 🚀 Publish / 🔀 Reshuffle."""
    bndl = await asyncio.to_thread(queue_store.get_bundle, bid)
    if bndl is None or bndl["status"] != "open":
        await q.answer(f"Bundle #{bid} is "
                       f"{bndl['status'] if bndl else 'gone'}.", show_alert=True)
        return
    rows = await asyncio.to_thread(queue_store.bundle_videos, bid)
    if not rows:
        await q.answer("The bundle is empty.", show_alert=True)
        return
    states = {}
    for row in rows:
        st = _pending.get(row["card_id"])
        if st is None or st.get("bundle_video") != row["id"]:
            st = _bundle_state(row, bid, bndl["platforms"])
        states[str(row["id"])] = st
    order = list(states)
    need = [i + 1 for i, v in enumerate(order)
            if not (states[v].get("info") or "").strip()]
    if need:
        await q.answer("Waiting for an info: reply on video "
                       + ", ".join(map(str, need)) + ".", show_alert=True)
        return
    busy = [i + 1 for i, v in enumerate(order)
            if _task_running(states[v].get("headline_task"))]
    if busy:
        await q.answer("Headlines are still being written for video "
                       + ", ".join(map(str, busy)) + " — tap again in a moment.",
                       show_alert=True)
        return
    if not await asyncio.to_thread(queue_store.claim_bundle, bid, "open",
                                   "rendering"):
        await q.answer()
        return
    await q.answer()
    accounts = [b["name"] for b in _bundle_accounts()]
    keys = {v: states[v].get("video_key") or "" for v in order}
    hist = await asyncio.to_thread(_history, accounts, keys)
    assignment, carried = bundle.assign(order, accounts, hist)
    notes = []
    if carried:
        new_bid = await asyncio.to_thread(queue_store.open_bundle, CHAT_ID,
                                          bndl["platforms"])
        for v in carried:
            await asyncio.to_thread(queue_store.save_bundle_video, int(v),
                                    bundle_id=new_bid)
            states[v]["bundle_id"] = new_bid
            order.remove(v)
        notes.append(f"↪️ {len(carried)} video{'s' if len(carried) != 1 else ''}"
                     f" carried over to the next bundle (#{new_bid})")
    for v in order:
        states[v]["mode"] = "rendering"
        _pending.pop(next((r["card_id"] for r in rows if str(r["id"]) == v),
                          None), None)
    for a in bundle.clashes(assignment, hist):
        notes.append(f"⚠️ {a} has posted video "
                     f"{order.index(assignment[a]) + 1} before — no other "
                     "way to fill every account")
    chat = q.message.chat
    status = _track(await chat.send_message(
        f"⏳ rendering bundle #{bid}: {len(order)} video"
        f"{'s' if len(order) != 1 else ''} → {len(assignment)} accounts …"))
    plan = {"assignment": assignment, "renders": {}, "notes": notes,
            "failures": [], "slots": [],
            "videos": {v: {"n": i + 1, "state": _persistable(states[v])}
                       for i, v in enumerate(order)}}
    try:
        with _busy("rendering a bundle"):
            await _render_pairs(context.bot, chat, bid, states, assignment,
                                plan, bndl["platforms"])
    except Exception as exc:
        log.exception("bundle #%d render failed", bid)
        plan["failures"].append(f"❌ render stopped: {str(exc)[:200]}")
    plan["slots"] = _slots_for(plan)
    for v in order:   # the source files now belong to the plan, not a picker
        plan["videos"][v]["state"] = _persistable(states[v])
    await asyncio.to_thread(queue_store.update_bundle, bid, plan=plan,
                            status="ready", message_id=status.message_id)
    if not plan["slots"]:
        await status.edit_text(_summary(bid, plan, bndl["platforms"])
                               + "\n❌ nothing rendered — nothing to publish",
                               reply_markup=bundle.open_keyboard(bid))
        await asyncio.to_thread(queue_store.claim_bundle, bid, "ready", "open")
        return
    if config.BUNDLE_AUTO_PUBLISH:
        await _publish_bundle(context, bid, status)
        return
    await status.edit_text(_summary(bid, plan, bndl["platforms"]),
                           reply_markup=bundle.ready_keyboard(bid))


async def _reshuffle_bundle(q, context, bid: int) -> None:
    """🔀 Reshuffle: a new random assignment; only the pairs never rendered
    before are rendered (the old ones stay on disk until publish/cancel)."""
    if not await asyncio.to_thread(queue_store.claim_bundle, bid, "ready",
                                   "rendering"):
        await q.answer("This bundle can't be reshuffled now.", show_alert=True)
        return
    await q.answer()
    bndl = await asyncio.to_thread(queue_store.get_bundle, bid)
    plan = bndl["plan"]
    order = list(plan["videos"])
    states = {v: _bundle_state({"id": int(v), "state": plan["videos"][v]["state"]},
                               bid, bndl["platforms"]) for v in order}
    accounts = [b["name"] for b in _bundle_accounts()]
    keys = {v: states[v].get("video_key") or "" for v in order}
    hist = await asyncio.to_thread(_history, accounts, keys)
    old = plan["assignment"]
    for _ in range(10):   # a re-draw that changes nothing is no reshuffle
        assignment, _carried = bundle.assign(order, accounts, hist)
        if assignment != old or len(order) == 1:
            break
    plan.update(assignment=assignment, failures=[])
    await q.edit_message_text(f"⏳ reshuffling bundle #{bid} …")
    try:
        with _busy("rendering a bundle"):
            await _render_pairs(context.bot, q.message.chat, bid, states,
                                assignment, plan, bndl["platforms"])
    except Exception as exc:
        log.exception("bundle #%d reshuffle failed", bid)
        plan["failures"].append(f"❌ render stopped: {str(exc)[:200]}")
    plan["slots"] = _slots_for(plan)
    await asyncio.to_thread(queue_store.update_bundle, bid, plan=plan,
                            status="ready")
    await q.edit_message_text(_summary(bid, plan, bndl["platforms"]),
                              reply_markup=bundle.ready_keyboard(bid))


def _unused_files(plan: dict, used: set) -> list:
    """Every render and source file of a plan that `used` doesn't contain."""
    paths = [r["path"] for per in plan["renders"].values() for r in per.values()]
    for d in plan["videos"].values():
        paths += list(d["state"].get("files") or ())
    return [p for p in paths if p not in used]


async def _publish_bundle(context, bid: int, message) -> None:
    """🚀 Publish: one scheduled post per account at its timetable slot."""
    if not await asyncio.to_thread(queue_store.claim_bundle, bid, "ready",
                                   "scheduled"):
        return
    bndl = await asyncio.to_thread(queue_store.get_bundle, bid)
    plan = bndl["plan"]
    brands = {b["name"]: b for b in branded.available_brands(config.BRANDS)}
    copies = bundle.copies({a: v for _, a, v in plan["slots"]})
    start = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
        seconds=config.BUNDLE_FIRST_POST_S)
    notes, used = [], set()
    for at, account, vid in plan["slots"]:
        b, r = brands.get(account), plan["renders"][vid][account]
        if b is None:
            notes.append(f"⚠️ {account}: no longer configured — skipped")
            continue
        when = start + dt.timedelta(seconds=at)
        if b.get("ig") and "ig" in bndl["platforms"]:
            quota = await asyncio.to_thread(ig_graph.publishing_quota, b["ig"])
            if quota and quota[0] >= quota[1]:
                when += dt.timedelta(hours=24)
                notes.append(f"⏭ {account}: Instagram quota full "
                             f"({quota[0]}/{quota[1]}) — rolled to "
                             f"{schedule.fmt(when)}")
        render = {"brand": b, "path": r["path"], "headline": r["headline"],
                  "kind": "video"}
        offered = [p for p in branded.platforms_for([render], r["duration"])
                   if p["platform"] in bndl["platforms"]]
        if not offered:
            notes.append(f"⚠️ {account}: none of the bundle's platforms set "
                         "up — skipped")
            continue
        vstate = plan["videos"][vid]["state"]
        st = {"text": vstate.get("text", ""), "info": vstate.get("info", ""),
              "footage": vstate.get("footage") or {},
              "source": vstate.get("source") or {},
              "video_key": vstate.get("video_key", ""),
              "renders": [render], "platforms": offered,
              "sel_platforms": set(range(len(offered))),
              "captions": {account: r.get("caption", "")} if r.get("caption") else {},
              "bundle": {"id": bid, "video_key": vstate.get("video_key", ""),
                         "copy_count": copies.get(vid), "account": account,
                         "video": plan["videos"][vid]["n"]}}
        sched_id = await asyncio.to_thread(
            queue_store.add_scheduled, when, CHAT_ID, message.message_id,
            schedule.to_payload(st), bid)
        if context.job_queue is not None:
            _arm_scheduled(context.job_queue, sched_id, when)
        used.add(r["path"])
    _remove_files(_unused_files(plan, used))
    plan["notes"] = plan["notes"] + notes
    await asyncio.to_thread(queue_store.update_bundle, bid, plan=plan)
    log.info("bundle #%d scheduled: %d posts", bid, len(used))
    await message.edit_text(
        _summary(bid, plan, bndl["platforms"],
                 start.astimezone(), scheduled=True),
        reply_markup=bundle.scheduled_keyboard(bid))


async def _cancel_bundle(q, bid: int) -> None:
    """✕ on an open or rendered bundle: nothing has been scheduled yet."""
    bndl = await asyncio.to_thread(queue_store.get_bundle, bid)
    status = bndl["status"] if bndl else "gone"
    if status not in ("open", "ready") or not await asyncio.to_thread(
            queue_store.claim_bundle, bid, status, "cancelled"):
        await q.answer(f"Bundle #{bid} is {status}.", show_alert=True)
        return
    await q.answer()
    for row in await asyncio.to_thread(queue_store.bundle_videos, bid):
        st = _pending.pop(row["card_id"], None)
        if st is not None:
            _cleanup(st)
        _remove_files(row["state"].get("files") or ())
    if bndl["plan"]:
        _remove_files(_unused_files(bndl["plan"], set()))
    log.info("bundle #%d cancelled", bid)
    await q.edit_message_text(f"✕ bundle #{bid} closed — nothing was published")


async def _cancel_bundle_posts(q, context, bid: int) -> None:
    """✕ on a scheduled bundle: cancel every post still waiting."""
    n = 0
    for row in await asyncio.to_thread(queue_store.scheduled_for_bundle, bid,
                                       "pending"):
        if not queue_store.claim_scheduled(row["id"], "pending", "cancelled"):
            continue
        n += 1
        if context.job_queue is not None:
            for job in context.job_queue.get_jobs_by_name(
                    _sched_job_name(row["id"])):
                job.schedule_removal()
        _remove_files(r["path"] for r in row["payload"]["renders"])
    await q.answer(f"{n} post{'s' if n != 1 else ''} cancelled.", show_alert=True)
    try:
        await q.edit_message_text(q.message.text + f"\n✕ {n} waiting post"
                                  f"{'s' if n != 1 else ''} cancelled")
    except BadRequest:
        pass


async def _on_bundle_callback(q, context) -> None:
    """"u:<verb>:<bundle id>" — DB-routed, so it works after a restart."""
    try:
        _, verb, bid = q.data.split(":", 2)
        bid = int(bid)
    except ValueError:
        await q.answer()
        return
    if verb == "go":
        await _render_bundle(q, context, bid)
    elif verb == "pub":
        await q.answer()
        await _publish_bundle(context, bid, q.message)
    elif verb == "rs":
        await _reshuffle_bundle(q, context, bid)
    elif verb == "x":
        await _cancel_bundle(q, bid)
    elif verb == "xs":
        await _cancel_bundle_posts(q, context, bid)
    else:
        await q.answer()


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/report — every account's latest reel on Instagram, Facebook and
    YouTube with its views / likes / comments (modules/telegram/report.py).
    Read live from the platforms, so it takes a few seconds."""
    msg = update.effective_message
    note = _track(await msg.reply_text("📊 collecting the latest reel of every "
                                       "account …"))
    try:
        results = await asyncio.to_thread(reach_report.collect, config.BRANDS)
        pages = reach_report.render(results)
    except Exception as exc:  # collect never raises; render is pure — belt and braces
        log.error("report failed: %s", exc)
        await note.edit_text(f"❌ report failed: {str(exc)[:300]}")
        return
    await note.edit_text(pages[0], parse_mode="HTML",
                         disable_web_page_preview=True)
    for page in pages[1:]:
        _track(await msg.chat.send_message(page, parse_mode="HTML",
                                           disable_web_page_preview=True))


# What the group's command menu (the "/" button) offers. /autopilot and /next
# still work but are left out: unattended posting is prohibited (CLAUDE.md).
_MENU = [
    BotCommand("report", "📊 latest reel of every account: views, likes, comments"),
    BotCommand("bundle", "the open bundle with its buttons"),
    BotCommand("scheduled", "posts waiting to go out"),
    BotCommand("queue", "what's in the news queue"),
]


async def cmd_bundle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/bundle — the open bundle (or the rendered one waiting for Publish)
    with its buttons, for when the operator scrolled away from them."""
    msg = update.effective_message
    bndl = await asyncio.to_thread(queue_store.current_bundle, CHAT_ID)
    if bndl is not None:
        rows = await asyncio.to_thread(queue_store.bundle_videos, bndl["id"])
        views = [dict(_pending.get(r["card_id"]) or r["state"]) for r in rows]
        _track(await msg.reply_text(bundle.open_text(bndl["id"], views),
                                    reply_markup=bundle.open_keyboard(bndl["id"])))
        return
    ready = await asyncio.to_thread(queue_store.bundles_with_status, "ready")
    if ready:
        b = ready[-1]
        _track(await msg.reply_text(_summary(b["id"], b["plan"], b["platforms"]),
                                    reply_markup=bundle.ready_keyboard(b["id"])))
        return
    _track(await msg.reply_text("📦 no open bundle — tap 📦 Bundle on a "
                                "video's card to start one"))


def _restore_bundles(app) -> None:
    """Startup: open bundles' video lines back into _pending (their info
    replies and taps keep working); a render a restart cut off goes back to
    open, with a note."""
    for bndl in queue_store.bundles_with_status("rendering"):
        queue_store.claim_bundle(bndl["id"], "rendering", "open")
        app.create_task(app.bot.send_message(
            CHAT_ID, f"⚠️ bundle #{bndl['id']}'s render was interrupted by a "
            "restart — /bundle, then 🎬 Render bundle again"))
    for bndl in queue_store.bundles_with_status("open"):
        for row in queue_store.bundle_videos(bndl["id"]):
            if row["card_id"]:
                _pending[row["card_id"]] = _bundle_state(
                    row, bndl["id"], bndl["platforms"])
        log.info("bundle #%d restored (open)", bndl["id"])


# --------------------------------------------------------------------------- #
# Reel insights and the weekly report                                          #
# --------------------------------------------------------------------------- #

# A permission problem is reported to the group once per process, not per reel.
_insights_warned = False


async def _insights_job(context) -> None:
    """Hourly: Reels insights (views, reach, shares, saves, comments) and the
    account's follower count at 24 h and 72 h after each logged reel."""
    global _insights_warned
    for window, hours in (("24h", 24), ("72h", 72)):
        rows = await asyncio.to_thread(queue_store.reels_due,
                                       f"insights_{window}", hours)
        for r in rows:
            data, why = await asyncio.to_thread(ig_graph.reel_insights,
                                                r["media_id"], r["account"])
            fol = await asyncio.to_thread(ig_graph.followers, r["account"])
            await asyncio.to_thread(queue_store.save_insights, r["id"], window,
                                    data if data is not None else {"error": why},
                                    fol)
            if data is None and not _insights_warned:
                _insights_warned = True
                log.error("reel insights unreadable for %s: %s", r["account"], why)
                await context.bot.send_message(
                    CHAT_ID, f"⚠️ can't read Reels insights for {r['account']}: "
                    f"{why}\nThe token needs the instagram_business_manage_"
                    "insights permission; the weekly report will lack views "
                    "until it has it.")


async def _weekly_report_job(context) -> None:
    """Mondays: last week's reels per account / bundle / copy count, then the
    Account status reminder."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
    rows = await asyncio.to_thread(queue_store.reels_since, since)
    _track(await context.bot.send_message(
        CHAT_ID, bundle.report_text(rows, "last 7 days")))
    _track(await context.bot.send_message(CHAT_ID, bundle.REMINDER))


# --------------------------------------------------------------------------- #
# Scheduled publishing (modules/telegram/schedule.py)                         #
# --------------------------------------------------------------------------- #
#
# 🕒 Schedule on the publish picker stores the post in queue_store's
# `scheduled` table and arms a JobQueue job for it; startup re-arms whatever is
# still pending, so a deploy doesn't lose a schedule. The renders move into
# media/scheduled/ — the nightly sweep and the startup sweep only look at the
# flat media/ directory, and either would delete a render that has to wait
# longer than a day. A post that was mid-publish when the process died is NOT
# fired again (some platforms may already have it): it is marked failed and
# the operator is told to check.

_SCHED_DIR = os.path.join(config.TG_DATA_DIR, "media", "scheduled")


def _sched_job_name(sched_id: int) -> str:
    return f"sched-{sched_id}"


def _arm_scheduled(job_queue, sched_id: int, when: dt.datetime) -> None:
    delay = (when - dt.datetime.now(dt.timezone.utc)).total_seconds()
    job_queue.run_once(_fire_scheduled, when=max(1.0, delay), data=sched_id,
                       name=_sched_job_name(sched_id))


def _remove_files(paths) -> None:
    for path in paths:
        try:
            os.remove(path)
        except OSError:
            pass


async def _schedule_post(context, state: dict, message, when: dt.datetime,
                         reply_msg=None) -> None:
    """Turn an open publish picker into a scheduled post. `message` is the
    picker; `reply_msg` is the operator's time reply, when that's how the time
    came in (errors are answered there)."""
    async def refuse(why: str) -> None:
        if reply_msg is not None:
            _track(await reply_msg.reply_text(f"🕒 can't schedule: {why}"))

    why = schedule.check(when)
    if why:
        await refuse(why)
        return
    if context.job_queue is None:
        await refuse("the bot has no job queue (python-telegram-bot[job-queue])")
        return
    if not state["sel_platforms"]:
        await refuse("no platform is ticked")
        return

    state["mode"] = "scheduled"  # nothing else may act on this picker now
    _pending.pop(message.message_id, None)
    renders = {r["path"] for r in state["renders"]}
    try:
        os.makedirs(_SCHED_DIR, exist_ok=True)
        for r in state["renders"]:
            new = os.path.join(_SCHED_DIR, os.path.basename(r["path"]))
            os.replace(r["path"], new)
            r["path"] = new
    except OSError as exc:
        log.error("scheduling failed moving the renders: %s", exc)
        state["files"] += [r["path"] for r in state["renders"]]
        _cleanup(state)
        await message.edit_text(f"❌ couldn't schedule: {exc}")
        return
    # Everything else this post downloaded (the source clip) can go now.
    state["files"] = [p for p in state["files"] if p not in renders]
    _cleanup(state)

    sched_id = queue_store.add_scheduled(when, CHAT_ID, message.message_id,
                                         schedule.to_payload(state))
    _arm_scheduled(context.job_queue, sched_id, when)
    ticked = schedule.ticked_labels(state["platforms"], state["sel_platforms"])
    log.info("scheduled #%d for %s → %s", sched_id, when.isoformat(),
             ", ".join(ticked))
    await message.edit_text(
        schedule.scheduled_text(sched_id, when, ticked, state.get("text", "")),
        reply_markup=schedule.cancel_keyboard(sched_id))


async def _fire_scheduled(context) -> None:
    await _run_scheduled(context.bot, context.job.data)


async def _run_scheduled(bot, sched_id: int) -> None:
    """Publish one scheduled post, exactly once."""
    if not queue_store.claim_scheduled(sched_id, "pending", "running"):
        return  # cancelled, or already fired
    row = queue_store.get_scheduled(sched_id)
    brands = branded.available_brands(config.BRANDS)
    state, missing = schedule.from_payload(row["payload"], brands)
    state["note"] = f"🕒 scheduled #{sched_id}"
    if missing:
        state["note"] += "\n⚠️ no longer configured, skipped: " + ", ".join(missing)
    try:
        msg = _track(await bot.send_message(
            CHAT_ID, f"⏳ scheduled #{sched_id} is going out …",
            reply_to_message_id=row["message_id"],
            allow_sending_without_reply=True))
        if not state["sel_platforms"]:
            _remove_files(state["files"])
            await msg.edit_text(state["note"] + "\n❌ nothing left to publish")
            queue_store.claim_scheduled(sched_id, "running", "failed")
        else:
            await _do_publish(msg, bot, state)
            failed = state.get("failed_platforms") or []
            if state.get("bundle") and failed:
                # Only the platforms that failed go again on 🔁 Retry.
                payload = row["payload"]
                payload["selected"] = [j for j, p in enumerate(payload["platforms"])
                                       if p["platform"] in failed]
                queue_store.set_scheduled_payload(sched_id, payload)
                queue_store.claim_scheduled(sched_id, "running", "failed")
                account = state["bundle"].get("account", "this account")
                _track(await bot.send_message(
                    CHAT_ID, f"❌ bundle #{state['bundle'].get('id')}: "
                    f"{account} failed on " + ", ".join(failed),
                    reply_to_message_id=msg.message_id,
                    allow_sending_without_reply=True,
                    reply_markup=bundle.retry_keyboard(sched_id, account)))
            else:
                queue_store.claim_scheduled(sched_id, "running", "done")
    except Exception:
        log.exception("scheduled #%d failed", sched_id)
        queue_store.claim_scheduled(sched_id, "running", "failed")
        _remove_files(state["files"])
    try:
        await bot.edit_message_text(
            f"🕒 scheduled #{sched_id} went out {schedule.fmt(schedule.now())} "
            "— results below",
            chat_id=CHAT_ID, message_id=row["message_id"])
    except Exception:  # card deleted (weekly wipe) — the results still landed
        pass


async def _on_schedule_retry(q, context) -> None:
    """🔁 Retry ("s:<id>:r") on a failed bundle post: the failed platforms
    only, now. DB-routed like Cancel."""
    try:
        sched_id = int(q.data.split(":")[1])
    except (IndexError, ValueError):
        await q.answer()
        return
    if not queue_store.claim_scheduled(sched_id, "failed", "pending"):
        await q.answer("Nothing to retry — it already went, or is going.",
                       show_alert=True)
        return
    await q.answer()
    try:
        await q.edit_message_text(q.message.text + "\n🔁 retrying …")
    except BadRequest:
        pass
    context.application.create_task(_run_scheduled(context.bot, sched_id))


async def _on_schedule_cancel(q, context) -> None:
    """✕ Cancel schedule ("s:<id>:x") — DB-routed, so it works after a restart."""
    try:
        sched_id = int(q.data.split(":")[1])
    except (IndexError, ValueError):
        await q.answer()
        return
    if not queue_store.claim_scheduled(sched_id, "pending", "cancelled"):
        row = queue_store.get_scheduled(sched_id)
        status = row["status"] if row else "gone"
        await q.answer(f"Too late — this post is {status}.", show_alert=True)
        return
    await q.answer()
    if context.job_queue is not None:
        for job in context.job_queue.get_jobs_by_name(_sched_job_name(sched_id)):
            job.schedule_removal()
    row = queue_store.get_scheduled(sched_id)
    _remove_files(r["path"] for r in row["payload"]["renders"])
    log.info("scheduled #%d cancelled", sched_id)
    await q.edit_message_text(f"✕ scheduled #{sched_id} cancelled — nothing "
                              "was published")


def _restore_scheduled(app) -> None:
    """Startup: re-arm pending posts; report any a restart cut off mid-publish."""
    for row in queue_store.scheduled_with_status("running"):
        queue_store.claim_scheduled(row["id"], "running", "failed")
        _remove_files(r["path"] for r in row["payload"]["renders"])
        log.error("scheduled #%d was interrupted mid-publish by a restart — "
                  "some platforms may have it, the rest don't", row["id"])
        app.create_task(app.bot.send_message(
            CHAT_ID, f"⚠️ scheduled #{row['id']} was interrupted mid-publish by "
            "a restart. Some platforms may already have it — check the "
            "accounts before reposting.",
            reply_to_message_id=row["message_id"],
            allow_sending_without_reply=True))
    pending = queue_store.scheduled_with_status("pending")
    for row in pending:
        _arm_scheduled(app.job_queue, row["id"], row["due_at"])
    if pending:
        log.info("scheduled posts re-armed: %s", ", ".join(
            f"#{r['id']} {schedule.fmt(r['due_at'])}" for r in pending))


async def cmd_scheduled(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/scheduled — what is waiting to go out, soonest first."""
    _track(update.effective_message)
    rows = queue_store.scheduled_with_status("pending", "running")
    if not rows:
        _track(await update.effective_message.reply_text("🕒 nothing scheduled"))
        return
    lines = []
    for r in rows:
        p = r["payload"]
        ticked = [p["platforms"][i]["label"] for i in p["selected"]
                  if i < len(p["platforms"])]
        state = " (publishing now)" if r["status"] == "running" else ""
        lines.append(f"#{r['id']} · {schedule.fmt(r['due_at'])} "
                     f"({schedule.fmt_in(r['due_at'])}){state} → "
                     f"{' · '.join(ticked)}\n   📰 {p.get('text', '')[:120]}")
    _track(await update.effective_message.reply_text(
        "🕒 scheduled:\n\n" + "\n\n".join(lines)))


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

    # Scheduled posts and bundles live in the DB, not in _pending — routed
    # before it.
    if (q.data or "").startswith("s:"):
        if q.data.endswith(":r"):
            await _on_schedule_retry(q, context)
        else:
            await _on_schedule_cancel(q, context)
        return
    if (q.data or "").startswith(f"{bundle.NS}:"):
        await _on_bundle_callback(q, context)
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

    if data == "submit" and not (state["sel_tg"] or state["sel_yt"] or state["sel_tw"]):
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

    if data.startswith("x:"):
        state["sel_tw"] ^= {int(data.split(":", 1)[1])}  # toggle
        await q.edit_message_reply_markup(_keyboard(state))
        return

    if data.startswith("e:"):
        state["sel_em"] ^= {int(data.split(":", 1)[1])}  # toggle
        await q.edit_message_reply_markup(_keyboard(state))
        return

    if data.startswith("g:"):
        gs = _dest_groups(state)
        i = int(data.split(":", 1)[1])
        if i < len(gs):   # a keyboard from before the config changed
            sel = groups.toggle_dest_group(gs[i], _sel_map(state))
            state["sel_tg"], state["sel_yt"], state["sel_tw"] = (
                sel["tg"], sel["yt"], sel["tw"])
        await q.edit_message_reply_markup(_keyboard(state))
        return

    if data in ("custom", "groups"):
        state["custom"] = data == "custom"
        await q.edit_message_reply_markup(_keyboard(state))
        return

    # "All" covers every destination offered for this post — including the
    # ones no brand claims, which no group row can reach.
    if data == "all":
        tg_dests, yt_dests, tw_dests = _dest_lists(state)
        state["sel_tg"] = set(range(len(tg_dests)))
        state["sel_yt"] = set(range(len(yt_dests)))
        state["sel_tw"] = set(range(len(tw_dests)))
        await q.edit_message_reply_markup(_keyboard(state))
        return

    # "All" is about channels only — nobody wants all eight reactions at once.
    # "None" is a full reset, reactions included.
    if data == "none":
        state["sel_tg"] = set()
        state["sel_yt"] = set()
        state["sel_tw"] = set()
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
        dests_tw = [config.TW_DESTINATIONS[i] for i in sorted(state["sel_tw"])]
        emojis = [EMOJI_SERVICES[i] for i in sorted(state["sel_em"])]
        _pending.pop(q.message.message_id, None)
        names = ([d["chat_id"] for d in dests_tg]
                 + [f"YT {d['chat_id']}" for d in dests_yt]
                 + [f"𝕏 {d['chat_id']}" for d in dests_tw])
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

        if dests_tw:
            posted, errors = await _post_to_twitter(
                context.bot, state["text"], state["media"], dests_tw
            )
            if posted:
                lines.append("✅ 𝕏: " + ", ".join(posted))
            for account, err in errors:
                lines.append(f"❌ 𝕏 {account}: {err}")

        _cleanup(state)
        await q.edit_message_text("\n".join(lines))


def _sweep_orphans() -> None:
    """Picker state is memory-only, so URL downloads left behind by a restart
    have no owner — remove them. Same for public-media copies older than an
    hour: a crash between expose() and cleanup() must not leave a file
    public forever."""
    try:
        swept = public_media.sweep()
        if swept:
            log.info("public media: swept %d leftover file(s)", swept)
    except Exception as exc:
        log.warning("public media sweep failed: %s", exc)
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


async def _media_sweep_job(context) -> None:
    """Nightly 04:30 local: delete media older than TG_MEDIA_KEEP_H.

    _sweep_orphans() only runs at startup and only knows two prefixes; this is
    the durable half, and it is what keeps data/media from growing without
    bound (it reached 17 GB / 3176 files before this job existed). Blocking
    unlinks go to a thread so the bot keeps answering while it runs."""
    media_dir = os.path.join(config.TG_DATA_DIR, "media")
    try:
        deleted, freed, failed = await asyncio.to_thread(
            cleanup.sweep_media, media_dir, config.TG_MEDIA_KEEP_H)
    except Exception:
        log.exception("media sweep crashed — tomorrow retries")
        return
    if failed:
        # One aggregate line, not one per file: errmail mails every ERROR.
        log.warning("media sweep: %d file(s) could not be deleted", failed)
    log.info("media sweep: %d file(s), %.2f GB freed", deleted, freed / 2**30)


IG_TOKEN_MAX_AGE_DAYS = 7


async def _ig_token_refresh_job(context) -> None:
    """Daily: refresh every IG_GRAPH token older than IG_TOKEN_MAX_AGE_DAYS
    (long-lived tokens last 60 days; refreshing weekly keeps a wide margin).
    A failed refresh is logged at ERROR so errmail mails the operator — a
    token that silently expires means IG publishing dies in ~2 months."""
    try:
        results = await asyncio.to_thread(
            ig_graph.refresh_stale, config.IG_GRAPH_ACCOUNTS, IG_TOKEN_MAX_AGE_DAYS)
    except Exception:
        log.exception("IG token refresh job crashed — tomorrow retries")
        return
    for account, result in results:
        if result.get("status") == "success":
            log.info("IG token refreshed for %s", account)
        else:
            log.error("IG token refresh FAILED for %s: %s — re-mint it via the "
                      "Meta app if this persists (60-day expiry)",
                      account, result.get("error"))


async def _heartbeat_job(context) -> None:
    """Dead-man's switch: prove the event loop is alive to healthchecks.io.
    Running as a JobQueue job is the point — a wedged loop stops the pings."""
    await asyncio.to_thread(heartbeat.ping, config.HEALTHCHECK_URL_NEWSBOT)


async def _on_start(app) -> None:
    """Queue the autopilot's first tick once the event loop is running, register
    the weekly cleanup, and connect the big-file client so its state is known
    (and logged) before the first post rather than discovered mid-upload."""
    autopilot.schedule(app)
    try:
        await app.bot.set_my_commands(_MENU, scope=BotCommandScopeChat(CHAT_ID))
    except Exception as exc:  # a missing menu is cosmetic; the commands still work
        log.warning("command menu not set: %s", exc)
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
        # 04:30, half an hour after the weekly wipe so the two never overlap.
        # No `days=` — this one runs every night.
        app.job_queue.run_daily(_media_sweep_job,
                                time=dt.time(4, 30, tzinfo=local_tz),
                                name="media-sweep")
        log.info("media sweep scheduled: daily 04:30 %s (keeping < %dh)",
                 local_tz, config.TG_MEDIA_KEEP_H)
        if config.IG_GRAPH_ACCOUNTS:
            # first=120 s: a fresh deploy checks the tokens right away, so a
            # broken refresh shows up in the log today, not in a week.
            app.job_queue.run_repeating(_ig_token_refresh_job, interval=24 * 3600,
                                        first=120, name="ig-token-refresh")
            log.info("IG token refresh scheduled daily for %s (tokens older than %d d)",
                     ", ".join(config.IG_GRAPH_ACCOUNTS), IG_TOKEN_MAX_AGE_DAYS)
        _restore_scheduled(app)
        _restore_bundles(app)
        app.job_queue.run_repeating(_insights_job, interval=3600, first=300,
                                    name="reel-insights")
        app.job_queue.run_daily(_weekly_report_job,
                                time=dt.time(9, 0, tzinfo=local_tz),
                                days=(cleanup.MONDAY,), name="weekly-report")
        log.info("bundles restored; reel insights hourly; weekly report "
                 "Mondays 09:00 %s", local_tz)
    else:
        log.warning("no job queue — weekly cleanup and scheduled posts NOT "
                    "scheduled (install python-telegram-bot[job-queue])")
    if await mtproto.ensure_ready():
        log.info("big files: on — videos up to 2 GB can be branded and sent to YouTube")
    else:
        log.info("big files: off (%s) — videos over 20 MB can only be posted "
                 "to Telegram channels", mtproto.unavailable_reason())


async def _on_shutdown(app) -> None:
    """Release the Telethon session file on the way out."""
    await mtproto.close()


# Telegram expires a callback query id within about a minute. A tap on a picker
# left over from before a restart arrives after that window (the update sat in
# the getUpdates backlog while the bot was down), so the spinner ack raises.
_STALE_CALLBACK = "query is too old"


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log handler exceptions; the stale-callback ack at WARNING, not ERROR.

    Nothing failed to publish when it fires - only `q.answer()`, the spinner
    ack - but errmail mails every ERROR, and without an error handler PTB logs
    each one. Everything else is logged at ERROR exactly as before, so real
    failures still reach the operator.
    """
    err = context.error
    if isinstance(err, BadRequest) and _STALE_CALLBACK in str(err).lower():
        log.warning("stale callback query ignored: %s", err)
        return
    log.error("unhandled error while processing %s", update, exc_info=err)


def main() -> None:
    errmail.install("news_bot")  # every logged ERROR -> one email to the operator
    _write_busy()  # nothing is running yet: clear a marker a crash left behind
    _sweep_orphans()
    queue_store.init()  # the autopilot reads/writes the same DB as the collector
    # PTB's 5 s read timeout is too tight for the VPS: Telegram delivered a
    # "⏳ downloading …" reply but answered after 5 s, the send raised
    # TimedOut and the handler died before the download started — leaving a
    # note that never updates (2026-09-28).
    app = (Application.builder().token(config.NEWS_BOT_TOKEN)
           .connect_timeout(15).read_timeout(30).write_timeout(60)
           .post_init(_on_start).post_shutdown(_on_shutdown).build())
    control = filters.Chat(CHAT_ID)
    app.add_handler(CommandHandler("queue", cmd_queue, filters=control))
    app.add_handler(CommandHandler("autopilot", cmd_autopilot, filters=control))
    app.add_handler(CommandHandler("asks", cmd_asks, filters=control))
    app.add_handler(CommandHandler("next", cmd_next, filters=control))
    app.add_handler(CommandHandler("scheduled", cmd_scheduled, filters=control))
    app.add_handler(CommandHandler("bundle", cmd_bundle, filters=control))
    app.add_handler(CommandHandler("report", cmd_report, filters=control))
    # Chat filter limits the bot to the control group; ~COMMAND skips /commands.
    app.add_handler(MessageHandler(control & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)
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
        "twitter: %d account(s): %s",
        len(config.TW_DESTINATIONS),
        ", ".join(f"{d['chat_id']}({d['lang'] or 'raw'})" for d in config.TW_DESTINATIONS) or "—",
    )
    log.info(
        "instagram (graph api): %d account(s): %s | media hosting: %s",
        len(config.IG_GRAPH_ACCOUNTS), ", ".join(config.IG_GRAPH_ACCOUNTS) or "—",
        config.PUBLIC_MEDIA_BASE_URL or "NOT CONFIGURED (IG publishing will fail)",
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
