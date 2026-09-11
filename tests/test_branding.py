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
    # The blur is computed on a 1/8 copy and upscaled — visually identical to
    # the old full-res gblur sigma=30, ~12 % cheaper (measured on the VPS).
    assert "scale=135:240:force_original_aspect_ratio=increase" in graph
    assert "gblur=sigma=4" in graph                        # blur-fill canvas
    assert "scale=1080:1920[bg]" in graph                  # upscaled back
    assert "scale=1080:1920:force_original_aspect_ratio=decrease[fg]" in graph
    assert "scale=205:-1[logo0]" in graph                  # logo width
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
    assert "fade=t=out:st=5:d=1.5:alpha=1" in graph        # banner fade-out
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


def test_filter_graph_multi_builds_the_canvas_once():
    jobs = [
        {"font": "/f/a.ttf", "text_path": "/t/a.txt", "style": None},
        {"font": "/f/b.ttf", "text_path": "/t/b.txt",
         "style": dict(branding.DEFAULT_STYLE, font_size=60)},
    ]
    graph = branding._filter_graph_multi(jobs)
    assert graph.count("gblur") == 1                       # shared canvas
    assert "split=2[c0][c1]" in graph
    # Each branch: its own logo input, textfile, style, labeled output.
    assert "[1:v]scale=205:-1[logo0]" in graph
    assert "[2:v]scale=205:-1[logo1]" in graph
    assert "textfile='/t/a.txt'" in graph
    assert "textfile='/t/b.txt'" in graph
    assert "fontsize=60" in graph
    assert "[v0]" in graph and "[v1]" in graph


def test_encode_args_use_the_speed_preset():
    i = branding.ENCODE_ARGS.index("-preset")
    assert branding.ENCODE_ARGS[i + 1] == branding.PRESET == "superfast"


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
def test_render_multi_end_to_end(tmp_path):
    src = str(tmp_path / "src.mp4")
    logo_a = str(tmp_path / "a" / "logo.png")
    logo_b = str(tmp_path / "b" / "logo.png")
    os.makedirs(os.path.dirname(logo_a))
    os.makedirs(os.path.dirname(logo_b))
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=size=640x360:rate=24:duration=1", src],
        check=True, capture_output=True)
    for logo in (logo_a, logo_b):
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
             "-i", "color=red:size=100x100", "-frames:v", "1", logo],
            check=True, capture_output=True)

    outs = branding.render_branded_multi(src, [
        {"headline": "first brand", "logo_path": logo_a,
         "out_path": str(tmp_path / "out_a.mp4")},
        {"headline": "second brand", "logo_path": logo_b,
         "out_path": str(tmp_path / "out_b.mp4")},
    ])

    from modules.youtube import shorts_format
    assert len(outs) == 2
    for out in outs:
        assert os.path.isfile(out)
        w, h, dur = shorts_format.probe(out)
        assert (w, h) == (1080, 1920)
        assert 0.5 < dur < 2.0
        assert not os.path.exists(out + ".txt")   # textfiles cleaned up


def test_render_multi_missing_logo_raises_before_ffmpeg(tmp_path):
    with pytest.raises(FileNotFoundError):
        branding.render_branded_multi(str(tmp_path / "in.mp4"), [
            {"headline": "x", "logo_path": str(tmp_path / "nope.png"),
             "out_path": str(tmp_path / "out.mp4")},
        ])


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


# --------------------------------------------------------------------------- #
# load_writing_style — the one non-visual key in style.json                   #
# --------------------------------------------------------------------------- #


def test_writing_style_is_read_from_style_json(tmp_path):
    (tmp_path / "style.json").write_text(
        '{"background": "#000000", "writing_style": "Two paragraphs."}',
        encoding="utf-8")
    assert branding.load_writing_style(str(tmp_path)) == "Two paragraphs."


def test_writing_style_is_whitespace_collapsed(tmp_path):
    """A descriptor is written across lines in the file and has to arrive at
    the model as one paragraph."""
    (tmp_path / "style.json").write_text(
        '{"writing_style": "Two   paragraphs.\\n\\nNever more."}',
        encoding="utf-8")
    assert branding.load_writing_style(str(tmp_path)) == \
        "Two paragraphs. Never more."


def test_writing_style_is_capped(tmp_path):
    (tmp_path / "style.json").write_text(
        '{"writing_style": "%s"}' % ("x" * 5000), encoding="utf-8")
    assert len(branding.load_writing_style(str(tmp_path))) == \
        branding.MAX_WRITING_STYLE


def test_a_brand_with_no_style_file_has_no_voice(tmp_path):
    assert branding.load_writing_style(str(tmp_path)) == ""


def test_a_brand_whose_style_file_is_broken_has_no_voice(tmp_path):
    """Never raises: a caption voice is not worth failing a publish over."""
    (tmp_path / "style.json").write_text("{not json", encoding="utf-8")
    assert branding.load_writing_style(str(tmp_path)) == ""


def test_a_non_string_style_is_ignored(tmp_path):
    (tmp_path / "style.json").write_text('{"writing_style": 42}',
                                         encoding="utf-8")
    assert branding.load_writing_style(str(tmp_path)) == ""


