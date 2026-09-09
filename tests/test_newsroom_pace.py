"""modules/newsroom/pace.py — when one channel may post.

Pure and offline by construction: the module takes `now` as an argument and
reads no config, so every case below is an exact assertion rather than a sleep.
"""

from datetime import datetime, timedelta, timezone

import pytest

from modules.newsroom import pace

NOW = datetime(2026, 9, 9, 6, 0, 0, tzinfo=timezone.utc)


def ago(minutes: float) -> str:
    return (NOW - timedelta(minutes=minutes)).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# The calendar day                                                            #
# --------------------------------------------------------------------------- #


def test_utc_day_is_the_date_alone():
    assert pace.utc_day("2026-09-09T04:22:00+00:00") == "2026-09-09"
    assert pace.utc_day(NOW) == "2026-09-09"


def test_utc_day_reads_a_naive_stamp_as_utc():
    assert pace.utc_day("2026-09-09T04:22:00") == "2026-09-09"


def test_utc_day_normalises_an_offset():
    # 23:30 in UTC+2 is still the 9th in UTC — one day, decided in one place.
    assert pace.utc_day("2026-09-09T23:30:00+02:00") == "2026-09-09"


def test_utc_day_of_junk_is_empty():
    # The caller treats "" as "no readable date" and falls back to today
    # rather than silently dropping the article.
    assert pace.utc_day("not-a-date") == ""
    assert pace.utc_day(None) == ""


# --------------------------------------------------------------------------- #
# The settle delay                                                            #
# --------------------------------------------------------------------------- #


def test_settle_stays_inside_its_range():
    for i in range(50):
        assert 45 * 60 <= pace.settle_seconds(f"@chan{i}", "2026-09-09", 45, 120) < 120 * 60


def test_settle_is_stable_across_calls():
    # The whole point: re-drawing on every 5-minute poll would let the channel
    # post on the lowest of 288 draws, which is the floor.
    first = pace.settle_seconds("@acme", "2026-09-09", 45, 120)

    assert all(pace.settle_seconds("@acme", "2026-09-09", 45, 120) == first
               for _ in range(5))


def test_settle_differs_per_channel():
    spread = {pace.settle_seconds(f"@chan{i}", "2026-09-09", 45, 120) for i in range(10)}

    assert len(spread) == 10  # seven channels must not post in lockstep


def test_settle_differs_per_day():
    # Otherwise a channel posts at the same minute every morning.
    assert (pace.settle_seconds("@acme", "2026-09-09", 45, 120)
            != pace.settle_seconds("@acme", "2026-09-10", 45, 120))


def test_a_flat_range_is_an_exact_delay():
    assert pace.settle_seconds("@acme", "2026-09-09", 60, 60) == 60 * 60


def test_a_reversed_range_is_read_the_right_way_round():
    lo = pace.settle_seconds("@acme", "2026-09-09", 120, 45)

    assert 45 * 60 <= lo < 120 * 60


# --------------------------------------------------------------------------- #
# The hold                                                                    #
# --------------------------------------------------------------------------- #


def test_a_fresh_article_is_held():
    # Posting on sight ships the FIRST article of the morning burst, not the
    # last.
    assert pace.hold_remaining("@acme", ago(5), NOW, 60, 60) == pytest.approx(55 * 60)


def test_a_settled_article_is_released():
    assert pace.hold_remaining("@acme", ago(90), NOW, 60, 60) == 0.0


def test_the_hold_is_measured_from_the_newest_article():
    # A burst still arriving keeps resetting the wait, which is what makes
    # "the latest" mean the day's last and not the first through the door.
    assert pace.hold_remaining("@acme", ago(10), NOW, 60, 60) > 0
    assert pace.hold_remaining("@acme", ago(70), NOW, 60, 60) == 0.0


def test_an_undated_article_is_never_held():
    # Failing open: an article with no readable date must not sit in the queue
    # forever.
    assert pace.hold_remaining("@acme", "not-a-date", NOW, 60, 60) == 0.0
    assert pace.hold_remaining("@acme", None, NOW, 60, 60) == 0.0


def test_a_real_burst_releases_once_after_its_last_article():
    # wsmirror, 2026-09-09: 03:48 03:50 04:02 04:09 04:22, flat 60 min settle.
    burst = [datetime(2026, 9, 9, 3, 48, tzinfo=timezone.utc),
             datetime(2026, 9, 9, 3, 50, tzinfo=timezone.utc),
             datetime(2026, 9, 9, 4, 2, tzinfo=timezone.utc),
             datetime(2026, 9, 9, 4, 9, tzinfo=timezone.utc),
             datetime(2026, 9, 9, 4, 22, tzinfo=timezone.utc)]
    newest = burst[-1]

    at_0500 = datetime(2026, 9, 9, 5, 0, tzinfo=timezone.utc)
    at_0525 = datetime(2026, 9, 9, 5, 25, tzinfo=timezone.utc)

    assert pace.hold_remaining("@acme", newest, at_0500, 60, 60) > 0
    assert pace.hold_remaining("@acme", newest, at_0525, 60, 60) == 0.0


# --------------------------------------------------------------------------- #
# The log line                                                                #
# --------------------------------------------------------------------------- #


def test_format_wait_reads_like_a_clock():
    assert pace.format_wait(1 * 3600 + 5 * 60) == "1h05m"
    assert pace.format_wait(42 * 60) == "42m"
    assert pace.format_wait(0) == "0m"
