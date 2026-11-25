"""Offline tests for the vision driver (Phase 3): the screen classifier on
SCRUBBED dumps recorded on IG 457 (tests/fixtures/ig_vision/), and the whole
carousel-with-music dry run replayed against those screens by a fake phone.
No phone, no network."""

import os

import pytest
from PIL import Image

from modules.instagram.vision import flows, scrub, uixml
from modules.instagram.vision import screens as scr
from modules.instagram.vision.driver import Driver, Stop

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "ig_vision")
CAPTION = "Тестовая карусель 🎵 — пробный запуск\n\n#test #brand #новости"


def fx(name):
    with open(os.path.join(FIX, name + ".xml"), encoding="utf-8") as f:
        return f.read()


# --------------------------------------------------------------------------- #
# classifier on real (scrubbed) screens                                       #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name,expected", [
    ("home", "home"), ("profile", "profile"), ("gallery", "gallery"), ("gallery_12", "gallery"),
    ("gallery_123", "gallery"), ("editor", "editor"), ("editor_music", "editor"),
    ("music_picker", "music_picker"), ("music_search_open", "music_picker"),
    ("music_search", "music_picker"), ("music_preview", "music_preview"),
    ("popup_sharing_info", "popup:sharing_info"), ("composer", "composer"),
    ("caption_editor", "caption_editor"), ("caption_set", "caption_editor"),
    ("composer_final", "composer"),
])
def test_classify_recorded_screens(name, expected):
    assert scr.classify(fx(name)).name == expected


def test_security_lock_and_unknown_popup():
    dialog = ('<hierarchy><node package="com.instagram.android" bounds="[0,0][1080,2340]" text="" '
              'resource-id="" content-desc="" class="android.widget.FrameLayout">'
              '<node package="com.instagram.android" bounds="[100,900][980,1000]" text="{t}" '
              'resource-id="" content-desc="" class="android.widget.TextView" />'
              '<node package="com.instagram.android" bounds="[100,1100][980,1200]" text="Continue" '
              'resource-id="com.instagram.android:id/igds_button" content-desc="" class="android.widget.Button" />'
              '</node></hierarchy>')
    assert scr.classify(dialog.format(t="Help us confirm it's you")).name == "security"
    assert scr.classify(dialog.format(t="Something brand new")).name == "popup:unknown"
    lock = ('<hierarchy><node package="com.android.systemui" bounds="[0,0][1080,2340]" text="" '
            'resource-id="com.android.systemui:id/keyguard_status_view" content-desc="" /></hierarchy>')
    assert scr.classify(lock).name == "lockscreen"


def test_gallery_readers():
    s = scr.classify(fx("gallery"))
    thumbs = scr.gallery_thumbs(s)
    assert [t["number"] for t in thumbs[:3]] == [None, None, None]
    assert [t["selected"] for t in thumbs[:3]] == [True, False, False]
    assert not scr.multi_select_on(s)
    # the row hidden behind the POST/STORY/REEL strip is not offered
    strip_top = min(n.bounds[1] for n in s.nodes if n.rid.startswith("cam_dest_"))
    assert all(t["node"].bounds[3] <= strip_top for t in thumbs)
    s = scr.classify(fx("gallery_123"))
    assert [t["number"] for t in scr.gallery_thumbs(s)[:4]] == [1, 2, 3, None]
    assert scr.multi_select_on(s)


def test_music_composer_profile_readers():
    rows = scr.track_rows(scr.classify(fx("music_search")))
    assert (rows[0]["title"], rows[0]["artist"]) == ("Lazy River", "Phury")
    s = scr.classify(fx("composer_final"))
    assert scr.composer_track(s) == ("Lazy River", "Phury")
    assert scr.caption_text(s) == scr.dump_form(CAPTION)
    assert scr.caption_text(scr.classify(fx("composer"))) == ""  # the hint isn't a caption
    info = scr.profile_info(scr.classify(fx("profile")))
    assert info["account"] == "test_account" and info["posts"] == 108
    assert scr.dump_form("a🎵b") == "a..b"


def test_fixtures_are_scrubbed():
    for name in os.listdir(FIX):
        xml = open(os.path.join(FIX, name), encoding="utf-8").read()
        assert "vestra" not in xml.lower(), name
        assert "com.android.systemui" not in xml, name
        assert {n.get("package") for n in uixml.nodes(xml)} <= {"com.instagram.android"}, name


