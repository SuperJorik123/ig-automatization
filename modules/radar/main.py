"""Radar — the live process: listens, polls, alerts. Never posts anywhere else.

    py -m modules.radar --login   one-time login of the LISTENING account: scan a QR
                                  with its phone (--phone for number + code instead)
    py -m modules.radar --join    join channels.json's channels, slowly
    py -m modules.radar --check   which channels are joined / missing
    py -m modules.radar --hazards print what the hazard feeds would alert now
    py -m modules.radar           run (systemd: deploy/radar.service)

Everything it sends goes to RADAR_CHAT_ID only. It holds no posting token of
any platform (see settings.py) — the alerts-only exception to the no-unattended-
posting rule depends on that.
"""
import argparse
import asyncio
import datetime as dt
import logging
import os
import random
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

from modules.radar import alert as A, calendar_, settings as S
from modules.radar.pipeline import Pipeline
from modules.radar.sources import hazards, reddit, telegram as tgsrc
from modules.radar.store import Store

log = logging.getLogger("radar")

FAIL_WARN_AFTER = 3


def _markup(rows):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    if not rows:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row]
                                 for row in rows])


def _handles(entity) -> set:
    """Every @username a channel answers to, lower-cased. Channels with
    collectible (Fragment) usernames keep them in `usernames`, and their
    `username` is then None — matching only `username` missed 3 of 22."""
    names = {(getattr(entity, "username", "") or "").lower()}
    for u in getattr(entity, "usernames", None) or []:
        names.add((getattr(u, "username", "") or "").lower())
    names.discard("")
    return names


class Notifier:
    """Puts things in the Radar group: the bot writes, the listening account
    forwards the videos (a bot can't forward from a channel it isn't in)."""

    def __init__(self, bot, client=None, clock=time.time):
        self.bot, self.client, self.clock = bot, client, clock

    async def _send(self, text, rows=None, reply_to=None, preview=False):
        msg = await self.bot.send_message(
            S.CHAT_ID, text, parse_mode="HTML", reply_markup=_markup(rows),
            reply_to_message_id=reply_to, disable_web_page_preview=not preview,
            allow_sending_without_reply=True)
        return msg.message_id

    async def _video(self, item_row):
        """Bring a Telegram video into the group; its message id, or None.
        Forward first (no download at all); a channel that forbids forwarding
        gets a copy when it's under RADAR_MAX_COPY_MB."""
        if self.client is None or item_row is None or not item_row["source"].startswith("tg:"):
            return None
        import json
        ref = json.loads(item_row["ref"] or "{}")
        try:
            sent = await self.client.forward_messages(S.CHAT_ID, ref["msg_id"], ref["chat_id"])
            sent = sent[0] if isinstance(sent, list) else sent
            return sent.id
        except Exception as exc:
            log.info("forward refused (%s) — copying instead", exc)
        if item_row["media_size"] > S.MAX_COPY_MB * 1024 * 1024:
            return None
        try:
            msg = await self.client.get_messages(ref["chat_id"], ids=ref["msg_id"])
            with tempfile.TemporaryDirectory() as tmp:
                path = await self.client.download_media(msg, file=os.path.join(tmp, "clip"))
                sent = await self.client.send_file(S.CHAT_ID, path, supports_streaming=True)
            return sent.id
        except Exception as exc:
            log.warning("couldn't copy the video: %s", exc)
            return None

    async def alert(self, inc, items, video_row):
        reply_to = await self._video(video_row)
        text = A.incident_text(inc, items, self.clock())
        return await self._send(text, A.keyboard(inc), reply_to=reply_to,
                                preview=reply_to is None)

    async def update(self, inc, items, note, new_video_row, ping):
        text = A.incident_text(inc, items, self.clock(), note=note)
        try:
            await self.bot.edit_message_text(text, S.CHAT_ID, inc["alert_msg_id"],
                                             parse_mode="HTML", reply_markup=_markup(A.keyboard(inc)),
                                             disable_web_page_preview=True)
        except Exception as exc:
            log.info("alert edit skipped: %s", exc)
        if ping:
            vid = await self._video(new_video_row)
            await self._send(f"👀 {note}", reply_to=vid or inc["alert_msg_id"])

    async def hazard(self, item):
        return await self._send(A.hazard_text(item, self.clock()))

    async def text(self, html_text):
        return await self._send(html_text)


