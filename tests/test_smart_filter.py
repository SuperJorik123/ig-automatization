"""Routing policy: which channels a scored item reaches."""

from modules.telegram import smart_filter


def dest(chat_id, *regions, lang="en"):
    return {"chat_id": chat_id, "lang": lang, "regions": set(regions)}


EU = dest("@news_eu", "eu")
RU = dest("@news_ru", "ru", lang="ru")
WORLD = dest("@news_world", "us", "eu")
ANY = dest("@news_any")  # no regions configured -> catch-all


PHOTO = {"type": "photo", "path": "a.jpg"}


def item(score=80, regions=("eu",), media=(PHOTO,)):
    """A collected story. Media by default — text-only posts aren't news."""
    return {"id": 1, "score": score, "regions": list(regions), "media": list(media)}


# --- matches ---------------------------------------------------------------


def test_exact_region_matches():
    assert smart_filter.matches(["eu"], EU)


def test_other_region_does_not_match():
    assert not smart_filter.matches(["ru"], EU)


def test_global_items_reach_every_channel():
    assert smart_filter.matches(["global"], EU)
    assert smart_filter.matches(["global"], RU)


def test_multi_region_channel_matches_any_of_its_regions():
    assert smart_filter.matches(["us"], WORLD)
    assert smart_filter.matches(["eu"], WORLD)
    assert not smart_filter.matches(["ru"], WORLD)


def test_channel_without_regions_is_a_catch_all():
    assert smart_filter.matches(["ru"], ANY)
    assert smart_filter.matches([], ANY)


def test_item_without_regions_only_reaches_catch_alls():
    """An unrouted item shouldn't leak into region-specific channels."""
    assert not smart_filter.matches([], EU)


# --- telegram_targets ------------------------------------------------------


def test_telegram_targets_filters_by_region():
    got = smart_filter.telegram_targets(item(regions=["eu"]), [EU, RU, WORLD], min_score=60)
    assert [d["chat_id"] for d in got] == ["@news_eu", "@news_world"]


def test_telegram_targets_respects_the_score_floor():
    assert smart_filter.telegram_targets(item(score=59), [EU], min_score=60) == []
    assert smart_filter.telegram_targets(item(score=60), [EU], min_score=60) == [EU]


def test_text_only_stories_are_not_news():
    """A source post with no photo/video is commentary, not news."""
    assert smart_filter.telegram_targets(item(media=[]), [EU], min_score=60) == []


def test_a_photo_makes_it_news():
    with_photo = item(media=[{"type": "photo", "path": "a.jpg"}])
    assert smart_filter.telegram_targets(with_photo, [EU], min_score=60) == [EU]


def test_a_video_makes_it_news():
    with_video = item(media=[{"type": "video", "path": "a.mp4"}])
    assert smart_filter.telegram_targets(with_video, [EU], min_score=60) == [EU]


def test_an_album_makes_it_news():
    album = item(media=[{"type": "photo", "path": "a.jpg"},
                        {"type": "video", "path": "b.mp4"}])
    assert smart_filter.telegram_targets(album, [EU], min_score=60) == [EU]


def test_is_news_can_be_switched_off(monkeypatch):
    from shared import config

    monkeypatch.setattr(config, "NEWS_REQUIRE_MEDIA", False)
    assert smart_filter.is_news(item(media=[]))
    assert smart_filter.telegram_targets(item(media=[]), [EU], min_score=60) == [EU]


def test_telegram_targets_handles_an_unscored_item():
    assert smart_filter.telegram_targets({"regions": ["eu"], "media": []}, [EU], min_score=60) == []


# --- youtube_targets -------------------------------------------------------


def test_youtube_targets_needs_a_video():
    no_video = item(score=90, media=[{"type": "photo", "path": "a.jpg"}])
    assert smart_filter.youtube_targets(no_video, [EU], min_score=70) == []

    with_video = item(score=90, media=[{"type": "video", "path": "a.mp4"}])
    assert smart_filter.youtube_targets(with_video, [EU], min_score=70) == [EU]


def test_youtube_targets_ignores_region():
    """YT channels are language-targeted; every one gets the qualifying video."""
    it = item(score=90, regions=["ru"], media=[{"type": "video", "path": "a.mp4"}])
    assert smart_filter.youtube_targets(it, [EU, WORLD], min_score=70) == [EU, WORLD]


def test_youtube_floor_is_higher_than_telegram_by_default():
    """Shorts uploads cost API quota; Telegram posts don't."""
    from shared import config

    assert config.YT_AUTO_MIN_SCORE >= config.TG_AUTO_MIN_SCORE


# --- best_of: the head-to-head choice made at post time --------------------


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