def test_scrub_replaces_handles_and_counts():
    xml = ('<hierarchy><node package="com.instagram.android" bounds="[0,0][9,9]" text="real.handle" '
           'resource-id="com.instagram.android:id/action_bar_title" content-desc="" />'
           '<node package="com.instagram.android" bounds="[0,0][9,9]" text="" resource-id="" '
           'content-desc="real.handle\'s story, 0 of 3, Unseen." />'
           '<node package="com.instagram.android" bounds="[0,0][9,9]" text="42" '
           'resource-id="com.instagram.android:id/profile_header_familiar_followers_value" content-desc="" />'
           '<node package="com.instagram.android" bounds="[0,0][9,9]" text="Мой личный текст о себе" '
           'resource-id="" content-desc="" />'
           '<node package="com.android.systemui" bounds="[0,0][9,9]" text="Carrier" resource-id="" '
           'content-desc="" /></hierarchy>')
    out = scrub.scrub(xml, account="real.handle")
    assert "real.handle" not in out and "test_account" in out
    assert "Carrier" not in out and "Мой" not in out and 'text="42"' not in out


# --------------------------------------------------------------------------- #
# the flow, replayed                                                          #
# --------------------------------------------------------------------------- #

def _gallery_1():
    """Multi-select just switched on: only slide 1 numbered (not recorded on
    its own, derived from gallery_12)."""
    return fx("gallery_12").replace(
        'content-desc="Selected media number 2 Photo', 'content-desc="Unselected Photo')


class FakePhone:
    """Replays recorded screens. `routes[state]` = [(key, next_state)]: a tap
    goes to next_state when ANY node under the tap point matches key (its
    resource-id or rid prefix, content-desc or text) — like Android, a tap on
    a row's title is a tap on the row."""

    serial = "FAKE"

    def __init__(self, screens, routes, start="home", lose_taps=None):
        self.screens = screens
        self.routes = routes
        self.state = start
        self.lose_taps = dict(lose_taps or {})
        self.taps, self.texts, self.pushed, self.swipes, self.shells = [], [], [], 0, []
        self.backs = 0

    # reading
    def dump_hierarchy(self):
        return self.screens[self.state]

    def screenshot(self):
        return Image.new("RGB", (108, 234), "gray")

    def app_current(self):
        return {"package": "com.instagram.android", "activity": "x"}

    def shell(self, cmd):
        self.shells.append(cmd)
        return "2026-11-25 16:07:30\n" if cmd.startswith("date") else ""

    def is_locked(self):
        return False

    # acting
    def keep_awake(self):
        pass

    def app_start(self, pkg, stop=False):
        self.state = "home"

    def push(self, local, remote):
        self.pushed.append(local)

    def _under(self, x, y):
        return [n for n in uixml.nodes(self.screens[self.state])
                if n.bounds[0] <= x <= n.bounds[2] and n.bounds[1] <= y <= n.bounds[3]]

    def tap(self, x, y):
        under = self._under(x, y)
        self.taps.append((self.state, under[-1].rid if under else "", x, y))
        for key, nxt in self.routes.get(self.state, []):
            if any(n.rid == key or (key.endswith("*") and n.rid.startswith(key[:-1]))
                   or n.desc == key or n.desc.split(", ", 1)[0] == key or n.text == key
                   for n in under):
                if self.lose_taps.get(key):
                    self.lose_taps[key] -= 1
                    return
                self.state = nxt
                return

    def swipe(self, *a):
        self.swipes += 1

    def back(self):
        self.backs += 1
        if self.state == "switcher":
            self.state = "profile"
        elif self.state not in ("home", "profile", "profile_other") and "popup_start_over" in self.screens:
            self.state = "popup_start_over"

    def set_text(self, rid, text):
        self.texts.append((rid, text))
        nxt = {"music_search_open": "music_search", "caption_editor": "caption_set"}.get(self.state)
        if nxt:
            self.state = nxt
        return True


