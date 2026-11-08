"""shared/video_edit.py — the `edit:` reply: parse, resolve, the ffmpeg pass."""

import shutil
import subprocess

import pytest

from modules.telegram import branded
from shared import video_edit as ve


def _have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


# --- the reply prefix -------------------------------------------------------

def test_edit_prefix_is_its_own_field():
    assert branded.parse_reply("edit: 23-26") == ("edit", "23-26")
    assert branded.parse_reply("EDIT :23-26") == ("edit", "23-26")
    assert branded.parse_reply("edit:") == ("edit", "")


def test_a_headline_that_starts_with_edit_is_still_a_headline():
    assert branded.parse_reply("Editors walk out at the Times")[0] == "text"


# --- parse ------------------------------------------------------------------

def test_clauses_need_no_commas():
    # Typed in the control group exactly like this (2026-10-18).
    spec = ve.parse("hook 0:18-0:21 crop left 1% crop right 2%")
    assert spec["hook"] == [18, 21]
    assert spec["crop"] == {"left": 0.01, "right": 0.02}
    spec = ve.parse("23-26 cut end 2 crop top 8% left 5%")
    assert spec["hook"] == [23, 26]
    assert spec["cut_end"] == {"s": 2}
    assert spec["crop"] == {"top": 0.08, "left": 0.05}


def test_a_bare_range_is_the_hook():
    assert ve.parse("23-26")["hook"] == [23, 26]
    assert ve.parse("hook 0:23 - 0:26")["hook"] == [23, 26]
    assert ve.parse("23.5–25")["hook"] == [23.5, 25]


def test_crop_takes_several_sides_and_spaced_percents():
    assert ve.parse("crop bottom 10%")["crop"] == {"bottom": 0.1}
    assert ve.parse("crop top 8 % left 5%")["crop"] == {"top": 0.08,
                                                        "left": 0.05}


def test_cuts():
    spec = ve.parse("cut start 2, cut end 10%; cut 40-45")
    assert spec["cut_start"] == {"s": 2}
    assert spec["cut_end"] == {"pct": 10}
    assert spec["cuts"] == [[40, 45]]


def test_everything_at_once():
    spec = ve.parse("23-26, crop bottom 10%, cut end 2")
    assert spec["hook"] == [23, 26] and spec["crop"] == {"bottom": 0.1}
    assert spec["cut_end"] == {"s": 2}


def test_empty_is_no_edit():
    assert ve.parse("") == {} and ve.is_noop({})


@pytest.mark.parametrize("bad", [
    "26-23",                      # backwards
    "23-60",                      # a 37 s hook
    "23-23.2",                    # a flicker
    "1-3, 5-7",                   # two hooks
    "crop middle 10%",
    "crop bottom 10",             # no percent
    "crop bottom 50%",            # past the per-side cap
    "crop top 35%, crop bottom 35%",
    "cut sideways 3",
    "make it pop",
])
def test_bad_edits_are_refused_with_a_message(bad):
    with pytest.raises(ValueError) as err:
        ve.parse(bad)
    assert str(err.value)


# --- resolve ----------------------------------------------------------------

def test_hook_then_the_whole_clip():
    plan = ve.resolve(ve.parse("23-26"), 45)
    assert plan["segments"] == [[23, 26], [0, 45]]
    assert plan["duration"] == 48


def test_cuts_shape_the_main_part_and_the_hook_stays_on_the_original():
    plan = ve.resolve(ve.parse("23-26, cut start 2, cut end 10%, cut 30-35"), 50)
    assert plan["segments"] == [[23, 26], [2, 30], [35, 45]]
    assert plan["duration"] == 3 + 28 + 10


def test_a_hook_past_the_end_is_refused_and_one_overhanging_is_clamped():
    with pytest.raises(ValueError):
        ve.resolve(ve.parse("50-53"), 45)
    assert ve.resolve(ve.parse("43-46"), 45)["segments"][0] == [43, 45]


def test_cuts_that_leave_nothing_are_refused():
    with pytest.raises(ValueError):
        ve.resolve(ve.parse("cut start 50%, cut end 50%"), 20)


def test_describe_shows_the_result_or_the_problem():
    line = ve.describe(ve.parse("23-26, crop bottom 10%"), 45)
    assert "hook 0:23–0:26" in line and "bottom 10%" in line
    assert "0:48" in line
    assert "⚠️" in ve.describe(ve.parse("50-53"), 45)
    assert ve.describe({}) == ""


def test_key_is_stable_and_tells_edits_apart():
    assert ve.key(ve.parse("23-26")) == ve.key(ve.parse("hook 23-26"))
    assert ve.key(ve.parse("23-26")) != ve.key(ve.parse("23-27"))


# --- the ffmpeg pass --------------------------------------------------------

def test_command_uses_one_seeked_input_per_segment():
    plan = ve.resolve(ve.parse("23-26, crop bottom 10%"), 45)
    cmd = ve.build_command("in.mp4", plan, "out.mp4", audio=True)
    assert cmd.count("-i") == 2
    assert cmd[cmd.index("-ss") + 1] == "23.000"
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "concat=n=2:v=1:a=1" in graph and "crop=" in graph
    silent = ve.build_command("in.mp4", plan, "out.mp4", audio=False)
    assert "a=0" in silent[silent.index("-filter_complex") + 1]
    assert "-c:a" not in silent


def _make_clip(path, seconds=6, audio=True):
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
           "-i", f"testsrc2=size=320x568:rate=25:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True)


@pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg not on PATH")
@pytest.mark.parametrize("audio", [True, False])
def test_render_hook_crop_and_cut(tmp_path, audio):
    from modules.youtube import shorts_format
    src, out = tmp_path / "src.mp4", tmp_path / "out.mp4"
    _make_clip(src, audio=audio)
    spec = ve.parse("3-5, crop bottom 10% left 10%, cut end 1")
    ve.render(str(src), spec, str(out), 6)
    w, h, duration = shorts_format.probe(str(out))
    assert (w, h) == (288, 510)          # 320*0.9, 568*0.9 rounded to even
    assert abs(duration - 7) < 0.15      # 2 s hook + 5 s kept


# --- ▶ Preview edit ---------------------------------------------------------

def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_preview_button_only_while_there_is_an_edit():
    assert "b:pv" not in _callbacks(branded.plan_keyboard(bundle=True))
    assert "b:pv" in _callbacks(branded.plan_keyboard(bundle=True, preview=True))


def test_bundle_line_offers_the_same_preview():
    from modules.telegram import bundle
    assert "b:pv" not in _callbacks(bundle.line_keyboard())
    assert "b:pv" in _callbacks(bundle.line_keyboard(preview=True))
