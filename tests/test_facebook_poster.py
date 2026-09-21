"""Offline tests for modules/facebook/poster.py — no Facebook calls.

requests.request / requests.post are replaced by scripted fakes so the four
publish paths, the Reels three-phase sequence and the error contract can be
exercised without a token or a Page.
"""

import json

import pytest

from modules.facebook import poster


# --------------------------------------------------------------------------- #
# fakes                                                                        #
# --------------------------------------------------------------------------- #


class FakeResp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("FACEBOOK_ACME_ACCESS_TOKEN", "EAAtoken")
    monkeypatch.setenv("FACEBOOK_ACME_PAGE_ID", "123")
    return "acme"


class Recorder(list):
    """The recorded calls, carrying the scripted reply queue (and, for reels,
    the rupload POSTs) as attributes — a plain list takes no attributes."""
    replies: list
    uploads: list


@pytest.fixture
def calls(monkeypatch):
    """Records every requests.request call and replies from a scripted queue."""
    recorded, replies = Recorder(), []

    def fake_request(method, url, params=None, data=None, files=None,
                     timeout=None):
        recorded.append({"method": method, "url": url, "params": params or {},
                         "data": data or {}, "files": files})
        return FakeResp(replies.pop(0) if replies else {"id": "1"})

    monkeypatch.setattr(poster.requests, "request", fake_request)
    recorded.replies = replies
    return recorded


# --------------------------------------------------------------------------- #
# env-prefix rule (same as the Twitter and Instagram posters) + creds          #
# --------------------------------------------------------------------------- #


def test_env_prefix_uppercases_and_replaces_non_alnum():
    assert poster._env_prefix("frontiva24") == "FACEBOOK_FRONTIVA24_"
    assert poster._env_prefix("my.brand-2") == "FACEBOOK_MY_BRAND_2_"


