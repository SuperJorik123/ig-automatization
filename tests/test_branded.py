"""modules/telegram/branded.py — pure picker logic for the Brand-it flow."""

import pytest

from modules.telegram import branded


def _brand(name="mir", lang="en", tg="@mir", yt="mir", tw="", group=""):
    return {"name": name, "lang": lang, "group": group, "tg": tg, "yt": yt,
            "tw": tw, "logo": f"/nonexistent/{name}/logo.png"}


def _render(brand):
    return {"brand": brand, "path": f"/tmp/{brand['name']}.mp4", "headline": "h"}


# --- available_brands ------------------------------------------------------

def test_available_brands_flags_missing_logo(tmp_path):
    with_logo = dict(_brand("a"), logo=str(tmp_path / "logo.png"))
    (tmp_path / "logo.png").write_bytes(b"png")
    out = branded.available_brands([with_logo, _brand("b")])
    assert out[0]["has_logo"] is True
    assert out[1]["has_logo"] is False


# --- platforms_for ---------------------------------------------------------

def test_platforms_only_those_a_brand_is_configured_for():
    plats = branded.platforms_for([_render(_brand(tg="@mir", yt="", tw="mir"))], 60)
    assert [p["platform"] for p in plats] == ["tg", "tw"]
    assert [p["label"] for p in plats] == ["TG", "X"]


def test_platform_carries_only_the_brands_it_covers():
    a = _render(_brand("a", tg="@a", yt="a"))
    b = _render(_brand("b", tg="@b", yt=""))
    plats = branded.platforms_for([a, b], 60)
    by_key = {p["platform"]: p for p in plats}
    assert [r["brand"]["name"] for r in by_key["tg"]["renders"]] == ["a", "b"]
    assert [r["brand"]["name"] for r in by_key["yt"]["renders"]] == ["a"]


def test_platforms_hide_youtube_beyond_shorts_cap():
    renders = [_render(_brand(tg="@mir", yt="mir"))]
    assert [p["platform"] for p in branded.platforms_for(renders, 60)] == ["tg", "yt"]
    assert [p["platform"] for p in branded.platforms_for(renders, 181)] == ["tg"]


def test_platform_drops_out_when_no_brand_has_it():
    # Only brand has no X account -> no X row at all, so every ticked row
    # is guaranteed to publish something.
    plats = branded.platforms_for([_render(_brand(tw=""))], 60)
    assert "tw" not in [p["platform"] for p in plats]


# --- expand ----------------------------------------------------------------

def test_expand_crosses_selected_platforms_with_their_brands():
    a, b = _render(_brand("a", tg="@a", yt="a")), _render(_brand("b", tg="@b", yt=""))
    plats = branded.platforms_for([a, b], 60)
    pairs = branded.expand(plats, {0, 1})          # TG + YT
    assert [(p["render"]["brand"]["name"], p["platform"]) for p in pairs] == [
        ("a", "tg"), ("b", "tg"), ("a", "yt")]
    assert pairs[0]["label"] == "a → TG"
    assert pairs[-1]["label"] == "a → YT"


def test_expand_of_nothing_is_empty():
    plats = branded.platforms_for([_render(_brand())], 60)
    assert branded.expand(plats, set()) == []


def test_expanded_pairs_carry_their_render():
    r = _render(_brand())
    plats = branded.platforms_for([r], 60)
    pairs = branded.expand(plats, set(range(len(plats))))
    assert all(p["render"] is r for p in pairs)


# --- keyboards -------------------------------------------------------------

def _buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


def _editor(brands, selected, custom=False, layouts=(), layout=None,
            available=(), platforms=()):
    return branded.plan_edit_keyboard(brands, selected, custom, list(layouts),
                                      layout, list(available), set(platforms))


def test_plan_keyboard_is_go_change_asis_cancel():
    data = [b.callback_data for b in _buttons(branded.plan_keyboard())]
    assert data == ["b:go", "b:edit", "b:asis", "b:cancel"]


