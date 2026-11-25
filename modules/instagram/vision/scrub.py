"""Scrub a recorded hierarchy dump into a test fixture that can be committed.

    py modules/instagram/vision/scrub.py data/ig_screens/<day>/<file>.xml -o tests/fixtures/ig_vision/composer.xml

What goes:
* every node that isn't Instagram's (status bar, carrier, notifications);
* every IG username / display name found in the dump (story tray, feed
  headers, profile title, "Reel by …", "Profile picture of …") → user1, user2…
  — the account the dump was taken on becomes `test_account` / `Test Account`;
* follower / following / view counts → 0, profile bio and any other
  free text that isn't UI chrome → "text".
What stays: resource-ids, UI labels, bounds, flags, track titles (public
library), gallery dates and the caption field (it holds only test text).

Screenshots are never scrubbed or committed — they stay in data/ig_screens/.
Always read the scrubbed file before committing it; `leftovers()` lists the
free text that survived so you can check.
"""

import argparse
import re
import xml.etree.ElementTree as ET

PKG = "com.instagram.android"

_HANDLE_PATTERNS = [
    re.compile(r"^(\S+)'s story"),
    re.compile(r"^Profile picture of (\S+)"),
    re.compile(r"^(\S+) posted a"),
    re.compile(r"^Reel by (\S+) at row"),
    re.compile(r"^Photo by (\S+) at row"),
    re.compile(r"^Post by (\S+) at row"),
    re.compile(r"^([A-Za-z0-9._]+), \d+ (?:comments?|likes?|notifications?|messages?)\b"),
]
_HANDLE_RIDS = {"username", "row_feed_photo_profile_name", "action_bar_title", "action_bar_large_title"}
_NAME_RIDS = {"profile_header_full_name_above_vanity", "profile_header_full_name"}
_COUNT_RIDS = {"profile_header_familiar_followers_value", "profile_header_familiar_following_value"}
# Free text that may stay: IG's own UI words, short labels, numbers, dates.
_KEEP_RIDS = {"caption_input_text_view", "song_title", "artist_name", "music_track_title",
              "music_track_subtitle", "ig_text", "suggestion_text", "header", "new_post_title",
              "action_bar_textview_title", "media_thumbnail_tray_button_text", "slideout_iconview_text",
              "gallery_grid_item_label", "audio_suggested_text", "caption_format_dropdown",
              "tag_people_string", "location_label", "profile_header_familiar_post_count_value",
              "profile_header_familiar_post_count_label", "profile_header_familiar_followers_label",
              "profile_header_familiar_following_label", "igds_prism_chip_label",
              "pog_bubble_text", "map_content_education", "row_search_edit_text"}
_UI_TEXT_RE = re.compile(r"^[\x20-\x7e]{0,40}$")  # short plain-ASCII labels


def _rid(el):
    rid = el.get("resource-id", "")
    return rid.split(":id/", 1)[1] if ":id/" in rid else rid


def _strip_foreign(parent):
    for child in list(parent):
        if child.tag == "node" and child.get("package") not in (PKG, None):
            parent.remove(child)
        else:
            _strip_foreign(child)


def scrub(xml, account=None, display_name=None, rename=None):
    """`rename` = {real handle: fixture handle} for handles a test needs to
    name (e.g. the account a switch goes to); everyone else is userN."""
    root = ET.fromstring(xml)
    _strip_foreign(root)
    els = [el for el in root.iter("node")]

    from modules.instagram.vision import screens, uixml

    handles, names = [], []
    sheet = screens.Screen("x", uixml.nodes(ET.tostring(root, encoding="unicode")))
    handles += [r["handle"] for r in screens.account_rows(sheet)]
    for el in els:
        for attr in ("text", "content-desc"):
            v = el.get(attr, "")
            for pat in _HANDLE_PATTERNS:
                m = pat.match(v)
                if m:
                    handles.append(m.group(1))
        if _rid(el) in _HANDLE_RIDS and el.get("text") and el.get("text") != "Your story":
            handles.append(el.get("text").strip())
        if _rid(el) in _NAME_RIDS and el.get("text"):
            names.append(el.get("text").strip())
    if account:
        handles.insert(0, account)
    if display_name:
        names.insert(0, display_name)

    mapping = dict(rename or {})
    for h in handles:
        h = h.strip().lstrip("@")
        if h and h not in mapping:
            mapping[h] = "test_account" if account and h == account else f"user{len(mapping) + 1}"
    if account:
        mapping[account] = "test_account"
    for nm in names:
        if nm and nm not in mapping:
            mapping[nm] = "Test Account"
    # longest first so "vestra.24" is replaced before "vestra"
    subs = sorted(mapping.items(), key=lambda kv: -len(kv[0]))

    for el in els:
        rid = _rid(el)
        for attr in ("text", "content-desc"):
            v = el.get(attr)
            if not v:
                continue
            for old, new in subs:
                v = re.sub(rf"(?<![\w.]){re.escape(old)}(?![\w])", new, v)
            if rid in _COUNT_RIDS:
                v = "0"
            elif re.match(r"^\d[\d.,]*[KM]?(followers|following)$", v):
                v = "0" + re.sub(r"^[\d.,]+[KM]?", "", v)
            elif re.search(r"\bviews? in the last", v):
                v = "0 views in the last 30 days."
            elif rid not in _KEEP_RIDS and not _UI_TEXT_RE.match(v) and not any(
                new in v for _old, new in subs
            ) and not v.startswith(("Select track", "Save ", "Selected", "Unselected", "Pause track")):
                v = "text"
            el.set(attr, v)
    return ET.tostring(root, encoding="unicode")


def leftovers(xml):
    """Every text / content-desc in a scrubbed dump, for a human to read
    before committing it."""
    root = ET.fromstring(xml)
    out = set()
    for el in root.iter("node"):
        for attr in ("text", "content-desc"):
            if el.get(attr):
                out.add(el.get(attr))
    return sorted(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Scrub a hierarchy dump into a committable fixture.")
    ap.add_argument("src")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--account", help="the IG handle the dump was taken on")
    ap.add_argument("--name", help="its display name")
    ap.add_argument("--show", action="store_true", help="print the surviving text")
    args = ap.parse_args(argv)
    with open(args.src, encoding="utf-8") as f:
        out = scrub(f.read(), args.account, args.name)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(out)
    if args.show:
        for t in leftovers(out):
            print(" ", t)


if __name__ == "__main__":
    main()
