"""
modules/telegram/mtproto.py — big-file transfers through a Telethon USER client.

Why this exists: the HTTP Bot API refuses `getFile` above 20 MB and `sendFile`
above 50 MB, so a clip you upload into the control group by hand could not be
pulled onto disk for the YouTube leg or for branding. Your own account has no
such ceiling (2 GB, 4 GB with Premium) and the collector already proves the
MTProto path works — this module reuses the same credentials to fetch the
ORIGINAL message and download it whole.

file_id vs message id: a Bot-API `file_id` is meaningless to a user client (the
access hashes are per-account), so the bot records `chat_id` + `msg_id` when the
message arrives and this module re-fetches the message from there. That also
means the account must be a member of the chat — it is, it's your control group.

Session: its own file. A Telethon session is a SQLite database that cannot be
shared by two live processes, and collector.py holds `collector.session`. One
interactive login, once:

    py modules/telegram/mtproto.py --login --qr      # scan with the phone
    py modules/telegram/mtproto.py --login --phone 078424624

Prefer --qr. Telegram delivers login codes IN the app (never by SMS) whenever
the account has an active session, and offers no fallback when that message
can't be found; scanning a QR from Settings -> Devices sidesteps codes
entirely. Local phone numbers are converted to international form for you.

Everything degrades gracefully. No credentials, no session, or a failed
download and `ensure_ready()` returns False — the caller falls back to the Bot
API's 20 MB path and says so in the group.
"""

import asyncio
import logging
import os
import sys

# Make the repo root importable so `from shared` resolves when run directly
# (`py modules/telegram/mtproto.py --login`).
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from telethon import TelegramClient  # noqa: E402
from telethon.tl.types import DocumentAttributeVideo  # noqa: E402

from shared import config  # noqa: E402

log = logging.getLogger("mtproto")

SESSION = os.path.join(config.TG_DATA_DIR, "bigfile.session")

# One client per process, created lazily inside the running loop (Telethon binds
# to the loop it is constructed on). _ready is tri-state: None = never tried.
_client = None
_ready = None
_lock = asyncio.Lock()


def human_size(n: int) -> str:
    """Size for operator-facing messages: MB up to a gigabyte, then GB."""
    mb = n / 1024 / 1024
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def is_configured() -> bool:
    """True when .env has the MTProto credentials and big files are enabled.

    Says nothing about whether the session is logged in — that needs a
    connection, which is `ensure_ready()`'s job."""
    return bool(
        config.TG_BIG_FILES and config.TELEGRAM_API_ID and config.TELEGRAM_API_HASH
    )


def unavailable_reason() -> str:
    """Why big-file transfer is off, phrased for the control group."""
    if not config.TG_BIG_FILES:
        return "big-file transfer is disabled (TG_BIG_FILES=0 in .env)"
    if not (config.TELEGRAM_API_ID and config.TELEGRAM_API_HASH):
        return ("TELEGRAM_API_ID / TELEGRAM_API_HASH missing in .env "
                "(get them at my.telegram.org)")
    return "not logged in — run `py modules/telegram/mtproto.py --login` once"


async def ensure_ready() -> bool:
    """Connect the user client and confirm it's authorised. Cached: the first
    call pays the connection, later ones are free. Never raises — a dead
    network here must only cost the caller its big-file path, not the post."""
    global _client, _ready
    if _ready is not None:
        return _ready
    async with _lock:
        if _ready is not None:  # another coroutine won the race
            return _ready
        if not is_configured():
            log.info("big-file transfer off: %s", unavailable_reason())
            _ready = False
            return False
        try:
            os.makedirs(config.TG_DATA_DIR, exist_ok=True)
            client = TelegramClient(
                SESSION, int(config.TELEGRAM_API_ID), config.TELEGRAM_API_HASH
            )
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                log.warning("big-file transfer off: %s", unavailable_reason())
                _ready = False
                return False
            # Populates the entity cache. Without it, resolving the control
            # group by its numeric id can fail with "could not find the input
            # entity" on a session that has never seen the chat.
            await client.get_dialogs()
            _client, _ready = client, True
            me = await client.get_me()
            log.info("big-file transfer ready as @%s (up to 2 GB)",
                     getattr(me, "username", None) or me.id)
            return True
        except Exception as exc:  # bad credentials, no network, corrupt session
            log.warning("big-file transfer unavailable: %s", exc)
            _ready = False
            return False


async def close() -> None:
    """Disconnect on shutdown so the session file is released cleanly."""
    global _client, _ready
    if _client is not None:
        try:
            await _client.disconnect()
        except Exception:
            pass
    _client, _ready = None, None


