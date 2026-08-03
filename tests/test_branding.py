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
    assert "scale=180:-1[logo]" in graph                   # logo width
    assert "overlay=W-w-40:40" in graph                    # top-right margin
    assert "fontsize=36" in graph
    assert "boxcolor=black@0.55" in graph
    assert "boxborderw=20" in graph
    assert "setsar=1" in graph                             # clean 1:1 SAR out
    assert ":x=60:" in graph                               # left-aligned
    assert "fade=t=out:st=10:d=1.5:alpha=1" in graph       # banner fade-out
    assert "y=h*0.72" in graph
    assert "textfile='/t/text.txt'" in graph               # never inline text
    assert "fontfile='/f/font.ttf'" in graph


def test_render_raises_on_missing_logo(tmp_path):
    with pytest.raises(FileNotFoundError):
        branding.render_branded(
            str(tmp_path / "in.mp4"), "headline",
            str(tmp_path / "nope.png"), str(tmp_path / "out.mp4"),
        )


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
