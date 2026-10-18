"""Offline tests for shared/vision.py — no OpenRouter calls.

The client is a scripted fake, so what is exercised here is the contract that
makes a model safe to put in front of a publish: `describe` NEVER RAISES. A
missing key, a dead gateway, prose where JSON was asked for, a truncated
object, an oversized clip — every one of them returns {}, and an empty dict is
what the caption pipeline already handles as "no footage", which is exactly
what shipped before any of this existed.

The ffmpeg pre-pass is exercised against a clip ffmpeg generates for the test,
and skipped where ffmpeg isn't installed.
"""

import json
import os
import shutil
import subprocess

import pytest

from shared import config, vision


GOOD = {
    "on_screen_text": "02/04/2025 18:41:07  CAM 3",
    "speech": "Someone shouts: turn around, turn around!",
    "language": "English",
    "setting": "outdoor, a residential street, daytime",
    "people_count": "3",
}


class _Fake:
    """Stands in for the OpenAI client: returns `content`, or raises `exc`."""

    def __init__(self, content="", exc=None):
        self.content = content
        self.exc = exc
        self.calls = []
        self.chat = self                      # client.chat.completions.create
        self.completions = self

    def create(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        msg = type("M", (), {"content": self.content})
        return type("R", (), {"choices": [type("C", (), {"message": msg})]})


@pytest.fixture
def on(monkeypatch):
    """Analysis enabled — made explicit so a developer's .env can't turn these
    green for the wrong reason."""
    monkeypatch.setattr(config, "IG_VISION_ENABLED", True)
    monkeypatch.setattr(config, "IG_VISION_MODEL", "google/gemini-2.5-flash")


@pytest.fixture
def photo(tmp_path):
    """A real file on disk — describe() reads bytes before it calls anything."""
    path = tmp_path / "hero.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 64)   # JPEG magic + filler
    return str(path)


def _client(monkeypatch, **kw):
    fake = _Fake(**kw)
    monkeypatch.setattr(vision, "_client", fake)
    return fake


# --------------------------------------------------------------------------- #
# the never-raises contract                                                   #
# --------------------------------------------------------------------------- #


def test_as_prompt_is_one_media_notes_line_without_empty_fields():
    line = vision.as_prompt(dict(GOOD, speech="", people_count=""))
    assert line == ("MEDIA NOTES: on-screen text: 02/04/2025 18:41:07  CAM 3; "
                    "language: English; setting: outdoor, a residential "
                    "street, daytime")
    assert vision.as_prompt({}) == ""
    assert vision.as_prompt({"summary": "old narration"}) == ""


def test_describe_returns_the_parsed_analysis(on, monkeypatch, photo):
    _client(monkeypatch, content=json.dumps(GOOD))
    out = vision.describe(photo, "Man walks past bear")
    assert out == GOOD


def test_no_api_key_returns_empty(on, monkeypatch, photo):
    monkeypatch.setattr(vision, "_client", None)
    assert vision.describe(photo, "Man walks past bear") == {}


def test_disabled_returns_empty_without_calling(on, monkeypatch, photo):
    """The kill switch has to cut the billed call, not just the output."""
    fake = _client(monkeypatch, content=json.dumps(GOOD))
    monkeypatch.setattr(config, "IG_VISION_ENABLED", False)
    assert vision.describe(photo, "Man walks past bear") == {}
    assert fake.calls == []


def test_missing_file_returns_empty(on, monkeypatch, tmp_path):
    fake = _client(monkeypatch, content=json.dumps(GOOD))
    assert vision.describe(str(tmp_path / "gone.mp4"), "headline") == {}
    assert fake.calls == []


def test_api_failure_returns_empty(on, monkeypatch, photo):
    _client(monkeypatch, exc=RuntimeError("502 from the gateway"))
    assert vision.describe(photo, "Man walks past bear") == {}


def test_prose_instead_of_json_returns_empty(on, monkeypatch, photo):
    _client(monkeypatch, content="Sure! Here is what I see in the image.")
    assert vision.describe(photo, "Man walks past bear") == {}


def test_empty_completion_returns_empty(on, monkeypatch, photo):
    _client(monkeypatch, content="")
    assert vision.describe(photo, "Man walks past bear") == {}


def test_json_with_every_field_empty_returns_empty(on, monkeypatch, photo):
    """Nothing the model was sure of is the same as no notes — and an object
    carrying only the old narration keys is nothing at all now."""
    _client(monkeypatch, content=json.dumps(
        {"on_screen_text": "", "speech": "", "summary": "A hug.",
         "beats": ["a", "b"]}))
    assert vision.describe(photo, "Man walks past bear") == {}


# --------------------------------------------------------------------------- #
# cleaning up after a model told not to use wrappers                          #
# --------------------------------------------------------------------------- #


def test_fenced_json_is_unwrapped(on, monkeypatch, photo):
    _client(monkeypatch,
            content="```json\n" + json.dumps(GOOD) + "\n```")
    assert vision.describe(photo, "h")["on_screen_text"] == GOOD["on_screen_text"]


def test_json_with_a_preamble_is_recovered(on, monkeypatch, photo):
    _client(monkeypatch,
            content="Here is the analysis:\n" + json.dumps(GOOD) + "\nHope that helps!")
    assert vision.describe(photo, "h")["on_screen_text"] == GOOD["on_screen_text"]


def test_missing_keys_are_defaulted(on, monkeypatch, photo):
    _client(monkeypatch, content=json.dumps({"speech": "Help!"}))
    out = vision.describe(photo, "h")
    assert out == {"on_screen_text": "", "speech": "Help!", "language": "",
                   "setting": "", "people_count": ""}


def test_wrong_types_are_coerced_or_dropped(on, monkeypatch, photo):
    _client(monkeypatch, content=json.dumps({
        "on_screen_text": ["EXIT", "Platform 2"],
        "people_count": 3,
        "speech": {"a": 1},
        "summary": "A bear.",
    }))
    out = vision.describe(photo, "h")
    assert out["on_screen_text"] == "EXIT; Platform 2"
    assert out["people_count"] == "3"
    assert out["speech"] == ""
    assert "summary" not in out


def test_a_model_still_judging_the_headline_is_ignored(on, monkeypatch, photo):
    """The headline check is gone: stray verdict fields never reach a caller."""
    _client(monkeypatch, content=json.dumps({
        "speech": "Help!", "headline_ok": False,
        "headline_suggestion": "Another headline",
    }))
    out = vision.describe(photo, "h")
    assert "headline_ok" not in out and "headline_suggestion" not in out


def test_the_prompt_no_longer_asks_for_a_headline_verdict(on, monkeypatch, photo):
    fake = _client(monkeypatch, content=json.dumps(GOOD))
    vision.describe(photo, "Man walks past bear")
    sent = json.dumps(fake.calls[0]["messages"], ensure_ascii=False)
    assert "headline_ok" not in sent and "CHECK THE HEADLINE" not in sent


# --------------------------------------------------------------------------- #
# what actually goes over the wire                                            #
# --------------------------------------------------------------------------- #


def test_a_photo_is_sent_as_an_image_part(on, monkeypatch, photo):
    fake = _client(monkeypatch, content=json.dumps(GOOD))
    vision.describe(photo, "Man walks past bear")
    parts = fake.calls[0]["messages"][-1]["content"]
    kinds = [p["type"] for p in parts]
    assert "image_url" in kinds and "video_url" not in kinds
    url = next(p["image_url"]["url"] for p in parts if p["type"] == "image_url")
    assert url.startswith("data:image/jpeg;base64,")


def test_the_headline_is_not_sent_with_the_media(on, monkeypatch, photo):
    """The headline used to go along as "context", and context is what talked
    the model into describing the headline instead of the media."""
    fake = _client(monkeypatch, content=json.dumps(GOOD))
    vision.describe(photo, "Man walks past bear")
    sent = json.dumps(fake.calls[0]["messages"], ensure_ascii=False)
    assert "Man walks past bear" not in sent


def test_the_prompt_forbids_describing_actions(on, monkeypatch, photo):
    fake = _client(monkeypatch, content=json.dumps(GOOD))
    vision.describe(photo)
    system = fake.calls[0]["messages"][0]["content"]
    assert "Do NOT describe or interpret what people do" in system
    for key in ("on_screen_text", "speech", "language", "setting",
                "people_count"):
        assert f'"{key}"' in system
    assert '"summary"' not in system and '"beats"' not in system


def test_no_headline_still_analyses_the_media(on, monkeypatch, photo):
    """A post can reach the gate with no headline typed yet — the analysis is
    what tells the operator what to write."""
    fake = _client(monkeypatch, content=json.dumps(GOOD))
    assert vision.describe(photo, "")["speech"] == GOOD["speech"]
    assert len(fake.calls) == 1


def test_a_video_is_sent_as_a_video_part(on, monkeypatch, tmp_path):
    """The ffmpeg pre-pass is stubbed — what is asserted is the content part
    the analysis copy is wrapped in, not the encode."""
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"\x00" * 128)
    small = tmp_path / "clip.analysis.mp4"
    small.write_bytes(b"\x00" * 32)
    monkeypatch.setattr(vision, "_analysis_copy",
                        lambda path: (str(small), lambda: None))
    fake = _client(monkeypatch, content=json.dumps(GOOD))
    vision.describe(str(src), "Man walks past bear")
    parts = fake.calls[0]["messages"][-1]["content"]
    url = next(p["video_url"]["url"] for p in parts if p["type"] == "video_url")
    assert url.startswith("data:video/mp4;base64,")


