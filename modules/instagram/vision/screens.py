"""Which Instagram screen a hierarchy dump shows — pure, offline-testable.

`classify(xml_or_nodes) -> Screen(name, nodes)`. Names:

    security        a checkpoint / "confirm it's you" / captcha — the driver STOPS
    lockscreen      Android keyguard / AOD in front — the driver STOPS
    other_app       something that isn't Instagram is in front (e.g. Play Protect)
    popup:<kind>    a known IG popup the driver knows how to dismiss (POPUPS)
    popup:unknown   a dialog-shaped screen the driver doesn't know — STOPS
    music_preview   music picker with a track previewing (select bar at the bottom)
    music_picker    music picker (search box + track list)
    caption_editor  full-screen Caption editor (Done top-right)
    composer        final "New post" screen with Share
    editor          carousel editor (Audio / Text / … + Next in the tray)
    gallery         "New post" gallery picker
    profile         own Profile tab
    home            Home feed
    account_switcher
    unknown

Every rule was read off real IG 457 dumps (data/ig_screens/2026-11-25, scrubbed
copies in tests/fixtures/ig_vision/). Order matters: overlays (security,
popups, the music sheet) are checked before the screens they sit on, because
the screen underneath stays in the dump.
"""

import re
from dataclasses import dataclass, field

from modules.instagram.vision import uixml

PKG = uixml.PKG

# Text that means a security check. Never solved by the driver (hard rule 4).
SECURITY_RE = re.compile(
    r"confirm (it'?s|that it'?s) you|help us confirm|suspicious (login|activity)|"
    r"we detected|unusual (login|activity)|security code|enter (the )?code|"
    r"captcha|i'?m not a robot|verify your (account|identity)|we suspended|"
    r"account (has been )?(disabled|suspended)|try again later|"
    r"we restrict certain activity|action blocked",
    re.I,
)

# Known popups: kind -> (how to recognise it from texts, label of the button to tap)
POPUPS = {
    "sharing_info": (lambda t: "Sharing posts" in t and "OK" in t, "OK"),
    "reel_draft": (lambda t: "Start new video" in t, "Start new video"),
    # Back out of the editor on IG 457: "Start over? If you go back now, you
    # will lose this draft." — Start over / Save draft / Keep editing.
    "start_over": (lambda t: "Start over?" in t and "Start over" in t, "Start over"),
    "discard_post": (lambda t: any(s.startswith("Discard") for s in t)
                     and any(k in " ".join(t) for k in ("Keep editing", "Save draft", "Discard post",
                                                       "Discard edits", "lose")), "Discard"),
}


@dataclass
class Screen:
    name: str
    nodes: list = field(default_factory=list, repr=False)

    def rid(self, rid):
        return uixml.find(self.nodes, rid=rid, min_size=2)

    def first(self, rid=None, text=None, desc=None, desc_prefix=None):
        hits = uixml.find(self.nodes, rid=rid, text=text, desc=desc, desc_prefix=desc_prefix, min_size=2)
        return hits[0] if hits else None

    @property
    def is_popup(self):
        return self.name.startswith("popup:")


def _ig_nodes(ns):
    return [n for n in ns if n.get("package") == PKG]