THUMB = "gallery_grid_item_thumbnail"
ROUTES = {
    "home": [("profile_tab", "profile"), ("Create", "gallery")],
    "profile": [("feed_tab", "home")],
    "gallery": [("multi_select_slide_button_alt", "gallery_1")],
    "gallery_1": [("Unselected Photo thumbnail created on November 25, 2026 16:07", "gallery_12")],
    "gallery_12": [("Unselected Photo thumbnail created on November 25, 2026 16:07", "gallery_123")],
    "gallery_123": [("next_button_textview", "editor")],
    "editor": [("Audio", "music_picker"), ("media_thumbnail_tray_button", "popup_sharing_info")],
    "music_picker": [("row_search_edit_text", "music_search_open")],
    "music_search": [("audio_browser_track_4_*", "music_preview")],
    "music_preview": [("audio_bar_select_tap_target", "editor_music")],
    "editor_music": [("media_thumbnail_tray_button", "popup_sharing_info")],
    "popup_sharing_info": [("OK", "composer")],
    "composer": [("caption_input_text_view", "caption_editor")],
    "caption_set": [("caption_done_button", "composer_final")],
}
NAMES = ["home", "profile", "gallery", "gallery_12", "gallery_123", "editor", "music_picker",
         "music_search_open", "music_search", "music_preview", "editor_music", "popup_sharing_info",
         "composer", "caption_editor", "caption_set", "composer_final"]


def _phone(**kw):
    screens = {n: fx(n) for n in NAMES}
    screens["gallery_1"] = _gallery_1()
    return FakePhone(screens, ROUTES, **kw)


def _driver(phone):
    t = [0.0]

    def clock():
        t[0] += 0.25
        return t[0]

    return Driver(phone, recorder=None, log=lambda *a: None, sleep=lambda s: None, clock=clock)


def _slides(tmp_path, n=3):
    paths = []
    for i in range(1, n + 1):
        p = tmp_path / f"s_{i}.jpg"
        p.write_bytes(b"x")
        paths.append(str(p))
    return paths


def _push(dev, paths):
    for p in paths:
        dev.push(p, "/sdcard/DCIM/Camera/x")


def test_dry_run_reaches_composer_and_never_shares(tmp_path):
    phone = _phone()
    drv = _driver(phone)
    res = flows.carousel_with_music(drv, _slides(tmp_path), CAPTION, "test_account",
                                    track="Lazy River", artist="Phury", push=_push)
    assert phone.state == "composer_final"
    assert res["shared"] is False and res["slides"] == 3 and res["posts_before"] == 108
    assert len(phone.pushed) == 3
    assert ("com.instagram.android:id/row_search_edit_text", "Lazy River") in phone.texts
    assert ("com.instagram.android:id/caption_input_text_view", CAPTION) in phone.texts
    assert not any(state == "composer_final" for state, *_ in phone.taps)  # nothing tapped on the last screen
    assert not any(rid in ("share_footer_button", "share_button") for _s, rid, *_ in phone.taps)


def test_lost_slide_tap_is_retried(tmp_path):
    phone = _phone(lose_taps={"Unselected Photo thumbnail created on November 25, 2026 16:07": 1})
    drv = _driver(phone)
    flows.carousel_with_music(drv, _slides(tmp_path), CAPTION, "test_account",
                              track="Lazy River", push=_push)
    assert phone.state == "composer_final"
    assert ("retry", "slide2", "first tap on the thumbnail didn't register") in drv.events


def test_wrong_account_without_switching_stops_before_anything_is_pushed(tmp_path):
    phone = _phone()
    with pytest.raises(Stop) as e:
        flows.carousel_with_music(_driver(phone), _slides(tmp_path), CAPTION, "other_account",
                                  push=_push, allow_switch=False)
    assert e.value.kind == "wrong_account"
    assert phone.pushed == []


def test_account_not_on_the_phone_stops_before_anything_is_pushed(tmp_path):
    phone = _switch_phone()
    with pytest.raises(Stop) as e:
        flows.carousel_with_music(_driver(phone), _slides(tmp_path), CAPTION, "nobody_here", push=_push)
    assert e.value.kind == "account_not_logged_in"
    assert "other_account" in e.value.message
    assert phone.pushed == []


