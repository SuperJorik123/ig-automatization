"""The dispatcher's cheapest filter: text-only posts never reach the scorer."""

from modules.telegram import dispatcher, queue_store


def boom(*a, **kw):
    raise AssertionError("this item should never have been scored")


def test_text_only_items_are_rejected_without_scoring(store, monkeypatch):
    monkeypatch.setattr(dispatcher.smart_filter, "evaluate", boom)
    item_id = queue_store.enqueue("tg:@src", "just some commentary", [])

    assert dispatcher.process_one() is True  # handled, queue not empty
    assert queue_store.get_item(item_id)["status"] == "rejected"


def test_items_with_media_are_scored(store, monkeypatch):
    monkeypatch.setattr(dispatcher.smart_filter, "evaluate",
                        lambda text, source_hint=None: {"score": 72, "regions": ["eu"],
                                                        "tier": "high"})
    monkeypatch.setattr(dispatcher.smart_filter, "youtube_targets", lambda item: [])
    item_id = queue_store.enqueue("tg:@src", "a story",
                                  [{"file_id": "p1", "type": "photo"}])

    assert dispatcher.process_one() is True
    item = queue_store.get_item(item_id)
    assert item["status"] == "queued"
    assert item["score"] == 72
    assert item["regions"] == ["eu"]


def test_an_empty_queue_is_reported(store):
    assert dispatcher.process_one() is False


def test_the_media_rule_can_be_switched_off(store, monkeypatch):
    from shared import config

    monkeypatch.setattr(config, "NEWS_REQUIRE_MEDIA", False)
    monkeypatch.setattr(dispatcher.smart_filter, "evaluate",
                        lambda text, source_hint=None: {"score": 65, "regions": ["global"],
                                                        "tier": "medium"})
    monkeypatch.setattr(dispatcher.smart_filter, "youtube_targets", lambda item: [])
    item_id = queue_store.enqueue("tg:@src", "text only", [])

    dispatcher.process_one()
    assert queue_store.get_item(item_id)["status"] == "queued"
