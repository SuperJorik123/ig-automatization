"""modules/newsroom/wp.py — the WordPress REST poller.

Offline: requests.get is replaced everywhere. The fixtures mirror real
wp/v2/posts payloads, including the shapes that make the naive path crash —
a missing _embedded, an embed that resolved to an error object, and an
excerpt-only post.
"""

import pytest
import requests

from modules.newsroom import wp

SITE = {"name": "acme", "wp_base": "https://acme.test/wp-json/wp/v2",
        "chat_id": "@acme"}


class FakeResponse:
    def __init__(self, payload=None, status=200, text=None, bad_json=False):
        self._payload = payload
        self.status_code = status
        self._bad_json = bad_json
        self.text = text if text is not None else "body"

    def json(self):
        if self._bad_json:
            raise ValueError("no json")
        return self._payload


def _post(wp_id=1, **over):
    p = {
        "id": wp_id,
        "link": f"https://acme.test/story-{wp_id}",
        "date_gmt": "2026-08-12T09:30:00",
        "title": {"rendered": f"Story {wp_id}"},
        "content": {"rendered": f"<p>Body {wp_id}</p>"},
        "excerpt": {"rendered": "<p>Short</p>"},
        "_embedded": {"wp:featuredmedia": [
            {"source_url": "https://acme.test/img.jpg", "mime_type": "image/jpeg",
             "media_type": "image"}
        ]},
    }
    p.update(over)
    return p


@pytest.fixture
def get(monkeypatch):
    """Records the outbound call and returns a canned response."""
    calls = []

    def fake_get(url, **kw):
        calls.append({"url": url, **kw})
        return fake_get.response

    fake_get.response = FakeResponse([_post()])
    fake_get.calls = calls
    monkeypatch.setattr(wp.requests, "get", fake_get)
    return fake_get


# --------------------------------------------------------------------------- #
# clean_html                                                                  #
# --------------------------------------------------------------------------- #


def test_entities_are_unescaped():
    assert wp.clean_html("<p>It&#8217;s here &amp; now</p>") == "It’s here & now"


def test_tags_are_stripped_and_paragraphs_kept():
    got = wp.clean_html("<p>One</p><p>Two</p>")

    assert got == "One\n\nTwo"


def test_br_becomes_a_single_newline():
    assert wp.clean_html("a<br>b") == "a\nb"


def test_script_and_style_content_is_dropped():
    # Unstripped, a stylesheet would be handed to the model as article prose.
    got = wp.clean_html("<p>Real</p><script>var x=1;</script><style>.a{}</style>")

    assert got == "Real"


def test_figcaption_is_dropped():
    got = wp.clean_html("<figure><img><figcaption>Photo: AFP</figcaption></figure><p>Text</p>")

    assert "AFP" not in got
    assert "Text" in got


def test_nbsp_becomes_a_normal_space():
    assert wp.clean_html("a&nbsp;b") == "a b"


def test_escaped_markup_in_the_copy_survives():
    # Unescaping before stripping would turn this into a tag and eat it.
    got = wp.clean_html("<p>Use &lt;script&gt; carefully</p>")

    assert got == "Use <script> carefully"


def test_blank_runs_are_collapsed():
    got = wp.clean_html("<p>A</p><div></div><div></div><p>B</p>")

    assert "\n\n\n" not in got


def test_empty_input_is_empty_output():
    assert wp.clean_html("") == ""
    assert wp.clean_html(None) == ""


# --------------------------------------------------------------------------- #
# normalise                                                                   #
# --------------------------------------------------------------------------- #


def test_normalise_extracts_the_featured_image():
    got = wp.normalise(_post())

    assert got["media_url"] == "https://acme.test/img.jpg"
    assert got["media_type"] == "photo"


def test_normalise_detects_video_media():
    got = wp.normalise(_post(_embedded={"wp:featuredmedia": [
        {"source_url": "https://acme.test/clip.mp4", "mime_type": "video/mp4",
         "media_type": "file"}]}))

    assert got["media_type"] == "video"


@pytest.mark.parametrize("embedded", [
    {},                                              # no _embedded at all
    {"wp:featuredmedia": []},                        # key present, list empty
    {"wp:featuredmedia": [{}]},                      # entry with no source_url
    {"wp:featuredmedia": [{"code": "rest_forbidden"}]},  # embed failed to resolve
    {"wp:featuredmedia": ["not-a-dict"]},
    {"wp:featuredmedia": None},
])
def test_missing_media_is_not_an_error(embedded):
    # An article with no usable featured media is ordinary: it becomes a
    # text-only Telegram post.
    got = wp.normalise(_post(_embedded=embedded))

    assert got["media_url"] is None
    assert got["media_type"] is None
    assert got["title"] == "Story 1"


def test_media_of_an_unknown_type_is_ignored():
    got = wp.normalise(_post(_embedded={"wp:featuredmedia": [
        {"source_url": "https://acme.test/doc.pdf", "mime_type": "application/pdf"}]}))

    assert got["media_url"] is None


def test_date_gmt_gets_an_explicit_utc_offset():
    # WordPress omits the offset on date_gmt; without it, ordering against
    # anything timezone-aware is wrong.
    got = wp.normalise(_post(date_gmt="2026-08-12T09:30:00"))

    assert got["published_at"] == "2026-08-12T09:30:00+00:00"


