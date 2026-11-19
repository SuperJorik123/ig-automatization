"""The news filter: what counts as news, and the head-to-head pick."""

from modules.telegram import smart_filter


def dest(chat_id, *regions, lang="en"):
    return {"chat_id": chat_id, "lang": lang, "regions": set(regions)}


EU = dest("@news_eu", "eu")


PHOTO = {"type": "photo", "path": "a.jpg"}


def item(score=80, regions=("eu",), media=(PHOTO,)):
    """A collected story. Media by default — text-only posts aren't news."""
    return {"id": 1, "score": score, "regions": list(regions), "media": list(media)}


# --- is_news: media required -----------------------------------------------


def test_text_only_stories_are_not_news():
    """A source post with no photo/video is commentary, not news."""
    assert not smart_filter.is_news(item(media=[]))


def test_a_photo_makes_it_news():
    assert smart_filter.is_news(item(media=[{"type": "photo", "path": "a.jpg"}]))


def test_a_video_makes_it_news():
    assert smart_filter.is_news(item(media=[{"type": "video", "path": "a.mp4"}]))


def test_an_album_makes_it_news():
    album = item(media=[{"type": "photo", "path": "a.jpg"},
                        {"type": "video", "path": "b.mp4"}])
    assert smart_filter.is_news(album)


def test_is_news_can_be_switched_off(monkeypatch):
    from shared import config

    monkeypatch.setattr(config, "NEWS_REQUIRE_MEDIA", False)
    assert smart_filter.is_news(item(media=[]))


# --- best_of: the head-to-head choice ------------------------------------


def pair(item_id, text, score=80):
    return ({"id": item_id, "text": text, "score": score, "regions": ["eu"]}, [EU])


def test_best_of_returns_the_model_s_choice(monkeypatch):
    seen = {}

    def fake_compare(texts):
        seen["texts"] = texts
        return 2

    monkeypatch.setattr(smart_filter.scorer, "compare", fake_compare)
    pairs = [pair(1, "a"), pair(2, "b"), pair(3, "c")]
    item, targets = smart_filter.best_of(pairs, top=5)

    assert item["id"] == 3
    assert targets == [EU]
    assert seen["texts"] == ["a", "b", "c"]


def test_best_of_only_compares_the_shortlist(monkeypatch):
    monkeypatch.setattr(smart_filter.scorer, "compare", lambda texts: len(texts) - 1)
    pairs = [pair(i, f"story {i}") for i in range(1, 8)]
    item, _ = smart_filter.best_of(pairs, top=3)
    assert item["id"] == 3  # the last of the first three, never #7


def test_best_of_skips_the_call_for_a_single_candidate(monkeypatch):
    def boom(texts):
        raise AssertionError("should not compare one story against itself")

    monkeypatch.setattr(smart_filter.scorer, "compare", boom)
    item, _ = smart_filter.best_of([pair(1, "only")], top=5)
    assert item["id"] == 1


def test_best_of_falls_back_to_the_top_score_when_comparison_fails(monkeypatch):
    """An API hiccup must cost you the comparison, never the post."""
    monkeypatch.setattr(smart_filter.scorer, "compare", lambda texts: None)
    item, _ = smart_filter.best_of([pair(1, "a"), pair(2, "b")], top=5)
    assert item["id"] == 1


def test_comparison_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(smart_filter.scorer, "compare",
                        lambda texts: (_ for _ in ()).throw(AssertionError("compared")))
    item, _ = smart_filter.best_of([pair(1, "a"), pair(2, "b")], top=1)
    assert item["id"] == 1


def test_best_of_handles_an_empty_list():
    assert smart_filter.best_of([]) == (None, None)