def test_editor_marks_selection_and_disables_missing_logo():
    brands = [dict(_brand("a"), has_logo=True), dict(_brand("b"), has_logo=False)]
    btns = _buttons(_editor(brands, {0}))
    assert btns[0].text.startswith("☑") and btns[0].callback_data == "b:t:0"
    assert btns[1].callback_data == "b:noop" and "no logo" in btns[1].text
    assert btns[-1].callback_data == "b:done"


def test_editor_brands_collapse_to_group_rows():
    brands = [dict(_brand("a", group="GMN"), has_logo=True),
              dict(_brand("b", group="JNN"), has_logo=True),
              dict(_brand("c", group="JNN"), has_logo=True)]
    btns = _buttons(_editor(brands, {1, 2}))
    assert [b.text for b in btns[:3]] == ["☐ GMN · 1 brand", "☑ JNN · 2 brands",
                                          "⚙ Custom…"]
    assert [b.callback_data for b in btns[:3]] == ["b:g:0", "b:g:1", "b:custom"]


def test_editor_group_row_shows_a_partial_tick():
    brands = [dict(_brand("a", group="JNN"), has_logo=True),
              dict(_brand("b", group="JNN"), has_logo=True)]
    assert _buttons(_editor(brands, {0}))[0].text.startswith("◪")


def test_editor_custom_lists_every_brand_and_offers_the_way_back():
    brands = [dict(_brand("a", group="GMN"), has_logo=True),
              dict(_brand("b"), has_logo=True)]
    btns = _buttons(_editor(brands, {0}, custom=True))
    assert [b.callback_data for b in btns[:3]] == ["b:t:0", "b:t:1", "b:groups"]


def test_editor_without_groups_stays_the_full_list():
    # No "group" anywhere: nothing to collapse, and no dead "⬅ Groups" row.
    brands = [dict(_brand("a"), has_logo=True), dict(_brand("b"), has_logo=True)]
    data = [b.callback_data for b in _buttons(_editor(brands, set()))]
    assert data == ["b:t:0", "b:t:1", "b:done"]


def test_editor_offers_designs_only_when_there_is_a_choice():
    brands = [dict(_brand("a"), has_logo=True)]
    one = [b.callback_data for b in _buttons(
        _editor(brands, {0}, layouts=branded.card_layouts(1), layout="solo"))]
    assert not any(d.startswith("b:lay:") for d in one)
    btns = _buttons(_editor(brands, {0}, layouts=branded.card_layouts(3),
                            layout="split"))
    lay = [b for b in btns if b.callback_data.startswith("b:lay:")]
    assert [b.callback_data for b in lay] == ["b:lay:insets", "b:lay:split",
                                              "b:lay:solo"]
    assert [b.text.startswith("● ") for b in lay] == [False, True, False]


def test_editor_platform_row_ticks_the_plan():
    brands = [dict(_brand("a"), has_logo=True)]
    btns = _buttons(_editor(brands, {0}, available=["tg", "ig"],
                            platforms={"ig"}))
    pk = [b for b in btns if b.callback_data.startswith("b:pk:")]
    assert [(b.text, b.callback_data) for b in pk] == [
        ("☐ TG", "b:pk:tg"), ("☑ IG", "b:pk:ig")]


def test_toggle_group_selects_then_clears_its_members():
    brands = [dict(_brand("a", group="GMN"), has_logo=True),
              dict(_brand("b", group="JNN"), has_logo=True)]
    sel = branded.toggle_brand_group(brands, {1}, 0)
    assert sel == {0, 1}
    assert branded.toggle_brand_group(brands, sel, 0) == {1}


def test_toggle_group_completes_a_partial_selection_before_clearing_it():
    brands = [dict(_brand("a", group="GMN"), has_logo=True),
              dict(_brand("b", group="GMN"), has_logo=True)]
    assert branded.toggle_brand_group(brands, {0}, 0) == {0, 1}


