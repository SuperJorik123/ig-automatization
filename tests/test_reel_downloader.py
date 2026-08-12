"""shared/reel_downloader.py — the gallery-dl photo fallback and the
video-first download chain. Fully offline: subprocess and yt-dlp are mocked."""

import json
import os

import pytest

from shared import config, reel_downloader


def fake_gdl(files, stderr=""):
    """subprocess.run stand-in: stages `files` ({name: bytes | dict}) into the
    directory the -D flag points at, exactly as the gallery-dl CLI would."""

    class Proc:
        returncode = 0
        stdout = ""

    Proc.stderr = stderr

    def run(cmd, capture_output=True, text=True):
        run.cmd = cmd
        out_dir = cmd[cmd.index("-D") + 1]
        os.makedirs(out_dir, exist_ok=True)
        for name, content in files.items():
            path = os.path.join(out_dir, name)
            if isinstance(content, dict):
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(content, fh)
            else:
                with open(path, "wb") as fh:
                    fh.write(content)
        return Proc()

    return run


def test_download_photos_renames_onto_stub(tmp_path, monkeypatch):
    run = fake_gdl({"insta_01.jpg": b"a", "insta_02.png": b"b"})
    monkeypatch.setattr(reel_downloader.subprocess, "run", run)
    stub = str(tmp_path / "manual_url_7.mp4")

    paths, caption = reel_downloader.download_photos("https://x.com/u/status/1", stub)

    assert [os.path.basename(p) for p in paths] == [
        "manual_url_7_1.jpg", "manual_url_7_2.png",
    ]
    assert all(os.path.isfile(p) for p in paths)
    assert caption == ""
    # the per-call temp dir must not be left behind
    assert not os.path.isdir(str(tmp_path / "manual_url_7_gdl"))


def test_download_photos_caption_from_info_json(tmp_path, monkeypatch):
    run = fake_gdl({
        "a.jpg": b"a",
        "a.jpg.json": {"description": "Breaking news caption"},
    })
    monkeypatch.setattr(reel_downloader.subprocess, "run", run)

    paths, caption = reel_downloader.download_photos(
        "https://instagram.com/p/abc", str(tmp_path / "s.mp4"))

    assert caption == "Breaking news caption"
    assert len(paths) == 1


def test_download_photos_caps_at_telegram_album_limit(tmp_path, monkeypatch):
    files = {f"img_{i:02d}.jpg": b"x" for i in range(14)}
    monkeypatch.setattr(reel_downloader.subprocess, "run", fake_gdl(files))

    paths, _ = reel_downloader.download_photos(
        "https://instagram.com/p/abc", str(tmp_path / "s.mp4"))

    assert len(paths) == reel_downloader.MAX_PHOTOS == 10


def test_download_photos_passes_cookie_flag_when_configured(tmp_path, monkeypatch):
    run = fake_gdl({"a.jpg": b"a"})
    monkeypatch.setattr(reel_downloader.subprocess, "run", run)
    monkeypatch.setattr(config, "GALLERY_DL_COOKIES_BROWSER", "chrome")

    reel_downloader.download_photos("https://instagram.com/p/abc",
                                    str(tmp_path / "s.mp4"))

    i = run.cmd.index("--cookies-from-browser")
    assert run.cmd[i + 1] == "chrome"


def test_download_photos_no_cookie_flag_by_default(tmp_path, monkeypatch):
    run = fake_gdl({"a.jpg": b"a"})
    monkeypatch.setattr(reel_downloader.subprocess, "run", run)
    monkeypatch.setattr(config, "GALLERY_DL_COOKIES_BROWSER", "")

    reel_downloader.download_photos("https://instagram.com/p/abc",
                                    str(tmp_path / "s.mp4"))

    assert "--cookies-from-browser" not in run.cmd


def test_download_photos_raises_when_nothing_downloaded(tmp_path, monkeypatch):
    monkeypatch.setattr(reel_downloader.subprocess, "run",
                        fake_gdl({}, stderr="login required"))

    with pytest.raises(RuntimeError, match="login required"):
        reel_downloader.download_photos("https://instagram.com/p/abc",
                                        str(tmp_path / "s.mp4"))


def test_download_any_video_first(monkeypatch):
    monkeypatch.setattr(reel_downloader, "download_media",
                        lambda url, dest, kind: ("/v.mp4", "vid cap"))
    monkeypatch.setattr(reel_downloader, "download_photos",
                        lambda url, stub: (_ for _ in ()).throw(AssertionError))

    assert reel_downloader.download_any("u", "/s.mp4") == (["/v.mp4"], "vid cap")


def test_download_any_falls_back_to_post_then_photos(monkeypatch):
    calls = []

    def dm(url, dest, kind):
        calls.append(kind)
        raise RuntimeError(f"yt-dlp {kind} failed")

    monkeypatch.setattr(reel_downloader, "download_media", dm)
    monkeypatch.setattr(reel_downloader, "download_photos",
                        lambda url, stub: (["/p_1.jpg", "/p_2.jpg"], "photo cap"))

    paths, caption = reel_downloader.download_any("u", "/s.mp4")

    assert calls == ["reel", "post"]
    assert paths == ["/p_1.jpg", "/p_2.jpg"] and caption == "photo cap"


def test_download_any_raises_the_first_error_when_all_fail(monkeypatch):
    def dm(url, dest, kind):
        raise RuntimeError(f"yt-dlp {kind} failed")

    monkeypatch.setattr(reel_downloader, "download_media", dm)
    monkeypatch.setattr(reel_downloader, "download_photos",
                        lambda url, stub: (_ for _ in ()).throw(
                            RuntimeError("gallery-dl failed")))

    with pytest.raises(RuntimeError, match="yt-dlp reel failed"):
        reel_downloader.download_any("u", "/s.mp4")
