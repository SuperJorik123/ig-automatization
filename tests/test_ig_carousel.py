"""Offline tests for the ADB carousel poster's pure halves: queue grouping
and reading the gallery's thumbnails out of a hierarchy dump."""

import json

import pytest

ig = pytest.importorskip("modules.instagram.upload_post")


def _touch(path, data=b"x"):
    path.write_bytes(data)
    return str(path)


def test_find_carousels_groups_numbered_files_in_slide_order(tmp_path):
    for name in ["010_10.jpg", "010_2.mp4", "010_1.jpg", "002.jpg", "lone_1.jpg"]:
        _touch(tmp_path / name)
    (tmp_path / "010.json").write_text(
        json.dumps({"caption": ["a", "", "b"], "hashtags": ["news", "#x"]}),
        encoding="utf-8",
    )
    groups = ig.find_carousels(str(tmp_path))
    assert list(groups) == ["010"]
    assert [p.split("\\")[-1].split("/")[-1] for p in groups["010"]] == [
        "010_1.jpg", "010_2.mp4", "010_10.jpg"
    ]

    paths, body, tags, meta = ig.find_next_carousel(str(tmp_path))
    assert paths == groups["010"]
    assert body == "a\n\nb"
    assert tags == ["news", "x"]
    assert meta.endswith("010.json")


def test_single_posts_skip_carousel_slides(tmp_path):
    _touch(tmp_path / "001_1.jpg")
    _touch(tmp_path / "001_2.jpg")
    _touch(tmp_path / "005.jpg")
    path, _body, _tags = ig.find_next_post(str(tmp_path))
    assert path.endswith("005.jpg")


def test_no_carousel(tmp_path):
    _touch(tmp_path / "001.jpg")
    assert ig.find_next_carousel(str(tmp_path)) == (None, "", [], None)


def _node(bounds, desc="", selected="false"):
    return (
        f'<node index="0" text="" resource-id="com.instagram.android:id/'
        f'gallery_grid_item_thumbnail" class="android.widget.ImageView" '
        f'content-desc="{desc}" selected="{selected}" bounds="{bounds}" />'
    )


def test_gallery_thumbnails_visual_order_and_state():
    xml = "<hierarchy>" + "".join([
        _node("[270,1200][540,1470]", "Unselected Photo thumbnail created on x"),
        _node("[0,1200][270,1470]", "Selected Photo thumbnail, 1"),
        _node("[0,1470][270,1740]", "", selected="true"),
        _node("[270,1470][540,1740]", ""),
        _node("[0,2330][270,2340]", "Unselected clipped"),  # sliver at the edge
        '<node resource-id="com.instagram.android:id/other" bounds="[0,0][9,9]" />',
    ]) + "</hierarchy>"
    thumbs = ig.gallery_thumbnails(xml)
    assert [t["bounds"][:2] for t in thumbs] == [(0, 1200), (270, 1200), (0, 1470), (270, 1470)]
    assert [t["selected"] for t in thumbs] == [True, False, True, None]


def test_upload_carousel_validates_before_touching_the_phone(tmp_path):
    one = _touch(tmp_path / "a_1.jpg")
    with pytest.raises(ValueError):
        ig.upload_carousel(None, [one], "", [])
    with pytest.raises(FileNotFoundError):
        ig.upload_carousel(None, [one, str(tmp_path / "missing.jpg")], "", [])
    txt = _touch(tmp_path / "a_2.txt")
    with pytest.raises(ValueError):
        ig.upload_carousel(None, [one, txt], "", [])
