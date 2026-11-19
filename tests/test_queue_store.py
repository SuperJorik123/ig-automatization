"""The store: the publication log, counters and message tracking."""

def add(store, text="a story", score=80, regions=("eu",), media=(), source="tg:@src"):
    item_id = store.enqueue(source, text, list(media))
    if score is not None:
        store.set_score(item_id, score, list(regions), "queued")
    return item_id


# --- the publication log --------------------------------------------------


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


# --- channel counters (the every-5th-post BulkFollows order) ---------------


def test_channel_counter_is_per_channel_and_monotonic(store):
    assert store.bump_channel_posts("@news_eu") == 1
    assert store.bump_channel_posts("@news_eu") == 2
    assert store.bump_channel_posts("@news_ru") == 1


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


# --- control-group message tracking -----------------------------------------

def test_track_and_clear_group_messages(store):
    store.track_group_message("-100777", 5)
    store.track_group_message("-100777", 3)
    store.track_group_message("-100777", 3)      # duplicate — idempotent
    store.track_group_message("-100999", 8)      # different chat

    assert store.tracked_message_ids("-100777") == [3, 5]
    store.clear_group_messages("-100777", [3])
    assert store.tracked_message_ids("-100777") == [5]
    assert store.tracked_message_ids("-100999") == [8]


def test_track_group_message_accepts_int_chat_id(store):
    store.track_group_message(-100777, 12)
    assert store.tracked_message_ids("-100777") == [12]
