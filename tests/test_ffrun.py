"""Time limits on ffmpeg, and the retry after a hang (offline; no real ffmpeg)."""
import os
import subprocess
import sys

import pytest

from shared import branding, ffrun


def test_limit_grows_with_duration_and_outputs():
    assert ffrun.limit_s(18, 2) == ffrun.BASE_S + ffrun.PER_CLIP_S * 2 * 18
    assert ffrun.limit_s(60, 2) > ffrun.limit_s(60, 1) > ffrun.limit_s(10, 1)


@pytest.mark.parametrize("duration", [None, 0, -1])
def test_unknown_duration_gets_the_flat_limit(duration):
    assert ffrun.limit_s(duration) == ffrun.UNKNOWN_DURATION_S


def test_measured_worst_case_fits_with_headroom():
    """236 s measured for a worst-case 60 s clip, two outputs, on the VPS."""
    assert ffrun.limit_s(60, 2) >= 3 * 236


def test_run_kills_a_hung_process():
    with pytest.raises(ffrun.Timeout, match="after 1 s"):
        ffrun.run([sys.executable, "-c", "import time; time.sleep(30)"], 1)


def test_timeout_is_a_runtime_error():
    """Callers that report RuntimeError (the edit path) keep working."""
    assert issubclass(ffrun.Timeout, RuntimeError)


def test_duration_of_never_raises(monkeypatch):
    def boom(*a, **k):
        raise OSError("no ffprobe")
    monkeypatch.setattr(ffrun.subprocess, "run", boom)
    assert ffrun.duration_of("x.mp4") is None


def _outs(cmd):
    return [a for a in cmd if a.endswith(".mp4") and "out_" in os.path.basename(a)]


def _stub(monkeypatch, hang):
    """ffmpeg stub: `hang(cmd)` decides which encodes never finish."""
    runs = []

    class Proc:
        returncode = 0
        stderr = ""
        stdout = "18.0\n"

    def fake(cmd, **kw):
        if cmd[0] == "ffprobe":
            return Proc()
        assert kw.get("timeout"), "every encode must have a limit"
        runs.append(cmd)
        for out in _outs(cmd):
            with open(out, "wb") as fh:
                fh.write(b"partial")
        if hang(cmd):
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        return Proc()

    monkeypatch.setattr(ffrun.subprocess, "run", fake)
    return runs


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(branding, "_filter_graph_multi", lambda p: "null")
    monkeypatch.setattr(branding, "_filter_graph", lambda *a: "null")
    prep = lambda head, logo, out, style: {  # noqa: E731
        "logo_path": logo, "out_path": out, "style": {}, "font": "f",
        "text_path": out + ".txt"}
    monkeypatch.setattr(branding, "_prepare_job", prep)
    return [{"headline": "h", "logo_path": "l.png",
             "out_path": str(tmp_path / f"out_{i}.mp4")} for i in range(3)]


def _twoloop(cmd):
    return "twoloop" in cmd


def test_normal_render_keeps_the_default_audio(monkeypatch, jobs):
    runs = _stub(monkeypatch, lambda c: False)
    branding.render_branded_multi("src.mp4", jobs, batch=3)
    assert len(runs) == 1 and not _twoloop(runs[0])


def test_hang_retries_once_with_the_safe_audio(monkeypatch, jobs):
    runs = _stub(monkeypatch, lambda c: not _twoloop(c))
    outs = branding.render_branded_multi("src.mp4", jobs, batch=3)
    assert len(runs) == 2 and _twoloop(runs[1])
    assert runs[1].index("-aac_coder") < runs[1].index("-b:a")
    assert all(os.path.exists(o) for o in outs)


def test_later_batches_start_on_the_safe_audio(monkeypatch, jobs):
    """Otherwise the same clip waits out the limit once per batch."""
    runs = _stub(monkeypatch, lambda c: not _twoloop(c))
    branding.render_branded_multi("src.mp4", jobs, batch=1)
    assert [_twoloop(c) for c in runs] == [False, True, True, True]


def test_hang_on_the_retry_raises_and_removes_every_output(monkeypatch, jobs):
    runs = _stub(monkeypatch, lambda c: True)
    with pytest.raises(ffrun.Timeout):
        branding.render_branded_multi("src.mp4", jobs, batch=3)
    assert len(runs) == 2
    assert not any(os.path.exists(j["out_path"]) for j in jobs)


def test_single_render_removes_partial_output_after_two_hangs(monkeypatch, jobs):
    _stub(monkeypatch, lambda c: True)
    job = jobs[0]
    with pytest.raises(ffrun.Timeout):
        branding.render_branded("src.mp4", "h", "l.png", job["out_path"])
    assert not os.path.exists(job["out_path"])


# --- metadata strip ---------------------------------------------------------

def _tags(path) -> str:
    return subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags:stream_tags",
         "-of", "default=nw=1", str(path)],
        capture_output=True, text=True, check=True).stdout


def _have_ffmpeg() -> bool:
    import shutil
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg not on PATH")
def test_strip_metadata_removes_source_tags_in_place(tmp_path):
    clip = tmp_path / "x.mp4"
    # What an X download looks like: the platform's name on the video track
    # and its processing time in the header.
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc2=size=160x284:rate=25:duration=1",
         "-f", "lavfi", "-i", "sine=duration=1",
         "-c:v", "libx264", "-c:a", "aac",
         "-metadata", "creation_time=2026-03-01T20:30:55Z",
         "-metadata", "comment=https://x.com/someone/status/1",
         "-metadata:s:v", "handler_name=Twitter-vork muxer", str(clip)],
        check=True)
    before = _tags(clip)
    assert "Twitter-vork muxer" in before and "creation_time" in before
    assert ffrun.strip_metadata(str(clip)) is True
    after = _tags(clip)
    for leak in ("Twitter", "creation_time", "x.com", "encoder", "Lavf"):
        assert leak not in after
    assert not (tmp_path / "x.clean.mp4").exists()
    assert ffrun.duration_of(str(clip)) == pytest.approx(1, abs=0.1)


def test_strip_metadata_never_raises(tmp_path):
    assert ffrun.strip_metadata(str(tmp_path / "missing.mp4")) is False
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"not touched")
    assert ffrun.strip_metadata(str(photo)) is False
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video")
    assert ffrun.strip_metadata(str(broken)) is False
    assert broken.read_bytes() == b"not a video"
    assert not (tmp_path / "broken.clean.mp4").exists()


def test_every_encode_drops_input_metadata():
    from shared import video_edit
    assert "-map_metadata" in branding.ENCODE_ARGS
    plan = video_edit.resolve(video_edit.parse("2-3"), 10)
    cmd = video_edit.build_command("in.mp4", plan, "out.mp4", audio=True)
    assert cmd[cmd.index("-map_metadata") + 1] == "-1"
    assert cmd[-1] == "out.mp4"