def test_track_not_found_stops(tmp_path):
    phone = _phone()
    with pytest.raises(Stop) as e:
        flows.carousel_with_music(_driver(phone), _slides(tmp_path), CAPTION, "test_account",
                                  track="No Such Song", push=_push)
    assert e.value.kind == "track_not_found"
    assert phone.swipes >= 1  # it scrolled the list before giving up


def test_music_on_composer_without_asking_stops(tmp_path):
    # Skipping --track while the recorded composer carries Lazy River must stop:
    # the driver never lets music it wasn't asked for through.
    phone = _phone()
    with pytest.raises(Stop) as e:
        flows.carousel_with_music(_driver(phone), _slides(tmp_path), CAPTION, "test_account", push=_push)
    assert e.value.kind == "music_mismatch"


def test_security_screen_mid_flow_stops(tmp_path):
    phone = _phone()
    phone.screens["editor"] = phone.screens["editor"].replace('text="Audio"', 'text="Confirm it\'s you"')
    with pytest.raises(Stop) as e:
        flows.carousel_with_music(_driver(phone), _slides(tmp_path), CAPTION, "test_account",
                                  track="Lazy River", push=_push)
    assert e.value.kind == "security"


def test_wrong_media_at_top_of_gallery_stops(tmp_path):
    phone = _phone()
    phone.shell = lambda cmd: "2026-11-26 09:00:00\n"  # thumbnails are from the day before
    with pytest.raises(Stop) as e:
        flows.carousel_with_music(_driver(phone), _slides(tmp_path), CAPTION, "test_account",
                                  track="Lazy River", push=_push)
    assert e.value.kind == "wrong_media"


def test_share_needs_both_switches(tmp_path):
    with pytest.raises(ValueError):
        flows.carousel_with_music(_driver(_phone()), _slides(tmp_path), CAPTION, "test_account",
                                  share=True, push=_push)


def test_run_cli_refuses_share_without_confirm_and_without_tty(tmp_path, monkeypatch):
    from modules.instagram.vision import run

    with pytest.raises(SystemExit):
        run.main(["--account", "a", "--files", *_slides(tmp_path), "--share"])
    with pytest.raises(SystemExit):
        run.main(["--files", *_slides(tmp_path)])  # no --account


def test_validate_and_caption():
    assert flows.full_caption("body", ["a", "#b"]) == "body\n\n#a #b"
    assert flows.full_caption("", ["a"]) == "#a"
    with pytest.raises(ValueError):
        flows.validate_slides(["only_one.jpg"])


def test_autoplay_video_counts_as_settled_when_the_dump_is_still():
    phone = _phone()
    shades = iter(range(0, 10_000, 40))
    phone.screenshot = lambda: Image.new("RGB", (108, 234), (next(shades) % 256,) * 3)
    drv = _driver(phone)
    assert drv.look().name == "home"
    assert not [e for e in drv.events if e[0] == "unstable"]


# --------------------------------------------------------------------------- #
# account switching                                                           #
# --------------------------------------------------------------------------- #

def test_switcher_screens_and_rows():
    s = scr.classify(fx("switcher"))
    assert s.name == "account_switcher"  # not "profile", which is still under the sheet
    rows = scr.account_rows(s)
    assert [(r["handle"], r["active"]) for r in rows[:2]] == [("test_account", True), ("other_account", False)]
    assert scr.classify(fx("switcher_scrolled")).name == "account_switcher"
    assert scr.profile_info(scr.classify(fx("profile_other")))["account"] == "other_account"


def _switch_phone(after_tap="profile_other"):
    screens = {n: fx(n) for n in NAMES + ["switcher", "profile_other"]}
    screens["gallery_1"] = _gallery_1()
    routes = dict(ROUTES)
    routes["profile"] = [("action_bar_username_container", "switcher"), ("feed_tab", "home")]
    routes["switcher"] = [("other_account", after_tap)]
    return FakePhone(screens, routes)


def test_switch_to_another_account_is_verified_on_profile():
    phone = _switch_phone()
    drv = _driver(phone)
    flows.open_fresh(drv)
    info = flows.ensure_account(drv, "@Other_Account")
    assert info["account"] == "other_account"
    assert phone.state == "profile_other"
    assert [s for s, *_ in phone.taps].count("switcher") == 1  # one tap on the row, nothing else