def test_the_analysis_copy_is_removed_afterwards(on, monkeypatch, tmp_path):
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"\x00" * 128)
    small = tmp_path / "clip.analysis.mp4"
    small.write_bytes(b"\x00" * 32)
    dropped = []
    monkeypatch.setattr(vision, "_analysis_copy",
                        lambda path: (str(small), lambda: dropped.append(1)))
    _client(monkeypatch, content=json.dumps(GOOD))
    vision.describe(str(src), "h")
    assert dropped == [1]


def test_the_analysis_copy_is_removed_when_the_call_fails(on, monkeypatch, tmp_path):
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"\x00" * 128)
    small = tmp_path / "clip.analysis.mp4"
    small.write_bytes(b"\x00" * 32)
    dropped = []
    monkeypatch.setattr(vision, "_analysis_copy",
                        lambda path: (str(small), lambda: dropped.append(1)))
    _client(monkeypatch, exc=RuntimeError("gateway down"))
    assert vision.describe(str(src), "h") == {}
    assert dropped == [1]


def test_a_failed_pre_pass_returns_empty_without_calling(on, monkeypatch, tmp_path):
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"\x00" * 128)

    def _boom(path):
        raise RuntimeError("ffmpeg not found")

    monkeypatch.setattr(vision, "_analysis_copy", _boom)
    fake = _client(monkeypatch, content=json.dumps(GOOD))
    assert vision.describe(str(src), "h") == {}
    assert fake.calls == []


