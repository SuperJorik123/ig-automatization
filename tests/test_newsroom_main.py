"""modules/newsroom/main.py — the per-site tick.

Offline: WordPress, the model, Telegram and the panel are all replaced. What is
tested is the control flow that decides whether a client's channel gets a post
— the backfill guard above all, because getting it wrong is visible to the
client's subscribers and cannot be undone.
"""

import asyncio
import importlib
import logging
from datetime import datetime, timedelta, timezone

import pytest

from shared import config


def run(coro):
    return asyncio.run(coro)


class FakeMessage:
    message_id = 412
    link = "https://t.me/acme/412"


class FakeBot:
    def __init__(self):
        self.sent = []

    async def _send(self, kind, **kw):
        self.sent.append({"kind": kind, **kw})
        return FakeMessage()

    async def send_message(self, **kw):
        return await self._send("message", **kw)

    async def send_photo(self, **kw):
        return await self._send("photo", **kw)

    async def send_video(self, **kw):
        return await self._send("video", **kw)


class FakeJobQueue:
    def __init__(self):
        self.jobs = []

    def run_once(self, cb, when, name=None):
        self.jobs.append({"when": when, "name": name})


def _today_at(hour: int, minute: int = 0) -> datetime:
    """A UTC timestamp on today's date — the day the frozen clock sits on."""
    return datetime.now(timezone.utc).replace(
        hour=hour, minute=minute, second=0, microsecond=0)


def _article(wp_id=1, **over):
    """One article, published today at 04:0<wp_id> UTC.

    Today's date matters now: the tick only ever posts an article the site
    published today. 04:00 mirrors the real bursts (03:30-06:30 UTC) and is
    two hours behind the frozen clock, so it is settled unless a test says
    otherwise."""
    a = {"wp_id": wp_id, "url": f"https://acme.test/{wp_id}", "title": f"Story {wp_id}",
         "body": "Body text.", "media_url": None, "media_type": None,
         "published_at": (_today_at(4) + timedelta(minutes=wp_id)).isoformat(
             timespec="seconds")}
    a.update(over)
    return a


def _site(**over):
    s = {"name": "acme", "wp_base": "https://acme.test/wp-json/wp/v2",
         "chat_id": "@acme", "views_phase1": [500, 5000],
         "service_views": "V1", "service_bonus": "B1",
         "emoji_pool": ["heart"], "emoji_count": [1, 1], "emoji_quantity": [10, 40],
         "rewrite_hint": ""}
    s.update(over)
    return s