class Radar:
    def __init__(self):
        self.store = Store(S.DB_PATH)
        self.client = None
        self.pipe = None
        self.health = {}       # source -> {"fails": n, "warned": bool, "ok": ts}
        self.seeded = set()

    # --- health -------------------------------------------------------------
    async def _ok(self, name):
        h = self.health.setdefault(name, {"fails": 0, "warned": False, "ok": 0})
        if h["warned"]:
            await self.pipe.notifier.text(f"✅ {A.esc(name)} is back")
        h.update(fails=0, warned=False, ok=time.time())

    async def _fail(self, name, exc):
        h = self.health.setdefault(name, {"fails": 0, "warned": False, "ok": 0})
        h["fails"] += 1
        log.warning("%s failed (%d): %s", name, h["fails"], exc)
        if h["fails"] >= FAIL_WARN_AFTER and not h["warned"]:
            h["warned"] = True
            await self.pipe.notifier.text(
                f"⚠️ {A.esc(name)} has failed {h['fails']}× in a row: {A.esc(str(exc))[:300]}")

    # --- jobs ---------------------------------------------------------------
    async def poll_reddit(self, _ctx=None):
        try:
            items = await asyncio.to_thread(reddit.fetch)
        except Exception as exc:
            await self._fail("Reddit", exc)
            return
        await self._ok("Reddit")
        for it in items:
            await self.pipe.ingest(it)

    async def poll_hazards(self, _ctx=None):
        for name, fn in hazards.FETCHERS.items():
            try:
                items = await asyncio.to_thread(fn)
            except Exception as exc:
                await self._fail(name.upper(), exc)
                continue
            await self._ok(name.upper())
            if name not in self.seeded:
                # First poll since start: alert only what's new to the DB.
                known = [it for it in items if self.store.db.execute(
                    "SELECT 1 FROM items WHERE source=? LIMIT 1", (it.source,)).fetchone()]
                if not known:
                    self.pipe.seed(items)
                    self.seeded.add(name)
                    continue
                self.seeded.add(name)
            for it in items:
                await self.pipe.ingest(it)

    async def check_spikes(self, _ctx=None):
        try:
            await self.pipe.check_spikes()
        except Exception:
            log.exception("spike check failed")

    async def briefing(self, _ctx=None):
        today = dt.datetime.now(ZoneInfo(S.TZ)).date()
        await self.pipe.notifier.text(self.pipe.briefing(today, calendar_.upcoming(today)))

    async def prune(self, _ctx=None):
        self.store.prune(time.time())

    # --- telegram listener ----------------------------------------------------
    async def start_listener(self):
        if not (S.API_ID and S.API_HASH):
            log.warning("no RADAR_API_ID/HASH — Telegram channels off")
            return
        from telethon import TelegramClient, events
        os.makedirs(S.DATA_DIR, exist_ok=True)
        client = TelegramClient(S.SESSION, int(S.API_ID), S.API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            await self.pipe.notifier.text(
                "⚠️ The listening account isn't logged in — Telegram channels are off. "
                "Run <code>py -m modules.radar --login</code> on the server.")
            await client.disconnect()
            return
        wanted = {h.lower() for h in tgsrc.load_channels()}
        watch = {}
        async for d in client.iter_dialogs():
            hit = _handles(d.entity) & wanted if d.is_channel else set()
            if hit:
                watch[d.id] = next(h for h in tgsrc.load_channels() if h.lower() in hit)
        missing = wanted - {u.lower() for u in watch.values()}
        if missing:
            log.warning("not joined (run --join): %s", ", ".join(sorted(missing)))
        self.client = client
        self.pipe.notifier.client = client

        async def handle(msgs, chat_id):
            uname = watch.get(chat_id)
            if not uname:
                return
            video = next((m for m in msgs if tgsrc.to_item(m, uname, chat_id).has_video), None)
            base = video or msgs[0]
            it = tgsrc.to_item(base, uname, chat_id)
            if not it.text:
                it.text = next((m.message for m in msgs if getattr(m, "message", "")), "")
            if it.has_video:
                try:
                    it.thumb = await client.download_media(base, bytes, thumb=-1)
                except Exception:
                    it.thumb = None
            try:
                await self.pipe.ingest(it)
            except Exception:
                log.exception("ingest failed for %s", it.link)

        @client.on(events.Album(chats=list(watch)))
        async def _on_album(event):
            await handle(event.messages, event.chat_id)

        @client.on(events.NewMessage(chats=list(watch)))
        async def _on_message(event):
            if event.message.grouped_id:
                return
            await handle([event.message], event.chat_id)

        log.info("listening to %d channel(s)", len(watch))

    # --- bot ---------------------------------------------------------------
    async def on_button(self, update, context):
        q = update.callback_query
        if q.message is None or q.message.chat_id != S.CHAT_ID:
            await q.answer()
            return
        parsed = A.parse_callback(q.data)
        if not parsed:
            await q.answer()
            return
        verb, inc_id, reason = parsed
        if verb == "x":
            await q.answer()
            await q.edit_message_reply_markup(_markup(A.reasons_keyboard(inc_id)))
            return
        inc = self.pipe.on_button(verb, inc_id, reason)
        await q.answer("noted" if verb == "xr" else "")
        if inc is None:
            return
        items = self.store.incident_items(inc_id)
        if items:
            await q.edit_message_text(A.incident_text(inc, items, time.time()), parse_mode="HTML",
                                      reply_markup=_markup(A.keyboard(inc)),
                                      disable_web_page_preview=True)

    async def cmd_status(self, update, context):
        if update.effective_chat.id != S.CHAT_ID:
            return
        now = time.time()
        counts = self.store.counts_since(now - 86400)
        fam = {}
        for src, n in counts.items():
            f = src.split(":", 1)[0]
            fam[f] = fam.get(f, 0) + n
        day = dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y-%m-%d")
        lines = ["📡 <b>Radar status</b> (last 24 h)",
                 "items: " + (", ".join(f"{k} {v}" for k, v in sorted(fam.items())) or "none"),
                 f"alerts: {self.store.alerts_since(now - 86400)}",
                 f"AI labels today: {self.store.ai_calls(day)}/{S.AI_MAX_PER_DAY}",
                 f"Telegram listener: {'on' if self.client else 'OFF'}"]
        for name, h in sorted(self.health.items()):
            mark = "✅" if not h["fails"] else f"⚠️ {h['fails']} fails"
            lines.append(f"{name}: {mark}")
        await update.effective_message.reply_html("\n".join(lines))

    async def cmd_brief(self, update, context):
        if update.effective_chat.id == S.CHAT_ID:
            await self.briefing()

    def run(self):
        from telegram.ext import Application, CallbackQueryHandler, CommandHandler

        if not S.BOT_TOKEN or not S.CHAT_ID:
            raise SystemExit(f"RADAR_BOT_TOKEN / RADAR_CHAT_ID missing in {S.ENV_FILE}")
        if not str(S.CHAT_ID).startswith("-100"):
            log.warning("RADAR_CHAT_ID isn't a supergroup (-100…): forwarded videos and "
                        "replies may not line up — upgrade the group")

        async def post_init(app):
            self.pipe = Pipeline(self.store, Notifier(app.bot))
            try:
                await self.start_listener()
            except Exception:
                log.exception("Telegram listener failed to start")
            jq = app.job_queue
            if S.REDDIT_CLIENT_ID and S.REDDIT_CLIENT_SECRET:
                jq.run_repeating(self.poll_reddit, S.REDDIT_POLL_S, first=20, name="reddit")
            else:
                log.info("Reddit off — no REDDIT_CLIENT_ID/SECRET (keyless JSON is 403)")
            jq.run_repeating(self.poll_hazards, S.HAZARD_POLL_S, first=10, name="hazards")
            jq.run_repeating(self.check_spikes, S.SPIKE_CHECK_S, first=60, name="spikes")
            jq.run_repeating(self.prune, 86400, first=300, name="prune")
            if S.BRIEFING_AT:
                hh, mm = (int(x) for x in S.BRIEFING_AT.split(":"))
                jq.run_daily(self.briefing, dt.time(hh, mm, tzinfo=ZoneInfo(S.TZ)), name="briefing")
            await app.bot.set_my_commands([("status", "What the radar is watching"),
                                           ("brief", "The briefing, now")])
            log.info("radar running — alerts to %s", S.CHAT_ID)

        async def post_shutdown(app):
            if self.client:
                await self.client.disconnect()

        app = (Application.builder().token(S.BOT_TOKEN)
               .post_init(post_init).post_shutdown(post_shutdown).build())
        app.add_handler(CallbackQueryHandler(self.on_button, pattern=r"^r:"))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("brief", self.cmd_brief))
        app.run_polling(allowed_updates=["message", "callback_query"])


