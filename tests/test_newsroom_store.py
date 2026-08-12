"""modules/newsroom/store.py — the client bot's SQLite state.

Offline by construction (stdlib sqlite3 only). Every test points the module's
database at a tmp dir, so the operator's real newsroom.db is never touched.
"""

import importlib

import pytest

from shared import config


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A fresh, initialised store backed by a tmp database.

    NR_DATA_DIR is read at import time into store._DB, so the module has to be
    reloaded after the patch rather than merely monkeypatched."""
    monkeypatch.setattr(config, "NR_DATA_DIR", str(tmp_path))
    from modules.newsroom import store as store_mod
    importlib.reload(store_mod)
    store_mod.init()
    return store_mod


def _article(wp_id=1, **over):
    a = {"wp_id": wp_id, "url": f"https://acme.test/{wp_id}", "title": f"Story {wp_id}",
         "body": "body text", "media_url": None, "media_type": None,
         "published_at": f"2026-08-1{wp_id}T10:00:00+00:00"}
    a.update(over)
    return a


# --------------------------------------------------------------------------- #
# Articles                                                                    #
# --------------------------------------------------------------------------- #


def test_init_is_idempotent(store):
    store.init()
    store.init()
    assert store.seen_ids("acme") == set()


def test_add_and_see_article(store):
    row_id = store.add_article("acme", _article(41))

    assert row_id is not None
    assert store.seen_ids("acme") == {41}


def test_same_wp_id_twice_is_a_silent_noop(store):
    # The poller refetches the last 20 posts every tick; overlap must be free.
    store.add_article("acme", _article(41))

    assert store.add_article("acme", _article(41)) is None
    assert len(store.pending("acme")) == 1


def test_same_wp_id_on_different_sites_are_distinct(store):
    # WordPress ids are per site — post 1 on two sites is two articles, and
    # collapsing them would silently drop one client's story.
    store.add_article("acme", _article(1))
    store.add_article("globex", _article(1))

    assert store.seen_ids("acme") == {1}
    assert store.seen_ids("globex") == {1}
    assert len(store.pending("acme")) == 1
    assert len(store.pending("globex")) == 1


def test_pending_is_oldest_first(store):
    # The API returns newest-first; the channel must read in publish order.
    store.add_article("acme", _article(3, published_at="2026-08-13T10:00:00+00:00"))
    store.add_article("acme", _article(1, published_at="2026-08-11T10:00:00+00:00"))
    store.add_article("acme", _article(2, published_at="2026-08-12T10:00:00+00:00"))

    assert [r["wp_id"] for r in store.pending("acme")] == [1, 2, 3]


def test_pending_excludes_non_new_statuses(store):
    a = store.add_article("acme", _article(1))
    store.add_article("acme", _article(2), status=store.SKIPPED)
    store.mark(a, store.POSTED)

    assert store.pending("acme") == []
    assert store.seen_ids("acme") == {1, 2}


def test_has_articles_drives_the_backfill_guard(store):
    assert store.has_articles("acme") is False

    store.add_article("acme", _article(1), status=store.SKIPPED)

    assert store.has_articles("acme") is True


def test_skipped_articles_are_never_posted_later(store):
    # The backfill guard records the first batch as seen; those must not
    # reappear as pending on the next tick.
    store.add_article("acme", _article(1), status=store.SKIPPED)

    assert store.pending("acme") == []
    assert store.add_article("acme", _article(1)) is None


# --------------------------------------------------------------------------- #
# Posts and counters                                                          #
# --------------------------------------------------------------------------- #


def test_record_post_returns_an_id_orders_can_use(store):
    a = store.add_article("acme", _article(1))

    post_id = store.record_post(a, "acme", "@acme", 412, "https://t.me/acme/412")

    assert isinstance(post_id, int)
    assert store.recent_posts("@acme")[0]["link"] == "https://t.me/acme/412"


def test_post_without_a_link_is_still_recorded(store):
    # A private channel or a dry-run yields no public link. The post counts
    # toward the channel total even though nothing can be ordered against it.
    a = store.add_article("acme", _article(1))

    post_id = store.record_post(a, "acme", "-1001234567890", 9, None)

    assert store.recent_posts("-1001234567890")[0]["link"] is None
    assert post_id is not None


def test_counter_increments_from_one(store):
    assert store.bump_channel_posts("@acme") == 1
    assert store.bump_channel_posts("@acme") == 2


def test_counters_are_independent_per_channel(store):
    # The single easiest thing to get wrong here: a shared counter would make
    # one site's 5th post fire the bonus order on another client's channel.
    for _ in range(4):
        store.bump_channel_posts("@acme")
    store.bump_channel_posts("@globex")

    assert store.bump_channel_posts("@acme") == 5
    assert store.bump_channel_posts("@globex") == 2


def test_counter_survives_a_reload(store, tmp_path, monkeypatch):
    # It is durable state, not a process counter: at one post a day an
    # in-memory count would never survive to the 5th post.
    for _ in range(3):
        store.bump_channel_posts("@acme")

    monkeypatch.setattr(config, "NR_DATA_DIR", str(tmp_path))
    importlib.reload(store)

    assert store.bump_channel_posts("@acme") == 4


def test_recent_posts_is_newest_first_and_limited(store):
    a = store.add_article("acme", _article(1))
    for i in range(7):
        store.record_post(a, "acme", "@acme", i, f"https://t.me/acme/{i}")

    got = store.recent_posts("@acme", limit=5)

    assert [r["message_id"] for r in got] == [6, 5, 4, 3, 2]


# --------------------------------------------------------------------------- #
# Orders                                                                      #
# --------------------------------------------------------------------------- #


def test_open_then_close_a_successful_order(store):
    a = store.add_article("acme", _article(1))
    post_id = store.record_post(a, "acme", "@acme", 1, "https://t.me/acme/1")

    oid = store.open_order(post_id, "views", "5108", 1200)
    store.close_order(oid, {"order": 998877})

    assert store.failed_orders() == []


def test_a_failed_order_stays_in_the_replay_list(store):
    # smm.place_order returns None on any failure and never raises, so this
    # table is the only place a lost order is visible.
    a = store.add_article("acme", _article(1))
    post_id = store.record_post(a, "acme", "@acme", 1, "https://t.me/acme/1")

    oid = store.open_order(post_id, "views", "5108", 1200)
    store.close_order(oid, None)

    (row,) = store.failed_orders()
    assert row["kind"] == "views"
    assert row["quantity"] == 1200
    assert row["error"]


def test_an_order_never_closed_counts_as_failed(store):
    # A crash between open_order and the panel call leaves this row behind —
    # which is the entire point of writing it before the call.
    a = store.add_article("acme", _article(1))
    post_id = store.record_post(a, "acme", "@acme", 1, "https://t.me/acme/1")

    store.open_order(post_id, "bonus", "9999", 10000)

    assert len(store.failed_orders()) == 1


def test_panel_response_without_an_order_id_is_a_failure(store):
    # The panel answers HTTP 200 with {"error": ...} for a bad service id;
    # smm turns that into None, but a truthy dict with no "order" must not
    # be recorded as a success either.
    a = store.add_article("acme", _article(1))
    post_id = store.record_post(a, "acme", "@acme", 1, "https://t.me/acme/1")

    oid = store.open_order(post_id, "views", "5108", 1200)
    store.close_order(oid, {"status": "queued"})

    (row,) = store.failed_orders()
    assert "no order id" in row["error"]