def test_existing_offset_is_left_alone():
    got = wp.normalise(_post(date_gmt="2026-08-12T09:30:00Z"))

    assert got["published_at"] == "2026-08-12T09:30:00Z"


def test_empty_content_falls_back_to_the_excerpt():
    # Some plugins empty content.rendered for anonymous readers.
    got = wp.normalise(_post(content={"rendered": ""},
                             excerpt={"rendered": "<p>Only the excerpt</p>"}))

    assert got["body"] == "Only the excerpt"


def test_missing_content_key_entirely():
    post = _post()
    del post["content"]

    assert wp.normalise(post)["body"] == "Short"


# --------------------------------------------------------------------------- #
# fetch_recent                                                                #
# --------------------------------------------------------------------------- #


def test_fetch_recent_requests_published_posts_with_embeds(get):
    wp.fetch_recent(SITE)

    (call,) = get.calls
    assert call["url"] == "https://acme.test/wp-json/wp/v2/posts"
    assert call["params"]["status"] == "publish"
    assert call["params"]["_embed"] == "1"
    assert call["params"]["order"] == "desc"


def test_fetch_recent_sends_no_date_watermark(get):
    # A watermark permanently loses backdated and scheduled posts; the whole
    # design depends on refetching and filtering by id instead.
    wp.fetch_recent(SITE)

    (call,) = get.calls
    assert "after" not in call["params"]
    assert "before" not in call["params"]


def test_fetch_recent_returns_normalised_articles(get):
    get.response = FakeResponse([_post(1), _post(2)])

    got = wp.fetch_recent(SITE)

    assert [a["wp_id"] for a in got] == [1, 2]
    assert got[0]["url"] == "https://acme.test/story-1"


def test_http_error_raises_runtime_error(get):
    # A REST API disabled by a security plugin answers 403 — one site being
    # down must be skippable, not fatal.
    get.response = FakeResponse(status=403, text="forbidden")

    with pytest.raises(RuntimeError, match="403"):
        wp.fetch_recent(SITE)


def test_non_json_body_raises_runtime_error(get, monkeypatch):
    monkeypatch.setattr(wp, "JSON_RETRY_DELAY_S", 0)
    get.response = FakeResponse(bad_json=True)

    with pytest.raises(RuntimeError, match="non-JSON"):
        wp.fetch_recent(SITE)


class SequencedGet:
    """requests.get that answers from a list, one response per call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.count = 0

    def __call__(self, url, params=None, timeout=None, headers=None):
        self.count += 1
        return self.responses.pop(0)


@pytest.fixture
def no_retry_wait(monkeypatch):
    monkeypatch.setattr(wp, "JSON_RETRY_DELAY_S", 0)


def test_non_json_body_is_retried_and_the_second_answer_used(monkeypatch, no_retry_wait):
    # abudhabitimes, 2026-11: PHP notices printed ahead of the JSON on an
    # uncached render, while the request right after it comes back clean.
    fake = SequencedGet(FakeResponse(bad_json=True, text="<br />\n<b>Deprecated</b>"),
                        FakeResponse([_post(7)]))
    monkeypatch.setattr(wp.requests, "get", fake)

    got = wp.fetch_recent(SITE)

    assert [a["wp_id"] for a in got] == [7]
    assert fake.count == 2


def test_non_json_twice_raises_and_shows_what_the_site_sent(monkeypatch, no_retry_wait):
    fake = SequencedGet(FakeResponse(bad_json=True, text="<b>Deprecated</b> one"),
                        FakeResponse(bad_json=True, text="<b>Deprecated</b> two"))
    monkeypatch.setattr(wp.requests, "get", fake)

    with pytest.raises(RuntimeError, match="non-JSON.*Deprecated"):
        wp.fetch_recent(SITE)
    assert fake.count == 2


def test_http_error_is_not_retried(monkeypatch, no_retry_wait):
    # The retry is for a garbled 200. Hammering a 403/500 helps nobody.
    fake = SequencedGet(FakeResponse(status=500, text="boom"))
    monkeypatch.setattr(wp.requests, "get", fake)

    with pytest.raises(RuntimeError, match="500"):
        wp.fetch_recent(SITE)
    assert fake.count == 1


def test_wordpress_error_object_raises_runtime_error(get):
    # WordPress reports rest_no_route as a JSON object, not an HTTP error.
    get.response = FakeResponse({"code": "rest_no_route", "message": "No route"})

    with pytest.raises(RuntimeError, match="unexpected payload"):
        wp.fetch_recent(SITE)


def test_network_failure_raises_runtime_error(monkeypatch):
    def boom(*a, **kw):
        raise requests.ConnectionError("dns")

    monkeypatch.setattr(wp.requests, "get", boom)

    with pytest.raises(RuntimeError, match="request failed"):
        wp.fetch_recent(SITE)


def test_one_malformed_post_does_not_lose_the_others(get):
    get.response = FakeResponse([_post(1), {"no": "id"}, _post(3)])

    got = wp.fetch_recent(SITE)

    assert [a["wp_id"] for a in got] == [1, 3]


def test_empty_site_returns_empty_list(get):
    get.response = FakeResponse([])

    assert wp.fetch_recent(SITE) == []
