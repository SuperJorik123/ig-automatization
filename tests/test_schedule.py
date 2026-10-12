"""modules/telegram/schedule.py (pure) + queue_store's scheduled table."""

import datetime as dt

from modules.telegram import schedule

TZ = schedule.TZ
NOW = dt.datetime(2026, 10, 13, 15, 20, tzinfo=TZ)  # Tue 13 Oct, 15:20


def at(*args):
    return dt.datetime(*args, tzinfo=TZ)


# --- parse_when --------------------------------------------------------------


def test_a_clock_time_later_today_is_today():
    assert schedule.parse_when("18:30", NOW) == at(2026, 10, 13, 18, 30)
    assert schedule.parse_when("at 18.30", NOW) == at(2026, 10, 13, 18, 30)


def test_a_clock_time_already_passed_is_tomorrow():
    assert schedule.parse_when("09:00", NOW) == at(2026, 10, 14, 9, 0)


def test_tomorrow():
    assert schedule.parse_when("Tomorrow 9:05", NOW) == at(2026, 10, 14, 9, 5)
    assert schedule.parse_when("tomorrow at 21:00", NOW) == at(2026, 10, 14, 21, 0)


def test_dates():
    assert schedule.parse_when("14.10 18:30", NOW) == at(2026, 10, 14, 18, 30)
    assert schedule.parse_when("at 1/11 08:00", NOW) == at(2026, 11, 1, 8, 0)
    assert schedule.parse_when("14.10.2026 18:30", NOW) == at(2026, 10, 14, 18, 30)
    # a date already gone this year, without a year, is next year's
    assert schedule.parse_when("01.02 10:00", NOW) == at(2027, 2, 1, 10, 0)


def test_relative():
    assert schedule.parse_when("+2h", NOW) == NOW + dt.timedelta(hours=2)
    assert schedule.parse_when("+90m", NOW) == NOW + dt.timedelta(minutes=90)
    assert schedule.parse_when("in 1h 30min", NOW) == NOW + dt.timedelta(minutes=90)


def test_not_a_time():
    for text in ("Truck runs red light", "", "25:00", "31.02 10:00", "+", "18"):
        assert schedule.parse_when(text, NOW) is None, text


def test_check_bounds():
    assert schedule.check(NOW + dt.timedelta(seconds=20), NOW)
    assert schedule.check(NOW + dt.timedelta(days=31), NOW)
    assert schedule.check(NOW + dt.timedelta(hours=2), NOW) == ""


def test_presets_are_all_in_the_future_and_absolute():
    late = at(2026, 10, 13, 20, 45)
    labels = [label for label, _ in schedule.presets(late)]
    assert labels[:3] == ["+1h · 21:45", "+3h · 23:45", "+6h · 02:45"]
    assert "Today 18:00" not in labels and "Today 21:00" not in labels
    assert "Tomorrow 09:00" in labels
    assert all(when > late for _, when in schedule.presets(late))


def test_fmt_in():
    assert schedule.fmt_in(NOW + dt.timedelta(minutes=200), NOW) == "in 3 h 20 min"
    assert schedule.fmt_in(NOW + dt.timedelta(days=1, hours=2), NOW) == "in 1 d 2 h"


# --- payload round trip -----------------------------------------------------


def _state():
    a = {"name": "a", "tw": "acc_a", "ig": "ig_a"}
    b = {"name": "b", "tw": "acc_b"}
    ra = {"brand": a, "path": "/m/a.mp4", "headline": "Hi A", "kind": "video"}
    rb = {"brand": b, "path": "/m/b.mp4", "headline": "Hi B", "kind": "video"}
    return {"text": "Hi", "info": "src", "footage": {"summary": "s"},
            "renders": [ra, rb],
            "platforms": [{"platform": "tw", "label": "X", "renders": [ra, rb]},
                          {"platform": "ig", "label": "IG", "renders": [ra]}],
            "sel_platforms": {0, 1}}


def test_payload_round_trips_against_current_brands():
    payload = schedule.to_payload(_state())
    brands = [{"name": "a", "tw": "acc_a", "ig": "ig_a"},
              {"name": "b", "tw": "acc_b"}]
    state, missing = schedule.from_payload(payload, brands)
    assert missing == []
    assert [p["label"] for p in state["platforms"]] == ["X", "IG"]
    assert state["sel_platforms"] == {0, 1}
    assert [r["brand"]["name"] for r in state["platforms"][0]["renders"]] == ["a", "b"]
    assert state["files"] == ["/m/a.mp4", "/m/b.mp4"]
    assert state["info"] == "src"


def test_a_removed_brand_or_account_drops_out():
    payload = schedule.to_payload(_state())
    # brand b deleted; brand a lost its IG account
    state, missing = schedule.from_payload(payload, [{"name": "a", "tw": "acc_a"}])
    assert missing == ["b"]
    assert [p["label"] for p in state["platforms"]] == ["X"]
    assert state["sel_platforms"] == {0}
    # files still lists both, so both renders get cleaned up
    assert state["files"] == ["/m/a.mp4", "/m/b.mp4"]


def test_an_unticked_platform_stays_unticked():
    st = _state()
    st["sel_platforms"] = {1}
    state, _ = schedule.from_payload(schedule.to_payload(st), [
        {"name": "a", "tw": "acc_a", "ig": "ig_a"}, {"name": "b", "tw": "acc_b"}])
    assert state["sel_platforms"] == {1}


# --- queue_store ------------------------------------------------------------


def test_store_add_claim_once(store):
    when = dt.datetime(2026, 10, 14, 18, 30, tzinfo=TZ)
    sid = store.add_scheduled(when, -100, 42, {"renders": [], "x": 1})
    row = store.get_scheduled(sid)
    assert row["due_at"] == when and row["status"] == "pending"
    assert row["payload"]["x"] == 1
    assert [r["id"] for r in store.scheduled_with_status("pending")] == [sid]
    assert store.claim_scheduled(sid, "pending", "running")
    assert not store.claim_scheduled(sid, "pending", "running")  # fires once
    assert not store.claim_scheduled(sid, "pending", "cancelled")  # too late
    assert store.claim_scheduled(sid, "running", "done")
    assert store.scheduled_with_status("pending", "running") == []
