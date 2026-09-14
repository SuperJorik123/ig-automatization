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


def test_gate_keyboard_has_both_choices():
    data = [b.callback_data for b in _buttons(branded.gate_keyboard())]
    assert data == ["b:asis", "b:brand"]


def test_brand_keyboard_marks_selection_and_disables_missing_logo():
    brands = [dict(_brand("a"), has_logo=True), dict(_brand("b"), has_logo=False)]
    btns = _buttons(branded.brand_keyboard(brands, {0}))
    assert btns[0].text.startswith("☑") and btns[0].callback_data == "b:t:0"
    assert btns[1].callback_data == "b:noop" and "no logo" in btns[1].text
    assert [b.callback_data for b in btns[-2:]] == ["b:render", "b:cancel"]


def test_brand_keyboard_collapses_to_group_rows():
    brands = [dict(_brand("a", group="GMN"), has_logo=True),
              dict(_brand("b", group="JNN"), has_logo=True),
              dict(_brand("c", group="JNN"), has_logo=True)]
    btns = _buttons(branded.brand_keyboard(brands, {1, 2}))
    assert [b.text for b in btns[:3]] == ["☐ GMN · 1 brand", "☑ JNN · 2 brands",
                                          "⚙ Custom…"]
    assert [b.callback_data for b in btns[:3]] == ["b:g:0", "b:g:1", "b:custom"]
    assert [b.callback_data for b in btns[-2:]] == ["b:render", "b:cancel"]


def test_brand_keyboard_group_row_shows_a_partial_tick():
    brands = [dict(_brand("a", group="JNN"), has_logo=True),
              dict(_brand("b", group="JNN"), has_logo=True)]
    btns = _buttons(branded.brand_keyboard(brands, {0}))
    assert btns[0].text.startswith("◪")


def test_brand_keyboard_custom_lists_every_brand_and_offers_the_way_back():
    brands = [dict(_brand("a", group="GMN"), has_logo=True),
              dict(_brand("b"), has_logo=True)]
    btns = _buttons(branded.brand_keyboard(brands, {0}, custom=True))
    assert [b.callback_data for b in btns[:3]] == ["b:t:0", "b:t:1", "b:groups"]


def test_brand_keyboard_without_groups_stays_the_full_list():
    # No "group" anywhere: nothing to collapse, and no dead "⬅ Groups" row.
    brands = [dict(_brand("a"), has_logo=True), dict(_brand("b"), has_logo=True)]
    data = [b.callback_data for b in _buttons(branded.brand_keyboard(brands, set()))]
    assert data == ["b:t:0", "b:t:1", "b:render", "b:cancel"]


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


def test_platform_keyboard_shows_brand_count():
    a, b = _render(_brand("a", tg="@a", yt="a")), _render(_brand("b", tg="@b", yt=""))
    btns = _buttons(branded.platform_keyboard(branded.platforms_for([a, b], 60), {0}))
    assert btns[0].text == "☑ TG · 2 brands"
    assert btns[1].text == "☐ YT · 1 brand"


# --- photo cards (Create post) ---------------------------------------------

def test_platforms_hide_youtube_for_photo_cards():
    r = dict(_render(_brand(tg="@mir", yt="mir", tw="mir")), kind="photo")
    assert [p["platform"] for p in branded.platforms_for([r], 0)] == ["tg", "tw"]


def test_card_gate_keyboard_has_both_choices():
    data = [b.callback_data for b in _buttons(branded.card_gate_keyboard())]
    assert data == ["b:asis", "b:card"]


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


# --- operator replies: headline vs caption ---------------------------------
#
# A reply to an open picker used to mean one thing: replace the headline. The
# operator can now hand the bot the Instagram caption itself, which is the one
# input the pipeline could never produce for them.

def test_a_plain_reply_is_still_the_headline():
    assert branded.parse_reply("Man walks past a bear") == \
        ("text", "Man walks past a bear")


def test_a_caption_prefix_sets_the_caption():
    field, value = branded.parse_reply("caption: An elderly man was walking")
    assert field == "caption"
    assert value == "An elderly man was walking"


def test_the_prefix_is_case_and_space_insensitive():
    for raw in ("Caption: x", "CAPTION:x", "caption :  x", "  caption:   x  "):
        assert branded.parse_reply(raw) == ("caption", "x"), raw


def test_a_caption_keeps_its_own_line_breaks():
    """A caption is paragraphs — only the prefix is stripped, never the shape."""
    body = "First paragraph.\n\nSecond paragraph.\n\n#bear #usa"
    assert branded.parse_reply("caption:\n" + body) == ("caption", body)


def test_an_empty_caption_reply_clears_the_override():
    """`caption:` alone is how the operator takes their text back off and
    lets the pipeline write one again."""
    assert branded.parse_reply("caption:") == ("caption", "")
    assert branded.parse_reply("caption:   ") == ("caption", "")


def test_prose_merely_containing_the_word_is_a_headline():
    assert branded.parse_reply("Caption contest winner announced")[0] == "text"


# --- the footage lines on the picker ---------------------------------------

def _footage(ok=True, **kw):
    out = {"summary": "A man walks past a bear.", "beats": [], "audible": "",
           "setting": "", "headline_ok": ok, "headline_note": "",
           "headline_suggestion": ""}
    out.update(kw)
    return out


def test_no_footage_adds_no_lines():
    assert branded.footage_lines({}) == []


def test_a_matching_headline_shows_only_what_was_seen():
    lines = branded.footage_lines(_footage())
    assert len(lines) == 1
    assert lines[0].startswith("👁")
    assert "A man walks past a bear." in lines[0]


def test_a_mismatched_headline_warns_and_suggests():
    lines = branded.footage_lines(_footage(
        ok=False, headline_note="the man in the clip is not the one named",
        headline_suggestion="Elderly man doesn't notice a bear beside him"))
    assert any(l.startswith("⚠️") for l in lines)
    joined = "\n".join(lines)
    assert "not the one named" in joined
    assert "Elderly man doesn't notice a bear beside him" in joined


def test_a_mismatch_without_a_suggestion_still_warns():
    lines = branded.footage_lines(_footage(ok=False, headline_note="unrelated"))
    assert any(l.startswith("⚠️") for l in lines)


def test_a_long_summary_is_trimmed_for_the_picker():
    """Telegram caps a message at 4096 and the picker already shows the
    headline — the analysis is an orientation line, not the report."""
    lines = branded.footage_lines(_footage(summary="word " * 400))
    assert len(lines[0]) < 400
