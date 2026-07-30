"""The store: per-platform eligibility, attempt counting, ask persistence."""

from datetime import datetime, timedelta, timezone


def add(store, text="a story", score=80, regions=("eu",), media=(), source="tg:@src"):
    item_id = store.enqueue(source, text, list(media))
    if score is not None:
        store.set_score(item_id, score, list(regions), "queued")
    return item_id


def age(store, item_id, hours):
    """Backdate an item — collected_at is written by enqueue()."""
    when = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    with store._conn() as c:
        c.execute("UPDATE items SET collected_at=? WHERE id=?", (when, item_id))


def ids(items):
    return [i["id"] for i in items]


# --- candidate selection ---------------------------------------------------


def test_unscored_items_are_not_candidates(store):
    add(store, "unscored", score=None)
    assert store.candidates("telegram", 60, 12) == []


def test_score_floor(store):
    low = add(store, "low", score=59)
    high = add(store, "high", score=60)
    assert ids(store.candidates("telegram", 60, 12)) == [high]
    assert low not in ids(store.candidates("telegram", 60, 12))


def test_best_score_first(store):
    add(store, "ok", score=65)
    best = add(store, "big", score=95)
    assert ids(store.candidates("telegram", 60, 12))[0] == best


def test_stale_items_are_skipped(store):
    fresh = add(store, "fresh")
    old = add(store, "old")
    age(store, old, 30)
    assert ids(store.candidates("telegram", 60, 12)) == [fresh]


def test_failed_and_rejected_are_excluded(store):
    dead = add(store, "dead")
    store.set_status(dead, "failed")
    assert store.candidates("telegram", 60, 12) == []


def test_limit_is_honoured(store):
    for i in range(5):
        add(store, f"story {i}")
    assert len(store.candidates("telegram", 60, 12, limit=2)) == 2


# --- per-platform eligibility ---------------------------------------------


def test_recording_a_post_removes_it_from_that_platform(store):
    item_id = add(store)
    store.record_post(item_id, "telegram", "@news_eu", "https://t.me/news_eu/1")
    assert store.candidates("telegram", 60, 12) == []


def test_a_youtube_post_stays_eligible_for_telegram(store):
    """The bug the posts table exists to fix: mark_posted used to set
    status='posted' and hide the item from every other platform."""
    item_id = add(store)
    store.record_post(item_id, "youtube", "mirnews")
    assert ids(store.candidates("telegram", 60, 12)) == [item_id]
    assert store.candidates("youtube", 60, 12) == []


def test_record_post_is_idempotent(store):
    item_id = add(store)
    store.record_post(item_id, "telegram", "@news_eu", "https://t.me/news_eu/1")
    store.record_post(item_id, "telegram", "@news_eu", "https://t.me/news_eu/1")
    assert store.links_for(item_id, "telegram") == [("@news_eu", "https://t.me/news_eu/1")]


def test_a_later_link_fills_in_a_missing_one(store):
    item_id = add(store)
    store.record_post(item_id, "telegram", "@news_eu", None)
    store.record_post(item_id, "telegram", "@news_eu", "https://t.me/news_eu/1")
    assert store.links_for(item_id, "telegram") == [("@news_eu", "https://t.me/news_eu/1")]


def test_partial_fan_out_records_only_what_went_out(store):
    item_id = add(store)
    store.record_post(item_id, "telegram", "@news_eu", "https://t.me/news_eu/1")
    assert [t for t, _ in store.links_for(item_id, "telegram")] == ["@news_eu"]


# --- attempts --------------------------------------------------------------


def test_three_failures_kill_an_item(store):
    item_id = add(store)
    assert store.bump_attempts(item_id) == 1
    assert store.candidates("telegram", 60, 12)  # still eligible
    store.bump_attempts(item_id)
    assert store.bump_attempts(item_id) == 3
    assert store.get_item(item_id)["status"] == "failed"
    assert store.candidates("telegram", 60, 12) == []


def test_a_successful_post_clears_the_attempt_count(store):
    item_id = add(store)
    store.bump_attempts(item_id)
    store.record_post(item_id, "telegram", "@news_eu", "https://t.me/news_eu/1")
    assert store.get_item(item_id)["attempts"] == 0


# --- asks ------------------------------------------------------------------


def test_ask_round_trip(store):
    item_id = add(store)
    chans = [{"chat_id": "@news_eu", "link": "https://t.me/news_eu/1"}]
    state = {"item_id": item_id, "mode": "all", "cur": 0, "chans": chans, "sel": {"0": [1]}}
    ask_id = store.open_ask(item_id, state)

    ask = store.get_ask(ask_id)
    assert ask["status"] == "open"
    assert ask["state"] == state
    assert ask["message_id"] is None

    store.bind_ask(ask_id, -100123, 4242)
    assert store.get_ask(ask_id)["message_id"] == 4242

    store.save_ask_state(ask_id, {**state, "mode": "per"})
    assert store.get_ask(ask_id)["state"]["mode"] == "per"

    assert [a["id"] for a in store.open_asks()] == [ask_id]
    store.close_ask(ask_id, "applied")
    assert store.open_asks() == []


# --- channel counters (the every-5th-post BulkFollows order) ---------------


def test_channel_counter_is_per_channel_and_monotonic(store):
    assert store.bump_channel_posts("@news_eu") == 1
    assert store.bump_channel_posts("@news_eu") == 2
    assert store.bump_channel_posts("@news_ru") == 1
    assert store.channel_post_counts() == {"@news_eu": 2, "@news_ru": 1}


def test_channel_counter_survives_a_restart(store):
    """The whole point: at one post a day, an in-memory counter would never
    reach the 5-post threshold."""
    for _ in range(4):
        store.bump_channel_posts("@news_eu")
    store.init()  # simulates a bot restart
    assert store.bump_channel_posts("@news_eu") == 5


def test_the_threshold_fires_on_every_fifth_post(store):
    from modules.telegram import reactions

    fired = [n for n in (store.bump_channel_posts("@news_eu") for _ in range(12))
             if n % reactions.CHANNEL_POST_THRESHOLD == 0]
    assert fired == [5, 10]


def test_init_is_idempotent(store):
    """Startup runs it every time, including against an existing database."""
    item_id = add(store)
    store.init()
    assert store.get_item(item_id) is not None
