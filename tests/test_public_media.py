"""shared/public_media.py — exposing a local file at a public URL for the
Instagram Graph API to fetch. Pure filesystem; no nginx involved."""

import os
import time

import pytest

from shared import config, public_media


@pytest.fixture
def pub(tmp_path, monkeypatch):
    d = tmp_path / "pub"
    d.mkdir()
    monkeypatch.setattr(config, "PUBLIC_MEDIA_DIR", str(d))
    monkeypatch.setattr(config, "PUBLIC_MEDIA_BASE_URL", "https://x.duckdns.org/m/")
    return d


def _src(tmp_path, name="clip.MP4", size=1000):
    p = tmp_path / name
    p.write_bytes(os.urandom(size))
    return p


def test_expose_copies_under_unguessable_name_with_real_extension(pub, tmp_path):
    src = _src(tmp_path, "brand_1_mir.MP4")
    url, cleanup = public_media.expose(str(src))
    assert url.startswith("https://x.duckdns.org/m/")
    name = url.rsplit("/", 1)[1]
    assert name.endswith(".mp4")            # lower-cased real extension
    assert "mir" not in name and len(name) > 20
    assert (pub / name).read_bytes() == src.read_bytes()   # full copy, same size
    assert src.exists()                     # the source is left alone


def test_two_exposes_never_collide(pub, tmp_path):
    src = _src(tmp_path)
    u1, c1 = public_media.expose(str(src))
    u2, c2 = public_media.expose(str(src))
    assert u1 != u2
    c1()
    c2()


def test_cleanup_deletes_and_is_idempotent(pub, tmp_path):
    url, cleanup = public_media.expose(str(_src(tmp_path)))
    name = url.rsplit("/", 1)[1]
    assert (pub / name).exists()
    cleanup()
    assert not (pub / name).exists()
    cleanup()   # second call must not raise


def test_base_url_without_trailing_slash_still_joins(pub, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_MEDIA_BASE_URL", "https://x.duckdns.org/m")
    url, cleanup = public_media.expose(str(_src(tmp_path)))
    assert url.startswith("https://x.duckdns.org/m/")
    assert "//" not in url.split("://", 1)[1]
    cleanup()


def test_missing_source_raises(pub, tmp_path):
    with pytest.raises(FileNotFoundError):
        public_media.expose(str(tmp_path / "nope.jpg"))


def test_empty_source_is_refused(pub, tmp_path):
    with pytest.raises(ValueError):
        public_media.expose(str(_src(tmp_path, size=0)))


def test_unconfigured_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_MEDIA_DIR", "")
    monkeypatch.setattr(config, "PUBLIC_MEDIA_BASE_URL", "")
    with pytest.raises(public_media.NotConfigured) as exc:
        public_media.expose(str(_src(tmp_path)))
    assert "PUBLIC_MEDIA_DIR" in str(exc.value)
    assert "PUBLIC_MEDIA_BASE_URL" in str(exc.value)


def test_sweep_removes_only_stale_files(pub, tmp_path):
    old = pub / "old.mp4"
    old.write_bytes(b"x")
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    fresh = pub / "fresh.mp4"
    fresh.write_bytes(b"x")
    assert public_media.sweep(max_age_s=3600) == 1
    assert not old.exists() and fresh.exists()


def test_sweep_is_a_noop_when_unconfigured(monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_MEDIA_DIR", "")
    assert public_media.sweep() == 0
