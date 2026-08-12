"""shared/config.py — the newsroom bot's per-site JSON loader.

Fully offline: every case writes site files into a tmp dir and calls
_load_sites on it directly, so the operator's real modules/newsroom/sites/
never affects the result.

The behaviour under test that matters operationally is the skipping: seven
client channels run off seven files, and one bad file must cost exactly one
channel, never the run.
"""

import json

from shared import config


def _write(directory, name, payload):
    path = directory / name
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload),
        encoding="utf-8",
    )
    return path


def _site(**over):
    base = {"name": "acme", "wp_base": "https://acme.test/wp-json/wp/v2",
            "chat_id": "@acme_news"}
    base.update(over)
    return base


def test_defaults_fill_in_for_omitted_keys(tmp_path):
    _write(tmp_path, "acme.json", _site())

    (site,) = config._load_sites(str(tmp_path))

    assert site["name"] == "acme"
    assert site["views_phase1"] == [500, 5000]
    assert site["emoji_count"] == [2, 4]
    assert site["service_views"] == ""
    assert site["enabled"] is True


def test_explicit_values_beat_defaults(tmp_path):
    _write(tmp_path, "acme.json", _site(views_phase1=[100, 200],
                                        service_views="777",
                                        emoji_pool=["heart"]))

    (site,) = config._load_sites(str(tmp_path))

    assert site["views_phase1"] == [100, 200]
    assert site["service_views"] == "777"
    assert site["emoji_pool"] == ["heart"]


def test_trailing_slash_stripped_from_wp_base(tmp_path):
    # wp_base is joined with "/posts"; a trailing slash would produce "//posts",
    # which some hosts 404 and others redirect.
    _write(tmp_path, "acme.json", _site(wp_base="https://acme.test/wp-json/wp/v2/"))

    (site,) = config._load_sites(str(tmp_path))

    assert site["wp_base"] == "https://acme.test/wp-json/wp/v2"


def test_disabled_site_is_skipped(tmp_path):
    _write(tmp_path, "acme.json", _site(enabled=False))

    assert config._load_sites(str(tmp_path)) == []


def test_example_template_is_never_loaded(tmp_path):
    _write(tmp_path, "example.json", _site(name="example"))

    assert config._load_sites(str(tmp_path)) == []


def test_malformed_json_is_skipped_not_raised(tmp_path):
    _write(tmp_path, "broken.json", "{ not json at all")
    _write(tmp_path, "acme.json", _site())

    names = [s["name"] for s in config._load_sites(str(tmp_path))]

    assert names == ["acme"]


def test_json_that_is_not_an_object_is_skipped(tmp_path):
    _write(tmp_path, "list.json", [1, 2, 3])
    _write(tmp_path, "acme.json", _site())

    names = [s["name"] for s in config._load_sites(str(tmp_path))]

    assert names == ["acme"]


def test_site_missing_a_required_key_is_skipped(tmp_path):
    _write(tmp_path, "nochat.json", {"name": "x", "wp_base": "https://x.test"})
    _write(tmp_path, "blankchat.json", _site(name="y", chat_id="   "))
    _write(tmp_path, "acme.json", _site())

    names = [s["name"] for s in config._load_sites(str(tmp_path))]

    assert names == ["acme"]


def test_missing_directory_yields_no_sites(tmp_path):
    # A fresh checkout has no sites/ dir yet — that is a state, not an error.
    assert config._load_sites(str(tmp_path / "does-not-exist")) == []


def test_non_json_files_are_ignored(tmp_path):
    _write(tmp_path, "README.md", "notes")
    _write(tmp_path, "acme.json", _site())

    names = [s["name"] for s in config._load_sites(str(tmp_path))]

    assert names == ["acme"]


def test_sites_are_ordered_by_filename(tmp_path):
    _write(tmp_path, "c.json", _site(name="charlie"))
    _write(tmp_path, "a.json", _site(name="alpha"))
    _write(tmp_path, "b.json", _site(name="bravo"))

    names = [s["name"] for s in config._load_sites(str(tmp_path))]

    assert names == ["alpha", "bravo", "charlie"]


def test_defaults_are_not_shared_between_sites(tmp_path):
    # {**_SITE_DEFAULTS, **raw} must not hand two sites the same list object —
    # a per-site mutation would otherwise leak across channels.
    _write(tmp_path, "a.json", _site(name="alpha"))
    _write(tmp_path, "b.json", _site(name="bravo"))

    first, second = config._load_sites(str(tmp_path))
    first["views_phase1"].append(99)

    assert second["views_phase1"] == [500, 5000]
