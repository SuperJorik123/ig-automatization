"""
telegram_bot.py — Telegram-triggered IG reel poster (multi-account).

On any Instagram or Twitter/X URL pasted to the configured Telegram
group, the bot:
  1. Replies with an inline keyboard listing every account in
     IG_ACCOUNTS so the user can toggle which accounts to post to
     (with All / None shortcuts and a final 'Post to selected' button).
  2. On submit: downloads the video + caption once (via reel_downloader
     / yt-dlp — for Twitter the trailing t.co redirect is stripped).
  3. Strips `#mirnews` from the caption.
  4. Writes the file + sidecar JSON into posts/.
  5. For each selected account, drives the phone via uiautomator2:
     switch profile (long-press Profile tab → tap target row) → run the
     standard upload_post flow → next account. Failures are per-account
     and don't stop the loop.
  6. Archives the file once after the loop (only if at least one
     account succeeded — total failure leaves it queued for manual
     retry).
  7. Replies with combined per-account status: ✅ for successes, ❌
     with the error for each failure.

Setup:
  - `pip install -r requirements.txt`
  - Fill `.env` with TELEGRAM_BOT_TOKEN (from @BotFather), TELEGRAM_CHAT_ID
    (group's numeric id; negative for groups), and IG_ACCOUNTS (comma-
    separated usernames logged in on the phone, no leading `@`).
  - Disable bot privacy mode in @BotFather (`/setprivacy` → Disable) so
    the bot sees plain group messages, not just commands.

Run:
  py telegram_bot.py
"""

import asyncio
import json
import os
import re
import sys
import traceback

import uiautomator2 as u2
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

HERE = os.path.dirname(os.path.abspath(__file__))
POSTS_DIR = os.path.join(HERE, "posts")

# Make sibling modules importable when run as a script.
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import reel_downloader  # noqa: E402
import upload_post as ig  # noqa: E402


# --------------------------------------------------------------------------- #
# Config                                                                      #
# --------------------------------------------------------------------------- #

load_dotenv(os.path.join(HERE, ".env"))
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
_CHAT_ID_RAW = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
if not TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN is missing — set it in .env.")
if not _CHAT_ID_RAW:
    raise SystemExit("TELEGRAM_CHAT_ID is missing — set it in .env.")
try:
    CHAT_ID = int(_CHAT_ID_RAW)
except ValueError as exc:
    raise SystemExit(
        f"TELEGRAM_CHAT_ID must be an integer, got {_CHAT_ID_RAW!r}"
    ) from exc

# Account list — drives the inline keyboard and the per-URL upload loop.
# Order in .env is preserved; that's the order rows render in the
# keyboard and the order uploads happen in.
_ACCOUNTS_RAW = os.environ.get("IG_ACCOUNTS", "").strip()
ACCOUNTS = [a.strip().lstrip("@") for a in _ACCOUNTS_RAW.split(",") if a.strip()]
if not ACCOUNTS:
    raise SystemExit(
        "IG_ACCOUNTS is empty — set it in .env to a comma-separated list of "
        "usernames logged in on the phone (no leading `@`)."
    )


# --------------------------------------------------------------------------- #
# Caption stripping                                                           #
# --------------------------------------------------------------------------- #

# Match `#mirnews` regardless of case, but only as a whole hashtag —
# the lookarounds reject `#mirnews1`, `things#mirnews`, etc.
_MIRNEWS_RE = re.compile(r"(?<!\w)#mirnews(?!\w)", re.IGNORECASE)


def strip_mirnews(caption: str) -> str:
    """Remove every `#mirnews` from `caption` and tidy whitespace left behind."""
    if not caption:
        return ""
    cleaned = _MIRNEWS_RE.sub("", caption)
    # Collapse runs of spaces/tabs that the removed tag left behind, and
    # any triple-or-more newlines that result when #mirnews stood alone
    # on a line.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


# --------------------------------------------------------------------------- #
# Filename allocation — match server.py so the two never collide              #
# --------------------------------------------------------------------------- #

_INDEX_RE = re.compile(r"^(\d+)\.(?:jpg|jpeg|png|mp4|mov|json)$", re.IGNORECASE)


def _next_index() -> int:
    """Walk posts/ + posts/posted/ for NNN.* files, return max+1. Never
    reuse a number even after archive — keeps the running queue unique."""
    used = set()
    for root, _dirs, files in os.walk(POSTS_DIR):
        for f in files:
            m = _INDEX_RE.match(f)
            if m:
                used.add(int(m.group(1)))
    return (max(used) + 1) if used else 1


# --------------------------------------------------------------------------- #
# The actual work — synchronous, runs in a worker thread                      #
# --------------------------------------------------------------------------- #

def process_url_for_accounts(url, accounts):
    """Download once, then upload to each account sequentially.

    Returns a dict {account: (success_bool, error_msg_or_None)}. One
    account's failure doesn't stop the loop — caller renders per-account
    status from the dict.

    Raises if the download itself fails (no account-specific work
    happened, so the caller renders a single 'download failed' reply)."""
    os.makedirs(POSTS_DIR, exist_ok=True)
    idx = _next_index()
    name = f"{idx:03d}"
    stub = os.path.join(POSTS_DIR, f"{name}.mp4")

    file_path, raw_caption = reel_downloader.download_media(url, stub, kind="reel")
    clean_caption = strip_mirnews(raw_caption)

    # Sidecar JSON in the shape upload_post.find_next_post reads —
    # caption as a list of lines, empty hashtag array (the caption
    # already contains any tags inline, and upload_post auto-detects
    # that case).
    json_path = os.path.splitext(file_path)[0] + ".json"
    with open(json_path, "w", encoding="utf-8") as fp:
        json.dump(
            {"caption": clean_caption.splitlines(), "hashtags": []},
            fp,
            ensure_ascii=False,
            indent=2,
        )

    results = {}
    d = u2.connect(ig.DEVICE_ID)
    for acc in accounts:
        try:
            ig.upload_post(
                d, file_path, clean_caption, [], kind="reel", target_account=acc
            )
            results[acc] = (True, None)
        except Exception as exc:
            tb_short = "".join(
                traceback.format_exception_only(type(exc), exc)
            ).strip()
            results[acc] = (False, tb_short)

    # Archive only when at least one account succeeded. If every one
    # failed (download blip, IG-side outage, …), leave the file in posts/
    # so it can be retried manually without re-running yt-dlp.
    if any(success for success, _ in results.values()):
        ig.archive_post(POSTS_DIR, file_path)

    return results


