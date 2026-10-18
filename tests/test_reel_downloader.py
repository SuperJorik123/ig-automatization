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
    monkeypatch.setattr(reel_downloader, "_download_media",
                        lambda url, dest, kind: ("/v.mp4", "vid cap", {}))
    monkeypatch.setattr(reel_downloader, "_download_photos",
                        lambda url, stub: (_ for _ in ()).throw(AssertionError))

    assert reel_downloader.download_any("u", "/s.mp4") == (["/v.mp4"], "vid cap")


def test_download_any_falls_back_to_post_then_photos(monkeypatch):
    calls = []

    def dm(url, dest, kind):
        calls.append(kind)
        raise RuntimeError(f"yt-dlp {kind} failed")

    monkeypatch.setattr(reel_downloader, "_download_media", dm)
    monkeypatch.setattr(reel_downloader, "_download_photos",
                        lambda url, stub: (["/p_1.jpg", "/p_2.jpg"], "photo cap", {}))

    paths, caption = reel_downloader.download_any("u", "/s.mp4")

    assert calls == ["reel", "post"]
    assert paths == ["/p_1.jpg", "/p_2.jpg"] and caption == "photo cap"


def test_download_any_raises_the_first_error_when_all_fail(monkeypatch):
    def dm(url, dest, kind):
        raise RuntimeError(f"yt-dlp {kind} failed")

    monkeypatch.setattr(reel_downloader, "_download_media", dm)
    monkeypatch.setattr(reel_downloader, "_download_photos",
                        lambda url, stub: (_ for _ in ()).throw(
                            RuntimeError("gallery-dl failed")))

    with pytest.raises(RuntimeError, match="yt-dlp reel failed"):
        reel_downloader.download_any("u", "/s.mp4")


# ── the source post ─────────────────────────────────────────────────────────

X_URL = "https://x.com/newsdesk/status/123"
IG_URL = "https://www.instagram.com/reel/AbC123/"


def test_fetch_returns_the_tweet_and_its_author(monkeypatch):
    monkeypatch.setattr(reel_downloader, "_download_media",
                        lambda url, dest, kind: ("/v.mp4", "Teen saves friend",
                                                 {"uploader_id": "newsdesk",
                                                  "uploader": "News Desk"}))
    paths, caption, source = reel_downloader.fetch(X_URL, "/s.mp4")
    assert source == {"platform": "x", "url": X_URL,
                      "text": "Teen saves friend", "author": "newsdesk"}


def test_instagram_author_is_the_username_not_the_full_name():
    meta = {"channel": "clips.daily", "uploader": "Clips Daily",
            "uploader_id": "1234567"}
    assert reel_downloader.source_post(IG_URL, "cap", meta)["author"] == "clips.daily"


def test_a_numeric_id_or_display_name_is_never_an_author():
    meta = {"uploader_id": "1234567", "uploader": "Clips Daily"}
    assert reel_downloader.source_post(IG_URL, "cap", meta)["author"] == ""
    assert reel_downloader.source_post(X_URL, "cap",
                                       {"uploader_id": "two words"})["author"] == ""


def test_gallery_dl_sidecars_give_the_author_too():
    assert reel_downloader.source_post(
        X_URL, "c", {"author": {"name": "newsdesk", "nick": "News"}})["author"] == "newsdesk"
    assert reel_downloader.source_post(
        IG_URL, "c", {"username": "clips.daily"})["author"] == "clips.daily"


def test_a_reddit_source_is_the_thread_context(monkeypatch):
    monkeypatch.setattr(reel_downloader, "reddit_context",
                        lambda url: "Title: Grandma vs thief\nPost text: London.")
    out = reel_downloader.source_post(
        "https://www.reddit.com/r/x/comments/abc", "Grandma vs thief",
        {"uploader": "throwaway1"})
    assert out["platform"] == "reddit"
    assert out["text"] == "Title: Grandma vs thief\nPost text: London."
    assert out["author"] == "throwaway1"


