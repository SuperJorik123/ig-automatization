"""
modules/newsroom/main.py — the client newsroom bot.

One JobQueue job per configured site. Each tick: fetch the site's recent
WordPress posts, rewrite the ones not seen before, publish each to that site's
Telegram channel, and place the BulkFollows orders.

Run it:

    py modules/newsroom/main.py                      # the scheduler
    py modules/newsroom/main.py --once               # one tick per site, then exit
    py modules/newsroom/main.py --once --site acme   # one tick for one site
    py modules/newsroom/main.py --once --dry-run     # ...publishing nothing
    py modules/newsroom/main.py --force-latest --site acme   # re-post the newest
                                                     # article end to end (test path)

This bot only SENDS — it registers no command handlers and reads no updates.
It still needs a token nobody else is polling with, because python-telegram-bot
starts a getUpdates loop regardless and two pollers on one token break each
other.
"""

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from telegram.ext import Application  # noqa: E402

from shared import config  # noqa: E402
from shared.monitoring import errmail, heartbeat  # noqa: E402
from modules.newsroom import orders, publish, rewrite, store, wp  # noqa: E402

log = logging.getLogger("newsroom")

# Seconds between each site's first tick at startup. Seven sites firing on the
# same second is a needless burst against seven origins and the panel; spread
# out, the startup log is also readable.
STAGGER_S = 10


async def _handle(bot, site: dict, row, job_queue=None) -> bool:
    """Rewrite, publish and order one article. Returns True if it shipped.

    Every failure path marks the article and returns — an article that cannot
    be posted must never be retried forever, and must never block the ones
    behind it."""
    article = dict(row)
    try:
        text = await asyncio.to_thread(rewrite.to_telegram, article, site)
    except Exception as exc:  # rewrite is never supposed to raise; belt and braces
        log.error("[%s] rewrite blew up for %s: %s", site["name"], article.get("url"), exc)
        store.mark(row["id"], store.FAILED)
        return False

    if not text.strip():
        log.warning("[%s] empty post for %s — skipped", site["name"], article.get("url"))
        store.mark(row["id"], store.SKIPPED)
        return False

    message_id, link = await publish.publish(bot, site, article, text)

    if config.NR_DRY_RUN:
        # Marked handled, NOT posted: leaving them pending would re-run the
        # rewrite (and its cost) on every tick, and would dump the whole
        # dry-run backlog into the channel the moment dry-run is switched off.
        store.mark(row["id"], store.SKIPPED)
        return False

    if message_id is None:
        store.mark(row["id"], store.FAILED)
        return False

    post_id = store.record_post(row["id"], site["name"], site["chat_id"], message_id, link)
    store.mark(row["id"], store.POSTED)
    await asyncio.to_thread(orders.after_publish, site, post_id, link)
    _schedule_reactions(job_queue, site, post_id, link)
    return True


def _schedule_reactions(job_queue, site: dict, post_id: int, link: str | None) -> None:
    """Order this post's reactions after NR_REACTION_DELAY_S.

    Reactions appearing in the same second as the post is the clearest bot tell
    there is. The delay is in-memory: a restart inside the window loses that
    post's reactions, which is acceptable — the post and its views already
    shipped, and persisting it would mean a second scheduler for the least
    valuable order of the three."""
    if job_queue is None:
        # --once has no scheduler and cannot sit idle for twenty minutes.
        log.info("[%s] no scheduler — ordering reactions immediately", site["name"])
        orders.order_reactions(site, post_id, link)
        return

    async def _fire(_ctx):
        await asyncio.to_thread(orders.order_reactions, site, post_id, link)

    job_queue.run_once(_fire, when=config.NR_REACTION_DELAY_S,
                       name=f"reactions:{site['name']}:{post_id}")


async def tick(bot, site: dict, job_queue=None) -> str:
    """One poll of one site. Returns a one-line summary for the log.

    Never raises: a site that is down, moved, or has had its REST API disabled
    costs itself a tick and nothing else. The other six keep posting."""
    name = site["name"]
    try:
        articles = await asyncio.to_thread(wp.fetch_recent, site)
    except RuntimeError as exc:
        log.error("%s", exc)
        return f"[{name}] fetch failed"

    first_run = not store.has_articles(name)
    seen = store.seen_ids(name)
    fresh = [a for a in articles if a["wp_id"] not in seen]

    if first_run and not config.NR_BACKFILL:
        # The guard that stops enabling a site from dumping twenty
        # back-articles into a live channel, in front of the client's
        # subscribers, with no way to undo it.
        for article in fresh:
            store.add_article(name, article, status=store.SKIPPED)
        log.info("[%s] first tick — %d existing article(s) recorded as seen, "
                 "none posted (NR_BACKFILL=0)", name, len(fresh))
        return f"[{name}] first tick, {len(fresh)} recorded as seen"

    for article in fresh:
        store.add_article(name, article)

    pending = store.pending(name)
    if not pending:
        return f"[{name}] nothing new"

    posted = 0
    for row in pending:
        try:
            posted += await _handle(bot, site, row, job_queue)
        except Exception as exc:
            # One article's failure must not wedge the site's queue.
            log.exception("[%s] unhandled error on article %s: %s",
                          name, row["wp_id"], exc)
            store.mark(row["id"], store.FAILED)

    return f"[{name}] {posted}/{len(pending)} posted"