def test_missing_token_is_a_failed_result_not_an_exception(monkeypatch):
    monkeypatch.delenv("FACEBOOK_NOPE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("FACEBOOK_NOPE_PAGE_ID", raising=False)
    out = poster.publish_text("hi", "nope")
    assert out["status"] == "failed"
    assert "FACEBOOK_NOPE_ACCESS_TOKEN" in out["error"]


def test_missing_page_id_is_named(monkeypatch):
    monkeypatch.setenv("FACEBOOK_HALF_ACCESS_TOKEN", "EAAtoken")
    monkeypatch.delenv("FACEBOOK_HALF_PAGE_ID", raising=False)
    out = poster.publish_photo("https://x/a.jpg", "hi", "half")
    assert out["status"] == "failed"
    assert "FACEBOOK_HALF_PAGE_ID" in out["error"]


# --------------------------------------------------------------------------- #
# message trim                                                                 #
# --------------------------------------------------------------------------- #


def test_short_message_untouched():
    assert poster.trim_message("hello") == "hello"


def test_long_message_cut_on_word_boundary_with_ellipsis():
    out = poster.trim_message(" ".join(["word"] * 40000))
    assert len(out) <= poster.MESSAGE_MAX
    assert out.endswith("…") and not out.endswith("wor…")


def test_one_giant_token_is_hard_cut():
    out = poster.trim_message("x" * (poster.MESSAGE_MAX + 500))
    assert len(out) == poster.MESSAGE_MAX and out.endswith("…")


# --------------------------------------------------------------------------- #
# text / photo / video                                                         #
# --------------------------------------------------------------------------- #


def test_text_posts_to_feed(creds, calls):
    calls.replies.append({"id": "123_456"})
    out = poster.publish_text("hello", creds)
    assert out == {"account": "acme", "id": "123_456", "status": "success"}
    assert calls[0]["url"].endswith("/123/feed")
    assert calls[0]["data"]["message"] == "hello"


def test_photo_url_goes_to_photos_edge_as_url(creds, calls):
    calls.replies.append({"id": "9", "post_id": "123_9"})
    out = poster.publish_photo("https://x/a.jpg", "cap", creds)
    assert out["status"] == "success"
    # post_id wins over id — it is what addresses the story and what DELETE takes
    assert out["id"] == "123_9"
    assert calls[0]["url"].endswith("/123/photos")
    assert calls[0]["data"]["url"] == "https://x/a.jpg"
    assert calls[0]["data"]["caption"] == "cap"
    assert calls[0]["files"] is None


def test_video_url_uses_file_url_and_description(creds, calls):
    calls.replies.append({"id": "7"})
    out = poster.publish_video("https://x/a.mp4", "cap", creds)
    assert out["status"] == "success" and out["id"] == "7"
    assert calls[0]["url"].endswith("/123/videos")
    assert calls[0]["data"]["file_url"] == "https://x/a.mp4"
    assert calls[0]["data"]["description"] == "cap"


def test_local_photo_is_uploaded_not_fetched(creds, calls, tmp_path):
    p = tmp_path / "card.jpg"
    p.write_bytes(b"jpegbytes")
    calls.replies.append({"id": "9", "post_id": "123_9"})
    out = poster.publish_photo(str(p), "cap", creds)
    assert out["status"] == "success"
    assert calls[0]["files"] is not None       # multipart, not a URL fetch
    assert "url" not in calls[0]["data"]


def test_draft_sends_published_false(creds, calls):
    poster.publish_photo("https://x/a.jpg", "cap", creds, published=False)
    assert calls[0]["data"]["published"] == "false"


def test_published_true_omits_the_field(creds, calls):
    poster.publish_photo("https://x/a.jpg", "cap", creds)
    assert "published" not in calls[0]["data"]


# --------------------------------------------------------------------------- #
# error contract                                                               #
# --------------------------------------------------------------------------- #


def test_api_error_body_becomes_a_failed_result(creds, monkeypatch):
    def fake_request(method, url, **kw):
        return FakeResp({"error": {"message": "Unable to fetch video file "
                                              "from URL",
                                   "code": 389, "error_subcode": 1363057}},
                        status=400)
    monkeypatch.setattr(poster.requests, "request", fake_request)
    out = poster.publish_video("https://x/a.mp4", "cap", creds)
    assert out["status"] == "failed"
    assert "389" in out["error"] and "1363057" in out["error"]


def test_transport_failure_becomes_a_failed_result(creds, monkeypatch):
    def boom(*a, **kw):
        raise poster.requests.RequestException("connection reset")
    monkeypatch.setattr(poster.requests, "request", boom)
    out = poster.publish_text("hi", creds)
    assert out["status"] == "failed" and "connection reset" in out["error"]


def test_non_json_reply_becomes_a_failed_result(creds, monkeypatch):
    monkeypatch.setattr(poster.requests, "request",
                        lambda *a, **kw: FakeResp("<html>502</html>", status=502))
    out = poster.publish_text("hi", creds)
    assert out["status"] == "failed" and "non-JSON" in out["error"]


def test_success_body_without_an_id_is_a_failure(creds, calls):
    calls.replies.append({"ok": True})
    out = poster.publish_text("hi", creds)
    assert out["status"] == "failed" and "no id" in out["error"]


# --------------------------------------------------------------------------- #
# reels                                                                        #
# --------------------------------------------------------------------------- #


@pytest.fixture
def reel_calls(monkeypatch, calls):
    """calls covers the Graph legs; this adds the rupload POST."""
    uploads = []

    def fake_post(url, headers=None, data=None, timeout=None):
        uploads.append({"url": url, "headers": headers or {}})
        return FakeResp({"success": True})

    monkeypatch.setattr(poster.requests, "post", fake_post)
    monkeypatch.setattr(poster.time, "sleep", lambda s: None)
    calls.uploads = uploads
    return calls


def test_reel_runs_start_upload_finish_in_order(creds, reel_calls, tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"0" * 2048)
    reel_calls.replies.extend([
        {"video_id": "v1", "upload_url": "https://rupload.facebook.com/x/v1"},
        {"success": True},                                   # finish
        {"status": {"video_status": "ready"}},               # status poll
    ])
    out = poster.publish_reel(str(clip), "cap", creds)
    assert out == {"account": "acme", "id": "v1", "status": "success"}

    start, finish, status = reel_calls[0], reel_calls[1], reel_calls[2]
    assert start["data"]["upload_phase"] == "start"
    assert finish["data"]["upload_phase"] == "finish"
    assert finish["data"]["video_id"] == "v1"
    assert finish["data"]["video_state"] == "PUBLISHED"
    assert finish["data"]["description"] == "cap"
    assert status["method"] == "GET"

    # the bytes went to the one-off host the start phase named, with the
    # OAuth header and the size — not to graph.facebook.com
    assert reel_calls.uploads[0]["url"] == "https://rupload.facebook.com/x/v1"
    assert reel_calls.uploads[0]["headers"]["Authorization"] == "OAuth EAAtoken"
    assert reel_calls.uploads[0]["headers"]["file_size"] == "2048"
    assert reel_calls.uploads[0]["headers"]["offset"] == "0"


def test_reel_refuses_a_url(creds, reel_calls):
    out = poster.publish_reel("https://x/a.mp4", "cap", creds)
    assert out["status"] == "failed"
    assert "local file" in out["error"]
    assert not reel_calls                    # nothing was called


def test_reel_start_without_an_upload_url_fails(creds, reel_calls, tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"0")
    reel_calls.replies.append({"video_id": "v1"})     # no upload_url
    out = poster.publish_reel(str(clip), "cap", creds)
    assert out["status"] == "failed" and "upload target" in out["error"]


def test_reel_processing_error_is_a_failure(creds, reel_calls, tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"0")
    reel_calls.replies.extend([
        {"video_id": "v1", "upload_url": "https://rupload.facebook.com/x/v1"},
        {"success": True},
        {"status": {"video_status": "error"}},
    ])
    out = poster.publish_reel(str(clip), "cap", creds)
    assert out["status"] == "failed" and "processing failed" in out["error"]


def test_unreadable_status_does_not_fail_a_committed_reel(creds, reel_calls,
                                                          tmp_path):
    """The upload is committed once finish returns, so a status call that
    errors must not turn a live Reel into a reported failure."""
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"0")
    reel_calls.replies.extend([
        {"video_id": "v1", "upload_url": "https://rupload.facebook.com/x/v1"},
        {"success": True},
        {"error": {"message": "temporarily unavailable", "code": 1}},
    ])
    out = poster.publish_reel(str(clip), "cap", creds)
    assert out["status"] == "success" and out["id"] == "v1"