def test_toggle_group_leaves_brands_outside_it_alone():
    brands = [dict(_brand("a", group="GMN"), has_logo=True),
              dict(_brand("b"), has_logo=True)]
    assert branded.toggle_brand_group(brands, {1}, 0) == {0, 1}


def test_platform_keyboard_rows_and_defaults():
    plats = branded.platforms_for([_render(_brand(tg="@mir", yt="mir"))], 60)
    btns = _buttons(branded.platform_keyboard(plats, set()))
    assert [b.callback_data for b in btns[:-2]] == ["b:p:0", "b:p:1"]
    assert all(b.text.startswith("☐") for b in btns[:-2])   # all OFF by default
    assert [b.callback_data for b in btns[-2:]] == ["b:publish", "b:cancel"]


def test_publish_button_names_the_preticked_platforms():
    plats = branded.platforms_for([_render(_brand(tg="@mir", yt="mir"))], 60)
    btns = _buttons(branded.platform_keyboard(plats, {0, 1}))
    assert btns[-2].text == "🚀 Publish → TG · YT"
    assert btns[-2].callback_data == "b:publish"


def test_platform_keyboard_shows_brand_count():
    a, b = _render(_brand("a", tg="@a", yt="a")), _render(_brand("b", tg="@b", yt=""))
    btns = _buttons(branded.platform_keyboard(branded.platforms_for([a, b], 60), {0}))
    assert btns[0].text == "☑ TG · 2 brands"
    assert btns[1].text == "☐ YT · 1 brand"


# --- photo cards (Create post) ---------------------------------------------

def test_platforms_hide_youtube_for_photo_cards():
    r = dict(_render(_brand(tg="@mir", yt="mir", tw="mir")), kind="photo")
    assert [p["platform"] for p in branded.platforms_for([r], 0)] == ["tg", "tw"]


def test_card_layouts_one_for_a_single_photo():
    assert [lay for lay, _ in branded.card_layouts(1)] == ["solo"]


def test_card_layouts_every_design_for_several_photos():
    assert [lay for lay, _ in branded.card_layouts(3)] == ["insets", "split",
                                                          "solo"]


# --- Instagram (Graph API) -------------------------------------------------

def test_platforms_include_ig_for_video_and_photo_renders():
    brand = dict(_brand(tg="@mir", yt="mir", tw=""), ig="mirgram")
    video = _render(brand)
    photo = dict(_render(brand), kind="photo")
    assert [p["platform"] for p in branded.platforms_for([video], 60)] == ["tg", "yt", "ig"]
    assert [p["platform"] for p in branded.platforms_for([photo], 0)] == ["tg", "ig"]
    plats = branded.platforms_for([video], 60)
    assert branded.expand(plats, {2})[0]["label"] == "mir → IG"


def test_brand_without_ig_slot_has_no_ig_platform():
    plats = branded.platforms_for([_render(_brand())], 60)
    assert "ig" not in [p["platform"] for p in plats]


# --- Facebook (Pages API) --------------------------------------------------

def test_platforms_include_fb_for_video_and_photo_renders():
    """Like Instagram, Facebook takes both render kinds — a video goes out as
    a Reel, a card as a photo post — so neither the Shorts cap nor the
    photo-card rule applies to it."""
    brand = dict(_brand(tg="@mir", yt="mir", tw=""), fb="mirpage")
    video = _render(brand)
    photo = dict(_render(brand), kind="photo")
    assert [p["platform"] for p in branded.platforms_for([video], 60)] == \
        ["tg", "yt", "fb"]
    assert [p["platform"] for p in branded.platforms_for([photo], 0)] == \
        ["tg", "fb"]


def test_fb_survives_a_clip_past_the_shorts_cap_that_drops_yt():
    brand = dict(_brand(tg="@mir", yt="mir", tw=""), fb="mirpage")
    plats = branded.platforms_for([_render(brand)], 181)
    assert [p["platform"] for p in plats] == ["tg", "fb"]


