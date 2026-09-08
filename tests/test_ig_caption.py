"""Offline tests for modules/instagram/caption.py — no OpenRouter calls.

The client is replaced by a scripted fake, so what is exercised here is the
part that has to hold when the model misbehaves: the never-raises contract
(every failure returns the bare headline, because the render is already made
and the operator has already tapped publish) and the cleanup of the wrappers a
chat model reaches for after being told not to use them.
"""

import pytest

from modules.instagram import caption
from shared import config


EXAMPLE = (
    "Released footage shows plane crash at Miami International Airport\n"
    "\n"
    "Newly released footage shows the moment an Amazon cargo plane overran "
    "the runway while landing at Miami International Airport.\n"
    "\n"
    "The Boeing 767, operating for Amazon Prime Air, crashed into multiple "
    "vehicles and caught fire. Five people were killed and five others were "
    "injured.\n"
    "\n"
    "#miami #florida #planecrash #aviation #amazon #breakingnews #usa"
)


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
    """Expansion enabled — the default, made explicit so a developer's .env
    can't turn these tests green for the wrong reason."""
    monkeypatch.setattr(config, "IG_CAPTION_ENABLED", True)
    monkeypatch.setattr(config, "IG_CAPTION_MODEL", "openai/gpt-5.5:online")


def _client(monkeypatch, **kw):
    fake = _Fake(**kw)
    monkeypatch.setattr(caption, "_client", fake)
    return fake


# --------------------------------------------------------------------------- #
# the never-raises contract                                                   #
# --------------------------------------------------------------------------- #


def test_expansion_returns_the_caption(on, monkeypatch):
    _client(monkeypatch, content=EXAMPLE)
    assert caption.expand("Released footage shows plane crash") == EXAMPLE


def test_no_api_key_returns_the_bare_headline(on, monkeypatch):
    monkeypatch.setattr(caption, "_client", None)
    assert caption.expand("Pentagon criticized over benefits") == \
        "Pentagon criticized over benefits"


def test_disabled_returns_the_bare_headline_without_calling(on, monkeypatch):
    """The kill switch has to cut the billed call, not just the output."""
    fake = _client(monkeypatch, content=EXAMPLE)
    monkeypatch.setattr(config, "IG_CAPTION_ENABLED", False)
    assert caption.expand("Pentagon criticized") == "Pentagon criticized"
    assert fake.calls == []


def test_api_failure_returns_the_bare_headline(on, monkeypatch):
    _client(monkeypatch, exc=RuntimeError("502 from the gateway"))
    assert caption.expand("Pentagon criticized") == "Pentagon criticized"


def test_empty_completion_returns_the_bare_headline(on, monkeypatch):
    _client(monkeypatch, content="   \n  ")
    assert caption.expand("Pentagon criticized") == "Pentagon criticized"


def test_empty_headline_stays_empty(on, monkeypatch):
    fake = _client(monkeypatch, content=EXAMPLE)
    assert caption.expand("   ") == ""
    assert fake.calls == []


def test_the_headline_is_the_user_message(on, monkeypatch):
    fake = _client(monkeypatch, content=EXAMPLE)
    caption.expand("Pentagon criticized")
    assert fake.calls[0]["model"] == "openai/gpt-5.5:online"
    assert fake.calls[0]["messages"][-1] == {"role": "user",
                                             "content": "Pentagon criticized"}


def test_neither_temperature_nor_max_tokens_is_sent(on, monkeypatch):
    """Both are rejected or wasted by current OpenAI models on OpenRouter, and
    either one failing costs the post its caption. See the module docstring."""
    fake = _client(monkeypatch, content=EXAMPLE)
    caption.expand("Pentagon criticized")
    assert "temperature" not in fake.calls[0]
    assert "max_tokens" not in fake.calls[0]


# --------------------------------------------------------------------------- #
# cleaning up after the model                                                 #
# --------------------------------------------------------------------------- #


def test_code_fence_is_stripped(on, monkeypatch):
    _client(monkeypatch, content="```\n" + EXAMPLE + "\n```")
    assert caption.expand("h") == EXAMPLE


def test_caption_label_is_stripped(on, monkeypatch):
    _client(monkeypatch, content="Caption:\n\n" + EXAMPLE)
    assert caption.expand("h") == EXAMPLE


def test_surrounding_quotes_are_stripped(on, monkeypatch):
    _client(monkeypatch, content='"' + EXAMPLE + '"')
    assert caption.expand("h") == EXAMPLE


def test_citation_markers_are_stripped_without_touching_the_prose(on, monkeypatch):
    _client(monkeypatch, content="Five people were killed [2]. Police said "
                                 "the cause [10] is under investigation.\n"
                                 "\n#miami #usa")
    out = caption.expand("h")
    assert out == ("Five people were killed. Police said the cause is under "
                   "investigation.\n\n#miami #usa")


def test_blank_line_runs_are_collapsed_to_one(on, monkeypatch):
    _client(monkeypatch, content="Headline\n\n\n\nA paragraph.\n\n\n#news")
    assert caption.expand("h") == "Headline\n\nA paragraph.\n\n#news"


def test_hashtag_line_survives_cleaning(on, monkeypatch):
    """The hashtags are the one part every downstream step must not lose: the
    translator copies them verbatim and IG indexes them."""
    _client(monkeypatch, content=EXAMPLE)
    assert caption.expand("h").splitlines()[-1] == (
        "#miami #florida #planecrash #aviation #amazon #breakingnews #usa")


# --------------------------------------------------------------------------- #
# Instagram's 2200-character ceiling                                          #
# --------------------------------------------------------------------------- #


def test_an_overlong_caption_is_trimmed_on_a_word_boundary(on, monkeypatch):
    long = " ".join(["word"] * 800)          # ~4000 chars
    _client(monkeypatch, content=long)
    out = caption.expand("h")
    assert len(out) <= 2200
    assert out.endswith("…")
    assert "wor…" not in out                 # never mid-word
