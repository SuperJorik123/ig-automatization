"""
modules/telegram/autopilot.py — the Telegram drip.

Every random 18-24 hours (TG_DRIP_MIN_S..TG_DRIP_MAX_S) it publishes the single
best story the smart filter is holding — sent to the channels whose region it
matters to, translated per channel — then places the per-post BulkFollows order
and asks the operator which reactions to buy.

    candidates (queue_store)      score + freshness + not yet posted here
      ->  region match            smart_filter.telegram_targets
      ->  head-to-head compare    smart_filter.best_of   <- picks the winner
      ->  translate + fan out     publisher
      ->  record + order  ->  reaction ask

The comparison matters at this cadence: scoring judges each story alone as it
arrives, so a day's queue clusters at 70-80 with no way to rank within it.
With one slot a day, the finalists are weighed against each other at the
moment of publishing.

It does not score anything: the dispatcher is the only writer of scores, so
there is exactly one brain and no race over the queue. With the dispatcher
stopped this module simply finds no candidates and says so.

Hosted by news_bot.py as a JobQueue job — the reaction ask needs callback
handling, and one bot token allows exactly one poller.

CLI (a scored queue is required; --dry-run sends nothing):
    py modules/telegram/autopilot.py --once --dry-run
    py modules/telegram/autopilot.py --once
"""

import asyncio
import logging
import os
import random
import sys
from datetime import datetime, timedelta, timezone

# Repo-root bootstrap for direct runs (`py modules/telegram/autopilot.py`).
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shared import config  # noqa: E402
from modules.telegram import publisher, queue_store, reactions, smart_filter  # noqa: E402

log = logging.getLogger(__name__)

# How many scored items to look at per tick. The top-scoring one may target
# regions you don't run a channel for; without a look-ahead it would sit at the
# head of the queue and block the drip forever.
CANDIDATE_LOOKAHEAD = 10

# Nothing to post → check back sooner than a full drip gap, since the
# collector may queue something at any moment.
IDLE_RETRY_S = 10 * 60

# First tick after the bot starts. Short enough to see it working, long enough
# that a restart loop doesn't machine-gun the queue.
STARTUP_DELAY_S = 5 * 60

_JOB_NAME = "tg-autopilot"

# Runtime on/off (the /autopilot command). Starts from TG_AUTOPILOT in .env.
_enabled = config.TG_AUTOPILOT


def is_enabled() -> bool:
    return _enabled


def set_enabled(on: bool) -> None:
    global _enabled
    _enabled = bool(on)
    log.info("autopilot %s", "enabled" if _enabled else "paused")


def ask_chat_id():
    """Where reaction asks go. Numeric ids are sent as ints; @usernames stay
    strings."""
    raw = config.TG_ASK_CHAT_ID
    try:
        return int(raw)
    except (TypeError, ValueError):
        return raw


def _present_media(item: dict) -> list:
    """The item's media, minus any local file that has since disappeared.

    Collected media lives on disk under data/media and can be cleaned up
    behind our back; posting the text alone beats failing every channel."""
    out = []
    for m in item.get("media") or []:
        if m.get("file_id") or (m.get("path") and os.path.exists(m["path"])):
            out.append(m)
        else:
            log.warning("item %s: media file missing, posting without it: %s",
                        item.get("id"), m.get("path"))
    return out


def pick(compare: bool = True) -> tuple[dict, list] | tuple[None, None]:
    """The (item, targets) pair to post right now, or (None, None).

    Two stages, both from the smart filter: eligible stories are gathered by
    score + region, then the finalists are compared head-to-head so the slot
    gets the best of the batch rather than whatever happened to score highest
    in isolation. `compare=False` skips that second call — a cheap preview
    that just reports the top-scoring candidate.

    No side effects, so /queue and --dry-run can call it freely."""
    dests = config.TG_DESTINATIONS
    if not dests:
        return None, None
    pairs = []
    for item in queue_store.candidates(
        "telegram", config.TG_AUTO_MIN_SCORE, config.TG_MAX_AGE_H, CANDIDATE_LOOKAHEAD
    ):
        targets = smart_filter.telegram_targets(item, dests)
        if targets:  # a story aimed only at regions you don't run is skipped
            pairs.append((item, targets))
    if not pairs:
        return None, None
    return smart_filter.best_of(pairs) if compare else pairs[0]


