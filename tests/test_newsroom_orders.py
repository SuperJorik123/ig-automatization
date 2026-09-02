"""modules/newsroom/orders.py — the BulkFollows order rules.

Offline: smm.place_order is replaced, so no panel call is ever made and no
money is ever spent by the suite.

The two behaviours worth the most here are the ones that fail SILENTLY in
production: an order placed with another site's service id, and a bonus order
fired on the wrong channel because the counter was shared.
"""

import importlib

import pytest

from shared import config


@pytest.fixture
def orders(tmp_path, monkeypatch):
    """orders + a store on a tmp database, with the panel stubbed out.

    Exposes `.placed`, the list of (link, quantity, service) that would have
    gone to BulkFollows, and `.result`, what the fake panel answers."""
    monkeypatch.setattr(config, "NR_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "NR_EMOJI_SERVICES", [
        {"name": "heart", "emoji": "❤️", "service": "5108"},
        {"name": "like", "emoji": "👍", "service": "5110"},
        {"name": "grinning", "emoji": "😃", "service": "5699"},
        {"name": "positive", "emoji": "positive", "service": "9271"},
    ])
    from modules.newsroom import store as store_mod
    importlib.reload(store_mod)
    store_mod.init()

    from modules.newsroom import orders as orders_mod
    importlib.reload(orders_mod)

    placed = []

    def fake_place(link, quantity, service):
        placed.append((link, quantity, service))
        return fake_place.result

    fake_place.result = {"order": 12345}
    monkeypatch.setattr(orders_mod.smm, "place_order", fake_place)

    orders_mod.placed = placed
    orders_mod.panel = fake_place
    orders_mod.store_mod = store_mod
    return orders_mod


def _site(**over):
    s = {"name": "acme", "chat_id": "@acme",
         "views_phase1": [500, 5000], "service_views": "V1", "service_bonus": "B1",
         "emoji_pool": ["heart", "like"], "emoji_count": [1, 2],
         "emoji_quantity": [10, 40]}
    s.update(over)
    return s


LINK = "https://t.me/acme/412"


# --------------------------------------------------------------------------- #
# Quantities                                                                  #
# --------------------------------------------------------------------------- #


def test_post_quantity_is_inside_the_sites_range(orders):
    site = _site(views_phase1=[500, 5000])

    assert all(500 <= orders.post_quantity(site) <= 5000 for _ in range(100))


def test_each_site_gets_its_own_range(orders):
    assert all(100 <= orders.post_quantity(_site(views_phase1=[100, 200])) <= 200
               for _ in range(50))


def test_reversed_range_is_tolerated(orders):
    assert all(100 <= orders.post_quantity(_site(views_phase1=[200, 100])) <= 200
               for _ in range(50))


def test_malformed_range_falls_back_instead_of_raising(orders):
    # A typo in one site file must cost a sensible default, not the order.
    assert 500 <= orders.post_quantity(_site(views_phase1="oops")) <= 5000
    assert 500 <= orders.post_quantity(_site(views_phase1=[])) <= 5000
    assert 500 <= orders.post_quantity(_site(views_phase1=None)) <= 5000


# --------------------------------------------------------------------------- #
# Emoji selection                                                             #
# --------------------------------------------------------------------------- #


def test_random_emojis_never_leaves_the_sites_pool(orders):
    # The catalogue contains shit/clown/throw up. Drawing one of those onto a
    # client's news post because it was in the global list is not recoverable.
    site = _site(emoji_pool=["heart", "like"], emoji_count=[2, 2])

    for _ in range(50):
        assert {e["name"] for e in orders.random_emojis(site)} <= {"heart", "like"}


def test_random_emojis_respects_the_count_range(orders):
    site = _site(emoji_pool=["heart", "like", "grinning", "positive"], emoji_count=[2, 3])

    for _ in range(50):
        assert 2 <= len(orders.random_emojis(site)) <= 3


def test_count_is_capped_at_the_pool_size(orders):
    site = _site(emoji_pool=["heart"], emoji_count=[3, 5])

    assert len(orders.random_emojis(site)) == 1


def test_no_duplicates_in_one_draw(orders):
    site = _site(emoji_pool=["heart", "like", "grinning"], emoji_count=[3, 3])

    names = [e["name"] for e in orders.random_emojis(site)]
    assert len(names) == len(set(names))


def test_empty_pool_yields_no_reactions(orders):
    assert orders.random_emojis(_site(emoji_pool=[])) == []
    assert orders.random_emojis(_site(emoji_pool=None)) == []


def test_unknown_emoji_name_is_dropped_not_fatal(orders):
    site = _site(emoji_pool=["heart", "nonsense"], emoji_count=[2, 2])

    names = {e["name"] for e in orders.random_emojis(site)}
    assert names == {"heart"}


# --------------------------------------------------------------------------- #
# channel_link                                                                #
# --------------------------------------------------------------------------- #


def test_channel_link_from_a_post_link(orders):
    assert orders.channel_link("@acme", LINK) == "https://t.me/acme"


def test_channel_link_from_a_username_when_no_post_link(orders):
    assert orders.channel_link("@acme", None) == "https://t.me/acme"


def test_channel_link_is_empty_for_a_bare_numeric_id(orders):
    # Nothing usable can be derived; the caller must skip the order.
    assert orders.channel_link("-1001234567890", None) == ""