def test_a_reddit_source_falls_back_to_the_title(monkeypatch):
    monkeypatch.setattr(reel_downloader, "reddit_context", lambda url: "")
    out = reel_downloader.source_post(
        "https://redd.it/abc", "Grandma vs thief", {})
    assert out["text"] == "Grandma vs thief"


def test_no_text_is_an_empty_string_never_a_failure():
    out = reel_downloader.source_post(IG_URL, "", None)
    assert out == {"platform": "instagram", "url": IG_URL, "text": "",
                   "author": ""}


def test_download_media_still_returns_two_values(tmp_path, monkeypatch):
    monkeypatch.setattr(reel_downloader.yt_dlp, "YoutubeDL", FakeYDL)
    assert len(reel_downloader.download_media(IG_URL, str(tmp_path / "r.mp4"))) == 2


# ── Reddit ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "https://www.reddit.com/r/aww/comments/90bu6w/heat_index_was_110_degrees/",
    "https://old.reddit.com/r/videos/comments/6rrwyj",
    "https://reddit.com/comments/124pp33",
    "https://www.reddit.com/user/creepyt0es/comments/nip71r/stickers/",
    "https://www.reddit.com/r/nextfuckinglevel/s/AbC123xyz",
    "https://redd.it/90bu6w",
    "https://v.redd.it/zv89llsvexdz",
])
def test_reddit_urls_are_supported(url):
    assert reel_downloader.is_reddit_url(url)
    assert reel_downloader.is_supported_url(f"look at this {url} wow")
    assert not reel_downloader.is_twitter_url(url)
    assert not reel_downloader.is_instagram_url(url)


@pytest.mark.parametrize("url", [
    "https://www.reddit.com/r/aww/",
    "https://www.reddit.com/",
    "https://notreddit.com/r/aww/comments/abc",
])
def test_reddit_non_post_urls_are_not_supported(url):
    assert not reel_downloader.is_reddit_url(url)


def test_reddit_match_captures_the_whole_post_url():
    m = reel_downloader.SUPPORTED_URL_RE.search(
        "x https://www.reddit.com/r/aww/comments/90bu6w/title/?utm=1 y")
    assert m.group(0) == "https://www.reddit.com/r/aww/comments/90bu6w"


def test_reddit_cookies_only_for_reddit_urls_and_only_when_present(tmp_path, monkeypatch):
    jar = tmp_path / "reddit_cookies.txt"
    monkeypatch.setattr(config, "REDDIT_COOKIES_FILE", str(jar))
    assert reel_downloader.reddit_cookies("https://redd.it/abc") is None
    jar.write_text("# Netscape HTTP Cookie File\n")
    assert reel_downloader.reddit_cookies("https://redd.it/abc") == str(jar)
    assert reel_downloader.reddit_cookies("https://x.com/a/status/1") is None


class FakeYDL:
    """yt_dlp.YoutubeDL stand-in that records its options."""
    opts = None

    def __init__(self, opts):
        FakeYDL.opts = opts
        self.path = opts["outtmpl"].replace("%(ext)s", "mp4")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download):
        open(self.path, "wb").close()
        return {"description": "cap"}

    def prepare_filename(self, info):
        return self.path


def test_reddit_download_muxes_audio_and_sends_cookies(tmp_path, monkeypatch):
    jar = tmp_path / "jar.txt"
    jar.write_text("x")
    monkeypatch.setattr(config, "REDDIT_COOKIES_FILE", str(jar))
    monkeypatch.setattr(reel_downloader.yt_dlp, "YoutubeDL", FakeYDL)

    reel_downloader.download_media("https://redd.it/abc", str(tmp_path / "r.mp4"))

    assert FakeYDL.opts["format"] == "bv*+ba/b"
    assert FakeYDL.opts["merge_output_format"] == "mp4"
    assert FakeYDL.opts["cookiefile"] == str(jar)


