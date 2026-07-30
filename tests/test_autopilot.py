"""One drip, end to end, with a stub bot: selection → publish → record →
order → ask. Nothing here touches Telegram, OpenRouter or BulkFollows."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from modules.telegram import autopilot, queue_store


class FakeBot:
    """Records what the autopilot would have sent."""

    def __init__(self):
        self.sent = []
        self.next_id = 1000

    async def send_message(self, chat_id, text, **kw):
        self.next_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "kw": kw})
        return type("Msg", (), {"message_id": self.next_id})()


EU = {"chat_id": "@news_eu", "lang": "en", "regions": {"eu"}}
RU = {"chat_id": "@news_ru", "lang": "ru", "regions": {"ru"}}
US = {"chat_id": "@news_us", "lang": "en", "regions": {"us"}}


@pytest.fixture
def rig(store, monkeypatch):
    """Autopilot wired to a temp DB, two channels, and stubbed side effects."""
    from shared import config

    monkeypatch.setattr(config, "TG_DESTINATIONS", [EU, RU, US])
    monkeypatch.setattr(config, "TG_AUTO_MIN_SCORE", 60)
    monkeypatch.setattr(config, "TG_MAX_AGE_H", 12)
    monkeypatch.setattr(config, "TG_ASK_CHAT_ID", "-100999")

    orders = []
    monkeypatch.setattr(autopilot.reactions, "record_posts",
                        lambda posted, links, emojis=None: orders.append((posted, links)))

    published = []

    def publish_returns(posted, errors=(), links=()):
        async def fake_publish(bot, text, media, dests):
            published.append({"text": text, "media": media,
                              "dests": [d["chat_id"] for d in dests]})
            return list(posted), list(errors), list(links)
        monkeypatch.setattr(autopilot.publisher, "publish", fake_publish)

    return type("Rig", (), {"orders": orders, "published": published,
                            "publish_returns": staticmethod(publish_returns)})()


# Media by default: a text-only post isn't news and would never be selected.
# file_id media needs no file on disk, which keeps these tests hermetic.
PHOTO = {"file_id": "photo-1", "type": "photo"}


def add(text="a story", score=80, regions=("eu",), media=(PHOTO,)):
    item_id = queue_store.enqueue("tg:@src", text, list(media))
    queue_store.set_score(item_id, score, list(regions), "queued")
    return item_id


def test_a_full_tick(rig):
    item_id = add(regions=["eu", "ru"])
    rig.publish_returns(
        posted=["@news_eu", "@news_ru"],
        links=[("@news_eu", "https://t.me/news_eu/1"),
               ("@news_ru", "https://t.me/news_ru/2")],
    )
    bot = FakeBot()

    result = asyncio.run(autopilot.tick(bot))

    # Only the two matching channels were targeted — @news_us serves 'us'.
    assert rig.published[0]["dests"] == ["@news_eu", "@news_ru"]
    assert f"item {item_id}" in result

    # Both posts recorded with their links.
    assert dict(queue_store.links_for(item_id, "telegram")) == {
        "@news_eu": "https://t.me/news_eu/1",
        "@news_ru": "https://t.me/news_ru/2",
    }
    # Per-post BulkFollows orders fired without waiting for the operator.
    assert rig.orders and rig.orders[0][0] == ["@news_eu", "@news_ru"]

    # An ask was opened, delivered, and bound to its message.
    (ask,) = queue_store.open_asks()
    assert ask["message_id"] == 1001  # bound to the message the stub bot sent
    assert [c["chat_id"] for c in ask["state"]["chans"]] == ["@news_eu", "@news_ru"]
    assert bot.sent[0]["chat_id"] == -100999  # numeric ask chat sent as an int


def test_an_item_is_never_posted_twice(rig):
    add()
    rig.publish_returns(posted=["@news_eu"], links=[("@news_eu", "https://t.me/news_eu/1")])
    bot = FakeBot()

    asyncio.run(autopilot.tick(bot))
    second = asyncio.run(autopilot.tick(bot))

    assert len(rig.published) == 1
    assert second.startswith("nothing to post")


def test_global_items_reach_every_channel(rig):
    add(regions=["global"])
    rig.publish_returns(posted=["@news_eu", "@news_ru", "@news_us"])
    asyncio.run(autopilot.tick(FakeBot()))
    assert rig.published[0]["dests"] == ["@news_eu", "@news_ru", "@news_us"]


def test_an_item_no_channel_wants_does_not_block_the_drip(rig, monkeypatch):
    """A top-scoring story for a region you don't run must be stepped over."""
    from shared import config

    monkeypatch.setattr(config, "TG_DESTINATIONS", [EU])
    add("kremlin news", score=99, regions=["ru"])
    wanted = add("brussels news", score=70, regions=["eu"])
    rig.publish_returns(posted=["@news_eu"], links=[("@news_eu", "https://t.me/news_eu/1")])

    asyncio.run(autopilot.tick(FakeBot()))
    assert rig.published[0]["text"] == "brussels news"
    assert queue_store.links_for(wanted, "telegram")


