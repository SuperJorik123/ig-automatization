"""modules/newsroom/pace.py — the one-post-per-channel-per-day rule.

Pure and offline by construction: the module takes `now` as an argument and
reads no config, so every case below is an exact assertion rather than a
sleep.
"""

from datetime import datetime, timedelta, timezone

import pytest

from modules.newsroom import pace

NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)


def ago(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# The cooldown                                                                #
# --------------------------------------------------------------------------- #


def test_a_channel_that_never_posted_may_post_now():
    assert pace.cooldown_remaining("@acme", None, NOW, 24, 3) == 0.0
    assert pace.cooldown_remaining("@acme", "", NOW, 24, 3) == 0.0


def test_a_post_an_hour_ago_blocks_for_the_rest_of_the_day():
    left = pace.cooldown_remaining("@acme", ago(1), NOW, 24, 0)

    assert left == pytest.approx(23 * 3600)


def test_the_window_opens_again_after_the_interval():
    assert pace.cooldown_remaining("@acme", ago(24.5), NOW, 24, 0) == 0.0


def test_a_zero_interval_disables_the_cooldown():
    assert pace.cooldown_remaining("@acme", ago(0.01), NOW, 0, 0) == 0.0


def test_a_naive_timestamp_is_read_as_utc():
    naive = (NOW - timedelta(hours=1)).replace(tzinfo=None).isoformat()

    assert pace.cooldown_remaining("@acme", naive, NOW, 24, 0) == pytest.approx(23 * 3600)


def test_an_unparseable_timestamp_fails_open():
    # One bad row must not wedge a client's channel forever; the post that
    # follows writes a good timestamp.
    assert pace.cooldown_remaining("@acme", "not-a-date", NOW, 24, 3) == 0.0


# --------------------------------------------------------------------------- #
# The jitter                                                                  #
# --------------------------------------------------------------------------- #


def test_jitter_stays_inside_its_range():
    for i in range(50):
        j = pace.jitter_seconds(f"@chan{i}", ago(24), 3)
        assert -3 * 3600 <= j < 3 * 3600


def test_jitter_is_stable_across_calls():
    # The whole point: re-drawing on every 5-minute poll would let the channel
    # post on the lowest of 288 draws, which is the floor.
    first = pace.jitter_seconds("@acme", ago(24), 3)

    assert all(pace.jitter_seconds("@acme", ago(24), 3) == first for _ in range(5))


def test_jitter_differs_per_channel():
    stamp = ago(24)
    spread = {pace.jitter_seconds(f"@chan{i}", stamp, 3) for i in range(10)}

    assert len(spread) == 10  # seven channels must not post in lockstep


def test_jitter_moves_with_the_window():
    assert pace.jitter_seconds("@acme", ago(24), 3) != pace.jitter_seconds("@acme", ago(48), 3)


def test_jitter_swings_both_ways():
    # 24 h ± 3 h: some channels come due early, some late. A one-sided jitter
    # would make 24 h a floor instead of the average.
    stamp = ago(24)  # exactly one interval old, so jitter is all that is left
    signs = {pace.jitter_seconds(f"@chan{i}", stamp, 3) > 0 for i in range(20)}

    assert signs == {True, False}


def test_the_real_gap_is_the_interval_plus_or_minus_the_jitter():
    # The window the client was promised: never shorter than 21 h, never
    # longer than 27 h.
    for i in range(50):
        chan, stamp = f"@chan{i}", ago(21)
        due_after = 21 * 3600 + pace.cooldown_remaining(chan, stamp, NOW, 24, 3)
        assert 21 * 3600 <= due_after <= 27 * 3600


def test_an_early_draw_opens_the_window_before_the_interval():
    # Find a channel whose draw came in negative and check it may post at 22 h.
    stamp = ago(22)
    early = [f"@chan{i}" for i in range(20)
             if pace.jitter_seconds(f"@chan{i}", stamp, 3) < -2 * 3600]

    assert early, "no channel drew a large negative offset — check the seeding"
    assert pace.cooldown_remaining(early[0], stamp, NOW, 24, 3) == 0.0


def test_zero_jitter_is_off():
    assert pace.jitter_seconds("@acme", ago(24), 0) == 0.0


# --------------------------------------------------------------------------- #
# The log line                                                                #
# --------------------------------------------------------------------------- #


def test_format_wait_reads_like_a_clock():
    assert pace.format_wait(18 * 3600 + 42 * 60) == "18h42m"
    assert pace.format_wait(42 * 60) == "42m"
    assert pace.format_wait(0) == "0m"
