"""The reaction ask's state machine, exercised without a Telegram connection."""

from modules.telegram import reactions

CHANS = [
    {"chat_id": "@news_eu", "link": "https://t.me/news_eu/412"},
    {"chat_id": "@news_ru", "link": "https://t.me/news_ru/377"},
]

HEART, LIKE, DISLIKE = 0, 1, 2


def press(state, *verbs):
    """Apply a run of button presses, returning (state, last action)."""
    action = None
    for v in verbs:
        state, action = reactions.reduce(state, v)
    return state, action


def test_new_state():
    s = reactions.new_state(41, CHANS)
    assert s["mode"] == "all"
    assert s["cur"] == 0
    assert [c["chat_id"] for c in s["chans"]] == ["@news_eu", "@news_ru"]
    assert s["sel"] == {"0": [], "1": []}


def test_all_mode_toggles_every_channel_at_once():
    s, _ = press(reactions.new_state(41, CHANS), f"t{HEART}")
    assert s["sel"] == {"0": [HEART], "1": [HEART]}


def test_toggling_twice_clears_it():
    s, _ = press(reactions.new_state(41, CHANS), f"t{HEART}", f"t{HEART}")
    assert s["sel"] == {"0": [], "1": []}


def test_per_channel_mode_starts_from_the_shared_selection():
    """Pick the common set first, then adjust the odd channel out."""
    s, _ = press(reactions.new_state(41, CHANS), f"t{HEART}", "pc", f"t{LIKE}")
    assert s["mode"] == "per"
    assert s["sel"]["0"] == [HEART, LIKE]  # current channel got the extra one
    assert s["sel"]["1"] == [HEART]        # the other kept the shared set


def test_navigation_wraps():
    s = reactions.new_state(41, CHANS)
    s, _ = press(s, "pc", "nx")
    assert s["cur"] == 1
    s, _ = press(s, "nx")
    assert s["cur"] == 0
    s, _ = press(s, "pv")
    assert s["cur"] == 1


def test_per_channel_toggle_only_touches_the_current_channel():
    s, _ = press(reactions.new_state(41, CHANS), "pc", "nx", f"t{DISLIKE}")
    assert s["sel"]["0"] == []
    assert s["sel"]["1"] == [DISLIKE]


def test_apply_and_skip_are_reported_as_actions():
    assert press(reactions.new_state(41, CHANS), "ap")[1] == "apply"
    assert press(reactions.new_state(41, CHANS), "sk")[1] == "skip"


def test_unknown_verb_is_ignored():
    s = reactions.new_state(41, CHANS)
    out, action = reactions.reduce(s, "bogus")
    assert action is None and out["sel"] == s["sel"]


def test_reduce_does_not_mutate_the_input():
    """States are persisted between taps; in-place edits would corrupt them."""
    s = reactions.new_state(41, CHANS)
    reactions.reduce(s, f"t{HEART}")
    assert s["sel"] == {"0": [], "1": []}


def test_orders_from_pairs_every_channel_with_every_emoji():
    s, _ = press(reactions.new_state(41, CHANS), f"t{HEART}", f"t{LIKE}")
    orders = reactions.orders_from(s)
    assert len(orders) == 4
    assert {(c, e["name"]) for c, _, e in orders} == {
        ("@news_eu", "heart"), ("@news_eu", "like"),
        ("@news_ru", "heart"), ("@news_ru", "like"),
    }


def test_orders_skip_channels_without_a_public_link():
    chans = [CHANS[0], {"chat_id": "-1001234567890", "link": None}]
    s, _ = press(reactions.new_state(41, chans), f"t{HEART}")
    assert [c for c, _, _ in reactions.orders_from(s)] == ["@news_eu"]


def test_no_selection_orders_nothing():
    assert reactions.orders_from(reactions.new_state(41, CHANS)) == []


def test_summary_names_each_channel():
    s, _ = press(reactions.new_state(41, CHANS), f"t{HEART}", "pc", f"t{LIKE}")
    text = reactions.summary(s, applied=True)
    assert "@news_eu" in text and "@news_ru" in text
    assert reactions.summary(s, applied=False).startswith("✕")


def test_render_produces_a_keyboard_for_both_modes():
    s = reactions.new_state(41, CHANS)
    text, kb = reactions.render(7, s)
    assert "2 channel(s)" in text
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"r:7:t{HEART}" in data and "r:7:ap" in data and "r:7:pc" in data

    s, _ = press(s, "pc")
    text, kb = reactions.render(7, s)
    assert "1/2" in text
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "r:7:nx" in data and "r:7:pv" in data


def test_callback_data_fits_telegrams_64_byte_cap():
    _, kb = reactions.render(999999, reactions.new_state(41, CHANS))
    assert all(len(b.callback_data.encode()) <= 64
               for row in kb.inline_keyboard for b in row)


def test_channel_link_drops_the_message_id():
    assert reactions.channel_link("@c", "https://t.me/c/412") == "https://t.me/c"
    assert reactions.channel_link("@c", None) == "https://t.me/c"
    assert reactions.channel_link("-100123", None) == ""