def test_total_publish_failure_bumps_attempts_and_opens_no_ask(rig):
    item_id = add()
    rig.publish_returns(posted=[], errors=[("@news_eu", "chat not found")])
    bot = FakeBot()

    result = asyncio.run(autopilot.tick(bot))

    assert "every channel failed" in result
    assert queue_store.get_item(item_id)["attempts"] == 1
    assert queue_store.open_asks() == []
    assert bot.sent == []
    assert queue_store.links_for(item_id, "telegram") == []


def test_partial_failure_still_posts_records_and_asks(rig):
    item_id = add(regions=["eu", "ru"])
    rig.publish_returns(
        posted=["@news_eu"],
        errors=[("@news_ru", "bot is not an admin")],
        links=[("@news_eu", "https://t.me/news_eu/1")],
    )

    result = asyncio.run(autopilot.tick(FakeBot()))

    assert "✅" in result and "❌" in result
    assert [t for t, _ in queue_store.links_for(item_id, "telegram")] == ["@news_eu"]
    (ask,) = queue_store.open_asks()
    assert [c["chat_id"] for c in ask["state"]["chans"]] == ["@news_eu"]


def test_a_channel_without_a_link_is_still_recorded(rig):
    """Numeric private channels can post without yielding a public URL."""
    item_id = add()
    rig.publish_returns(posted=["@news_eu"], links=[])
    asyncio.run(autopilot.tick(FakeBot()))
    assert queue_store.links_for(item_id, "telegram") == [("@news_eu", None)]
    (ask,) = queue_store.open_asks()
    assert ask["state"]["chans"][0]["link"] is None


def test_missing_media_files_are_dropped_not_fatal(rig, tmp_path):
    real = tmp_path / "clip.mp4"
    real.write_bytes(b"x")
    add(media=[{"path": str(real), "type": "video"},
               {"path": str(tmp_path / "gone.jpg"), "type": "photo"}])
    rig.publish_returns(posted=["@news_eu"])

    asyncio.run(autopilot.tick(FakeBot()))

    assert rig.published[0]["media"] == [{"path": str(real), "type": "video"}]


def test_text_only_stories_are_never_picked(rig):
    """The media rule, from the drip's side."""
    add("just some commentary", media=[])
    rig.publish_returns(posted=["@news_eu"])
    assert asyncio.run(autopilot.tick(FakeBot())).startswith("nothing to post")


def test_an_item_whose_media_all_vanished_is_failed_not_posted(rig, tmp_path):
    """It qualified when collected; posting the bare text would break the rule."""
    item_id = add(media=[{"path": str(tmp_path / "gone.jpg"), "type": "photo"}])
    rig.publish_returns(posted=["@news_eu"])

    result = asyncio.run(autopilot.tick(FakeBot()))

    assert "media files are gone" in result
    assert rig.published == []
    assert queue_store.get_item(item_id)["status"] == "failed"


def test_dry_run_changes_nothing(rig):
    item_id = add()
    rig.publish_returns(posted=["@news_eu"])
    bot = FakeBot()

    result = asyncio.run(autopilot.tick(bot, dry_run=True))

    assert result.startswith("DRY RUN")
    assert rig.published == [] and bot.sent == []
    assert queue_store.links_for(item_id, "telegram") == []


def test_no_destinations_is_reported_not_crashed(rig, monkeypatch):
    from shared import config

    monkeypatch.setattr(config, "TG_DESTINATIONS", [])
    add()
    assert "no TG_DESTINATIONS" in asyncio.run(autopilot.tick(FakeBot()))


def test_unscored_items_are_never_posted(rig):
    """The dispatcher is the only scorer; the drip must not guess."""
    queue_store.enqueue("tg:@src", "unscored story", [])
    rig.publish_returns(posted=["@news_eu"])
    assert asyncio.run(autopilot.tick(FakeBot())).startswith("nothing to post")


def test_the_comparison_decides_which_story_goes_out(rig, monkeypatch):
    """Scores get you onto the shortlist; the head-to-head picks the winner."""
    add("the obvious one", score=95)
    add("the better story", score=90)
    monkeypatch.setattr(autopilot.smart_filter.scorer, "compare", lambda texts: 1)
    rig.publish_returns(posted=["@news_eu"], links=[("@news_eu", "https://t.me/news_eu/1")])

    asyncio.run(autopilot.tick(FakeBot()))

    assert rig.published[0]["text"] == "the better story"


def test_a_comparison_failure_still_posts_the_top_score(rig, monkeypatch):
    add("top score", score=95)
    add("runner up", score=90)
    monkeypatch.setattr(autopilot.smart_filter.scorer, "compare", lambda texts: None)
    rig.publish_returns(posted=["@news_eu"])

    asyncio.run(autopilot.tick(FakeBot()))

    assert rig.published[0]["text"] == "top score"


