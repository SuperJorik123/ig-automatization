"""modules/telegram/headlines.py — offline: the model is a fake client."""

import json
import types

from modules.telegram import headlines

ANSWER = {
    "headlines": ["Driver swerves seconds before truck crosses red light.",
                  "“Truck runs red light at busy Miami junction”",
                  "Dashcam catches the moment a truck runs a red light",
                  "driver swerves seconds before truck crosses red light",
                  "One swerve kept this driver alive",
                  "A sixth option nobody asked for"],
    "pick": 4,
    "why": "names the stakes, holds the ending",
    "notes": "verify the location before posting",
}


class _Fake:
    def __init__(self, content="", exc=None):
        self.calls, self._content, self._exc = [], content, exc
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        if self._exc:
            raise self._exc
        msg = types.SimpleNamespace(content=self._content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


def test_parse_tidies_dedupes_caps_and_keeps_the_pick():
    out = headlines.parse("```json\n" + json.dumps(ANSWER) + "\n```")
    assert out["headlines"] == [
        "Driver swerves seconds before truck crosses red light",
        "Truck runs red light at busy Miami junction",
        "Dashcam catches the moment a truck runs a red light",
        "One swerve kept this driver alive",
        "A sixth option nobody asked for",
    ]
    assert out["pick"] == 3  # "One swerve…" moved up past the dropped duplicate
    assert out["notes"] == "verify the location before posting"


def test_parse_rejects_prose_and_empty_lists():
    assert headlines.parse("Here are some headlines: 1. foo") is None
    assert headlines.parse('{"headlines": []}') is None
    assert headlines.parse('{"headlines": "nope"}') is None


def test_a_bad_pick_falls_back_to_the_first():
    out = headlines.parse(json.dumps({"headlines": ["a", "b"], "pick": "x"}))
    assert out["pick"] == 0


def test_propose_sends_the_info_and_never_raises(monkeypatch):
    fake = _Fake(json.dumps(ANSWER))
    monkeypatch.setattr(headlines, "_client", fake)
    out = headlines.propose("Source: WSVN. Truck ran a red light in Miami.",
                            current="Truck runs red light")
    assert len(out["headlines"]) == 5
    user = fake.calls[0]["messages"][-1]["content"]
    assert "Truck ran a red light in Miami" in user
    assert "Truck runs red light" in user
    assert ":online" not in fake.calls[0]["model"]

    monkeypatch.setattr(headlines, "_client", _Fake(exc=RuntimeError("502")))
    assert headlines.propose("info") is None


def test_no_info_means_no_call(monkeypatch):
    fake = _Fake(json.dumps(ANSWER))
    monkeypatch.setattr(headlines, "_client", fake)
    assert headlines.propose("   ") is None
    assert fake.calls == []


def test_card_lines_star_the_pick_and_mark_the_choice():
    opts = headlines.parse(json.dumps(ANSWER))
    block = headlines.card_lines(opts, chosen=0)[0]
    assert "1. ● Driver swerves" in block
    assert "4. One swerve kept this driver alive ⭐" in block
    assert "⚠️ verify the location" in block
    assert headlines.card_lines(None) == []


def test_emoji_are_stripped_from_every_option():
    raw = {"headlines": ["🚨 Truck runs red light 🚗💨", "Driver ✅ survives ⭐",
                         "Family reunion 👨‍👩‍👧 at the gate", "1️⃣ swerve saved him",
                         "😂😂"],
           "pick": 0}
    out = headlines.parse(json.dumps(raw))
    assert out["headlines"] == ["Truck runs red light", "Driver survives",
                                "Family reunion at the gate", "1 swerve saved him"]


def test_strip_emoji_keeps_letters_and_punctuation():
    text = "Café owner’s 3-hour rescue — “unbelievable”… Zürich, 100%?"
    assert headlines.strip_emoji(text) == text


def test_the_prompt_forbids_emoji():
    assert "ABSOLUTELY NO EMOJI" in headlines._SYSTEM
