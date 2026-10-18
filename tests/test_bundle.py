"""Offline tests for modules/telegram/bundle.py — the assignment of bundle
videos to accounts, the publish timetable, and the weekly report maths."""

import random

import pytest

from modules.telegram import bundle

ACCOUNTS = [f"acc{i}" for i in range(10)]


# --------------------------------------------------------------------------- #
# assignment                                                                  #
# --------------------------------------------------------------------------- #


def test_three_videos_on_ten_accounts_split_4_3_3():
    out, carried = bundle.assign(["a", "b", "c"], ACCOUNTS)
    assert set(out) == set(ACCOUNTS) and carried == []
    assert sorted(bundle.copies(out).values()) == [3, 3, 4]


def test_the_draw_differs_between_runs():
    draws = {tuple(sorted(bundle.assign(["a", "b", "c"], ACCOUNTS)[0].items()))
             for _ in range(20)}
    assert len(draws) > 1


def test_ten_videos_one_each():
    out, carried = bundle.assign(list("abcdefghij"), ACCOUNTS)
    assert sorted(out.values()) == list("abcdefghij") and carried == []


def test_one_video_goes_everywhere():
    out, _ = bundle.assign(["a"], ACCOUNTS)
    assert set(out.values()) == {"a"} and len(out) == 10


def test_twelve_videos_carry_two_over():
    videos = list("abcdefghijkl")
    out, carried = bundle.assign(videos, ACCOUNTS)
    assert len(out) == 10 and len(set(out.values())) == 10
    assert carried == ["k", "l"]          # the last to join wait


def test_a_paused_account_gets_nothing_and_the_split_uses_the_rest():
    active = [a for a in ACCOUNTS if a != "acc3"]
    out, _ = bundle.assign(["a", "b", "c"], active)
    assert "acc3" not in out and len(out) == 9
    assert sorted(bundle.copies(out).values()) == [3, 3, 3]


@pytest.mark.parametrize("seed", range(30))
def test_history_is_avoided_whenever_possible(seed):
    history = {"acc0": {"a"}, "acc1": {"a", "b"}, "acc2": {"b"},
               "acc3": {"c"}, "acc4": {"a", "c"}}
    out, _ = bundle.assign(["a", "b", "c"], ACCOUNTS, history,
                           rng=random.Random(seed))
    assert bundle.clashes(out, history) == []
    assert bundle.even_split_ok(out)


def test_an_unavoidable_clash_still_gives_every_account_a_video():
    history = {a: {"a"} for a in ACCOUNTS}
    out, _ = bundle.assign(["a"], ACCOUNTS, history)
    assert len(out) == 10            # posts everywhere; the clash is reported
    assert len(bundle.clashes(out, history)) == 10


def test_split_sizes():
    assert bundle.split_sizes(3, 10) == [4, 3, 3]
    assert bundle.split_sizes(12, 10) == [1] * 10
    assert bundle.split_sizes(0, 10) == [] and bundle.split_sizes(3, 0) == []


def test_no_accounts_carries_everything():
    assert bundle.assign(["a", "b"], []) == ({}, ["a", "b"])


# --------------------------------------------------------------------------- #
# timetable                                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("videos", [["a"], ["a", "b", "c"], list("abcdefghij")])
@pytest.mark.parametrize("seed", range(10))
def test_timetable_respects_the_copy_cap_and_the_gaps(videos, seed):
    rng = random.Random(seed)
    out, _ = bundle.assign(videos, ACCOUNTS, rng=rng)
    slots = bundle.timetable(out, rng=rng)
    assert sorted(a for _, a, _ in slots) == sorted(ACCOUNTS)
    assert bundle.check_timetable(slots)
    times = [t for t, _, _ in slots]
    assert times == sorted(times) and times[0] == 0
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(g >= 119 for g in gaps)


def test_ten_different_videos_go_out_in_2_to_8_minute_gaps():
    out, _ = bundle.assign(list("abcdefghij"), ACCOUNTS)
    times = [t for t, _, _ in bundle.timetable(out)]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(119 <= g <= 481 for g in gaps)


def test_one_video_waits_for_the_cap():
    out, _ = bundle.assign(["a"], ACCOUNTS)
    slots = bundle.timetable(out, rng=random.Random(1))
    assert bundle.check_timetable(slots)
    assert slots[-1][0] >= 4 * 1800       # 5 pairs, 30 min apart


def test_check_timetable_catches_three_in_thirty_minutes():
    assert not bundle.check_timetable([(0, "x", "a"), (600, "y", "a"),
                                       (1200, "z", "a")])
    assert bundle.check_timetable([(0, "x", "a"), (600, "y", "a"),
                                   (1800, "z", "a")])


# --------------------------------------------------------------------------- #
# messages                                                                    #
# --------------------------------------------------------------------------- #


def test_a_video_line_says_what_it_still_needs():
    assert "waiting for info" in bundle.video_line(1, {"text": "H"})
    assert bundle.video_line(2, {"text": "H", "info": "i",
                                 "headline_pick": 0}) == "2. H ⭐"


def test_summary_lists_every_account_with_its_time():
    import datetime as dt
    start = dt.datetime(2026, 10, 18, 14, 0)
    slots = [(0, "acc0", "a"), (300, "acc1", "b"), (600, "acc2", "a")]
    text = bundle.summary_text(7, {"a": "Head A", "b": "Head B"}, slots,
                               start, ["IG", "X"])
    assert text.startswith("📦 Bundle #7 ready: 2 videos → 3 accounts (2 / 1)")
    assert "acc0 14:00 · acc2 14:10" in text and "acc1 14:05" in text
    assert "on IG · X" in text


def test_buttons_are_db_routed_by_bundle_id():
    kb = bundle.ready_keyboard(7).inline_keyboard
    data = [b.callback_data for row in kb for b in row]
    assert data == ["u:pub:7", "u:rs:7", "u:x:7"]


# --------------------------------------------------------------------------- #
# report                                                                      #
# --------------------------------------------------------------------------- #


ROWS = [
    {"account": "acc0", "bundle_id": 1, "copy_count": 4,
     "insights_72h": {"views": 60_000}, "followers_at_post": 100,
     "followers_72h": 130},
    {"account": "acc1", "bundle_id": 1, "copy_count": 4,
     "insights_24h": {"views": 2_000}},
    {"account": "acc0", "bundle_id": 2, "copy_count": 1,
     "insights_72h": {"views": 12_000}},
    {"account": "acc2", "bundle_id": None, "copy_count": 1},
]


def test_account_stats():
    s = bundle.account_stats(ROWS)
    assert s["acc0"] == {"posts": 2, "follows": 30, "median": 36_000,
                         "hits10k": 2, "hits50k": 1}
    assert s["acc2"]["median"] is None


def test_bundle_spread_and_copies():
    assert bundle.bundle_spread(ROWS)[1] == (60_000, 31_000, 1.9)
    comp = bundle.copies_comparison(ROWS)
    assert comp["4+"] == (31_000, 2) and comp["1-2"] == (12_000, 1)


def test_report_text_mentions_every_section():
    text = bundle.report_text(ROWS, "last 7 days")
    assert "Per account" in text and "Per bundle" in text
    assert "on 4+ accounts" in text
    assert "no reels" in bundle.report_text([], "x")