# --------------------------------------------------------------------------- #
# dispatch                                                                     #
# --------------------------------------------------------------------------- #


def test_post_media_sends_photos_to_the_photo_edge(creds, calls, tmp_path):
    p = tmp_path / "a.jpg"
    p.write_bytes(b"x")
    calls.replies.append({"id": "1", "post_id": "123_1"})
    poster.post_media(str(p), "cap", creds)
    assert calls[0]["url"].endswith("/photos")


def test_post_media_defaults_video_to_a_reel(creds, reel_calls, tmp_path):
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"x")
    reel_calls.replies.extend([
        {"video_id": "v1", "upload_url": "https://rupload.facebook.com/x/v1"},
        {"success": True},
        {"status": {"video_status": "ready"}},
    ])
    poster.post_media(str(clip), "cap", creds)
    assert reel_calls[0]["url"].endswith("/video_reels")


def test_post_media_as_reel_false_uses_the_feed(creds, calls, tmp_path):
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"x")
    calls.replies.append({"id": "1"})
    poster.post_media(str(clip), "cap", creds, as_reel=False)
    assert calls[0]["url"].endswith("/videos")


def test_unsupported_extension_is_a_failed_result(creds, calls):
    out = poster.post_media("notes.txt", "cap", creds)
    assert out["status"] == "failed" and ".txt" in out["error"]
    assert not calls


def test_upload_post_wrapper_joins_caption_and_tags(creds, calls, tmp_path):
    p = tmp_path / "a.jpg"
    p.write_bytes(b"x")
    calls.replies.append({"id": "1", "post_id": "123_1"})
    out = poster.upload_post(None, str(p), "body", "#a #b",
                             kind="post", target_account=creds)
    assert out["status"] == "success"
    assert calls[0]["data"]["caption"] == "body\n\n#a #b"