def test_an_oversized_copy_gives_up_without_calling(on, monkeypatch, tmp_path):
    """Better a caption with no footage than a 60 MB request body."""
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"\x00" * 128)
    big = tmp_path / "clip.analysis.mp4"
    big.write_bytes(b"\x00" * 4096)
    monkeypatch.setattr(config, "IG_VISION_MAX_MB", 0.001)   # ~1 KB
    monkeypatch.setattr(vision, "_analysis_copy",
                        lambda path: (str(big), lambda: None))
    fake = _client(monkeypatch, content=json.dumps(GOOD))
    assert vision.describe(str(src), "h") == {}
    assert fake.calls == []


# --------------------------------------------------------------------------- #
# the ffmpeg pre-pass, against a clip ffmpeg makes for us                     #
# --------------------------------------------------------------------------- #


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None,
                                  reason="ffmpeg not on PATH")


@pytest.fixture
def clip(tmp_path):
    """Two seconds of 720x1280 colour bars with a tone, ~real encoder output."""
    out = str(tmp_path / "source.mp4")
    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc=size=720x1280:rate=30:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", out],
        capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip(f"ffmpeg could not build the fixture: {proc.stderr[-200:]}")
    return out


@needs_ffmpeg
def test_the_pre_pass_shrinks_the_clip(clip):
    small, drop = vision._analysis_copy(clip)
    try:
        assert os.path.getsize(small) < os.path.getsize(clip)
    finally:
        drop()
    assert not os.path.exists(small)


