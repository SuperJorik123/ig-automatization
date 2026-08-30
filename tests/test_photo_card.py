"""shared/photo_card.py — news-card renderer, offline (Pillow only — the
rembg subject cut-out is stubbed out so no model is needed)."""

import pytest
from PIL import Image, ImageDraw

from shared import photo_card


@pytest.fixture(autouse=True)
def _no_rembg(monkeypatch):
    monkeypatch.setattr(photo_card, "subject_cutout", lambda hero: None)


def test_subject_cutout_layer_is_drawn_over_the_insets(tmp_path, monkeypatch):
    hero = _photo(tmp_path / "hero.jpg", (1080, 1350), (0, 0, 0))
    red = _photo(tmp_path / "a.jpg", (400, 400), (255, 0, 0))
    # Fake matte: a solid green block exactly where the left inset sits.
    def fake_cutout(h):
        cut = Image.new("RGBA", h.size, (0, 0, 0, 0))
        ImageDraw.Draw(cut).rectangle((0, 0, 300, 400), fill=(0, 255, 0, 255))
        return cut
    monkeypatch.setattr(photo_card, "subject_cutout", fake_cutout)
    out = photo_card.render_card(hero, "x", _logo(tmp_path / "logo.png"),
                                 str(tmp_path / "c.jpg"), insets=[red])
    im = Image.open(out)
    cy = round(photo_card.INSET_CENTER_Y * im.height)
    r, g, b = im.getpixel((60, cy))[:3]
    assert g > 200 and r < 50, "subject must cover the inset"


def _photo(path, size=(800, 1200), color=(120, 120, 120)):
    im = Image.new("RGB", size, color)
    ImageDraw.Draw(im).ellipse((300, 300, 500, 500), fill=(230, 200, 180))
    im.save(path)
    return str(path)


def _logo(path):
    im = Image.new("RGBA", (400, 150), (0, 0, 0, 0))
    ImageDraw.Draw(im).rectangle((0, 0, 399, 149), fill=(255, 255, 255, 255))
    im.save(path)
    return str(path)


def test_render_card_default_size_with_two_insets(tmp_path):
    hero = _photo(tmp_path / "hero.jpg")
    a = _photo(tmp_path / "a.jpg", (500, 500), (200, 50, 50))
    b = _photo(tmp_path / "b.jpg", (640, 480), (50, 50, 200))
    out = photo_card.render_card(hero, "A headline that wraps onto several lines",
                                 _logo(tmp_path / "logo.png"),
                                 str(tmp_path / "card.jpg"), insets=[a, b])
    im = Image.open(out)
    assert im.size == photo_card.DEFAULT_SIZE
    # Bottom rows sit on the opaque scrim: near-black under the headline block
    # except where the white glyphs are.
    px = im.getpixel((5, im.height - 5))
    assert max(px) < 20


def test_render_card_square_no_insets(tmp_path):
    hero = _photo(tmp_path / "hero.jpg", (1600, 900))
    out = photo_card.render_card(hero, "Short", _logo(tmp_path / "logo.png"),
                                 str(tmp_path / "sq.jpg"), size=(1080, 1080))
    assert Image.open(out).size == (1080, 1080)


def test_inset_sits_inside_left_edge(tmp_path):
    hero = _photo(tmp_path / "hero.jpg", (1080, 1350), (0, 0, 0))
    red = _photo(tmp_path / "a.jpg", (400, 400), (255, 0, 0))
    out = photo_card.render_card(hero, "x", _logo(tmp_path / "logo.png"),
                                 str(tmp_path / "c.jpg"), insets=[red])
    im = Image.open(out)
    cy = round(photo_card.INSET_CENTER_Y * im.height)
    # Inside the circle: red photo. Far right at the same Y: untouched hero
    # (only ONE inset, so nothing mirrored on the right).
    assert im.getpixel((60, cy))[0] > 200
    assert max(im.getpixel((im.width - 60, cy))) < 30


def test_long_headline_shrinks_but_keeps_every_word(tmp_path):
    hero = _photo(tmp_path / "hero.jpg")
    words = ["word"] * 40
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    font, lines = photo_card._fit_font(draw, " ".join(words), photo_card.default_font(),
                                       972, 300, 110, 40)
    assert " ".join(lines).split() == words
    assert font.size <= 110
