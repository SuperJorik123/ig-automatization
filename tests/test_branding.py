"""shared/branding.py — offline: wrapping, path escaping, filter-graph
construction. The one end-to-end render test invokes ffmpeg and skips when
it isn't on PATH."""

import os
import shutil
import subprocess

import pytest

from shared import branding


# --- wrap_headline ---------------------------------------------------------

def test_wrap_short_headline_single_line():
    assert branding.wrap_headline("Hello world") == "Hello world"


def test_wrap_collapses_whitespace_and_newlines():
    assert branding.wrap_headline("  a \n  b\t c  ") == "a b c"


def test_wrap_splits_on_word_boundaries():
    out = branding.wrap_headline("aaaa bbbb cccc dddd eeee ffff",
                                 width=10, max_lines=3)
    assert out.split("\n") == ["aaaa bbbb", "cccc dddd", "eeee ffff"]


def test_wrap_defaults_never_truncate():
    out = branding.wrap_headline("word " * 40)
    assert "…" not in out
    assert out.replace("\n", " ") == ("word " * 40).strip()


def test_wrap_truncates_beyond_max_lines_with_ellipsis():
    out = branding.wrap_headline("word " * 40, width=10, max_lines=3)
    lines = out.split("\n")
    assert len(lines) == 3
    assert lines[-1].endswith("…")
    assert all(len(ln) <= 10 for ln in lines)


def test_wrap_empty_returns_empty():
    assert branding.wrap_headline("") == ""


# --- ffmpeg path escaping --------------------------------------------------

def test_ff_path_escapes_windows_drive_colon_and_backslashes():
    assert branding._ff_path(r"C:\repo\assets\f.ttf") == "C\\:/repo/assets/f.ttf"


# --- filter graph ----------------------------------------------------------

def test_filter_graph_contains_fixed_design_constants():
    graph = branding._filter_graph("/f/font.ttf", "/t/text.txt")
    assert "scale=1080:1920:force_original_aspect_ratio=increase" in graph
    assert "gblur=sigma=30" in graph                       # blur-fill canvas
    assert "scale=205:-1[logo]" in graph                   # logo width
    # Top-right margins, matched to the reference render — not equal.
    assert "overlay=W-w-82:102" in graph
    assert "fontsize=49" in graph
    assert "boxcolor=black@0.55" in graph
    assert "boxborderw=12|7|12|7" in graph                 # top|right|bottom|left
    assert "setsar=1" in graph                             # clean 1:1 SAR out
    # Fixed banner: same left edge and same width for every headline.
    assert "x=110" not in graph                            # x is the TEXT, not the box
    assert "x=117" in graph
    assert "boxw=842" in graph                             # 856 box - 2x7 border
    assert "fade=t=out:st=10:d=1.5:alpha=1" in graph       # banner fade-out
    assert "y=h*0.775" in graph
    assert "textfile='/t/text.txt'" in graph               # never inline text
    assert "fontfile='/f/font.ttf'" in graph


def test_filter_graph_applies_style_colors_font_size():
    style = dict(branding.DEFAULT_STYLE, background="0xC90A0A",
                 background_alpha=1.0, text="0x122E44", font_size=60)
    graph = branding._filter_graph("/f/font.ttf", "/t/text.txt", style)
    assert "boxcolor=0xC90A0A@1.0" in graph
    assert "fontcolor=0x122E44" in graph
    assert "fontsize=60" in graph


# --- load_style ------------------------------------------------------------

def _style(tmp_path, payload):
    (tmp_path / branding.STYLE_FILE).write_text(payload, encoding="utf-8")
    return branding.load_style(str(tmp_path))


def test_load_style_missing_file_returns_defaults(tmp_path):
    assert branding.load_style(str(tmp_path)) == branding.DEFAULT_STYLE


def test_load_style_hex_colors_become_ffmpeg_colors(tmp_path):
    style = _style(tmp_path, '{"background": "#c90a0a", "text": "#122e44"}')
    assert style["background"] == "0xC90A0A"
    assert style["text"] == "0x122E44"


def test_load_style_explicit_background_is_opaque_by_default(tmp_path):
    assert _style(tmp_path, '{"background": "#ffffff"}')["background_alpha"] == 1.0
    # ...unless the brand asks for translucency
    style = _style(tmp_path, '{"background": "#ffffff", "background_alpha": 0.4}')
    assert style["background_alpha"] == 0.4


def test_load_style_keeps_defaults_for_unset_keys(tmp_path):
    style = _style(tmp_path, '{"background": "#000000"}')
    assert style["text"] == branding.DEFAULT_STYLE["text"]
    assert style["font"] == branding.FONT_PATH
    assert style["font_size"] == branding.FONT_SIZE


@pytest.mark.parametrize("payload", [
    '{"background": "#12345"}',        # not 6 hex digits
    '{"text": "#nothex"}',
    '{"font_size": 4}',                # out of range
    '{"background_alpha": 2}',
    '{"background": "#000000",',       # broken JSON
    '["not", "an", "object"]',
])
def test_load_style_rejects_bad_values(tmp_path, payload):
    with pytest.raises(ValueError):
        _style(tmp_path, payload)