def test_private_channel_post_link_still_yields_a_channel_url(orders):
    got = orders.channel_link("-1001234567890", "https://t.me/c/1234567890/9")

    assert got == "https://t.me/c/1234567890"


# --------------------------------------------------------------------------- #
# Orders                                                                      #
# --------------------------------------------------------------------------- #


def test_views_order_uses_the_sites_service_id(orders):
    # The silent failure this prevents: one site's views billed against
    # another site's service.
    orders.order_post(_site(service_views="SITE_A"), post_id=1, link=LINK)

    (link, qty, service) = orders.placed[0]
    assert service == "SITE_A"
    assert link == LINK


def test_bonus_order_uses_the_sites_bonus_service_and_channel_link(orders):
    orders.order_channel_threshold(_site(service_bonus="BONUS_A"), post_id=1,
                                   post_link=LINK)

    (link, qty, service) = orders.placed[0]
    assert service == "BONUS_A"
    assert qty == orders.THRESHOLD_QUANTITY
    assert link == "https://t.me/acme"  # channel, not the post


def test_bonus_order_skipped_when_no_channel_url_can_be_derived(orders):
    orders.order_channel_threshold(_site(chat_id="-1001234567890"), post_id=1,
                                   post_link=None)

    assert orders.placed == []


def test_emoji_order_uses_the_emojis_own_service(orders):
    heart = next(e for e in orders.EMOJI_SERVICES if e["name"] == "heart")

    orders.order_emoji(_site(), post_id=1, link=LINK, emoji=heart)

    (link, qty, service) = orders.placed[0]
    assert service == heart["service"]
    assert 10 <= qty <= 40


# --------------------------------------------------------------------------- #
# after_publish: the every-5th rule                                           #
# --------------------------------------------------------------------------- #


def _publish_n(orders, site, n):
    for _ in range(n):
        orders.after_publish(site, post_id=1, link=LINK)


def test_every_post_gets_a_views_order(orders):
    _publish_n(orders, _site(), 3)

    assert [p[2] for p in orders.placed] == ["V1", "V1", "V1"]


def test_bonus_fires_on_the_fifth_post_not_the_fourth(orders):
    site = _site()

    _publish_n(orders, site, 4)
    assert "B1" not in [p[2] for p in orders.placed]

    orders.after_publish(site, post_id=1, link=LINK)
    assert "B1" in [p[2] for p in orders.placed]


def test_bonus_fires_again_on_the_tenth(orders):
    _publish_n(orders, _site(), 10)

    assert [p[2] for p in orders.placed].count("B1") == 2


def test_channels_count_independently(orders):
    # A shared counter would fire this client's bonus order on that client's
    # channel — accepted by the panel, invisible in the log, paid for out of
    # the wrong balance.
    a, b = _site(chat_id="@acme", service_bonus="B_A"), _site(chat_id="@globex",
                                                             service_bonus="B_B")

    _publish_n(orders, a, 4)
    _publish_n(orders, b, 1)
    orders.after_publish(a, post_id=1, link=LINK)

    services = [p[2] for p in orders.placed]
    assert "B_A" in services
    assert "B_B" not in services


def test_counter_still_advances_without_a_link(orders):
    # Skipping the bump would put the channel permanently out of phase with
    # the every-5th rule.
    site = _site()

    for _ in range(4):
        orders.after_publish(site, post_id=1, link=None)
    orders.after_publish(site, post_id=1, link=LINK)

    assert "B1" in [p[2] for p in orders.placed]


def test_no_link_places_no_views_order(orders):
    orders.after_publish(_site(), post_id=1, link=None)

    assert orders.placed == []


# --------------------------------------------------------------------------- #
# Bookkeeping                                                                 #
# --------------------------------------------------------------------------- #


def test_every_order_is_recorded_in_the_store(orders):
    orders.order_post(_site(), post_id=7, link=LINK)

    assert orders.store_mod.failed_orders() == []  # succeeded, so nothing pending


def test_a_failed_panel_call_lands_in_the_replay_list(orders):
    orders.panel.result = None

    orders.order_post(_site(), post_id=7, link=LINK)

    (row,) = orders.store_mod.failed_orders()
    assert row["kind"] == "views"
    assert row["service"] == "V1"


def test_a_failed_order_does_not_stop_the_bonus_bookkeeping(orders):
    # The panel being down must not desynchronise the counter.
    orders.panel.result = None
    site = _site()

    _publish_n(orders, site, 5)

    assert [p[2] for p in orders.placed].count("B1") == 1


def test_reaction_orders_are_filed_under_their_emoji(orders):
    orders.panel.result = None

    orders.order_reactions(_site(emoji_pool=["heart"], emoji_count=[1, 1]),
                           post_id=7, link=LINK)

    (row,) = orders.store_mod.failed_orders()
    assert row["kind"] == "emoji:heart"


def test_order_reactions_returns_the_count(orders):
    n = orders.order_reactions(_site(emoji_pool=["heart", "like"], emoji_count=[2, 2]),
                               post_id=7, link=LINK)

    assert n == 2
    assert len(orders.placed) == 2


def test_order_reactions_without_a_link_places_nothing(orders):
    assert orders.order_reactions(_site(), post_id=7, link=None) == 0
    assert orders.placed == []
