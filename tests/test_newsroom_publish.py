"""modules/newsroom/publish.py — composing and sending one post.

Offline: the bot is a fake recording object. The cases that matter are the
length arithmetic around the article link (step 6 of the client's flow fails
silently if the link is truncated) and the degradation paths.
"""

import asyncio

import pytest

from modules.newsroom import publish


def run(coro):
    """Drive one coroutine to completion. The repo does not depend on
    pytest-asyncio — tests/test_autopilot.py and test_cleanup.py use the same
    plain asyncio.run."""
    return asyncio.run(coro)


class FakeMessage:
    def __init__(self, message_id=412, link="https://t.me/acme/412"):
        self.message_id = message_id
        self.link = link


class FakeBot:
    """Records calls; each send_* can be told to raise."""

    def __init__(self):
        self.calls = []
        self.fail = set()
        self.message = FakeMessage()

    async def _record(self, kind, **kw):
        self.calls.append({"kind": kind, **kw})
        if kind in self.fail:
            raise RuntimeError(f"{kind} rejected")
        return self.message

    async def send_message(self, **kw):
        return await self._record("message", **kw)

    async def send_photo(self, **kw):
        return await self._record("photo", **kw)

    async def send_video(self, **kw):
        return await self._record("video", **kw)


SITE = {"name": "acme", "chat_id": "@acme"}
URL = "https://acme.test/story-1"


def _article(**over):
    a = {"wp_id": 1, "url": URL, "title": "T", "body": "B",
         "media_url": "https://acme.test/img.jpg", "media_type": "photo"}
    a.update(over)
    return a


@pytest.fixture(autouse=True)
def live(monkeypatch):
    """Default every test to live mode; the dry-run tests opt back in."""
    monkeypatch.setattr(publish.config, "NR_DRY_RUN", False)


@pytest.fixture
def bot():
    return FakeBot()


# --------------------------------------------------------------------------- #
# compose                                                                     #
# --------------------------------------------------------------------------- #


def test_link_is_appended_after_the_body():
    got = publish.compose("The news.", URL, has_media=True)

    assert got == f"The news.{publish.LINK_PREFIX}{URL}"


def test_no_url_leaves_the_body_alone():
    assert publish.compose("The news.", "", has_media=True) == "The news."


def test_long_body_with_media_is_trimmed_to_the_caption_limit():
    got = publish.compose("x" * 2000, URL, has_media=True)

    assert len(got) <= publish.CAPTION_LIMIT


def test_the_link_survives_the_trim():
    # The failure this prevents: a truncated link is a broken link on a post
    # that otherwise looks like it shipped fine.
    got = publish.compose("x" * 2000, URL, has_media=True)

    assert got.endswith(URL)


def test_trimmed_body_is_marked_with_an_ellipsis():
    got = publish.compose("x" * 2000, URL, has_media=True)

    assert "…" in got


def test_text_only_posts_get_the_larger_limit():
    got = publish.compose("x" * 2000, URL, has_media=False)

    assert len(got) > publish.CAPTION_LIMIT
    assert got.endswith(URL)


def test_text_only_post_is_still_capped_at_the_message_limit():
    got = publish.compose("x" * 9000, URL, has_media=False)

    assert len(got) <= publish.TEXT_LIMIT
    assert got.endswith(URL)


def test_a_body_that_exactly_fits_is_not_trimmed():
    room = publish.CAPTION_LIMIT - len(publish.LINK_PREFIX) - len(URL)
    got = publish.compose("x" * room, URL, has_media=True)

    assert "…" not in got
    assert len(got) == publish.CAPTION_LIMIT


def test_absurdly_long_url_yields_the_link_alone():
    # Pathological, but the link is the point of the post, so it is what wins.
    long_url = "https://acme.test/" + "s" * 1200
    got = publish.compose("The news.", long_url, has_media=True)

    assert long_url in got


def test_whitespace_only_body_still_carries_the_link():
    got = publish.compose("   \n  ", URL, has_media=True)

    assert URL in got


# --------------------------------------------------------------------------- #
# publish                                                                     #
# --------------------------------------------------------------------------- #


def test_photo_article_is_sent_as_a_photo(bot):
    mid, link = run(publish.publish(bot, SITE, _article(), "The news."))

    (call,) = bot.calls
    assert call["kind"] == "photo"
    assert call["photo"] == "https://acme.test/img.jpg"
    assert call["chat_id"] == "@acme"
    assert URL in call["caption"]
    assert (mid, link) == (412, "https://t.me/acme/412")


def test_video_article_is_sent_as_a_video(bot):
    run(publish.publish(bot, SITE, _article(media_url="https://acme.test/c.mp4",
                                              media_type="video"), "The news."))

    assert bot.calls[0]["kind"] == "video"
    assert bot.calls[0]["video"] == "https://acme.test/c.mp4"


def test_article_without_media_is_sent_as_a_message(bot):
    run(publish.publish(bot, SITE, _article(media_url=None, media_type=None),
                          "The news."))

    assert bot.calls[0]["kind"] == "message"
    assert URL in bot.calls[0]["text"]


def test_unknown_media_type_falls_back_to_text(bot):
    run(publish.publish(bot, SITE, _article(media_type="audio"), "The news."))

    assert bot.calls[0]["kind"] == "message"


def test_media_rejection_retries_as_text(bot):
    # Telegram refuses a remote URL often enough to matter: a slow origin, a
    # file past its URL-fetch ceiling, a hotlink rule. The news still ships.
    bot.fail = {"photo"}

    mid, link = run(publish.publish(bot, SITE, _article(), "The news."))

    assert [c["kind"] for c in bot.calls] == ["photo", "message"]
    assert mid == 412


def test_text_retry_uses_the_larger_limit(bot):
    # Dropping the image lifts the cap from 1024 to 4096, so the retry should
    # not still be carrying a caption-sized body.
    bot.fail = {"photo"}

    run(publish.publish(bot, SITE, _article(), "x" * 2000))

    assert len(bot.calls[1]["text"]) > publish.CAPTION_LIMIT


def test_total_failure_returns_nothing_and_does_not_raise(bot):
    bot.fail = {"photo", "message"}

    assert run(publish.publish(bot, SITE, _article(), "The news.")) == (None, None)


def test_text_only_failure_does_not_retry(bot):
    bot.fail = {"message"}

    assert run(publish.publish(bot, SITE, _article(media_url=None), "x")) == (None, None)
    assert len(bot.calls) == 1


def test_message_without_a_public_link_still_returns_its_id(bot):
    # A private channel with no username yields no .link; the post counts but
    # nothing can be ordered against it.
    bot.message = FakeMessage(message_id=9, link=None)

    assert run(publish.publish(bot, SITE, _article(), "The news.")) == (9, None)


# --------------------------------------------------------------------------- #
# Dry run                                                                     #
# --------------------------------------------------------------------------- #


def test_dry_run_sends_nothing(bot, monkeypatch):
    monkeypatch.setattr(publish.config, "NR_DRY_RUN", True)

    assert run(publish.publish(bot, SITE, _article(), "The news.")) == (None, None)
    assert bot.calls == []


def test_dry_run_logs_the_exact_text(bot, monkeypatch, caplog):
    monkeypatch.setattr(publish.config, "NR_DRY_RUN", True)

    with caplog.at_level("INFO"):
        run(publish.publish(bot, SITE, _article(), "The news."))

    assert URL in caplog.text
    assert "@acme" in caplog.text
