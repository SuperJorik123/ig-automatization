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


# --- layouts ---------------------------------------------------------------

def _striped(path, size=(2000, 1000)):
    """Gray photo with a red stripe at 85..95 % of its width — a marker that
    only a crop window steered to the right-hand side can contain."""
    im = Image.new("RGB", size, (120, 120, 120))
    d = ImageDraw.Draw(im)
    d.rectangle((round(size[0] * 0.85), 0, round(size[0] * 0.95), size[1]),
                fill=(255, 0, 0))
    im.save(path)
    return str(path)


def _row_has_red(im, y):
    return any(im.getpixel((x, y))[0] > 180 and im.getpixel((x, y))[1] < 80
               for x in range(0, im.width, 4))


def test_split_layout_gives_each_photo_its_own_half(tmp_path):
    a = _photo(tmp_path / "a.jpg", (900, 1200), (255, 0, 0))
    b = _photo(tmp_path / "b.jpg", (900, 1200), (0, 0, 255))
    out = photo_card.render_card(a, "x", _logo(tmp_path / "logo.png"),
                                 str(tmp_path / "c.jpg"), insets=[b],
                                 layout="split")
    im = Image.open(out)
    y = 200
    assert im.getpixel((im.width // 4, y))[0] > 180        # left panel = a
    assert im.getpixel((im.width * 3 // 4, y))[2] > 180    # right panel = b


def test_split_layout_gives_three_photos_a_third_each(tmp_path):
    a = _photo(tmp_path / "a.jpg", (900, 1200), (255, 0, 0))
    b = _photo(tmp_path / "b.jpg", (900, 1200), (0, 255, 0))
    c = _photo(tmp_path / "c.jpg", (900, 1200), (0, 0, 255))
    out = photo_card.render_card(a, "x", _logo(tmp_path / "logo.png"),
                                 str(tmp_path / "d.jpg"), insets=[b, c],
                                 layout="split")
    im = Image.open(out)
    y = 200
    assert im.getpixel((im.width // 6, y))[0] > 180
    assert im.getpixel((im.width // 2, y))[1] > 180
    assert im.getpixel((im.width * 5 // 6, y))[2] > 180


def test_split_panels_leave_no_seam_at_the_edges(tmp_path):
    a = _photo(tmp_path / "a.jpg", (900, 1200), (255, 0, 0))
    b = _photo(tmp_path / "b.jpg", (900, 1200), (0, 0, 255))
    c = _photo(tmp_path / "c.jpg", (900, 1200), (0, 255, 0))
    out = photo_card.render_card(a, "x", _logo(tmp_path / "logo.png"),
                                 str(tmp_path / "d.jpg"), insets=[b, c],
                                 layout="split")
    im = Image.open(out)
    y = 200
    for x in range(im.width):
        assert max(im.getpixel((x, y))) > 180, f"gap at x={x}"


def test_split_panel_crop_follows_the_subject(tmp_path, monkeypatch):
    hero = _striped(tmp_path / "hero.jpg")
    other = _photo(tmp_path / "b.jpg", (900, 1200), (0, 0, 255))
    logo = _logo(tmp_path / "logo.png")

    def render(fx, name):
        monkeypatch.setattr(photo_card, "subject_focus", lambda p: (fx, 0.5))
        return Image.open(photo_card.render_card(
            hero, "x", logo, str(tmp_path / name), insets=[other],
            layout="split"))

    assert not _row_has_red(render(0.5, "centre.jpg"), 200)
    assert _row_has_red(render(0.9, "subject.jpg"), 200)


def test_solo_layout_draws_no_inset_circle(tmp_path):
    hero = _photo(tmp_path / "hero.jpg", (1080, 1350), (0, 0, 0))
    red = _photo(tmp_path / "a.jpg", (400, 400), (255, 0, 0))
    out = photo_card.render_card(hero, "x", _logo(tmp_path / "logo.png"),
                                 str(tmp_path / "c.jpg"), insets=[red],
                                 layout="solo")
    im = Image.open(out)
    cy = round(photo_card.INSET_CENTER_Y * im.height)
    assert max(im.getpixel((60, cy))) < 30, "solo must ignore the insets"


def test_unknown_layout_raises(tmp_path):
    hero = _photo(tmp_path / "hero.jpg")
    with pytest.raises(ValueError):
        photo_card.render_card(hero, "x", _logo(tmp_path / "logo.png"),
                               str(tmp_path / "c.jpg"), layout="mosaic")


# --- subject_focus ---------------------------------------------------------

def test_subject_focus_falls_back_to_the_centre_without_a_matte(tmp_path):
    path = _photo(tmp_path / "hero.jpg")
    assert photo_card.subject_focus(path) == (0.5, photo_card.EYELINE)


def test_subject_focus_is_the_mattes_centroid(tmp_path, monkeypatch):
    path = _photo(tmp_path / "hero.jpg", (800, 800))

    def fake_cutout(img):
        # Right quarter of whatever size the matte runs at -> centroid 0.875.
        w, h = img.size
        cut = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(cut).rectangle((round(w * 0.75), 0, w - 1, h - 1),
                                      fill=(9, 9, 9, 255))
        return cut

    monkeypatch.setattr(photo_card, "subject_cutout", fake_cutout)
    fx, fy = photo_card.subject_focus(path)
    assert fx == pytest.approx(0.875, abs=0.01)
    assert fy == pytest.approx(0.5, abs=0.01)


def test_subject_focus_runs_the_matte_once_per_photo(tmp_path, monkeypatch):
    path = _photo(tmp_path / "hero.jpg", (400, 400))
    calls = []

    def fake_cutout(img):
        calls.append(1)
        return None

    monkeypatch.setattr(photo_card, "subject_cutout", fake_cutout)
    photo_card.subject_focus(path)
    photo_card.subject_focus(path)
    assert len(calls) == 1


def test_subject_focus_mattes_a_thumbnail_not_the_full_photo(tmp_path, monkeypatch):
    path = _photo(tmp_path / "hero.jpg", (3000, 2000))
    seen = []

    def fake_cutout(img):
        seen.append(img.size)
        return None

    monkeypatch.setattr(photo_card, "subject_cutout", fake_cutout)
    photo_card.subject_focus(path)
    assert max(seen[0]) <= photo_card.FOCUS_MATTE_PX