# --- CLI ----------------------------------------------------------------------
async def _login(qr: bool):
    from telethon import TelegramClient
    os.makedirs(S.DATA_DIR, exist_ok=True)
    client = TelegramClient(S.SESSION, int(S.API_ID), S.API_HASH)
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"already logged in as @{me.username or me.id}")
            return
        if qr:
            # Scan with the account's phone — no number, no code. The token
            # lives ~30 s, so it's redrawn until scanned (Ctrl+C gives up).
            from getpass import getpass

            import qrcode
            from telethon import errors
            login = await client.qr_login()
            while True:
                q = qrcode.QRCode(border=1)
                q.add_data(login.url)
                q.print_ascii(invert=True)
                print("On the SPARE account's phone: Telegram → Settings → Devices → "
                      "Link Desktop Device, and scan this. Waiting… (Ctrl+C to give up)")
                try:
                    await login.wait(30)
                    break
                except asyncio.TimeoutError:
                    await login.recreate()
                    print("\n(expired — here's a fresh one)\n")
                except errors.SessionPasswordNeededError:
                    await client.sign_in(password=getpass("2FA password: "))
                    break
        else:
            # Phone login wants the international form: +40…, not 07…
            await client.start()
        me = await client.get_me()
        print(f"✅ logged in as @{me.username or me.id} — session at {S.SESSION}")
    finally:
        await client.disconnect()