def test_preview_skips_the_comparison(rig, monkeypatch):
    """/queue is a status command — it must not burn a model call."""
    add("top score", score=95)
    add("other", score=90)
    monkeypatch.setattr(autopilot.smart_filter.scorer, "compare",
                        lambda texts: (_ for _ in ()).throw(AssertionError("compared")))

    item, targets = autopilot.pick(compare=False)

    assert item["text"] == "top score"
    assert [d["chat_id"] for d in targets] == ["@news_eu"]


def test_only_region_matching_stories_reach_the_comparison(rig, monkeypatch):
    """A high-scoring story for a region you don't run isn't a finalist."""
    from shared import config

    seen = {}
    add("moscow only", score=99, regions=["ru"])
    add("brussels", score=70, regions=["eu"])

    def fake_compare(texts):
        seen["texts"] = texts
        return 0

    monkeypatch.setattr(autopilot.smart_filter.scorer, "compare", fake_compare)
    monkeypatch.setattr(config, "TG_DESTINATIONS", [EU])
    rig.publish_returns(posted=["@news_eu"])

    asyncio.run(autopilot.tick(FakeBot()))

    assert seen == {}  # one finalist left — no comparison needed
    assert rig.published[0]["text"] == "brussels"


def test_next_delay_stays_inside_the_configured_window(monkeypatch):
    from shared import config

    monkeypatch.setattr(config, "TG_DRIP_MIN_S", 100)
    monkeypatch.setattr(config, "TG_DRIP_MAX_S", 200)
    assert all(100 <= autopilot.next_delay() <= 200 for _ in range(50))


def test_default_cadence_is_daily():
    from shared import config

    assert config.TG_DRIP_MIN_S == 18 * 3600
    assert config.TG_DRIP_MAX_S == 24 * 3600


def test_freshness_window_outlives_the_gap_between_posts():
    """A window shorter than the drip gap would starve the queue: everything
    collected since the last post expires before the next tick fires."""
    from shared import config

    assert config.TG_MAX_AGE_H * 3600 >= config.TG_DRIP_MAX_S


def test_first_tick_after_a_restart_waits_out_the_last_post(store, monkeypatch):
    """Otherwise a daily cadence fires once per restart."""
    from shared import config

    monkeypatch.setattr(config, "TG_DRIP_MIN_S", 3600)
    monkeypatch.setattr(config, "TG_DRIP_MAX_S", 3600)
    item_id = add()
    queue_store.record_post(item_id, "telegram", "@news_eu", None)

    delay = autopilot.startup_delay()

    # Posted seconds ago, so nearly the whole hour is still owed.
    assert autopilot.STARTUP_DELAY_S < delay <= 3600


def test_first_tick_is_prompt_when_nothing_was_ever_posted(store):
    assert autopilot.startup_delay() == autopilot.STARTUP_DELAY_S


def test_a_pinned_first_tick_wins_over_the_rhythm(store, monkeypatch):
    from shared import config

    item_id = add()
    queue_store.record_post(item_id, "telegram", "@news_eu", None)  # just posted

    target = (datetime.now().astimezone() + timedelta(hours=2)).replace(second=0, microsecond=0)
    monkeypatch.setattr(config, "TG_FIRST_TICK", target.strftime("%H:%M"))

    delay = autopilot.startup_delay()
    assert abs(delay - 2 * 3600) <= 61  # to the pinned minute, not the last post


def test_a_pinned_time_already_past_lands_tomorrow(store, monkeypatch):
    from shared import config

    target = datetime.now().astimezone() - timedelta(hours=1)
    monkeypatch.setattr(config, "TG_FIRST_TICK", target.strftime("%H:%M"))

    assert abs(autopilot.startup_delay() - 23 * 3600) <= 3660


def test_a_malformed_pin_is_ignored(store, monkeypatch):
    from shared import config

    monkeypatch.setattr(config, "TG_FIRST_TICK", "9pm")
    assert autopilot.startup_delay() == autopilot.STARTUP_DELAY_S


def test_no_pin_means_normal_behaviour(store, monkeypatch):
    from shared import config

    monkeypatch.setattr(config, "TG_FIRST_TICK", "")
    assert autopilot.startup_delay() == autopilot.STARTUP_DELAY_S


def test_first_tick_is_prompt_when_the_last_post_is_old(store, monkeypatch):
    from shared import config

    monkeypatch.setattr(config, "TG_DRIP_MIN_S", 60)
    monkeypatch.setattr(config, "TG_DRIP_MAX_S", 60)
    item_id = add()
    queue_store.record_post(item_id, "telegram", "@news_eu", None)
    with queue_store._conn() as c:  # backdate the publication
        old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(timespec="seconds")
        c.execute("UPDATE posts SET posted_at=?", (old,))

    assert autopilot.startup_delay() == autopilot.STARTUP_DELAY_S


def test_enable_toggle():
    autopilot.set_enabled(False)
    assert not autopilot.is_enabled()
    autopilot.set_enabled(True)
    assert autopilot.is_enabled()