def test_right_account_already_active_needs_no_switcher():
    phone = _switch_phone()
    drv = _driver(phone)
    flows.open_fresh(drv)
    assert flows.ensure_account(drv, "test_account")["account"] == "test_account"
    assert "switcher" not in [s for s, *_ in phone.taps] and phone.state == "profile"


def test_switch_that_does_not_take_stops():
    phone = _switch_phone(after_tap="profile")  # IG stays on the old account
    drv = _driver(phone)
    flows.open_fresh(drv)
    with pytest.raises(Stop) as e:
        flows.ensure_account(drv, "other_account")
    assert e.value.kind == "wrong_account"


# --------------------------------------------------------------------------- #
# sweep                                                                       #
# --------------------------------------------------------------------------- #

def _sweep_phone():
    phone = _switch_phone()
    phone.screens["popup_start_over"] = fx("popup_start_over")
    phone.routes["popup_start_over"] = [("Start over", "home")]
    return phone


def test_music_library_tells_creator_from_business():
    rf = {"title": "Move", "desc": "Select track Move by X, royalty-free, 1:45"}
    own = {"title": "Original audio", "desc": "Select track Original audio by someone, 0:16"}
    licensed = {"title": "Shape of You", "desc": "Select track Shape of You by Ed Sheeran, 3:53"}
    assert flows.music_library([rf, own]) == "royalty-free only"
    assert flows.music_library([rf, licensed]) == "full"
    assert flows.music_library([]) is None


def test_list_accounts_reads_the_switcher_and_closes_it():
    from modules.instagram.vision import sweep

    phone = _sweep_phone()
    handles, active = sweep.list_accounts(_driver(phone))
    assert active == "test_account"
    assert handles[:2] == ["test_account", "other_account"] and len(handles) >= 8
    assert phone.state == "profile"


def test_sweep_dry_runs_cleans_up_and_never_shares(tmp_path, monkeypatch):
    from modules.instagram.vision import sweep

    phone = _sweep_phone()
    pushed = []

    def push(dev, paths):
        out = [f"/sdcard/DCIM/Camera/igv_1_{i:02d}.jpg" for i in range(1, len(paths) + 1)]
        pushed.extend(out)
        return out

    monkeypatch.setattr(flows, "push_slides", push)
    results = sweep.sweep(_driver(phone), _slides(tmp_path), CAPTION, "Lazy River",
                          accounts=["test_account", "nobody_here"], probe="Ed Sheeran")
    assert [r["status"] for r in results] == ["ok", "account_not_logged_in"]
    assert results[0]["library"] == "royalty-free only" and results[0]["posts"] == 108
    assert ("com.instagram.android:id/row_search_edit_text", "Ed Sheeran") in phone.texts
    assert "popup_start_over" in [s for s, *_ in phone.taps]  # the draft was thrown away
    for p in pushed:  # and every pushed slide deleted again
        assert any(c.startswith("rm ") and p.rsplit("/", 1)[1] in c for c in phone.shells)
    assert not any(rid in ("share_footer_button", "share_button") for _s, rid, *_ in phone.taps)
    assert "account_not_logged_in" in sweep.summary(results)


def test_sweep_aborts_on_a_security_check(tmp_path, monkeypatch):
    from modules.instagram.vision import sweep

    phone = _sweep_phone()
    phone.screens["editor"] = phone.screens["editor"].replace('text="Audio"', 'text="Confirm it\'s you"')
    monkeypatch.setattr(flows, "push_slides", lambda dev, paths: [])
    results = sweep.sweep(_driver(phone), _slides(tmp_path), CAPTION, "Lazy River",
                          accounts=["test_account", "other_account"])
    assert [r["status"] for r in results] == ["security"]  # the second account was never touched
    assert phone.backs == 0  # nothing pressed on the security screen


def test_remove_slides_only_touches_its_own_files():
    phone = _phone()
    flows.remove_slides(phone, ["/sdcard/DCIM/Camera/igv_9_01.jpg", "/sdcard/DCIM/Camera/IMG_0001.jpg"])
    assert any("igv_9_01.jpg" in c for c in phone.shells)
    assert not any("IMG_0001" in c for c in phone.shells)


def test_run_cli_sweep_never_shares():
    from modules.instagram.vision import run

    with pytest.raises(SystemExit):
        run.main(["--sweep", "--share", "--i-confirm-share"])