def classify(xml_or_nodes):
    ns = uixml.nodes(xml_or_nodes) if isinstance(xml_or_nodes, str) else xml_or_nodes
    ig = _ig_nodes(ns)
    rids = {n.rid for n in ig if n.width > 1 and n.height > 1}
    texts = [t for n in ig for t in (n.text, n.desc) if t]
    joined = "\n".join(texts)

    # Anything else in front of IG first: the keyguard, then another app.
    pkgs = {n.get("package") for n in ns if n.width > 1 and n.height > 1}
    if any("keyguard" in n.rid or "aodservice" in n.get("package") for n in ns) and not ig:
        return Screen("lockscreen", ns)
    if SECURITY_RE.search(joined):
        return Screen("security", ns)
    if not ig:
        return Screen("other_app" if pkgs - {"com.android.systemui"} else "unknown", ns)

    for kind, (match, _button) in POPUPS.items():
        if match(texts):
            return Screen(f"popup:{kind}", ns)

    if "row_search_edit_text" in rids and ("layout_container_bottom_sheet" in rids
                                           or any(r.startswith("audio_browser_track_") for r in rids)):
        if "audio_bar_select_tap_target" in rids:
            return Screen("music_preview", ns)
        return Screen("music_picker", ns)
    # Two layouts per account: a ✓ `caption_done_button` (vestra.24) or an
    # "OK" `next_button_textview` (westorn.24, 2026-11-25) — both titled Caption.
    if "caption_input_text_view" in rids and (
        "caption_done_button" in rids
        or any(n.rid == "action_bar_textview_title" and n.text == "Caption" for n in ig)
    ):
        return Screen("caption_editor", ns)
    if {"share_footer_button", "share_button"} & rids and "caption_input_text_view" in rids:
        return Screen("composer", ns)
    if "media_thumbnail_tray_button" in rids and any(n.desc == "Audio" for n in ig):
        return Screen("editor", ns)
    if "gallery_grid_item_thumbnail" in rids and ("new_post_title" in rids or "cam_dest_feed" in rids):
        return Screen("gallery", ns)
    # The switcher is a sheet ON the Profile screen — the profile stays in
    # the dump, so it must be recognised first.
    if any(n.desc == "Dismiss" for n in ig) and (
        account_rows(Screen("x", ns)) or re.search(r"Add Instagram|Go to Meta Account settings", joined)
    ):
        return Screen("account_switcher", ns)
    if "action_bar_title" in rids and any(r.startswith("profile_header_") for r in rids):
        return Screen("profile", ns)
    # Home comes in (at least) two layouts per account: the Instagram logo
    # title, or a "For you ⌄" feed picker (seen on eur24news, 2026-11-25).
    # What they share: the tab bar + the action-bar Create button.
    tab_bar = {"feed_tab", "profile_tab"} <= rids
    if any(n.desc == "Instagram Home Feed" for n in ig) or (
        tab_bar and (
            any(n.rid == "action_bar_left_button" and n.desc == "Create" for n in ig)
            or "title_text" in rids
            or any(n.rid == "feed_tab" and n.flag("selected") for n in ig)
        )
    ):
        return Screen("home", ns)
    # Shaped like a dialog (a few buttons, no tab bar) but not one we know.
    if "feed_tab" not in rids and len(ig) < 25 and any(
        n.get("class", "").endswith("Button") or n.rid in ("igds_button", "primary_button") for n in ig
    ):
        return Screen("popup:unknown", ns)
    return Screen("unknown", ns)


def popup_button(screen):
    """The node to tap to dismiss a known popup, or None."""
    kind = screen.name.split(":", 1)[1] if screen.is_popup else None
    if kind not in POPUPS:
        return None
    label = POPUPS[kind][1]
    ig = _ig_nodes(screen.nodes)
    for n in ig:
        if n.text == label or n.desc == label:
            # A label's TextView is often not the clickable view; tapping
            # inside its bounds still lands on the button it sits in.
            return n
    for n in ig:
        if n.text.startswith(label) or n.desc.startswith(label):
            return n
    return None


# --------------------------------------------------------------------------- #
# Readers for things the flow verifies                                        #
# --------------------------------------------------------------------------- #

_SEL_NUM_RE = re.compile(r"^Selected media number (\d+)")