def test_fb_pair_label_and_expand():
    brand = dict(_brand(tg="", yt="", tw=""), fb="mirpage")
    plats = branded.platforms_for([_render(brand)], 60)
    assert [p["platform"] for p in plats] == ["fb"]
    assert branded.expand(plats, {0})[0]["label"] == "mir → FB"


def test_brand_without_fb_slot_has_no_fb_platform():
    plats = branded.platforms_for([_render(_brand())], 60)
    assert "fb" not in [p["platform"] for p in plats]


def test_fb_row_appears_in_the_platform_keyboard():
    brand = dict(_brand(tg="@mir", yt="", tw=""), fb="mirpage")
    plats = branded.platforms_for([_render(brand)], 60)
    btns = _buttons(branded.platform_keyboard(plats, {1}))
    assert [b.text for b in btns[:2]] == ["☐ TG · 1 brand", "☑ FB · 1 brand"]
    assert [b.callback_data for b in btns[:2]] == ["b:p:0", "b:p:1"]


# --- operator replies: headline vs info ------------------------------------
#
# A reply to an open picker used to mean one thing: replace the headline. The
# operator can also hand the Instagram caption what they already know about
# the story — its source and facts — behind an `info:` prefix.

def test_a_plain_reply_is_still_the_headline():
    assert branded.parse_reply("Man walks past a bear") == \
        ("text", "Man walks past a bear")


def test_an_info_prefix_sets_the_info():
    field, value = branded.parse_reply("info: Filmed in Asheville, via WLOS")
    assert field == "info"
    assert value == "Filmed in Asheville, via WLOS"


def test_the_prefix_is_case_and_space_insensitive():
    for raw in ("Info: x", "INFO:x", "info :  x", "  info:   x  "):
        assert branded.parse_reply(raw) == ("info", "x"), raw


def test_info_keeps_its_own_line_breaks():
    body = "Source: WLOS.\n\nThe man is 81."
    assert branded.parse_reply("info:\n" + body) == ("info", body)


def test_an_empty_info_reply_clears_it():
    assert branded.parse_reply("info:") == ("info", "")
    assert branded.parse_reply("info:   ") == ("info", "")


def test_the_old_caption_prefix_is_just_a_headline_now():
    assert branded.parse_reply("caption: x")[0] == "text"


def test_prose_merely_containing_the_word_is_a_headline():
    assert branded.parse_reply("Info session announced for voters")[0] == "text"


# --- the footage lines on the picker ---------------------------------------

def _footage(**kw):
    out = {"summary": "A man walks past a bear.", "beats": [], "audible": "",
           "setting": ""}
    out.update(kw)
    return out


def test_no_footage_adds_no_lines():
    assert branded.footage_lines({}) == []


def test_the_picker_shows_only_what_was_seen():
    lines = branded.footage_lines(_footage())
    assert len(lines) == 1
    assert lines[0].startswith("👁")
    assert "A man walks past a bear." in lines[0]


def test_there_is_no_headline_warning_any_more():
    """An old analysis dict carrying a complaint must not bring the check back."""
    lines = branded.footage_lines(_footage(headline_ok=False,
                                           headline_note="unrelated"))
    assert len(lines) == 1 and "⚠️" not in lines[0]


def test_a_long_summary_is_trimmed_for_the_picker():
    """Telegram caps a message at 4096 and the picker already shows the
    headline — the analysis is an orientation line, not the report."""
    lines = branded.footage_lines(_footage(summary="word " * 400))
    assert len(lines[0]) < 400


# --- the plan card ----------------------------------------------------------

def _plan_brands():
    return [dict(_brand("a", group="GMN", tg="@a", yt="a", tw="a"), ig="a",
                 has_logo=True),
            dict(_brand("b", group="GMN", tg="@b", yt="", tw=""), ig="",
                 has_logo=True),
            dict(_brand("c", group="JNN", tg="@c", yt="", tw=""), ig="c",
                 has_logo=True),
            dict(_brand("d", group="JNN", tg="", yt="", tw=""), ig="",
                 has_logo=False)]