@needs_ffmpeg
def test_the_pre_pass_keeps_the_audio(clip):
    """Your example needs the shouting: a silent analysis copy cannot report
    what bystanders were saying."""
    small, drop = vision._analysis_copy(clip)
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", small],
            capture_output=True, text=True)
        assert "audio" in proc.stdout
    finally:
        drop()


@needs_ffmpeg
def test_the_pre_pass_trims_to_the_cap(clip, monkeypatch):
    monkeypatch.setattr(config, "IG_VISION_MAX_S", 1)
    small, drop = vision._analysis_copy(clip)
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", small],
            capture_output=True, text=True)
        assert float(proc.stdout.strip()) <= 1.6      # 1s + keyframe slack
    finally:
        drop()


@needs_ffmpeg
def test_a_zero_cap_sends_the_whole_clip(clip, monkeypatch):
    """The default: nothing is trimmed, so the end of the clip is analysed."""
    monkeypatch.setattr(config, "IG_VISION_MAX_S", 0)
    small, drop = vision._analysis_copy(clip)
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", small],
            capture_output=True, text=True)
        assert float(proc.stdout.strip()) >= 1.9      # all two seconds
    finally:
        drop()


def test_the_whole_clip_is_the_default():
    assert config._int_env("IG_VISION_MAX_S_UNSET_FOR_TEST", 0) == 0
    import inspect
    assert '_int_env("IG_VISION_MAX_S", 0)' in inspect.getsource(config)


@needs_ffmpeg
def test_describe_end_to_end_against_a_real_encode(on, monkeypatch, clip):
    """Everything but the model: a real clip, a real pre-pass, a real base64."""
    fake = _client(monkeypatch, content=json.dumps(GOOD))
    out = vision.describe(clip, "Colour bars appear on screen")
    assert out["setting"] == GOOD["setting"]
    parts = fake.calls[0]["messages"][-1]["content"]
    url = next(p["video_url"]["url"] for p in parts if p["type"] == "video_url")
    assert url.startswith("data:video/mp4;base64,")
    assert len(url) > 1000


@needs_ffmpeg
@pytest.mark.parametrize("size,want", [("1920x1080", "1280,720"),
                                       ("1080x1920", "720,1280"),
                                       ("640x360", "640,360")])
def test_the_analysis_copy_is_720p_on_the_short_edge(tmp_path, size, want):
    """Landscape and portrait alike reach 720p; a smaller clip is never
    upscaled. (Scaling the width alone once made a 1080p landscape 480x270.)"""
    src = str(tmp_path / "src.mp4")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    f"testsrc=size={size}:rate=30:duration=1",
                    "-pix_fmt", "yuv420p", src], check=True)
    small, drop = vision._analysis_copy(src)
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=width,height",
             "-of", "csv=p=0", small], capture_output=True, text=True)
        assert proc.stdout.strip() == want
    finally:
        drop()