def test_non_reddit_download_is_unchanged(tmp_path, monkeypatch):
    jar = tmp_path / "jar.txt"
    jar.write_text("x")
    monkeypatch.setattr(config, "REDDIT_COOKIES_FILE", str(jar))
    monkeypatch.setattr(reel_downloader.yt_dlp, "YoutubeDL", FakeYDL)

    reel_downloader.download_media("https://www.instagram.com/reel/abc",
                                   str(tmp_path / "r.mp4"))

    assert FakeYDL.opts["format"] == "mp4/best"
    assert "cookiefile" not in FakeYDL.opts and "merge_output_format" not in FakeYDL.opts


def test_gallery_dl_gets_the_reddit_cookie_file(tmp_path, monkeypatch):
    jar = tmp_path / "jar.txt"
    jar.write_text("x")
    monkeypatch.setattr(config, "REDDIT_COOKIES_FILE", str(jar))
    monkeypatch.setattr(config, "GALLERY_DL_COOKIES_BROWSER", "chrome")
    run = fake_gdl({"a.jpg": b"img"})
    monkeypatch.setattr(reel_downloader.subprocess, "run", run)

    reel_downloader.download_photos("https://redd.it/abc", str(tmp_path / "s.mp4"))

    assert run.cmd[run.cmd.index("--cookies") + 1] == str(jar)
    assert "--cookies-from-browser" not in run.cmd


def test_reddit_caption_is_the_title_not_the_body(tmp_path, monkeypatch):
    class TitledYDL(FakeYDL):
        def extract_info(self, url, download):
            open(self.path, "wb").close()
            return {"title": "Grandma puts thief in a headlock",
                    "description": "long body text"}
    monkeypatch.setattr(reel_downloader.yt_dlp, "YoutubeDL", TitledYDL)

    _, caption = reel_downloader.download_media(
        "https://www.reddit.com/r/x/comments/abc", str(tmp_path / "r.mp4"))

    assert caption == "Grandma puts thief in a headlock"


class FakeResp:
    def __init__(self, url, data=None):
        self.url, self._data = url, data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _listing(selftext="", comments=()):
    return [
        {"data": {"children": [{"data": {
            "subreddit": "PublicFreakout", "link_flair_text": "Arrest",
            "title": "Grandma vs thief", "selftext": selftext}}]}},
        {"data": {"children": [{"data": {"body": b, "is_submitter": op}}
                               for b, op in comments]}},
    ]


def test_reddit_context_keeps_body_and_op_comments_only(monkeypatch):
    import requests
    calls = []

    def get(url, **kw):
        calls.append(url)
        return FakeResp(url, _listing(
            "It happened in London.",
            [("random guess", False), ("Context: Oxford St, Tuesday", True)]))
    monkeypatch.setattr(requests, "get", get)

    out = reel_downloader.reddit_context(
        "https://www.reddit.com/r/PublicFreakout/comments/1x8hpy5")

    assert calls == ["https://www.reddit.com/comments/1x8hpy5.json"]
    assert "r/PublicFreakout" in out and "unverified" in out
    assert "Title: Grandma vs thief" in out and "Flair: Arrest" in out
    assert "Post text: It happened in London." in out
    assert "Uploader's comment: Context: Oxford St, Tuesday" in out
    assert "random guess" not in out


def test_reddit_context_follows_short_links(monkeypatch):
    import requests
    calls = []

    def get(url, **kw):
        calls.append(url)
        if "v.redd.it" in url:  # the redirect lands on the post, page 403s
            return FakeResp("https://www.reddit.com/r/x/comments/1x8hpy5/t/")
        return FakeResp(url, _listing())
    monkeypatch.setattr(requests, "get", get)

    assert "Title: Grandma vs thief" in reel_downloader.reddit_context(
        "https://v.redd.it/ry8utnk2o2wh1")
    assert calls[1] == "https://www.reddit.com/comments/1x8hpy5.json"


def test_reddit_context_never_raises(monkeypatch):
    import requests

    def boom(*a, **kw):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(requests, "get", boom)

    assert reel_downloader.reddit_context("https://redd.it/abc") == ""
    assert reel_downloader.reddit_context("https://x.com/a/status/1") == ""