def describe(item: dict, targets: list) -> str:
    """One-line preview of a pending post — used by /queue and --dry-run."""
    head = (item.get("text") or "(no text)").strip().replace("\n", " ")[:120]
    return (
        f"item {item['id']} · score {item['score']} · "
        f"regions {','.join(item['regions']) or '—'} · "
        f"{len(item.get('media') or [])} media\n"
        f"→ {', '.join(d['chat_id'] for d in targets)}\n"
        f"📝 {head}"
    )


async def deliver_ask(bot, ask_id: int, state: dict) -> None:
    """Send (or re-send) an ask's message and remember where it landed.

    Failure is logged, not raised: the post is already out and its per-post
    order already placed, and the ask row stays 'open' so /asks can retry."""
    text, keyboard = reactions.render(ask_id, state)
    try:
        msg = await bot.send_message(
            chat_id=ask_chat_id(), text=text, reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        log.error("could not send reaction ask %d: %s", ask_id, exc)
        return
    # Ask messages count for the weekly control-group wipe too.
    queue_store.track_group_message(ask_chat_id(), msg.message_id)
    queue_store.bind_ask(ask_id, ask_chat_id(), msg.message_id)


async def refresh_ask(bot, ask: dict) -> None:
    """Re-render an open ask in place, or send it again if its message is gone
    (or was never sent because the bot died between publishing and asking).
    Backs /asks."""
    if ask.get("message_id") and ask.get("chat_id"):
        text, keyboard = reactions.render(ask["id"], ask["state"])
        try:
            await bot.edit_message_text(
                chat_id=ask["chat_id"], message_id=ask["message_id"], text=text,
                reply_markup=keyboard, disable_web_page_preview=True,
            )
            return
        except Exception as exc:
            # Nothing changed since the last render — the message is already
            # correct, and re-sending would just duplicate it.
            if "not modified" in str(exc).lower():
                return
            log.warning("ask %d: edit failed (%s) — sending a fresh message", ask["id"], exc)
    await deliver_ask(bot, ask["id"], ask["state"])


async def tick(bot, dry_run: bool = False) -> str:
    """Run one drip. Returns a human-readable summary for the log and for
    whoever typed /next. Never raises — the caller is a scheduled job."""
    item, targets = pick()
    if item is None:
        if not config.TG_DESTINATIONS:
            return "no TG_DESTINATIONS configured — nothing to do"
        return "nothing to post (no scored item matches a channel)"

    if dry_run:
        return "DRY RUN — would post:\n" + describe(item, targets)

    media = _present_media(item)
    if config.NEWS_REQUIRE_MEDIA and not media:
        # It qualified as news when it was collected, but the files have since
        # been deleted — posting the text alone would break the media-only
        # rule, and the files are never coming back.
        queue_store.set_status(item["id"], "failed")
        log.warning("item %s: all media files are gone — skipped", item["id"])
        return f"item {item['id']}: media files are gone — skipped"

    log.info("autopilot posting %s", describe(item, targets).replace("\n", " | "))
    posted, errors, links = await publisher.publish(bot, item["text"], media, targets)
    for chat_id, err in errors:
        log.error("item %s: publish to %s failed: %s", item["id"], chat_id, err)

    if not posted:
        n = queue_store.bump_attempts(item["id"])
        return f"item {item['id']}: every channel failed (attempt {n})"

    by_chat = dict(links)
    for chat_id in posted:
        queue_store.record_post(item["id"], "telegram", chat_id, by_chat.get(chat_id))

    # The ask row is written before anything slow happens, so a crash between
    # publishing and asking still leaves something for /asks to recover.
    state = reactions.new_state(
        item["id"], [{"chat_id": c, "link": by_chat.get(c)} for c in posted]
    )
    ask_id = queue_store.open_ask(item["id"], state)

    # Per-post orders are blocking HTTP and independent of the reactions —
    # they fire now, whether or not the operator ever answers the ask.
    await asyncio.to_thread(reactions.record_posts, posted, links, None)
    await deliver_ask(bot, ask_id, state)

    summary = f"✅ item {item['id']} → " + ", ".join(posted)
    if errors:
        summary += " | ❌ " + ", ".join(c for c, _ in errors)
    return summary


# --------------------------------------------------------------------------- #
# Scheduling (news_bot hosts the job)                                         #
# --------------------------------------------------------------------------- #


def next_delay() -> int:
    """A fresh random gap. run_once + reschedule rather than run_repeating —
    a repeating job would be a fixed period, which is exactly the metronome
    we're avoiding."""
    lo, hi = config.TG_DRIP_MIN_S, config.TG_DRIP_MAX_S
    return random.randint(min(lo, hi), max(lo, hi))


def _pinned_delay() -> int | None:
    """Seconds until TG_FIRST_TICK ("HH:MM", local time), or None when it
    isn't set. Today if that hour is still ahead, otherwise tomorrow."""
    raw = config.TG_FIRST_TICK
    if not raw:
        return None
    try:
        hh, mm = (int(p) for p in raw.split(":", 1))
        now = datetime.now().astimezone()
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    except ValueError:
        log.error("TG_FIRST_TICK=%r is not HH:MM — ignoring it", raw)
        return None
    if target <= now:
        target += timedelta(days=1)
    log.info("first tick pinned to %s (TG_FIRST_TICK=%s)",
             target.strftime("%Y-%m-%d %H:%M %Z"), raw)
    return int((target - now).total_seconds())


def startup_delay() -> int:
    """Delay for the first tick after the bot starts.

    TG_FIRST_TICK wins when set. Otherwise this picks up the rhythm where the
    last post left it: if the previous drip was recent, it waits out the
    remainder of a normal gap; if not, it posts shortly after boot. Without
    that, a daily cadence would fire once per restart — five restarts, five
    posts in an afternoon."""
    pinned = _pinned_delay()
    if pinned is not None:
        return pinned

    last = queue_store.last_post_at("telegram")
    if last is None:
        return STARTUP_DELAY_S
    try:
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
    except ValueError:  # unparseable timestamp — treat it as ancient
        return STARTUP_DELAY_S
    return max(STARTUP_DELAY_S, int(next_delay() - elapsed))


async def _job(context) -> None:
    """JobQueue callback: tick if enabled, then always reschedule."""
    delay = next_delay()
    try:
        if not is_enabled():
            log.debug("autopilot paused — skipping tick")
        else:
            result = await tick(context.bot)
            log.info("autopilot: %s", result)
            if result.startswith("nothing to post"):
                delay = min(delay, IDLE_RETRY_S)
    except Exception:
        log.exception("autopilot tick crashed — rescheduling anyway")
    finally:
        schedule(context.application, delay)


def schedule(app, delay: int | None = None) -> None:
    """Queue the next tick. Any pending tick is dropped first so /next and a
    restart can't leave two drips running."""
    if app.job_queue is None:
        # PTB only builds a JobQueue when APScheduler is installed. Say so
        # once, loudly — the bot is otherwise fully functional, just manual.
        log.error("no JobQueue (install python-telegram-bot[job-queue]) — "
                  "autopilot disabled; manual posting and /next still work")
        return
    for job in app.job_queue.get_jobs_by_name(_JOB_NAME):
        job.schedule_removal()
    wait = startup_delay() if delay is None else delay
    log.info("next autopilot tick in %.1f h", wait / 3600)
    app.job_queue.run_once(_job, wait, name=_JOB_NAME)


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


async def _main_async(dry_run: bool) -> None:
    queue_store.init()
    if dry_run:
        item, targets = pick()
        print("nothing to post" if item is None
              else "DRY RUN — would post:\n" + describe(item, targets))
        return
    from telegram import Bot  # imported here so --dry-run needs no token

    bot = Bot(config.NEWS_BOT_TOKEN)
    async with bot:
        print(await tick(bot))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = set(sys.argv[1:])
    if "--once" not in args:
        raise SystemExit(
            "usage: py modules/telegram/autopilot.py --once [--dry-run]\n"
            "(the drip itself runs inside news_bot.py)"
        )
    asyncio.run(_main_async("--dry-run" in args))