async def _join():
    from telethon import TelegramClient, errors
    from telethon.tl.functions.channels import JoinChannelRequest
    client = TelegramClient(S.SESSION, int(S.API_ID), S.API_HASH)
    await client.connect()
    try:
        joined = set()
        async for d in client.iter_dialogs():
            if d.is_channel:
                joined |= _handles(d.entity)
        todo = [h for h in tgsrc.load_channels() if h.lower() not in joined]
        print(f"{len(todo)} channel(s) to join")
        for h in todo:
            try:
                await client(JoinChannelRequest(h))
                print(f"joined @{h}")
            except errors.FloodWaitError as e:
                print(f"Telegram says wait {e.seconds}s — stopping; run --join again later")
                return
            except Exception as exc:
                print(f"@{h}: {exc}")
            # Slowly: mass-joining is what gets an account limited.
            await asyncio.sleep(random.uniform(40, 90))
    finally:
        await client.disconnect()


async def _check():
    from telethon import TelegramClient
    client = TelegramClient(S.SESSION, int(S.API_ID), S.API_HASH)
    await client.connect()
    try:
        joined = set()
        async for d in client.iter_dialogs():
            if d.is_channel:
                joined |= _handles(d.entity)
        for h in tgsrc.load_channels():
            print(("✅ " if h.lower() in joined else "❌ not joined ") + "@" + h)
    finally:
        await client.disconnect()


def _hazards_now():
    now = time.time()
    for name, fn in hazards.FETCHERS.items():
        try:
            items = fn()
        except Exception as exc:
            print(f"{name}: FAILED {exc}")
            continue
        hits = [it for it in items if it.metrics.get("alert")]
        print(f"{name}: {len(items)} item(s), {len(hits)} would alert")
        for it in hits:
            print("  " + A.hazard_text(it, now).replace("\n", "\n  "))


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for noisy in ("httpx", "apscheduler", "telethon.network"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    p = argparse.ArgumentParser(prog="radar")
    p.add_argument("--login", action="store_true")
    p.add_argument("--phone", action="store_true",
                   help="log in with phone number + code instead of scanning a QR")
    p.add_argument("--join", action="store_true")
    p.add_argument("--check", action="store_true")
    p.add_argument("--hazards", action="store_true")
    a = p.parse_args(argv)
    if a.hazards:
        return _hazards_now()
    if a.login or a.join or a.check:
        if not (S.API_ID and S.API_HASH):
            raise SystemExit(f"RADAR_API_ID / RADAR_API_HASH missing in {S.ENV_FILE}")
        coro = _login(not a.phone) if a.login else _join() if a.join else _check()
        return asyncio.run(coro)
    Radar().run()


if __name__ == "__main__":
    sys.exit(main())