# --------------------------------------------------------------------------- #
# Async glue                                                                  #
# --------------------------------------------------------------------------- #

# The phone can only run one IG flow at a time and uiautomator2 calls are
# blocking, so we funnel every incoming URL through a single asyncio.Lock
# and offload the actual work to a worker thread (keeps the bot
# responsive to new chat messages while one upload is mid-flight).
_upload_lock = asyncio.Lock()

# Per-prompt state: maps the Telegram message_id of a keyboard prompt
# the bot sent → {"url": str, "selected": set[str]}. In-memory only;
# bot restart wipes pending selections, which then render as
# "prompt expired" if the user comes back to tap something.
_pending = {}


def _build_keyboard(selected):
    """Render the inline keyboard from the current selection state."""
    rows = []
    for acc in ACCOUNTS:
        mark = "☑" if acc in selected else "☐"
        rows.append([
            InlineKeyboardButton(f"{mark} @{acc}", callback_data=f"toggle:{acc}")
        ])
    rows.append([
        InlineKeyboardButton("All", callback_data="all"),
        InlineKeyboardButton("None", callback_data="none"),
    ])
    rows.append([InlineKeyboardButton("▶ Post to selected", callback_data="submit")])
    rows.append([InlineKeyboardButton("✕ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Detect IG / X URLs in incoming group messages and post the
    account-selection keyboard."""
    msg = update.effective_message
    if msg is None:
        return
    text = msg.text or msg.caption or ""
    m = reel_downloader.SUPPORTED_URL_RE.search(text)
    if not m:
        return
    url = m.group(0)

    prompt = await msg.reply_text(
        f"Post {url} to which accounts?",
        reply_markup=_build_keyboard(set()),
    )
    _pending[prompt.message_id] = {"url": url, "selected": set()}


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle taps on the account-selection keyboard."""
    q = update.callback_query

    # Defensive: only act on callbacks from prompts in the configured
    # chat. Anything else gets a silent ack so the spinner clears.
    if q.message is None or q.message.chat.id != CHAT_ID:
        await q.answer()
        return

    state = _pending.get(q.message.message_id)
    if state is None:
        await q.answer()
        await q.edit_message_text("(prompt expired — paste the URL again)")
        return

    data = q.data

    # Submit-with-empty-selection raises the only popup we ever show
    # (everything else is a silent ack), so handle it before the
    # blanket q.answer() below.
    if data == "submit" and not state["selected"]:
        await q.answer("Pick at least one account.", show_alert=True)
        return

    await q.answer()

    if data.startswith("toggle:"):
        acc = data.split(":", 1)[1]
        if acc in state["selected"]:
            state["selected"].discard(acc)
        else:
            state["selected"].add(acc)
        await q.edit_message_reply_markup(_build_keyboard(state["selected"]))
        return

    if data == "all":
        state["selected"] = set(ACCOUNTS)
        await q.edit_message_reply_markup(_build_keyboard(state["selected"]))
        return

    if data == "none":
        state["selected"] = set()
        await q.edit_message_reply_markup(_build_keyboard(state["selected"]))
        return

    if data == "cancel":
        _pending.pop(q.message.message_id, None)
        await q.edit_message_text(f"✕ cancelled: {state['url']}")
        return

    if data == "submit":
        url = state["url"]
        # Preserve .env order in the upload loop (not selection order).
        selected = [a for a in ACCOUNTS if a in state["selected"]]
        _pending.pop(q.message.message_id, None)
        await q.edit_message_text(
            f"⏳ queued for {', '.join('@' + a for a in selected)}: {url}"
        )
        async with _upload_lock:
            try:
                results = await asyncio.to_thread(
                    process_url_for_accounts, url, selected
                )
            except Exception as exc:
                tb_short = "".join(
                    traceback.format_exception_only(type(exc), exc)
                ).strip()
                await context.bot.send_message(
                    chat_id=q.message.chat.id,
                    text=f"❌ download failed for {url}: {tb_short}",
                )
                return
        ok = [a for a, (success, _) in results.items() if success]
        bad = [(a, err) for a, (success, err) in results.items() if not success]
        lines = []
        if ok:
            lines.append("✅ posted to " + ", ".join("@" + a for a in ok))
        for a, err in bad:
            lines.append(f"❌ @{a}: {err}")
        await context.bot.send_message(
            chat_id=q.message.chat.id, text="\n".join(lines)
        )


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #

def main() -> None:
    app = Application.builder().token(TOKEN).build()
    # Chat filter limits the handler to the configured group; ~COMMAND
    # skips slash-commands so we don't accidentally treat /start as a URL.
    app.add_handler(
        MessageHandler(filters.Chat(CHAT_ID) & ~filters.COMMAND, on_message)
    )
    app.add_handler(CallbackQueryHandler(on_callback))
    print(f"Bot listening on chat {CHAT_ID} — paste IG / X URLs in the group.")
    print(f"Accounts: {', '.join('@' + a for a in ACCOUNTS)}")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