async def force_latest(bot, site: dict) -> str:
    """Take the site's newest article through the whole pipeline, seen or not.

    The test path: a site whose recent articles were all recorded (or that has
    published nothing new) still yields one real end-to-end run — rewrite,
    publish, orders, reactions. Runs without a scheduler, so the reactions are
    ordered inline like any --once tick.

    A first contact still honours the backfill guard for everything BUT the
    forced article: the others are recorded as seen, otherwise this run would
    make has_articles() true and the next normal tick would post the whole
    backlog as fresh."""
    name = site["name"]
    try:
        articles = await asyncio.to_thread(wp.fetch_recent, site)
    except RuntimeError as exc:
        log.error("%s", exc)
        return f"[{name}] fetch failed"
    if not articles:
        return f"[{name}] site returned no articles"

    latest = articles[0]  # the API returns newest first
    for article in articles[1:]:
        if not store.get_article(name, article["wp_id"]):
            store.add_article(name, article, status=store.SKIPPED)

    store.add_article(name, latest)  # no-op when already recorded
    row = store.get_article(name, latest["wp_id"])
    log.info("[%s] FORCED re-post of latest article: %s", name, latest.get("url"))
    shipped = await _handle(bot, site, row, job_queue=None)
    return f"[{name}] forced latest: {'posted' if shipped else 'not posted'}"


async def _run_force_latest(site: dict) -> None:
    app = Application.builder().token(config.NR_BOT_TOKEN).build()
    async with app:
        log.info("%s", await force_latest(app.bot, site))


def _job(site: dict):
    """A JobQueue callback bound to one site."""
    async def run(context):
        log.info("%s", await tick(context.bot, site, context.job_queue))
    return run


async def _run_once(sites: list) -> None:
    """One tick per site with no scheduler — the verification path."""
    app = Application.builder().token(config.NR_BOT_TOKEN).build()
    async with app:
        for site in sites:
            log.info("%s", await tick(app.bot, site, job_queue=None))


async def _heartbeat_job(context) -> None:
    """Dead-man's switch for healthchecks.io — a JobQueue job on purpose, so a
    wedged event loop stops the pings."""
    await asyncio.to_thread(heartbeat.ping, config.HEALTHCHECK_URL_NEWSROOM)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    errmail.install("newsroom")  # every logged ERROR -> one email to the operator
    ap = argparse.ArgumentParser(description="WordPress -> Telegram news bot")
    ap.add_argument("--once", action="store_true", help="one tick per site, then exit")
    ap.add_argument("--site", help="limit to one site by name")
    ap.add_argument("--dry-run", action="store_true",
                    help="publish nothing and place no orders (forces NR_DRY_RUN)")
    ap.add_argument("--force-latest", action="store_true",
                    help="run the site's newest article through the full pipeline "
                         "even if already seen (test path; requires --site)")
    args = ap.parse_args()

    if args.force_latest and not args.site:
        # Forcing a re-post on every configured channel at once is never the
        # intent — make the target explicit.
        print("--force-latest requires --site <name>")
        return 1

    if args.dry_run:
        config.NR_DRY_RUN = True

    store.init()

    sites = config.NR_SITES
    if args.site:
        sites = [s for s in sites if s["name"] == args.site]
        if not sites:
            known = ", ".join(s["name"] for s in config.NR_SITES) or "none configured"
            print(f"unknown site {args.site!r} — known: {known}")
            return 1
    if not sites:
        print("no sites configured — add a JSON file under modules/newsroom/sites/")
        return 1
    if not config.NR_BOT_TOKEN:
        print("NR_BOT_TOKEN is not set")
        return 1

    log.info("newsroom: %d site(s) — %s%s", len(sites),
             ", ".join(s["name"] for s in sites),
             "  [DRY RUN]" if config.NR_DRY_RUN else "")

    if args.force_latest:
        asyncio.run(_run_force_latest(sites[0]))
        return 0

    if args.once:
        asyncio.run(_run_once(sites))
        return 0

    app = Application.builder().token(config.NR_BOT_TOKEN).build()
    for i, site in enumerate(sites):
        app.job_queue.run_repeating(
            _job(site),
            interval=config.NR_POLL_S,
            first=i * STAGGER_S,
            name=f"poll:{site['name']}",
        )
    if config.HEALTHCHECK_URL_NEWSROOM:
        app.job_queue.run_repeating(_heartbeat_job, interval=heartbeat.INTERVAL_S,
                                    first=10, name="heartbeat")
        log.info("heartbeat: pinging healthchecks every %ds", heartbeat.INTERVAL_S)
    app.run_polling()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
