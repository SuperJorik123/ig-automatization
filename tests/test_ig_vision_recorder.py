"""Offline tests for the IG vision driver's Phase 1: dump parsing + recorder.
No phone — a fake device stands in for uiautomator2."""

import json
import os

from PIL import Image

from modules.instagram.vision import recorder, uixml

XML = (
    '<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">'
    '<node index="0" text="" resource-id="" class="android.widget.FrameLayout" '
    'package="com.instagram.android" content-desc="" bounds="[0,0][1080,2340]">'
    '<node index="0" text="test_account.1" resource-id="com.instagram.android:id/action_bar_large_title" '
    'class="android.widget.TextView" package="com.instagram.android" content-desc="" '
    'selected="false" bounds="[40,120][400,190]" />'
    '<node index="1" text="" resource-id="com.instagram.android:id/profile_tab" '
    'class="android.widget.FrameLayout" package="com.instagram.android" content-desc="Profile" '
    'selected="true" bounds="[864,2200][1080,2340]" />'
    '<node index="2" text="" resource-id="" class="android.view.View" package="com.instagram.android" '
    'content-desc="" bounds="[0,0][0,0]" />'
    "</node></hierarchy>"
)


class FakeDevice:
    serial = "192.168.1.5:5555"

    def __init__(self, xml=XML):
        self.xml = xml
        self.shell_calls = []

    def screenshot(self):
        return Image.new("RGB", (1080, 2340), "white")

    def dump_hierarchy(self):
        return self.xml

    def app_current(self):
        return {"package": "com.instagram.android", "activity": ".activity.MainTabActivity"}

    def shell(self, cmd):
        self.shell_calls.append(cmd)
        if "dumpsys package" in cmd:
            return "    versionCode=389101234 minSdk=28 targetSdk=34\n    versionName=361.0.0.46.88\n"
        if "uimode night" in cmd:
            return "Night mode: yes\n"
        if "ro.product.model" in cmd:
            return "SM-S911B\n"
        return ""


def test_nodes_and_find():
    ns = uixml.nodes(XML)
    assert len(ns) == 4  # the [0,0][0,0] node still parses; find() filters by size
    tab = uixml.find(ns, rid="profile_tab")[0]
    assert tab.desc == "Profile" and tab.flag("selected") and tab.center == (972, 2270)
    assert uixml.find(ns, desc_prefix="prof")[0] is tab
    assert len(uixml.find(ns, min_size=1)) == 3
    assert uixml.packages(XML) == {"com.instagram.android"}
    assert uixml.nodes("<hierarchy><node") == []


def test_guess_account():
    assert uixml.guess_account(XML) == "test_account.1"
    assert uixml.guess_account(XML.replace("test_account.1", "Suggested for you")) is None


def test_parsers():
    assert recorder.parse_ig_version("versionCode=12 x\nversionName=1.2.3") == ("1.2.3", 12)
    assert recorder.parse_ig_version("") == (None, None)
    assert recorder.parse_night_mode("Night mode: no") is False
    assert recorder.parse_night_mode("Night mode: auto") is None
    assert recorder.safe_name("192.168.1.5:5555") == "192.168.1.5_5555"
    assert recorder.safe_name("music picker/1") == "music_picker_1"
    assert recorder.safe_name("") == "x"


def test_save_writes_png_xml_json(tmp_path):
    rec = recorder.Recorder(FakeDevice(), out_dir=str(tmp_path), account="wswire")
    meta = rec.save("composer", note="dry run")
    for key in ("png", "xml", "json"):
        assert os.path.isfile(meta["paths"][key])
    assert os.path.dirname(meta["paths"]["png"]).startswith(str(tmp_path))
    assert "192.168.1.5_5555_composer" in meta["files"]["png"]

    with open(meta["paths"]["json"], encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["step"] == "composer" and saved["note"] == "dry run"
    assert saved["serial"] == "192.168.1.5:5555" and saved["model"] == "SM-S911B"
    assert saved["resolution"] == [1080, 2340]
    assert saved["ig_version"] == "361.0.0.46.88" and saved["ig_version_code"] == 389101234
    assert saved["dark_mode"] is True
    assert saved["account"] == "wswire" and saved["account_guess"] == "test_account.1"
    assert saved["package"] == "com.instagram.android"
    assert "paths" not in saved  # absolute local paths stay out of the dataset
    with open(meta["paths"]["xml"], encoding="utf-8") as f:
        assert f.read() == XML
    assert rec.count == 1


def test_save_survives_missing_optional_fields(tmp_path):
    class Bare(FakeDevice):
        serial = None

        def shell(self, cmd):
            raise RuntimeError("adb gone")

        def app_current(self):
            raise RuntimeError("no")

    meta = recorder.Recorder(Bare(), out_dir=str(tmp_path)).save("x")
    assert meta["ig_version"] is None and meta["dark_mode"] is None and meta["package"] is None
    assert meta["serial"] == "unknown"