def gallery_thumbs(screen):
    """Gallery thumbnails fully visible ABOVE the POST/STORY/REEL strip, in
    visual order, as dicts {node, number, selected}. `number` is the
    multi-select position IG writes into the description ("Selected media
    number 2 …"), None when unpicked or in single-select."""
    strip_top = min((n.bounds[1] for n in screen.nodes if n.rid.startswith("cam_dest_")), default=10**6)
    out = []
    for n in uixml.find(screen.nodes, rid="gallery_grid_item_thumbnail", min_size=40):
        if n.bounds[3] > strip_top or n.height < 0.8 * n.width:
            continue  # hidden behind the destination strip or clipped
        m = _SEL_NUM_RE.match(n.desc)
        out.append({
            "node": n,
            "number": int(m.group(1)) if m else None,
            "selected": not n.desc.lower().startswith("unselected"),
        })
    out.sort(key=lambda t: (t["node"].bounds[1], t["node"].bounds[0]))
    return out


def multi_select_on(screen):
    """True when the gallery is in multi-select (the toggle reads 'Cancel' or
    any thumbnail carries a selection number)."""
    if any(t["number"] for t in gallery_thumbs(screen)):
        return True
    label = screen.first(rid="slideout_iconview_text")
    return bool(label and label.text.lower() == "cancel")


_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


def account_rows(screen):
    """Rows of the account-switcher sheet in visual order: dicts {node,
    handle, active}. A row is a clickable, id-less view whose description is
    the handle (optionally followed by ", 5 comments and 4 more") with a
    TextView of exactly that handle inside it; the active account's row is
    `selected`."""
    ig = [n for n in screen.nodes if n.get("package") == PKG]
    labels = [n for n in ig if n.text and not n.rid and _HANDLE_RE.match(n.text)]
    rows = []
    for n in ig:
        if n.rid or not n.flag("clickable") or not n.desc:
            continue
        head = n.desc.split(", ", 1)[0]
        if not _HANDLE_RE.match(head):
            continue
        lx, ly, rx, ry = n.bounds
        if any(t.text == head and lx <= t.bounds[0] and t.bounds[2] <= rx and ly <= t.bounds[1]
               and t.bounds[3] <= ry for t in labels):
            rows.append({"node": n, "handle": head, "active": n.flag("selected")})
    rows.sort(key=lambda r: r["node"].bounds[1])
    return rows


def track_rows(screen):
    """Music picker rows in visual order: dicts {node, title, artist, desc}."""
    rows = []
    for n in screen.nodes:
        if n.rid.startswith("audio_browser_track_") and n.height > 40:
            m = re.match(r"Select track (.+?) by (.+?)(?:, royalty-free)?, \d+:\d\d", n.desc)
            rows.append({
                "node": n,
                "title": m.group(1) if m else "",
                "artist": m.group(2) if m else "",
                "desc": n.desc,
            })
    rows.sort(key=lambda r: r["node"].bounds[1])
    return rows


def composer_track(screen):
    """(title, artist) of the music on the composer, or None."""
    t = screen.first(rid="music_track_title")
    if not t:
        return None
    a = screen.first(rid="music_track_subtitle")
    return (t.text, a.text if a else "")


def caption_text(screen):
    n = screen.first(rid="caption_input_text_view")
    if not n:
        return None
    return "" if n.text == n.get("hint") else n.text


def dump_form(text):
    """How uiautomator writes `text` into a dump: every character outside the
    BMP (emoji) becomes '..' (one dot per UTF-16 half)."""
    return "".join(".." if ord(c) > 0xFFFF else c for c in text)


def profile_info(screen):
    """{account, posts, first_tile} off the own-Profile screen."""
    title = screen.first(rid="action_bar_title")
    count = screen.first(rid="profile_header_familiar_post_count_value")
    tiles = [n for n in screen.nodes if n.rid == "image_button" and "row 1, column 1" in n.desc]
    posts = None
    if count:
        digits = count.text.replace(",", "").replace(" ", "")
        posts = int(digits) if digits.isdigit() else None
    return {
        "account": (title.text or title.desc).strip() if title else None,
        "posts": posts,
        "first_tile": tiles[0].desc if tiles else None,
    }