@pytest.fixture
def main(tmp_path, monkeypatch):
    """main + a tmp store, with every outbound edge stubbed.

    Exposes `.articles` (what WordPress returns), `.placed` (what would have
    gone to the panel) and `.store_mod`."""
    monkeypatch.setattr(config, "NR_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "NR_DRY_RUN", False)
    monkeypatch.setattr(config, "NR_BACKFILL", False)
    monkeypatch.setattr(config, "NR_REACTION_DELAY_S", 1200)
    # A flat settle so the tick's decisions are exact; pace.py owns the
    # per-channel spread and tests it there.
    monkeypatch.setattr(config, "NR_SETTLE_MIN_M", 60.0)
    monkeypatch.setattr(config, "NR_SETTLE_MAX_M", 60.0)
    monkeypatch.setattr(config, "NR_EMOJI_SERVICES", [
        {"name": "heart", "emoji": "❤️", "service": "5108"},
    ])

    from modules.newsroom import store as store_mod
    importlib.reload(store_mod)
    store_mod.init()

    from modules.newsroom import orders as orders_mod
    from modules.newsroom import publish as publish_mod
    from modules.newsroom import main as main_mod
    importlib.reload(orders_mod)
    importlib.reload(publish_mod)
    importlib.reload(main_mod)

    placed = []
    monkeypatch.setattr(orders_mod.smm, "place_order",
                        lambda link, quantity, service: placed.append(
                            (link, quantity, service)) or {"order": 1})

    main_mod.articles = [_article(1)]
    monkeypatch.setattr(main_mod.wp, "fetch_recent",
                        lambda site, limit=20: list(main_mod.articles))
    monkeypatch.setattr(main_mod.rewrite, "to_telegram",
                        lambda article, site=None: f"POST: {article['title']}")

    # 06:00 UTC today: after the sites' real bursts, and far from midnight so
    # the day boundary only moves when a test moves it (main.NOW += a day).
    main_mod.NOW = _today_at(6)
    monkeypatch.setattr(main_mod, "_now", lambda: main_mod.NOW)

    main_mod.placed = placed
    main_mod.store_mod = store_mod
    return main_mod


# --------------------------------------------------------------------------- #
# The backfill guard                                                          #
# --------------------------------------------------------------------------- #


def test_first_tick_records_without_posting(main):
    # Without this, enabling a site dumps twenty back-articles into a live
    # channel at once, in front of the client's subscribers.
    main.articles = [_article(1), _article(2), _article(3)]
    bot = FakeBot()

    summary = run(main.tick(bot, _site()))

    assert bot.sent == []
    assert "recorded as seen" in summary
    assert main.store_mod.seen_ids("acme") == {1, 2, 3}


def test_articles_after_the_first_tick_are_posted(main):
    main.articles = [_article(1)]
    run(main.tick(FakeBot(), _site()))

    main.articles = [_article(2), _article(1)]
    bot = FakeBot()
    run(main.tick(bot, _site()))

    assert len(bot.sent) == 1
    assert "Story 2" in bot.sent[0]["text"]


def test_backfill_flag_posts_the_first_batch(main, monkeypatch):
    # Backfill lifts the first-tick guard, not the daily cap: the newest of
    # the batch goes out and the rest are dropped like any other day.
    monkeypatch.setattr(main.config, "NR_BACKFILL", True)
    main.articles = [_article(1), _article(2)]
    bot = FakeBot()

    run(main.tick(bot, _site()))

    assert [s["text"].splitlines()[0] for s in bot.sent] == ["POST: Story 2"]


def test_first_tick_guard_keys_on_the_site_not_the_database(main):
    # A second site's first tick must be guarded even though the store is
    # no longer empty.
    run(main.tick(FakeBot(), _site(name="acme")))

    bot = FakeBot()
    run(main.tick(bot, _site(name="globex", chat_id="@globex")))

    assert bot.sent == []


# --------------------------------------------------------------------------- #
# The normal path                                                             #
# --------------------------------------------------------------------------- #


def test_the_newest_pending_article_is_the_one_posted(main):
    # One story a day means the channel should carry TODAY's, not the oldest
    # of a pile — store.pending() hands them over oldest-first.
    run(main.tick(FakeBot(), _site()))  # burn the first tick

    main.articles = [_article(4), _article(3), _article(2)]
    bot = FakeBot()
    run(main.tick(bot, _site()))

    titles = [s["text"].splitlines()[0] for s in bot.sent]
    assert titles == ["POST: Story 4"]


def test_the_articles_not_posted_are_dropped_not_queued(main):
    # The client's channel shows one current story a day; queueing the losers
    # would drip-feed yesterday's news tomorrow.
    run(main.tick(FakeBot(), _site()))
    main.articles = [_article(4), _article(3), _article(2)]
    summary = run(main.tick(FakeBot(), _site()))
    assert "posted 1, dropped 2" in summary

    main.NOW += timedelta(days=1)
    bot = FakeBot()

    assert "nothing new" in run(main.tick(bot, _site()))
    assert bot.sent == []


def test_an_article_is_never_posted_twice(main):
    run(main.tick(FakeBot(), _site()))
    main.articles = [_article(2)]
    run(main.tick(FakeBot(), _site()))

    bot = FakeBot()
    run(main.tick(bot, _site()))

    assert bot.sent == []


def test_nothing_new_is_a_quiet_tick(main):
    run(main.tick(FakeBot(), _site()))

    assert "nothing new" in run(main.tick(FakeBot(), _site()))


def test_publishing_places_the_views_order(main):
    run(main.tick(FakeBot(), _site()))
    main.articles = [_article(2)]

    # With a scheduler the reactions are deferred, so the views order is the
    # only one placed during the tick itself.
    run(main.tick(FakeBot(), _site(), job_queue=FakeJobQueue()))

    assert [p[2] for p in main.placed] == ["V1"]


def test_a_posted_article_is_recorded_with_its_message_id(main):
    run(main.tick(FakeBot(), _site()))
    main.articles = [_article(2)]

    run(main.tick(FakeBot(), _site()))

    (post,) = main.store_mod.recent_posts("@acme")
    assert post["message_id"] == 412
    assert post["link"] == "https://t.me/acme/412"


# --------------------------------------------------------------------------- #
# Reactions                                                                   #
# --------------------------------------------------------------------------- #


def test_reactions_are_scheduled_not_ordered_immediately(main):
    # Reactions landing in the same second as the post is the clearest bot
    # tell there is.
    run(main.tick(FakeBot(), _site()))
    main.articles = [_article(2)]
    jq = FakeJobQueue()

    run(main.tick(FakeBot(), _site(), job_queue=jq))

    assert len(jq.jobs) == 1
    assert jq.jobs[0]["when"] == 1200
    assert [p[2] for p in main.placed] == ["V1"]  # no emoji order yet


def test_without_a_scheduler_reactions_are_ordered_inline(main):
    # --once cannot sit idle for twenty minutes, so it trades the delay away
    # rather than dropping the order.
    run(main.tick(FakeBot(), _site()))
    main.articles = [_article(2)]

    run(main.tick(FakeBot(), _site(), job_queue=None))

    heart = next(e for e in main.orders.EMOJI_SERVICES if e["name"] == "heart")
    assert heart["service"] in [p[2] for p in main.placed]


# --------------------------------------------------------------------------- #
# Dry run                                                                     #
# --------------------------------------------------------------------------- #


def test_dry_run_sends_nothing_and_orders_nothing(main, monkeypatch):
    run(main.tick(FakeBot(), _site()))
    monkeypatch.setattr(main.config, "NR_DRY_RUN", True)
    main.articles = [_article(2)]
    bot = FakeBot()

    run(main.tick(bot, _site()))

    assert bot.sent == []
    assert main.placed == []


def test_dry_run_does_not_queue_a_backlog_for_go_live(main, monkeypatch):
    # Leaving dry-run articles pending would re-pay for the rewrite every tick
    # and then dump a week of backlog into the channel on go-live.
    run(main.tick(FakeBot(), _site()))
    monkeypatch.setattr(main.config, "NR_DRY_RUN", True)
    main.articles = [_article(2)]
    run(main.tick(FakeBot(), _site()))

    monkeypatch.setattr(main.config, "NR_DRY_RUN", False)
    bot = FakeBot()
    run(main.tick(bot, _site()))

    assert bot.sent == []


# --------------------------------------------------------------------------- #
# --force-latest                                                              #
# --------------------------------------------------------------------------- #


def test_force_latest_reposts_an_already_seen_article(main):
    run(main.tick(FakeBot(), _site()))  # article 1 recorded as seen
    bot = FakeBot()

    summary = run(main.force_latest(bot, _site()))

    assert "posted" in summary
    assert len(bot.sent) == 1
    assert "Story 1" in bot.sent[0]["text"]


def test_force_latest_picks_the_newest_article(main):
    run(main.tick(FakeBot(), _site()))
    main.articles = [_article(3), _article(2), _article(1)]  # API order: newest first
    bot = FakeBot()

    run(main.force_latest(bot, _site()))

    assert len(bot.sent) == 1
    assert "Story 3" in bot.sent[0]["text"]


def test_force_latest_runs_the_full_order_pipeline(main):
    run(main.tick(FakeBot(), _site()))

    run(main.force_latest(FakeBot(), _site()))

    kinds = [p[2] for p in main.placed]
    heart = next(e for e in main.orders.EMOJI_SERVICES if e["name"] == "heart")
    assert "V1" in kinds              # views order
    assert heart["service"] in kinds  # reactions, ordered inline (no scheduler)


def test_force_latest_on_a_virgin_site_keeps_the_backfill_guard(main):
    # Forcing the latest must not turn the other back-articles into pending
    # work — the next normal tick would dump them into the channel.
    main.articles = [_article(3), _article(2), _article(1)]
    run(main.force_latest(FakeBot(), _site()))

    bot = FakeBot()
    summary = run(main.tick(bot, _site()))

    assert bot.sent == []
    assert "nothing new" in summary


def test_force_latest_respects_dry_run(main, monkeypatch):
    run(main.tick(FakeBot(), _site()))
    monkeypatch.setattr(main.config, "NR_DRY_RUN", True)
    bot = FakeBot()

    run(main.force_latest(bot, _site()))

    assert bot.sent == []
    assert main.placed == []


# --------------------------------------------------------------------------- #
# Failure paths                                                               #
# --------------------------------------------------------------------------- #


def test_a_site_that_is_down_costs_only_its_own_tick(main, monkeypatch):
    def boom(site, limit=20):
        raise RuntimeError("acme: HTTP 403")

    monkeypatch.setattr(main.wp, "fetch_recent", boom)

    assert "fetch failed" in run(main.tick(FakeBot(), _site()))


def test_a_send_failure_marks_the_article_and_does_not_retry(main, monkeypatch):
    run(main.tick(FakeBot(), _site()))
    main.articles = [_article(2)]

    class DeadBot(FakeBot):
        async def send_message(self, **kw):
            raise RuntimeError("chat not found")

    run(main.tick(DeadBot(), _site()))

    bot = FakeBot()
    run(main.tick(bot, _site()))
    assert bot.sent == []  # marked failed, not retried forever


def test_one_bad_article_does_not_block_the_rest(main, monkeypatch):
    # The newest article fails, so nothing shipped and the window was never
    # used: the next tick must fall through to the next-freshest rather than
    # spend the day's slot on an article that cannot be posted.
    run(main.tick(FakeBot(), _site()))
    main.articles = [_article(2), _article(3)]

    def flaky(article, site=None):
        if article["wp_id"] == 3:
            raise RuntimeError("model exploded")
        return f"POST: Story {article['wp_id']}"

    monkeypatch.setattr(main.rewrite, "to_telegram", flaky)

    assert "0/2 posted" in run(main.tick(FakeBot(), _site()))

    bot = FakeBot()
    run(main.tick(bot, _site()))

    assert [s["text"].splitlines()[0] for s in bot.sent] == ["POST: Story 2"]


def test_an_empty_rewrite_is_skipped_not_posted(main, monkeypatch):
    run(main.tick(FakeBot(), _site()))
    main.articles = [_article(2)]
    monkeypatch.setattr(main.rewrite, "to_telegram", lambda a, s=None: "   ")
    bot = FakeBot()

    run(main.tick(bot, _site()))

    assert bot.sent == []


def test_fetch_failures_alert_only_on_the_third_in_a_row(main, monkeypatch, caplog):
    """Transient timeouts must not mail the operator: the first two failures
    are WARNINGs (errmail ignores them), the third in a row is the ERROR that
    becomes an email, and a success resets the streak."""
    calls = {"fail": True}

    def flaky(site, limit=20):
        if calls["fail"]:
            raise RuntimeError("acme: request failed: Read timed out.")
        return []

    monkeypatch.setattr(main.wp, "fetch_recent", flaky)
    monkeypatch.setattr(main.config, "NR_FETCH_ALERT_AFTER", 3)
    main._fetch_failures.clear()

    with caplog.at_level(logging.INFO, logger="newsroom"):
        for _ in range(2):
            assert "fetch failed" in run(main.tick(FakeBot(), _site()))
        assert [r.levelno for r in caplog.records if r.levelno >= logging.WARNING] == [
            logging.WARNING, logging.WARNING]

        caplog.clear()
        run(main.tick(FakeBot(), _site()))
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "3 times in a row" in errors[0].getMessage()
        assert "Read timed out" in errors[0].getMessage()

        # A fourth failure stays quiet — one email per outage, not one per tick.
        caplog.clear()
        run(main.tick(FakeBot(), _site()))
        assert not [r for r in caplog.records if r.levelno == logging.ERROR]

        # Recovery resets the streak: the next outage alerts on ITS third tick.
        caplog.clear()
        calls["fail"] = False
        run(main.tick(FakeBot(), _site()))
        assert any("reachable again" in r.getMessage() for r in caplog.records)
        assert main._fetch_failures.get("acme", 0) == 0

        calls["fail"] = True
        caplog.clear()
        for _ in range(2):
            run(main.tick(FakeBot(), _site()))
        assert not [r for r in caplog.records if r.levelno == logging.ERROR]


# --------------------------------------------------------------------------- #
# One post per channel per calendar day                                       #
# --------------------------------------------------------------------------- #


def _open_the_day(main):
    """Burn the first tick (backfill guard) so the channel is ready to post."""
    run(main.tick(FakeBot(), _site()))


def test_the_days_second_article_is_not_posted(main):
    _open_the_day(main)
    main.articles = [_article(2)]
    run(main.tick(FakeBot(), _site()))          # today's story ships

    main.articles = [_article(3)]
    bot = FakeBot()
    summary = run(main.tick(bot, _site()))

    assert bot.sent == []
    assert "already posted today" in summary


def test_a_tick_after_the_days_post_pays_for_no_rewrite(main, monkeypatch):
    # The gate sits in front of the only billed call in the flow — at
    # NR_POLL_S=300 there are 287 held ticks for every one that posts.
    _open_the_day(main)
    main.articles = [_article(2)]
    run(main.tick(FakeBot(), _site()))
    main.articles = [_article(3)]

    def boom(article, site=None):
        raise AssertionError("the model must not be called after today's post")

    monkeypatch.setattr(main.rewrite, "to_telegram", boom)

    run(main.tick(FakeBot(), _site()))


def test_a_new_day_posts_again(main):
    _open_the_day(main)
    main.articles = [_article(2)]
    run(main.tick(FakeBot(), _site()))

    main.NOW += timedelta(days=1)
    tomorrow = (_today_at(4) + timedelta(days=1)).isoformat()
    main.articles = [_article(3, published_at=tomorrow)]
    bot = FakeBot()

    run(main.tick(bot, _site()))

    assert [s["text"].splitlines()[0] for s in bot.sent] == ["POST: Story 3"]


# --------------------------------------------------------------------------- #
# The burst must settle                                                       #
# --------------------------------------------------------------------------- #


def test_the_first_article_of_a_burst_is_not_posted_on_sight(main):
    # The sites publish 2-5 articles over 5-25 minutes. Posting the moment one
    # appears ships the FIRST of the morning, not the latest.
    _open_the_day(main)
    main.articles = [_article(2, published_at=_today_at(5, 55).isoformat())]
    bot = FakeBot()

    summary = run(main.tick(bot, _site()))

    assert bot.sent == []
    assert "posting in" in summary


def test_a_burst_still_arriving_keeps_resetting_the_wait(main):
    # The wait is measured from the NEWEST article, so the last one through
    # the door is the one that ships.
    _open_the_day(main)
    main.articles = [_article(2, published_at=_today_at(4).isoformat()),
                     _article(3, published_at=_today_at(5, 50).isoformat())]
    bot = FakeBot()

    summary = run(main.tick(bot, _site()))

    assert bot.sent == []
    assert "2 today, posting in" in summary


def test_once_the_burst_settles_the_days_last_article_ships(main):
    _open_the_day(main)
    main.articles = [_article(2, published_at=_today_at(3, 48).isoformat()),
                     _article(3, published_at=_today_at(4, 2).isoformat()),
                     _article(4, published_at=_today_at(4, 22).isoformat())]
    bot = FakeBot()

    summary = run(main.tick(bot, _site()))

    assert [s["text"].splitlines()[0] for s in bot.sent] == ["POST: Story 4"]
    assert "posted 1, dropped 2" in summary


# --------------------------------------------------------------------------- #
# Only today's articles are eligible                                          #
# --------------------------------------------------------------------------- #


def test_yesterdays_leftovers_are_dropped_not_posted(main):
    # Without this the first tick after midnight ships yesterday's straggler
    # and today's burst waits for tomorrow — the channel locks a day behind.
    _open_the_day(main)
    yesterday = (_today_at(13, 37) - timedelta(days=1)).isoformat()
    main.articles = [_article(2, published_at=yesterday)]
    bot = FakeBot()

    summary = run(main.tick(bot, _site()))

    assert bot.sent == []
    assert "nothing published today, 1 dropped" in summary


def test_a_straggler_never_becomes_tomorrows_post(main):
    _open_the_day(main)
    main.articles = [_article(2)]
    run(main.tick(FakeBot(), _site()))                    # today's post
    straggler = _article(3, published_at=_today_at(13, 37).isoformat())
    main.articles = [straggler]
    run(main.tick(FakeBot(), _site()))                    # held: already posted

    main.NOW += timedelta(days=1)
    fresh = _article(4, published_at=(_today_at(4) + timedelta(days=1)).isoformat())
    main.articles = [straggler, fresh]
    bot = FakeBot()

    run(main.tick(bot, _site()))

    assert [s["text"].splitlines()[0] for s in bot.sent] == ["POST: Story 4"]


def test_an_article_with_no_date_is_treated_as_todays(main):
    # It came out of the last 20 the site published; dropping it silently
    # would be worse than posting it.
    _open_the_day(main)
    main.articles = [_article(2, published_at=None)]
    bot = FakeBot()

    run(main.tick(bot, _site()))

    assert len(bot.sent) == 1


# --------------------------------------------------------------------------- #
# Failure and dry run                                                         #
# --------------------------------------------------------------------------- #


def test_a_failed_post_does_not_spend_the_day(main):
    # Otherwise one dead send costs the channel its whole day.
    _open_the_day(main)
    main.articles = [_article(2), _article(3)]

    class DeadBot(FakeBot):
        async def send_message(self, **kw):
            raise RuntimeError("chat not found")

    run(main.tick(DeadBot(), _site()))          # article 3 fails
    bot = FakeBot()

    run(main.tick(bot, _site()))

    assert [s["text"].splitlines()[0] for s in bot.sent] == ["POST: Story 2"]


def test_a_dry_run_holds_the_channel_like_a_live_one(main, monkeypatch):
    # A dry run that drips faster than production is not a rehearsal.
    monkeypatch.setattr(main.config, "NR_DRY_RUN", True)
    main.articles = [_article(1)]
    run(main.tick(FakeBot(), _site()))          # first tick, recorded as seen
    main.articles = [_article(3), _article(2)]

    summary = run(main.tick(FakeBot(), _site()))

    assert "would post 1, dropped 1" in summary


def test_each_channel_has_its_own_day(main):
    # A shared gate would let the busiest site mute the other six.
    _open_the_day(main)
    main.articles = [_article(2)]
    run(main.tick(FakeBot(), _site()))          # acme has posted today
    main.articles = [_article(1)]
    run(main.tick(FakeBot(), _site(name="globex", chat_id="@globex")))  # first tick
    main.articles = [_article(2)]
    bot = FakeBot()

    run(main.tick(bot, _site(name="globex", chat_id="@globex")))

    assert len(bot.sent) == 1
