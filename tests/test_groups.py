"""modules/telegram/groups.py — the GMN / JNN account grouping both pickers
collapse behind."""

from modules.telegram import groups


def _brand(name, group="", tg="", yt="", tw="", has_logo=True):
    return {"name": name, "lang": "en", "group": group, "tg": tg, "yt": yt,
            "tw": tw, "logo": f"/nonexistent/{name}/logo.png",
            "has_logo": has_logo}


def _dest(chat_id, lang="en"):
    return {"chat_id": chat_id, "lang": lang, "regions": set()}


# --- brand_groups ----------------------------------------------------------

def test_brand_groups_collects_members_by_group_name():
    brands = [_brand("altenews", "JNN"), _brand("mirnews", "GMN"),
              _brand("wsmirror", "GMN")]
    assert groups.brand_groups(brands) == [
        {"name": "GMN", "members": {1, 2}},
        {"name": "JNN", "members": {0}},
    ]


def test_brand_groups_are_alphabetical():
    brands = [_brand("a", "ZZZ"), _brand("b", "AAA")]
    assert [g["name"] for g in groups.brand_groups(brands)] == ["AAA", "ZZZ"]


def test_brand_groups_ignores_ungrouped_brands():
    brands = [_brand("a", "GMN"), _brand("b", ""), _brand("c")]
    assert groups.brand_groups(brands) == [{"name": "GMN", "members": {0}}]


def test_brand_groups_skips_brands_without_a_logo():
    # A brand that can't render must not be dragged in by a group tick — the
    # picker shows it as a disabled 🚫 row under Custom.
    brands = [_brand("a", "GMN"), _brand("b", "GMN", has_logo=False)]
    assert groups.brand_groups(brands) == [{"name": "GMN", "members": {0}}]


def test_brand_groups_drops_a_group_left_with_no_usable_member():
    brands = [_brand("a", "GMN", has_logo=False), _brand("b", "JNN")]
    assert [g["name"] for g in groups.brand_groups(brands)] == ["JNN"]


def test_brand_groups_empty_without_any_group_field():
    assert groups.brand_groups([_brand("a"), _brand("b")]) == []


# --- dest_groups -----------------------------------------------------------

def test_dest_groups_matches_destinations_to_the_owning_brand():
    brands = [_brand("altenews", "JNN", tg="@AlteNewsMedia", yt="altenewsmedia"),
              _brand("mirnews", "GMN", tg="@MirNews")]
    out = groups.dest_groups(
        brands,
        tg=[_dest("@MirNews"), _dest("@AlteNewsMedia")],
        yt=[_dest("altenewsmedia")],
    )
    assert out == [
        {"name": "GMN", "tg": {0}, "yt": set(), "tw": set()},
        {"name": "JNN", "tg": {1}, "yt": {0}, "tw": set()},
    ]


def test_dest_groups_matching_ignores_case_and_the_at_sign():
    brands = [_brand("wswire", "JNN", tg="wswire", tw="WsWire")]
    out = groups.dest_groups(brands, tg=[_dest("@WSWIRE")], tw=[_dest("wswire")])
    assert out == [{"name": "JNN", "tg": {0}, "yt": set(), "tw": {0}}]


def test_dest_groups_leaves_unmatched_destinations_out():
    # A channel no brand claims is reachable only through Custom (and "All").
    brands = [_brand("mirnews", "GMN", tg="@MirNews")]
    out = groups.dest_groups(brands, tg=[_dest("@MirNews"), _dest("@Stranger")])
    assert out == [{"name": "GMN", "tg": {0}, "yt": set(), "tw": set()}]


def test_dest_groups_drops_a_group_with_no_destinations_at_all():
    # GMN's brands exist but none has an account configured yet — a row that
    # would select nothing is a trap, so it isn't offered.
    brands = [_brand("mirnews", "GMN"), _brand("wswire", "JNN", tg="@WsWire")]
    out = groups.dest_groups(brands, tg=[_dest("@WsWire")])
    assert [g["name"] for g in out] == ["JNN"]


def test_dest_groups_ignores_a_brand_with_a_blank_handle():
    # An empty tg field must not match an empty-ish destination id.
    brands = [_brand("mirnews", "GMN", tg="")]
    assert groups.dest_groups(brands, tg=[_dest("")]) == []


def test_dest_groups_counts_a_logoless_brands_channels():
    # Unlike rendering, publishing as-is needs no logo.
    brands = [_brand("mirnews", "GMN", tg="@MirNews", has_logo=False)]
    out = groups.dest_groups(brands, tg=[_dest("@MirNews")])
    assert out == [{"name": "GMN", "tg": {0}, "yt": set(), "tw": set()}]


# --- mark ------------------------------------------------------------------

def test_mark_reports_all_some_none():
    assert groups.mark({1, 2}, {1, 2}) == "☑"
    assert groups.mark({1}, {1, 2}) == "◪"
    assert groups.mark({3}, {1, 2}) == "☐"
    assert groups.mark(set(), set()) == "☐"


def test_mark_ignores_selection_outside_the_group():
    assert groups.mark({1, 2, 9}, {1, 2}) == "☑"


# --- dest_members / count --------------------------------------------------

def test_dest_count_totals_every_platform():
    g = {"name": "GMN", "tg": {0, 1}, "yt": {0}, "tw": set()}
    assert groups.dest_count(g) == 3


# --- dest_mark / toggle_dest_group -----------------------------------------

_G = {"name": "JNN", "tg": {0, 2}, "yt": {1}, "tw": set()}


def test_dest_mark_spans_every_platform():
    assert groups.dest_mark(_G, {"tg": {0, 2}, "yt": {1}}) == "☑"
    assert groups.dest_mark(_G, {"tg": {0, 2}}) == "◪"
    assert groups.dest_mark(_G, {"tg": set(), "yt": set()}) == "☐"


def test_dest_mark_ignores_selection_outside_the_group():
    assert groups.dest_mark(_G, {"tg": {0, 2, 9}, "yt": {1}, "tw": {5}}) == "☑"


def test_toggle_dest_group_fills_a_partial_group_across_platforms():
    out = groups.toggle_dest_group(_G, {"tg": {0}, "yt": set(), "tw": set()})
    assert out == {"tg": {0, 2}, "yt": {1}, "tw": set()}


def test_toggle_dest_group_clears_a_full_one_and_nothing_else():
    sel = {"tg": {0, 2, 7}, "yt": {1}, "tw": {3}}
    out = groups.toggle_dest_group(_G, sel)
    assert out == {"tg": {7}, "yt": set(), "tw": {3}}


def test_toggle_dest_group_does_not_mutate_the_selection_it_was_given():
    sel = {"tg": {0}, "yt": set(), "tw": set()}
    groups.toggle_dest_group(_G, sel)
    assert sel == {"tg": {0}, "yt": set(), "tw": set()}
