"""modules/newsroom/main.py — the per-site tick.

Offline: WordPress, the model, Telegram and the panel are all replaced. What is
tested is the control flow that decides whether a client's channel gets a post
— the backfill guard above all, because getting it wrong is visible to the
client's subscribers and cannot be undone.
"""

import asyncio
import importlib

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


def _article(wp_id=1, **over):
    a = {"wp_id": wp_id, "url": f"https://acme.test/{wp_id}", "title": f"Story {wp_id}",
         "body": "Body text.", "media_url": None, "media_type": None,
         "published_at": f"2026-08-{10 + wp_id:02d}T10:00:00+00:00"}
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
    monkeypatch.setattr(main.config, "NR_BACKFILL", True)
    main.articles = [_article(1), _article(2)]
    bot = FakeBot()

    run(main.tick(bot, _site()))

    assert len(bot.sent) == 2


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


def test_posts_are_sent_oldest_first(main):
    run(main.tick(FakeBot(), _site()))  # burn the first tick

    main.articles = [_article(4), _article(3), _article(2)]
    bot = FakeBot()
    run(main.tick(bot, _site()))

    titles = [s["text"].splitlines()[0] for s in bot.sent]
    assert titles == ["POST: Story 2", "POST: Story 3", "POST: Story 4"]


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
    run(main.tick(FakeBot(), _site()))
    main.articles = [_article(2), _article(3)]

    calls = {"n": 0}

    def flaky(article, site=None):
        calls["n"] += 1
        if article["wp_id"] == 2:
            raise RuntimeError("model exploded")
        return "POST: fine"

    monkeypatch.setattr(main.rewrite, "to_telegram", flaky)
    bot = FakeBot()

    run(main.tick(bot, _site()))

    assert len(bot.sent) == 1  # article 3 still shipped


def test_an_empty_rewrite_is_skipped_not_posted(main, monkeypatch):
    run(main.tick(FakeBot(), _site()))
    main.articles = [_article(2)]
    monkeypatch.setattr(main.rewrite, "to_telegram", lambda a, s=None: "   ")
    bot = FakeBot()

    run(main.tick(bot, _site()))

    assert bot.sent == []
