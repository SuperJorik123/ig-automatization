"""modules/telegram/branded.py — pure picker logic for the Brand-it flow."""

import pytest

from modules.telegram import branded


def _brand(name="mir", lang="en", tg="@mir", yt="mir", tw=""):
    return {"name": name, "lang": lang, "tg": tg, "yt": yt, "tw": tw,
            "logo": f"/nonexistent/{name}/logo.png"}


def _render(brand):
    return {"brand": brand, "path": f"/tmp/{brand['name']}.mp4", "headline": "h"}


# --- available_brands ------------------------------------------------------

def test_available_brands_flags_missing_logo(tmp_path):
    with_logo = dict(_brand("a"), logo=str(tmp_path / "logo.png"))
    (tmp_path / "logo.png").write_bytes(b"png")
    out = branded.available_brands([with_logo, _brand("b")])
    assert out[0]["has_logo"] is True
    assert out[1]["has_logo"] is False


# --- pairs_for -------------------------------------------------------------

def test_pairs_only_for_configured_platforms():
    pairs = branded.pairs_for([_render(_brand(tg="@mir", yt="", tw="mir"))], 60)
    assert [p["platform"] for p in pairs] == ["tg", "tw"]
    assert pairs[0]["label"] == "mir → TG"
    assert pairs[1]["label"] == "mir → X"


def test_pairs_hide_youtube_beyond_shorts_cap():
    renders = [_render(_brand(tg="@mir", yt="mir"))]
    assert [p["platform"] for p in branded.pairs_for(renders, 60)] == ["tg", "yt"]
    assert [p["platform"] for p in branded.pairs_for(renders, 181)] == ["tg"]


def test_pairs_carry_their_render():
    r = _render(_brand())
    assert all(p["render"] is r for p in branded.pairs_for([r], 60))


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


def test_publish_keyboard_rows_and_defaults():
    pairs = branded.pairs_for([_render(_brand(tg="@mir", yt="mir"))], 60)
    btns = _buttons(branded.publish_keyboard(pairs, set()))
    assert [b.callback_data for b in btns[:-2]] == ["b:p:0", "b:p:1"]
    assert all(b.text.startswith("☐") for b in btns[:-2])   # all OFF by default
    assert [b.callback_data for b in btns[-2:]] == ["b:publish", "b:cancel"]