def test_plan_platforms_are_what_the_selected_brands_have():
    brands = _plan_brands()
    assert branded.plan_platform_keys(brands, {1}, "video", True) == ["tg"]
    assert branded.plan_platform_keys(brands, {0, 2}, "video", True) == [
        "tg", "yt", "tw", "ig"]


def test_plan_never_offers_youtube_for_cards_or_while_uploads_are_off():
    brands = _plan_brands()
    assert "yt" not in branded.plan_platform_keys(brands, {0}, "photo", True)
    assert "yt" not in branded.plan_platform_keys(brands, {0}, "video", False)


def test_first_plan_is_every_usable_brand_and_platform():
    plan = branded.default_plan(_plan_brands(), "video", 0, {}, False)
    assert plan["sel_brands"] == {0, 1, 2}          # d has no logo
    assert plan["platforms"] == {"tg", "tw", "ig"}
    assert plan["layout"] is None


def test_plan_reopens_on_the_remembered_choice():
    saved = {"brands": ["c"], "platforms": ["ig"], "layout": "split"}
    plan = branded.default_plan(_plan_brands(), "photo", 3, saved, True)
    assert plan == {"sel_brands": {2}, "layout": "split", "platforms": {"ig"}}


def test_a_memory_that_no_longer_resolves_falls_back_to_everything():
    saved = {"brands": ["renamed"], "platforms": ["fb"], "layout": "gone"}
    plan = branded.default_plan(_plan_brands(), "photo", 3, saved, True)
    assert plan["sel_brands"] == {0, 1, 2}
    assert plan["platforms"] == {"tg", "tw", "ig"}
    assert plan["layout"] == "insets"


def test_a_single_photo_always_opens_on_its_one_design():
    plan = branded.default_plan(_plan_brands(), "photo", 1,
                                {"layout": "split"}, True)
    assert plan["layout"] == "solo"


def test_plan_memory_is_by_name_in_picker_order():
    mem = branded.plan_memory(_plan_brands(), {2, 0}, None, {"ig", "tg"}, 0, {})
    assert mem == {"brands": ["a", "c"], "platforms": ["tg", "ig"]}


def test_a_single_photo_never_overwrites_the_remembered_design():
    mem = branded.plan_memory(_plan_brands(), {0}, "solo", ["tg"], 1,
                              {"layout": "split"})
    assert mem["layout"] == "split"
    mem = branded.plan_memory(_plan_brands(), {0}, "insets", ["tg"], 2, mem)
    assert mem["layout"] == "insets"


def test_brands_summary_names_whole_groups():
    brands = _plan_brands()
    assert branded.brands_summary(brands, {0, 1}) == "GMN (2)"
    assert branded.brands_summary(brands, {0, 1, 2}) == "GMN + JNN (3)"


def test_brands_summary_lists_a_short_hand_pick_and_counts_a_long_one():
    brands = _plan_brands()
    assert branded.brands_summary(brands, {0}) == "a"
    assert branded.brands_summary(brands, set()) == "none"
    many = [dict(_brand(f"x{i}"), has_logo=True) for i in range(5)]
    assert branded.brands_summary(many, {0, 1, 2, 3}) == "4 brands"


def test_plan_lines_spell_the_platforms_out():
    lines = branded.plan_lines(_plan_brands(), {0, 1}, ["tg", "tw", "ig"])
    assert lines == ["Brands: GMN (2)", "Platforms: Telegram · X · Instagram"]


def test_plan_lines_show_the_design_only_when_there_was_a_choice():
    assert "Design: ⚪ Circles" in branded.plan_lines(
        _plan_brands(), {0}, ["tg"], "insets", 3)
    assert not any(line.startswith("Design") for line in branded.plan_lines(
        _plan_brands(), {0}, ["tg"], "solo", 1))