def test_load_style_does_not_return_the_voice(tmp_path):
    """The ffmpeg path must never see a paragraph of English prose."""
    (tmp_path / "style.json").write_text(
        '{"background": "#112233", "writing_style": "Two paragraphs."}',
        encoding="utf-8")
    assert "writing_style" not in branding.load_style(str(tmp_path))


# --- render_branded_multi batching -----------------------------------------
# Each extra output in one ffmpeg pass is another x264 encoder inside the same
# process (~390 MB at 1080x1920), so the pass is split into batches. These
# tests stub ffmpeg out — they are about how the jobs are grouped, not pixels.

def _logo(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    p = d / "logo.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n")     # _prepare_job only checks isfile
    return str(p)


def _multi_jobs(tmp_path, n):
    return [{"headline": f"brand {i}", "logo_path": _logo(tmp_path, f"b{i}"),
             "out_path": str(tmp_path / f"out_{i}.mp4")} for i in range(n)]


def _outs_of(cmd):
    return [c for c in cmd
            if c.endswith(".mp4") and os.path.basename(c).startswith("out_")]


def _stub_ffmpeg(monkeypatch, fail_on=None):
    """Record every ffmpeg command and write the outputs it was asked for.
    `fail_on` is the 0-based batch index that comes back non-zero."""
    runs = []

    class _Proc:
        def __init__(self, rc):
            self.returncode = rc
            self.stderr = "out of memory" if rc else ""

    def fake_run(cmd, **kw):
        runs.append(cmd)
        if fail_on is not None and len(runs) - 1 == fail_on:
            return _Proc(1)
        for out in _outs_of(cmd):
            with open(out, "wb") as fh:
                fh.write(b"fake mp4")
        return _Proc(0)

    monkeypatch.setattr(branding.subprocess, "run", fake_run)
    return runs


def test_render_multi_splits_into_batches(tmp_path, monkeypatch):
    runs = _stub_ffmpeg(monkeypatch)
    jobs = _multi_jobs(tmp_path, 5)

    branding.render_branded_multi(str(tmp_path / "src.mp4"), jobs, batch=2)

    assert [len(_outs_of(c)) for c in runs] == [2, 2, 1]


def test_render_multi_batched_returns_every_path_in_job_order(tmp_path, monkeypatch):
    _stub_ffmpeg(monkeypatch)
    jobs = _multi_jobs(tmp_path, 5)

    outs = branding.render_branded_multi(str(tmp_path / "src.mp4"), jobs, batch=2)

    assert outs == [j["out_path"] for j in jobs]
    assert all(os.path.isfile(o) for o in outs)


def test_render_multi_batch_failure_removes_earlier_batches_too(tmp_path, monkeypatch):
    """All-or-nothing survives batching: news_bot catches this and retries
    brand-by-brand, so a half-written set would leave orphan files behind and
    make the fallback's outputs ambiguous."""
    runs = _stub_ffmpeg(monkeypatch, fail_on=1)
    jobs = _multi_jobs(tmp_path, 5)

    with pytest.raises(RuntimeError):
        branding.render_branded_multi(str(tmp_path / "src.mp4"), jobs, batch=2)

    assert len(runs) == 2                                   # stopped at the failure
    assert not any(os.path.exists(j["out_path"]) for j in jobs)


def test_render_multi_batched_cleans_up_every_textfile(tmp_path, monkeypatch):
    _stub_ffmpeg(monkeypatch)
    jobs = _multi_jobs(tmp_path, 5)

    branding.render_branded_multi(str(tmp_path / "src.mp4"), jobs, batch=2)

    assert not any(os.path.exists(j["out_path"] + ".txt") for j in jobs)


def test_render_multi_bad_logo_in_a_later_batch_runs_no_ffmpeg(tmp_path, monkeypatch):
    """Every job is validated up front, so a broken brand fails on the tap
    rather than a batch and a half into the render."""
    runs = _stub_ffmpeg(monkeypatch)
    jobs = _multi_jobs(tmp_path, 5)
    jobs[4]["logo_path"] = str(tmp_path / "nope.png")

    with pytest.raises(FileNotFoundError):
        branding.render_branded_multi(str(tmp_path / "src.mp4"), jobs, batch=2)

    assert runs == []


def test_render_multi_batch_zero_means_one_pass(tmp_path, monkeypatch):
    runs = _stub_ffmpeg(monkeypatch)
    jobs = _multi_jobs(tmp_path, 5)

    branding.render_branded_multi(str(tmp_path / "src.mp4"), jobs, batch=0)

    assert len(runs) == 1
    assert len(_outs_of(runs[0])) == 5


def test_render_multi_defaults_to_the_module_batch_size(tmp_path, monkeypatch):
    monkeypatch.setattr(branding, "RENDER_BATCH", 3)
    runs = _stub_ffmpeg(monkeypatch)

    branding.render_branded_multi(str(tmp_path / "src.mp4"), _multi_jobs(tmp_path, 7))

    assert [len(_outs_of(c)) for c in runs] == [3, 3, 1]


def test_render_batch_default_fits_a_small_vps():
    """~0.5 GB + ~0.39 GB per brand, measured at 1080x1920 — four brands is
    1.65 GB, which needs 4 GB of RAM. Raising this raises the ceiling."""
    assert branding.RENDER_BATCH == 4