def _progress_logger(label: str, total: int):
    """Log every 10% — a multi-hundred-MB download is otherwise silent for
    minutes and looks like a hang in the terminal."""
    state = {"mark": 0}

    def cb(received: int, expected: int) -> None:
        expected = expected or total
        if not expected:
            return
        pct = int(received * 100 / expected)
        if pct >= state["mark"]:
            log.info("%s %d%% (%s / %s)", label, pct,
                     human_size(received), human_size(expected))
            state["mark"] = (pct // 10 + 1) * 10

    return cb


async def download(chat_id: int, msg_id: int, dest_stem: str) -> str:
    """Download the media of one message to disk and return the real path.

    `dest_stem` is a path WITHOUT an extension — Telethon appends the source's
    own one (.mp4/.mov/…), so nothing gets mislabelled. Raises if the client
    isn't ready, the message is gone, or it carries no media."""
    if not await ensure_ready():
        raise RuntimeError(unavailable_reason())
    msg = await _client.get_messages(chat_id, ids=msg_id)
    if msg is None:
        raise RuntimeError(f"message {msg_id} not found in chat {chat_id}")
    if not msg.media:
        raise RuntimeError(f"message {msg_id} carries no media")
    size = getattr(getattr(msg, "document", None), "size", 0) or 0
    log.info("downloading %s from %s/%s", human_size(size) if size else "media",
             chat_id, msg_id)
    path = await msg.download_media(
        file=dest_stem, progress_callback=_progress_logger("download", size)
    )
    if not path:
        raise RuntimeError("download produced no file")
    return path


async def send_video(chat_id: int, path: str, caption: str,
                     width: int = 0, height: int = 0, duration: int = 0):
    """Send a video the Bot API is too small to carry (>50 MB). Comes from your
    account rather than the bot — the only visible difference in the group."""
    if not await ensure_ready():
        raise RuntimeError(unavailable_reason())
    attrs = None
    if width and height:
        # Without explicit dimensions Telegram sizes the inline player from
        # defaults and plays the clip squashed.
        attrs = [DocumentAttributeVideo(duration=int(duration), w=int(width),
                                        h=int(height), supports_streaming=True)]
    total = os.path.getsize(path)
    return await _client.send_file(
        chat_id, path, caption=caption, attributes=attrs,
        supports_streaming=True,
        progress_callback=_progress_logger("upload", total),
    )


# --------------------------------------------------------------------------- #
# One-time login (--login)                                                    #
# --------------------------------------------------------------------------- #

# Country code assumed for a number typed in local form (leading 0). Moldova
# by default — override in .env if the account's number is from elsewhere.
DEFAULT_CC = (os.environ.get("TG_PHONE_CC", "373") or "373").lstrip("+")


def normalize_phone(raw: str, default_cc: str = None) -> str:
    """Local phone input -> the international form Telegram requires.

    Telegram rejects anything else with PhoneNumberInvalidError, so a Moldovan
    "078424624" has to become "+37378424624" before it ever leaves the machine:

        078424624       -> +37378424624   (leading 0 = local, swap in the CC)
        0037378424624   -> +37378424624   (00 = international prefix)
        +373 78 424 624 -> +37378424624   (separators dropped)
        37378424624     -> +37378424624   (already international, bare)
    """
    cc = (default_cc or DEFAULT_CC).lstrip("+")
    digits = "".join(ch for ch in (raw or "") if ch.isdigit() or ch == "+")
    if digits.startswith("+"):
        return "+" + digits[1:]
    if digits.startswith("00"):
        return "+" + digits[2:]
    if digits.startswith("0"):
        return "+" + cc + digits[1:]
    if not digits:
        raise ValueError("no phone number given")
    return "+" + digits


def _delivery_hint(sent) -> str:
    """Where Telegram says it put the code. This is the whole ballgame when
    "the code never arrives": Telegram picks in-app delivery whenever the
    account has an active session anywhere, and then `next_type` is None —
    there is no SMS fallback to wait for, however long you stare at the phone.
    """
    kind = type(getattr(sent, "type", None)).__name__
    n = getattr(getattr(sent, "type", None), "length", None)
    where = {
        "SentCodeTypeApp":
            "IN THE TELEGRAM APP — not by SMS. Open Telegram on any device "
            "that is already logged in as this account and read the chat "
            "named 'Telegram' (blue check). Check ARCHIVED CHATS too — the "
            "service chat is often buried there.",
        "SentCodeTypeSms": "by SMS.",
        "SentCodeTypeCall": "by phone call — pick up and note the digits.",
        "SentCodeTypeFlashCall": "by flash call.",
        "SentCodeTypeMissedCall": "by missed call — the code is the last "
                                  "digits of the calling number.",
    }.get(kind, f"via {kind or 'an unknown channel'}.")
    if n:
        where += f" The code is {n} digits."
    if getattr(sent, "next_type", None) is None and kind == "SentCodeTypeApp":
        where += " Telegram offers NO SMS fallback for this login."
    return where


def _print_qr(url: str) -> None:
    """Draw the login token as a QR in the terminal. `qrcode` is an optional
    dependency — without it the raw tg:// URL is still printed, which some
    desktop clients accept when opened directly."""
    try:
        import qrcode
    except ImportError:
        print("(pip install qrcode to see a scannable QR here)")
        print(url)
        return
    q = qrcode.QRCode(border=1)
    q.add_data(url)
    q.make(fit=True)
    q.print_ascii(invert=True)


async def _qr_login(client) -> None:
    """Log in by scanning a QR with an already-signed-in Telegram app — no
    code, no SMS. This is the way in when in-app codes never surface: the
    phone approves the session directly instead of relaying five digits.

    The token expires in well under a minute, so it is redrawn in a loop until
    it's scanned or the operator gives up."""
    from getpass import getpass

    from telethon import errors

    qr = await client.qr_login()
    print("\nOn your phone: Telegram → Settings → Devices → "
          "\"Link Desktop Device\", then scan this:\n")
    while True:
        _print_qr(qr.url)
        print("waiting for the scan … (Ctrl+C to give up)")
        try:
            await qr.wait(30)
            return
        except asyncio.TimeoutError:
            await qr.recreate()
            print("\n(token expired — here's a fresh one)\n")
        except errors.SessionPasswordNeededError:
            await client.sign_in(password=getpass("2FA password: "))
            return


async def _login_async(phone: str | None, force_sms: bool, qr: bool = False) -> None:
    """Interactive login with a proper teardown. The old `with client:` form
    left Telethon's loops running into a closed event loop on any failure,
    which is what buried the real error under pages of asyncio noise."""
    from getpass import getpass

    from telethon import errors

    os.makedirs(config.TG_DATA_DIR, exist_ok=True)
    client = TelegramClient(SESSION, int(config.TELEGRAM_API_ID),
                            config.TELEGRAM_API_HASH)
    try:
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"already logged in as @{getattr(me, 'username', None) or me.id}"
                  f" — nothing to do ({SESSION})")
            return

        if qr:
            await _qr_login(client)
            me = await client.get_me()
            print(f"✅ logged in as @{getattr(me, 'username', None) or me.id} — "
                  f"session saved to {SESSION}")
            print("news_bot.py can now download and send files up to 2 GB.")
            return

        if not phone:
            phone = input("Phone (078424624 or +37378424624): ")
        try:
            phone = normalize_phone(phone)
        except ValueError as exc:
            raise SystemExit(f"❌ {exc}")
        print(f"→ dialling {phone}")

        try:
            sent = await client.send_code_request(phone, force_sms=force_sms)
        except errors.PhoneNumberInvalidError:
            raise SystemExit(
                f"❌ Telegram says {phone} is not a valid number.\n"
                f"   Local numbers need the country code: 078424624 is "
                f"+{DEFAULT_CC}78424624.\n"
                f"   If the account isn't a +{DEFAULT_CC} one, set TG_PHONE_CC "
                f"in .env or type the number with a leading +.")
        except errors.FloodWaitError as exc:
            raise SystemExit(f"❌ too many attempts — Telegram wants "
                             f"{exc.seconds}s ({exc.seconds // 60} min) of quiet first.")
        print(f"📨 code sent {_delivery_hint(sent)}")

        code = input("Code: ").strip()
        try:
            await client.sign_in(phone=phone, code=code)
        except errors.SessionPasswordNeededError:
            await client.sign_in(password=getpass("2FA password: "))
        except errors.PhoneCodeInvalidError:
            raise SystemExit("❌ wrong code — run --login again.")
        except errors.PhoneCodeExpiredError:
            raise SystemExit("❌ that code expired — run --login again.")

        me = await client.get_me()
        print(f"✅ logged in as @{getattr(me, 'username', None) or me.id} — "
              f"session saved to {SESSION}")
        print("news_bot.py can now download and send files up to 2 GB.")
    finally:
        # Always tear the client down on the SAME loop that built it.
        try:
            await client.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if "--login" not in sys.argv:
        raise SystemExit(
            "usage: py modules/telegram/mtproto.py --login [--qr | --phone <number>] [--sms]\n"
            "  --qr     scan a QR with an already-signed-in Telegram app.\n"
            "           No code, no SMS — use this when in-app codes never arrive.\n"
            "  --phone  skip the prompt; local form (078424624) is accepted\n"
            "  --sms    ask Telegram for an SMS instead of an in-app code\n"
            "           (only works as a RETRY after an in-app code was sent)")
    if not (config.TELEGRAM_API_ID and config.TELEGRAM_API_HASH):
        raise SystemExit("TELEGRAM_API_ID / TELEGRAM_API_HASH missing in .env "
                         "(get them at my.telegram.org)")
    _phone = None
    if "--phone" in sys.argv:
        _i = sys.argv.index("--phone")
        if _i + 1 >= len(sys.argv):
            raise SystemExit("--phone needs a number after it")
        _phone = sys.argv[_i + 1]
    try:
        asyncio.run(_login_async(_phone, "--sms" in sys.argv, "--qr" in sys.argv))
    except KeyboardInterrupt:
        print("\naborted — nothing was saved")