def test_load_style_resolves_font_next_to_logo(tmp_path):
    (tmp_path / "brand.ttf").write_bytes(b"\x00")
    assert _style(tmp_path, '{"font": "brand.ttf"}')["font"] == str(
        tmp_path / "brand.ttf")


def test_load_style_unknown_font_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        _style(tmp_path, '{"font": "NoSuchFontAnywhere"}')


def test_shipped_brand_styles_are_valid():
    root = os.path.dirname(os.path.dirname(os.path.abspath(branding.__file__)))
    brands = os.path.join(root, "brands")
    for name in sorted(os.listdir(brands)):
        folder = os.path.join(brands, name)
        if os.path.isfile(os.path.join(folder, branding.STYLE_FILE)):
            branding.load_style(folder)      # raises if malformed


def test_render_raises_on_missing_logo(tmp_path):
    with pytest.raises(FileNotFoundError):
        branding.render_branded(
            str(tmp_path / "in.mp4"), "headline",
            str(tmp_path / "nope.png"), str(tmp_path / "out.mp4"),
        )


# --- unrenderable characters -----------------------------------------------

def test_strip_removes_keycap_and_flag_emoji():
    # "2️⃣" is digit + U+FE0F + U+20E3; no text font has the keycap glyph, so
    # drawtext drew a black box (newlinebug.mp4).
    out = branding.strip_unrenderable("attacked 2️⃣ women \U0001F1EB\U0001F1F7",
                                      branding.FONT_PATH)
    assert out == "attacked 2 women "


def test_strip_removes_invisible_formatting_characters():
    out = branding.strip_unrenderable("a​b﻿c‎", branding.FONT_PATH)
    assert out == "abc"


def test_strip_keeps_letters_the_font_lacks():
    # A Latin-only font must show boxes for Chinese, not silently drop words.
    assert branding.strip_unrenderable("香港 news", branding.FONT_PATH) == "香港 news"


def test_strip_keeps_supported_symbols():
    assert branding.strip_unrenderable("35°C — “quoted”", branding.FONT_PATH) == \
        "35°C — “quoted”"


# --- pixel wrapping --------------------------------------------------------

def test_wrap_to_px_keeps_every_row_inside_the_box():
    text = "Victim: \"I was sexually abused because of Xavier Becerra.\" " * 2
    rows = branding.wrap_to_px(text, branding.FONT_PATH, branding.FONT_SIZE)
    for row in rows.split("\n"):
        width = branding.text_width(row, branding.FONT_PATH, branding.FONT_SIZE)
        assert width is None or width <= branding.LINE_PX


def test_wrap_to_px_falls_back_to_character_wrapping(monkeypatch):
    monkeypatch.setattr(branding, "text_width", lambda *a, **k: None)
    rows = branding.wrap_to_px("word " * 40, "/no/such.ttf", branding.FONT_SIZE)
    assert "\n" in rows and "…" not in rows


def test_wrap_to_px_empty_returns_empty():
    assert branding.wrap_to_px("", branding.FONT_PATH, branding.FONT_SIZE) == ""


# --- end-to-end (needs ffmpeg) ---------------------------------------------

def _have_ffmpeg():
    return shutil.which("ffmpeg") and shutil.which("ffprobe")


@pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg not on PATH")
def test_render_end_to_end_geometry(tmp_path):
    src = str(tmp_path / "src.mp4")
    logo = str(tmp_path / "logo.png")
    out = str(tmp_path / "out.mp4")
    # 1-second horizontal test card + a small solid logo, both via ffmpeg.
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=size=640x360:rate=24:duration=1", src],
        check=True, capture_output=True)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "color=red:size=100x100", "-frames:v", "1", logo],
        check=True, capture_output=True)

    result = branding.render_branded(src, "Заголовок теста headline", logo, out)

    assert result == out and os.path.isfile(out)
    from modules.youtube import shorts_format
    w, h, dur = shorts_format.probe(out)
    assert (w, h) == (1080, 1920)
    assert 0.5 < dur < 2.0
    # the textfile temp must not be left behind
    assert not os.path.exists(out + ".txt")


@pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg not on PATH")
def test_render_writes_rows_separated_by_cr_only(tmp_path, monkeypatch):
    """ffmpeg 8's drawtext DRAWS a LF (as a missing-glyph box) while still
    breaking the row on it — the separator has to be a bare CR."""
    seen = {}
    real = branding.subprocess.run

    src = str(tmp_path / "src.mp4")
    logo = str(tmp_path / "logo.png")
    out = str(tmp_path / "out.mp4")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", "testsrc=size=640x360:rate=24:duration=1", src],
                   check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", "color=red:size=100x100", "-frames:v", "1", logo],
                   check=True, capture_output=True)

    def capture(cmd, *a, **kw):
        with open(out + ".txt", "rb") as fh:
            seen["bytes"] = fh.read()
        return real(cmd, *a, **kw)

    monkeypatch.setattr(branding.subprocess, "run", capture)
    branding.render_branded(src, "word " * 40, logo, out)

    assert b"\n" not in seen["bytes"]
    assert b"\r" in seen["bytes"]
