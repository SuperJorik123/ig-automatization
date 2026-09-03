"""Offline tests for modules/instagram/graph.py — no Instagram calls.

requests.request is replaced by a scripted fake so the container -> poll ->
publish sequence, the error paths and the .env rewrite can be exercised
without a token or a public URL.
"""

import datetime as dt
import os

import pytest

from modules.instagram import graph


# --------------------------------------------------------------------------- #
# env-prefix rule (same as modules/twitter/poster.py) + creds                  #
# --------------------------------------------------------------------------- #


def test_env_prefix_uppercases_and_replaces_non_alnum():
    assert graph._env_prefix("wswiremedia") == "IG_GRAPH_WSWIREMEDIA_"
    assert graph._env_prefix("my.brand-2") == "IG_GRAPH_MY_BRAND_2_"


def test_missing_creds_is_a_failed_result_not_an_exception(monkeypatch):
    monkeypatch.delenv("IG_GRAPH_NOPE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("IG_GRAPH_NOPE_USER_ID", raising=False)
    out = graph.publish_photo("https://x/a.jpg", "hi", "nope")
    assert out["status"] == "failed"
    assert "IG_GRAPH_NOPE_ACCESS_TOKEN" in out["error"]


# --------------------------------------------------------------------------- #
# caption trim                                                                #
# --------------------------------------------------------------------------- #


def test_short_caption_untouched():
    assert graph.trim_caption("hello") == "hello"


def test_long_caption_cut_on_word_boundary_with_ellipsis():
    text = " ".join(["word"] * 1000)
    out = graph.trim_caption(text)
    assert len(out) <= graph.CAPTION_MAX
    assert out.endswith("…")
    assert not out.endswith("wor…")   # no split word


def test_one_giant_token_is_hard_cut():
    out = graph.trim_caption("x" * 5000)
    assert len(out) == graph.CAPTION_MAX and out.endswith("…")


# --------------------------------------------------------------------------- #
# scripted HTTP fake                                                          #
# --------------------------------------------------------------------------- #


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload


class FakeHTTP:
    """Records every call; answers from a queue, in order."""

    def __init__(self):
        self.calls = []
        self.queue = []

    def __call__(self, method, url, params=None, data=None, timeout=None):
        self.calls.append((method, url, dict(params or {}), dict(data or {})))
        if not self.queue:
            raise AssertionError(f"unexpected call {method} {url}")
        return self.queue.pop(0)


@pytest.fixture
def http(monkeypatch):
    fake = FakeHTTP()
    monkeypatch.setattr(graph.requests, "request", fake)
    monkeypatch.setattr(graph.time, "sleep", lambda s: None)
    monkeypatch.setenv("IG_GRAPH_ACME_ACCESS_TOKEN", "IGAAtoken")
    monkeypatch.setenv("IG_GRAPH_ACME_USER_ID", "1784100")
    return fake


# --------------------------------------------------------------------------- #
# publish_reel: container -> poll -> publish                                  #
# --------------------------------------------------------------------------- #


def test_reel_publishes_after_n_polls(http):
    http.queue = [
        _Resp({"id": "C1"}),                          # POST /media
        _Resp({"status_code": "IN_PROGRESS", "id": "C1"}),
        _Resp({"status_code": "IN_PROGRESS", "id": "C1"}),
        _Resp({"status_code": "FINISHED", "id": "C1"}),
        _Resp({"id": "P9"}),                          # POST /media_publish
    ]
    out = graph.publish_reel("https://h/clip.mp4", "cap", "acme")
    assert out == {"account": "acme", "id": "P9", "status": "success"}

    m, url, params, data = http.calls[0]
    assert (m, url) == ("POST", f"{graph.API}/1784100/media")
    assert data["media_type"] == "REELS"
    assert data["video_url"] == "https://h/clip.mp4"
    assert data["caption"] == "cap"
    assert data["access_token"] == "IGAAtoken"

    polls = [c for c in http.calls if c[0] == "GET"]
    assert len(polls) == 3
    assert polls[0][1] == f"{graph.API}/C1"
    assert "status_code" in polls[0][2]["fields"]

    m, url, params, data = http.calls[-1]
    assert (m, url) == ("POST", f"{graph.API}/1784100/media_publish")
    assert data["creation_id"] == "C1"


def test_reel_error_status_is_a_failed_result(http):
    http.queue = [
        _Resp({"id": "C1"}),
        _Resp({"status_code": "ERROR", "status": "Error: Media type not supported"}),
    ]
    out = graph.publish_reel("https://h/clip.mp4", "cap", "acme")
    assert out["status"] == "failed"
    assert "Media type not supported" in out["error"]
    assert not [c for c in http.calls if c[1].endswith("/media_publish")]


def test_reel_poll_timeout_is_a_failed_result(http, monkeypatch):
    monkeypatch.setattr(graph, "POLL_TIMEOUT_S", 0)
    http.queue = [_Resp({"id": "C1"}), _Resp({"status_code": "IN_PROGRESS"})]
    out = graph.publish_reel("https://h/clip.mp4", "cap", "acme")
    assert out["status"] == "failed" and "timed out" in out["error"].lower()


def test_graph_error_body_is_surfaced(http):
    http.queue = [_Resp({"error": {"message": "Invalid OAuth access token.",
                                   "code": 190}}, status=400)]
    out = graph.publish_reel("https://h/clip.mp4", "cap", "acme")
    assert out["status"] == "failed"
    assert "Invalid OAuth access token" in out["error"]


# --------------------------------------------------------------------------- #
# publish_photo                                                               #
# --------------------------------------------------------------------------- #


def test_photo_uses_image_url_and_trims_caption(http):
    http.queue = [
        _Resp({"id": "C2"}),
        _Resp({"status_code": "FINISHED"}),
        _Resp({"id": "P2"}),
    ]
    out = graph.publish_photo("https://h/card.jpg", "w " * 3000, "acme")
    assert out["status"] == "success" and out["id"] == "P2"
    data = http.calls[0][3]
    assert data["image_url"] == "https://h/card.jpg"
    assert "media_type" not in data
    assert len(data["caption"]) <= graph.CAPTION_MAX


# --------------------------------------------------------------------------- #
# whoami                                                                      #
# --------------------------------------------------------------------------- #


def test_whoami_hits_me_on_the_instagram_host(http):
    http.queue = [_Resp({"user_id": "1784100", "username": "acme",
                         "account_type": "BUSINESS"})]
    out = graph.whoami("acme")
    assert out["username"] == "acme"
    m, url, params, _ = http.calls[0]
    assert url == f"{graph.API}/me"
    assert url.startswith("https://graph.instagram.com/")


# --------------------------------------------------------------------------- #
# refresh_token: rewrite the .env line in place                               #
# --------------------------------------------------------------------------- #


def test_refresh_rewrites_token_line_in_place(http, tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("A=1\nIG_GRAPH_ACME_ACCESS_TOKEN=IGAAtoken\nB=2\n", encoding="utf-8")
    http.queue = [_Resp({"access_token": "IGAAnew", "token_type": "bearer",
                         "expires_in": 5183944})]
    out = graph.refresh_token("acme", env_path=str(env))
    assert out["status"] == "success"

    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "A=1" and lines[-1] == "B=2"
    assert "IG_GRAPH_ACME_ACCESS_TOKEN=IGAAnew" in lines
    assert "IG_GRAPH_ACME_ACCESS_TOKEN=IGAAtoken" not in lines
    stamped = [l for l in lines if l.startswith("IG_GRAPH_ACME_TOKEN_REFRESHED=")]
    assert len(stamped) == 1
    # the live process sees the new token too — no restart needed
    assert os.environ["IG_GRAPH_ACME_ACCESS_TOKEN"] == "IGAAnew"

    m, url, params, _ = http.calls[0]
    assert url == "https://graph.instagram.com/refresh_access_token"
    assert params["grant_type"] == "ig_refresh_token"
    assert params["access_token"] == "IGAAtoken"


def test_refresh_replaces_existing_stamp_line(http, tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("IG_GRAPH_ACME_ACCESS_TOKEN=IGAAtoken\n"
                   "IG_GRAPH_ACME_TOKEN_REFRESHED=2020-01-01\n", encoding="utf-8")
    http.queue = [_Resp({"access_token": "IGAAnew", "expires_in": 1})]
    graph.refresh_token("acme", env_path=str(env))
    lines = env.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[1].startswith("IG_GRAPH_ACME_TOKEN_REFRESHED=")
    assert lines[1] != "IG_GRAPH_ACME_TOKEN_REFRESHED=2020-01-01"


def test_refresh_failure_leaves_env_untouched(http, tmp_path):
    env = tmp_path / ".env"
    env.write_text("IG_GRAPH_ACME_ACCESS_TOKEN=IGAAtoken\n", encoding="utf-8")
    http.queue = [_Resp({"error": {"message": "nope", "code": 190}}, status=400)]
    out = graph.refresh_token("acme", env_path=str(env))
    assert out["status"] == "failed" and "nope" in out["error"]
    assert env.read_text(encoding="utf-8") == "IG_GRAPH_ACME_ACCESS_TOKEN=IGAAtoken\n"


# --------------------------------------------------------------------------- #
# token age / refresh_stale                                                   #
# --------------------------------------------------------------------------- #


def test_token_age_reads_stamp(monkeypatch):
    monkeypatch.setenv("IG_GRAPH_ACME_TOKEN_REFRESHED", "2020-01-01")
    assert graph.token_age_days("acme") > 1000
    monkeypatch.delenv("IG_GRAPH_ACME_TOKEN_REFRESHED")
    assert graph.token_age_days("acme") is None


def test_refresh_stale_only_touches_old_tokens(monkeypatch):
    today = dt.date.today().isoformat()
    monkeypatch.setenv("IG_GRAPH_FRESH_TOKEN_REFRESHED", today)
    monkeypatch.setenv("IG_GRAPH_OLD_TOKEN_REFRESHED", "2020-01-01")
    monkeypatch.delenv("IG_GRAPH_UNKNOWN_TOKEN_REFRESHED", raising=False)
    refreshed = []
    monkeypatch.setattr(graph, "refresh_token",
                        lambda a, env_path=None: (refreshed.append(a),
                                                  {"status": "success"})[-1])
    out = graph.refresh_stale(["fresh", "old", "unknown"], max_age_days=7)
    assert refreshed == ["old", "unknown"]   # no stamp = unknown age = refresh
    assert [a for a, _ in out] == ["old", "unknown"]


def test_refresh_keeps_crlf_line_endings(http, tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(b"A=1\r\nIG_GRAPH_ACME_ACCESS_TOKEN=IGAAtoken\r\nB=2\r\n")
    http.queue = [_Resp({"access_token": "IGAAnew", "expires_in": 1})]
    graph.refresh_token("acme", env_path=str(env))
    raw = env.read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    assert raw.count(b"\r\n") == 4
