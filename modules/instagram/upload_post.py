"""
upload_post.py

Upload a single photo post to Instagram end-to-end:
  1. Pick the next image + sidecar JSON from `posts/` (alphabetical order).
  2. Push the image to the phone's Camera folder + trigger MediaScanner.
  3. Drive IG: Profile → + → Post → latest file → Next → Next → caption → Share.
  4. Archive the image + JSON into `posts/posted/`.

Post format: drop `001.jpg` in `posts/` and `001.json` next to it.

JSON schema (both fields optional):
    {
      "caption": "single string"  OR  ["paragraph 1", "", "paragraph 3"],
      "hashtags": ["travel", "sunset", "beachlife"]
    }
- caption: either a single string (\\n for line breaks) or an array of
  strings joined with \\n. Empty entries become blank lines.
- hashtags: array of strings. Leading `#` optional — script adds one.
  Entries must be alphanumeric + underscore; invalid ones are skipped.

How the caption reaches IG:
- caption body + hashtags are concatenated (body + blank line + space-
  separated #tags) and pasted in one shot via uiautomator2's FastInput IME.
  This mimics a user composing externally and pasting whole, and
  side-steps the OneUI clipboard SecurityException that blocks the
  shell from setting the system clipboard.

Most resource-id / text selectors below are best guesses — IG renames
things across versions. When a step fails, dump the UI of the current
screen with `py modules/instagram/dump_ui.py` and update the selector.
"""

import json
import os
import random
import re
import shutil
import sys
import time

import uiautomator2 as u2

# Make the repo root importable so `from shared import config` resolves when
# this file is run directly (`py modules/instagram/upload_post.py`): in that
# case sys.path[0] is this file's dir, not the repo root.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shared import config  # noqa: E402  (needs the sys.path bootstrap above)

# config loads the repo-root .env and resolves PHONE_ADDRESS (WiFi IP:port
# preferred, USB serial fallback). Re-exported here so `ig.DEVICE_ID` keeps
# working for server.py and the Telegram bot.
DEVICE_ID = config.DEVICE_ID
PKG = "com.instagram.android"


# --------------------------------------------------------------------------- #
# Generic helpers                                                             #
# --------------------------------------------------------------------------- #

def pause(a=1.5, b=2.5):
    time.sleep(random.uniform(a, b))


def _dump_screen(d, tag):
    """Write the current UI hierarchy to `ui_dump_<tag>.xml` so a
    selector miss leaves diagnostic state on disk without the user
    needing to manually run `dump_ui.py` at the right moment. Returns
    the file path so callers can mention it in error messages."""
    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        f"ui_dump_{tag}.xml",
    )
    try:
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(d.dump_hierarchy())
    except Exception as exc:
        # Diagnostic dump is best-effort — don't mask the original error.
        print(f"    (failed to write diagnostic dump {path}: {exc})")
        return None
    return path


def push_media_to_phone(d, local_path, suffix=""):
    """Push to /sdcard/DCIM/Camera/ and fire MediaScanner so IG's gallery
    picker sees the new file immediately rather than after the next rescan.
    Works for both photos (.jpg/.png) and videos (.mp4/.mov). `suffix`
    keeps the names of a carousel's files apart when they are pushed in
    the same second."""
    ext = os.path.splitext(local_path)[1] or ".jpg"
    filename = f"post_{int(time.time())}{suffix}{ext}"
    remote_path = f"/sdcard/DCIM/Camera/{filename}"
    d.push(local_path, remote_path)
    d.shell(
        f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
        f"-d file://{remote_path}"
    )
    return remote_path


def push_carousel_to_phone(d, paths):
    """Push a carousel's files so the gallery shows them in carousel order.

    IG's gallery is newest first, so the files go up in REVERSE (last
    slide first, first slide last) with a gap of over a second between
    them: the first slide ends up as the gallery's top-left thumbnail,
    the second next to it, and so on — and IG numbers a multi-select in
    the order the thumbnails are tapped, so tapping them left to right
    gives the carousel order. Without the gap the files share a
    timestamp and the gallery order between them is undefined."""
    for i, path in reversed(list(enumerate(paths, 1))):
        push_media_to_phone(d, path, suffix=f"_{i:02d}")
        if i > 1:
            time.sleep(1.2)


# --------------------------------------------------------------------------- #
# Queue parsing                                                               #
# --------------------------------------------------------------------------- #

def _resolve_caption_body(value):
    """Accept a string OR a list of strings (joined with \\n)."""
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(str(line) for line in value)
    return str(value)


_HASHTAG_OK = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
)


def _resolve_hashtags(value):
    """Accept an array of tags or a space-separated string. Strip `#`,
    validate alphanumeric + underscore; drop malformed entries with a warning."""
    if not value:
        return []
    if isinstance(value, str):
        candidates = value.split()
    elif isinstance(value, list):
        candidates = [str(t) for t in value]
    else:
        return []
    out = []
    for t in candidates:
        t = t.strip().lstrip("#").strip()
        if not t:
            continue
        if all(c in _HASHTAG_OK for c in t):
            out.append(t)
        else:
            print(f"  ⚠ skipping invalid hashtag: {t!r}")
    return out


def find_next_post(posts_dir):
    """Return (image_path, caption_body, hashtags) for the next queued post,
    or (None, "", []) if the queue is empty. Alphabetical order — prefix
    file names (001_, 002_, ...) to control posting sequence. A carousel's
    slides are skipped — `find_next_carousel` owns those."""
    in_carousel = {p for paths in find_carousels(posts_dir).values() for p in paths}
    images = sorted(
        os.path.join(posts_dir, f)
        for f in os.listdir(posts_dir)
        if f.lower().endswith((".jpg", ".jpeg", ".png"))
        and os.path.isfile(os.path.join(posts_dir, f))
        and os.path.join(posts_dir, f) not in in_carousel
    )
    if not images:
        return None, "", []
    image_path = images[0]
    body, hashtags = _read_meta(os.path.splitext(image_path)[0] + ".json")
    return image_path, body, hashtags


def _read_meta(meta_path):
    """(caption_body, hashtags) from a sidecar JSON; ("", []) when absent."""
    if not os.path.exists(meta_path):
        return "", []
    with open(meta_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return (
        _resolve_caption_body(data.get("caption")),
        _resolve_hashtags(data.get("hashtags")),
    )


# A carousel in the queue is `<stub>_1.jpg`, `<stub>_2.mp4`, … plus ONE
# `<stub>.json` — the same `<stub>_<n>` naming reel_downloader.download_photos
# gives a downloaded album. Photos and videos can be mixed.
CAROUSEL_MAX = 20  # IG's own limit per carousel post
_CAROUSEL_EXT = (".jpg", ".jpeg", ".png", ".mp4", ".mov")
_CAROUSEL_RE = re.compile(r"^(?P<stub>.+)_(?P<n>\d+)\.[^.]+$")


def find_carousels(posts_dir):
    """{stub: [paths in slide order]} for every carousel queued in
    `posts_dir`. A stub counts only when it has at least two numbered
    files, so a lone `x_1.jpg` stays an ordinary single post."""
    groups = {}
    for f in os.listdir(posts_dir):
        full = os.path.join(posts_dir, f)
        if not (f.lower().endswith(_CAROUSEL_EXT) and os.path.isfile(full)):
            continue
        m = _CAROUSEL_RE.match(f)
        if m:
            groups.setdefault(m["stub"], []).append((int(m["n"]), full))
    return {
        stub: [p for _n, p in sorted(items)]
        for stub, items in sorted(groups.items())
        if len(items) >= 2
    }


def find_next_carousel(posts_dir):
    """(paths, caption_body, hashtags, meta_path) for the alphabetically
    first queued carousel, or (None, "", [], None) when there is none."""
    carousels = find_carousels(posts_dir)
    if not carousels:
        return None, "", [], None
    stub, paths = next(iter(carousels.items()))
    meta_path = os.path.join(posts_dir, f"{stub}.json")
    body, hashtags = _read_meta(meta_path)
    return paths, body, hashtags, meta_path


# --------------------------------------------------------------------------- #
# Text input — paste the whole caption in one shot                            #
# --------------------------------------------------------------------------- #

def inject_text(d, text):
    """Paste `text` into the currently focused input field in one shot.

    The obvious path — `d.set_clipboard(text)` + KEYCODE_PASTE — is dead
    on modern Samsung/OneUI: shell UID 2000 can't claim the `android`
    package, so AppOpsManager blocks `ClipboardManager.setPrimaryClip`
    with `java.lang.SecurityException: Package android does not belong
    to 2000`.

    Workaround: activate uiautomator2's FastInput IME and use its
    broadcast-based text injection. Text appears instantly in the field
    — no per-char shell round-trips, no special-char escaping, no
    clipboard involvement at all. Visually identical to a paste.

    The FastInput IME APK ships with uiautomator2 and gets installed +
    enabled on first call. We flip back to the user's previous IME
    afterwards so the keyboard the user sees later is the one they had
    before the bot ran."""
    d.set_input_ime(True)
    try:
        time.sleep(0.3)
        d.send_keys(text)
    finally:
        d.set_input_ime(False)


# --------------------------------------------------------------------------- #
# Main flow                                                                   #
# --------------------------------------------------------------------------- #

def tap_profile_tab(d):
    """Tap the bottom-right Profile tab. IG renames resource IDs across
    versions, so we try a few stable selectors in order before giving up."""
    candidates = [
        d(resourceId=f"{PKG}:id/profile_tab"),
        d(description="Profile"),
        d(descriptionContains="Profile"),
    ]
    for sel in candidates:
        if sel.click_exists(timeout=3):
            return True
    return False


def tap_home_tab(d):
    """Tap the Home tab in the bottom nav. New IG (2025+) hosts the `+`
    create button on the Home action bar, so we route here before
    reaching for `+`. No-op effect when IG is already on Home."""
    candidates = [
        d(resourceId=f"{PKG}:id/feed_tab"),
        d(description="Home"),
        d(descriptionContains="Home"),
    ]
    for sel in candidates:
        if sel.click_exists(timeout=3):
            return True
    return False


def switch_account(d, target):
    """Open IG's quick-switch sheet (long-press on the Profile tab) and
    tap the row matching `target`. `target` is a bare username (no
    leading `@`). Raises RuntimeError with a UI-dump path when the row
    can't be located — paste the XML so we can refine the selector list.

    We don't verify the swap after tapping: post-switch IG usually lands
    on Home, not the new account's Profile, so any "read the active
    username" check ends up grabbing Home-feed labels ("Suggested for
    you", "Your story") and false-failing. The downstream
    tap_profile_tab → +  → … flow will naturally fail on the wrong
    account's quota / state if the swap somehow didn't take, which is
    enough signal."""
    target = target.strip().lstrip("@")

    # Be on the Profile tab first; some IG builds only surface the
    # quick-switch sheet when the long-press lands on the focused tab.
    tap_profile_tab(d)
    pause(0.5, 1.0)

    # Locate the Profile tab again, this time to long-press it rather
    # than click. Same candidate list as `tap_profile_tab`.
    tab = None
    for sel in [
        d(resourceId=f"{PKG}:id/profile_tab"),
        d(description="Profile"),
        d(descriptionContains="Profile"),
    ]:
        if sel.exists:
            tab = sel
            break
    if tab is None:
        raise RuntimeError(
            "Couldn't locate the Profile tab to long-press for account switch."
        )

    print(f"  → long-press Profile to open switcher (target @{target})")
    tab.long_click(duration=0.8)
    pause(1.0, 1.5)

    # Find and tap the row for the target account. Selectors are ordered
    # most-specific → most-permissive so we don't false-match a longer
    # username that happens to contain the target as a substring.
    row_candidates = [
        d(text=target),
        d(text=f"@{target}"),
        d(textStartsWith=target),
        d(description=target),
        d(descriptionContains=target),
        d(textContains=target),
    ]
    for sel in row_candidates:
        try:
            if sel.click_exists(timeout=2):
                break
        except Exception:
            continue
    else:
        dump_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "ui_dump_switcher.xml",
        )
        with open(dump_path, "w", encoding="utf-8") as fp:
            fp.write(d.dump_hierarchy())
        raise RuntimeError(
            f"Couldn't find @{target} in the account-switcher sheet. "
            f"UI dumped to {dump_path}."
        )

    pause(2.0, 3.0)  # IG swaps account data + redraws
    print(f"    (tapped @{target} in switcher)")


def tap_create_button(d):
    """Tap the `+` create button that opens the creation flow.

    New IG (2025+) places it at the top-left of the Home feed's action
    bar. The button itself is `NAF` (no resource-id, no content-desc),
    but the parent LinearLayout has a stable id — we click the parent
    and Android routes the tap to its single clickable child.

    Older IG built it into the Profile screen with a `Create`
    content-description; those selectors are kept as fallback.

    On miss, writes `ui_dump_create_button.xml`."""
    candidates = [
        # New: top-left of Home action bar.
        d(resourceId=f"{PKG}:id/action_bar_buttons_container_left"),
        # Older Profile-based selectors.
        d(description="Create"),
        d(description="New post"),
        d(descriptionContains="Create"),
        d(resourceId=f"{PKG}:id/action_bar_create_button"),
    ]
    for sel in candidates:
        if sel.click_exists(timeout=3):
            return True
    dump = _dump_screen(d, "create_button")
    if dump:
        print(f"    (+ button not found; dumped to {dump})")
    return False


def tap_post_type(d, kind="post"):
    """Pick the post destination.

    New IG (2025+) merges the post-type picker, gallery, and destination
    strip onto one screen. The destination labels are UPPERCASE
    (`POST` / `STORY` / `REEL` / `LIVE`) with stable `cam_dest_*`
    resource-ids — those are the primary selectors. Older IG used a
    separate bottom-sheet with mixed-case rows (`Reel`, `Post`, …);
    those selectors are kept as fallback.

    On miss, writes `ui_dump_post_type.xml` so a label drift is
    diagnosable without re-running the bot."""
    rid_by_kind = {
        "post": "cam_dest_feed",
        "story": "cam_dest_story",
        "reel": "cam_dest_clips",
        "live": "cam_dest_live",
    }
    upper = kind.upper()
    mixed = kind.capitalize()
    candidates = []
    rid = rid_by_kind.get(kind.lower())
    if rid:
        candidates.append(d(resourceId=f"{PKG}:id/{rid}"))
    candidates.extend([
        # New unified screen — uppercase label + accessibility desc.
        d(text=upper),
        d(description=upper),
        # Case-insensitive net for in-between IG versions.
        d(textMatches=f"(?i){re.escape(mixed)}"),
        # Older bottom-sheet rows (mixed case).
        d(text=mixed),
        d(description=mixed),
        d(textContains=mixed),
    ])
    for sel in candidates:
        try:
            if sel.click_exists(timeout=3):
                return True
        except Exception:
            continue
    dump = _dump_screen(d, "post_type")
    if dump:
        print(f"    (no {upper}/{mixed} row matched; dumped to {dump})")
    return False


def dismiss_reel_draft_modal(d, timeout=2.5):
    """If IG pops the 'Keep editing / Start new video' modal (only fires
    when an unfinished reel draft is on disk), always tap 'Start new
    video' so we begin fresh. Best-effort — returns False when the modal
    isn't present, which is the common case."""
    candidates = [
        d(text="Start new video"),
        d(textContains="new video"),
        d(description="Start new video"),
        d(descriptionContains="new video"),
    ]
    for sel in candidates:
        if sel.click_exists(timeout=timeout):
            print("    (draft modal: chose 'Start new video')")
            return True
    return False


def tap_latest_gallery_item(d):
    """Tap the freshest thumbnail in IG's gallery picker.

    Every real thumbnail (photo or video) has resource-id
    `gallery_grid_item_thumbnail`; the camera-open button that occupies
    the top-left slot of the reel picker doesn't, so it's safely skipped.
    Content descriptions read "Unselected Photo/Video thumbnail created
    on …" — using `descriptionStartsWith="Unselected"` as a secondary
    matcher catches both kinds in any locale that keeps that word.

    instance=0 is the latest file, which is the media we just pushed via
    `push_media_to_phone`."""
    selectors = [
        {"resourceId": f"{PKG}:id/gallery_grid_item_thumbnail"},
        {"descriptionStartsWith": "Unselected"},
        {"descriptionContains": "thumbnail"},
        # Legacy fallbacks for older IG builds that used a generic ID.
        {"descriptionContains": "Photo"},
        {"descriptionContains": "Video"},
        {"resourceId": f"{PKG}:id/gallery_grid_item"},
    ]
    for kwargs in selectors:
        try:
            if d(**kwargs, instance=0).click_exists(timeout=3):
                return True
        except Exception:
            continue
    return False


_NODE_RE = re.compile(r"<node\b[^>]*>")
_ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def gallery_thumbnails(xml):
    """The gallery's thumbnails in a hierarchy dump, in visual order (row
    by row, left to right): a list of dicts {bounds, desc, selected}.

    `selected` is True / False / None. IG describes an unpicked thumbnail
    as "Unselected Photo thumbnail created on …", so a description that
    doesn't start with "Unselected" is a picked one; the node's own
    `selected`/`checked` flag counts too. None means the dump gives no way
    to tell (no description at all) — the caller then taps blind.

    Read from the dump rather than through selector RPCs for the same
    reason as the hashtag strip: on the S23 the RPC has returned count=0
    for views the dump can see."""
    thumbs = []
    rid = f'{PKG}:id/gallery_grid_item_thumbnail'
    for node in _NODE_RE.findall(xml):
        attrs = dict(_ATTR_RE.findall(node))
        if attrs.get("resource-id") != rid:
            continue
        m = _BOUNDS_RE.fullmatch(attrs.get("bounds", ""))
        if not m:
            continue
        lx, ly, rx, ry = (int(g) for g in m.groups())
        if rx - lx < 20 or ry - ly < 20:  # clipped off the edge of the grid
            continue
        desc = attrs.get("content-desc", "")
        if attrs.get("selected") == "true" or attrs.get("checked") == "true":
            selected = True
        elif desc:
            selected = not desc.lower().startswith("unselected")
        else:
            selected = None
        thumbs.append({"bounds": (lx, ly, rx, ry), "desc": desc, "selected": selected})
    thumbs.sort(key=lambda t: (t["bounds"][1], t["bounds"][0]))
    return thumbs


def enable_multi_select(d):
    """Turn on the gallery's multi-select ("Select multiple" — the stacked
    squares icon above the grid). Returns True when a button was tapped.
    IG renames this one often; on a miss the caller falls back to a long
    press on the first thumbnail, which also enters multi-select on
    current builds."""
    candidates = [
        d(resourceId=f"{PKG}:id/multi_select_slide_button"),
        d(resourceId=f"{PKG}:id/gallery_multi_select_button"),
        d(resourceId=f"{PKG}:id/multi_select_button"),
        d(description="Select multiple"),
        d(text="Select multiple"),
        d(text="SELECT MULTIPLE"),
        d(descriptionContains="Select multiple"),
        d(descriptionContains="multiple"),
        d(textContains="Select multiple"),
    ]
    for sel in candidates:
        try:
            if sel.click_exists(timeout=2):
                return True
        except Exception:
            continue
    return False


def _tap_thumb(d, bounds, long=False):
    lx, ly, rx, ry = bounds
    x = random.randint(lx + (rx - lx) // 4, rx - (rx - lx) // 4)
    y = random.randint(ly + (ry - ly) // 4, ry - (ry - ly) // 4)
    if long:
        d.long_click(x, y, 0.8)
    else:
        d.click(x, y)


def select_carousel_items(d, n):
    """Pick the gallery's first `n` thumbnails, in order, as a carousel.

    push_carousel_to_phone put slide 1 at the top-left, slide 2 next to
    it, … so tapping them left to right numbers them 1..n. Two traps:

    * IG often counts the thumbnail that was being previewed as already
      picked (#1) the moment multi-select turns on — tapping it again
      would UNpick it. So each thumbnail is tapped only while the dump
      says it is unpicked.
    * Thumbnails past the visible grid aren't in the dump at all. With
      that many slides we stop with a dump instead of picking the wrong
      files.

    Raises RuntimeError (with a UI dump) when the count can't be met."""
    thumbs = gallery_thumbnails(d.dump_hierarchy())
    if len(thumbs) < n:
        dump = _dump_screen(d, "carousel_grid")
        raise RuntimeError(
            f"Only {len(thumbs)} gallery thumbnails are on screen, the carousel "
            f"needs {n}. UI dumped to {dump}."
        )

    if enable_multi_select(d):
        print("    (tapped Select multiple)")
        pause(0.8, 1.3)
    else:
        print("    (no Select multiple button — long-pressing the first thumbnail)")
        _tap_thumb(d, thumbs[0]["bounds"], long=True)
        pause(0.8, 1.3)

    blind = False
    for i in range(n):
        thumbs = gallery_thumbnails(d.dump_hierarchy())
        if len(thumbs) <= i:
            dump = _dump_screen(d, "carousel_grid")
            raise RuntimeError(
                f"Thumbnail {i + 1} of {n} disappeared from the grid. "
                f"UI dumped to {dump}."
            )
        state = thumbs[i]["selected"]
        if state is True:
            print(f"    slide {i + 1}: already picked")
            continue
        if state is None:
            blind = True
        print(f"    slide {i + 1}: tap")
        _tap_thumb(d, thumbs[i]["bounds"])
        pause(0.5, 1.0)

    thumbs = gallery_thumbnails(d.dump_hierarchy())
    picked = [t["selected"] for t in thumbs]
    if blind or None in picked[:n]:
        dump = _dump_screen(d, "carousel_grid")
        print(
            "    ⚠ couldn't read which thumbnails are picked; continuing blind. "
            f"Check the slides on the next screen. UI dumped to {dump}."
        )
        return
    wrong = [i + 1 for i in range(n) if not picked[i]]
    extra = sum(1 for p in picked[n:] if p)
    if wrong or extra:
        dump = _dump_screen(d, "carousel_grid")
        raise RuntimeError(
            f"Carousel selection is off: slides {wrong or '-'} unpicked, "
            f"{extra} extra thumbnail(s) picked. UI dumped to {dump}."
        )
    print(f"    ({n} slides picked)")


def tap_next(d, timeout=8):
    """Tap a Next button on the current screen.

    New IG places it at the top-right of the unified creation screen
    with resource-id `next_button_textview`. Older IG put it in
    different positions per screen (gallery top-right, filter
    bottom-left, reel-edit bottom-right) but kept the label `Next`. We
    try the stable id first, then text/description.

    On miss, writes `ui_dump_next.xml`."""
    candidates = [
        {"resourceId": f"{PKG}:id/next_button_textview"},
        {"text": "Next"},
        {"description": "Next"},
        {"descriptionContains": "Next"},
    ]
    for kwargs in candidates:
        try:
            if d(**kwargs).click_exists(timeout=timeout):
                return True
        except Exception:
            continue
    dump = _dump_screen(d, "next")
    if dump:
        print(f"    (Next button not found; dumped to {dump})")
    return False


_CAPTION_FOCUS_RE = re.compile(
    r'resource-id="com\.instagram\.android:id/caption_input_text_view"'
    r'[^>]*focused="true"'
)


def _keyboard_likely_up(d) -> bool:
    """The soft keyboard window isn't included in dump_hierarchy, but
    Android keeps the input field's `focused=true` flag set while the
    IME is open. If the caption field is focused, the keyboard is
    almost certainly on screen."""
    return bool(_CAPTION_FOCUS_RE.search(d.dump_hierarchy()))


def hide_keyboard_if_shown(d):
    """Press Back to dismiss the soft keyboard if the caption field is
    still focused. No-op otherwise — critical, because BACK on a closed
    keyboard navigates back from the composer and triggers a 'Discard
    post?' dialog."""
    if _keyboard_likely_up(d):
        d.press("back")
        time.sleep(0.7)


def tap_caption_ok_if_present(d, timeout=2.0):
    """On newer IG the caption field opens a full-screen editor with an
    'OK' (or 'Done') button at top-right. Tapping it commits the caption
    text and returns to the composer where Share lives — without it the
    composer never re-appears, and dismissing the keyboard via BACK can
    navigate the editor backwards instead of just closing the IME. Try
    OK first; the caller falls back to `hide_keyboard_if_shown` when
    this returns False (older IG inline-edits the caption and has no OK
    button)."""
    candidates = [
        d(text="OK"),
        d(text="Ok"),
        d(text="Done"),
        d(description="OK"),
        d(description="Done"),
    ]
    for sel in candidates:
        if sel.click_exists(timeout=timeout):
            print("    (tapped caption-editor OK)")
            return True
    return False


# The row's TextView resource-id is stable; we read its bounds live from
# `dump_hierarchy()` because the selector RPC on this device silently
# returns count=0 for elements in IG's autocomplete window even when the
# dump can see them.
_HASHTAG_ROW_BOUNDS_RE = re.compile(
    r'resource-id="com\.instagram\.android:id/row_hashtag_textview_tag_name"'
    r'[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"'
)


def _find_first_suggestion_bounds(d):
    """Return (lx, ly, rx, ry) of the first hashtag autocomplete row, or
    None if the strip isn't on screen right now."""
    xml = d.dump_hierarchy()
    m = _HASHTAG_ROW_BOUNDS_RE.search(xml)
    if not m:
        return None
    return tuple(int(g) for g in m.groups())


def tap_first_hashtag_suggestion(d, max_wait=6.0):
    """Wait for IG's hashtag autocomplete strip to appear, then tap the
    first row at its actual bounds. Returns True if a tap was issued,
    False if the strip never appeared within `max_wait` seconds — caller
    should treat that as a no-op (skip the tap, proceed to Share)."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        bounds = _find_first_suggestion_bounds(d)
        if bounds:
            lx, ly, rx, ry = bounds
            # Bias toward the visible tag text on the left half, with
            # jitter so we don't tap the same pixel every run.
            x = random.randint(lx + 30, lx + max(60, (rx - lx) // 2))
            y = random.randint(ly + 8, ry - 8)
            time.sleep(random.uniform(0.2, 0.5))  # brief "looking" beat
            print(f"    tap ({x},{y}) inside first suggestion row [{lx},{ly}]→[{rx},{ry}]")
            d.click(x, y)
            return True
        time.sleep(0.5)
    return False


def upload_post(d, image_path, caption_body, hashtags, kind="post", target_account=None):
    """Drive IG's new creation UI end to end:
        push media → open IG → [switch account] → Home → + → <kind>
        → latest file → Next → Next → caption + hashtags → Share.
    `kind` is "post" (photo) or "reel" (video) — the only branching point
    is which row gets tapped in the creation bottom-sheet; the rest of
    the flow is identical (or close enough that we adapt selectors when
    something diverges). Body + hashtags are pasted via FastInput IME.

    `target_account` (optional, bare username, no `@`): when set,
    switches to that account via the quick-switch sheet after landing on
    the Profile tab. None means "use whichever account IG opens with",
    which preserves single-account behavior for legacy callers (e.g.
    server.py's `/api/posts publish=true` path).

    `image_path` may also be a LIST of files: that is a carousel (see
    `upload_carousel`), posted through the Post tab with multi-select."""

    carousel = isinstance(image_path, (list, tuple))
    if carousel:
        if kind != "post":
            raise ValueError("A carousel goes through the Post tab (kind='post').")
        push_carousel_to_phone(d, image_path)
    else:
        push_media_to_phone(d, image_path)
    pause()

    # Wake the screen explicitly. `am start` (via app_start below) usually
    # wakes the device on its own, but if the launcher briefly renders the
    # lock screen first it can swallow the IG launch. Assumes no PIN/
    # pattern (Settings → Lock screen → Screen lock type → None on the
    # bot phone) — with a lock present this gets us to the lock screen,
    # not past it.
    d.screen_on()
    pause(0.4, 0.8)

    # Force-stop first so IG always cold-launches to the home feed. Without
    # this, a previous session can leave IG mid-flow (composer, settings,
    # DMs, …) and the Profile-tab tap below has nothing to hit.
    print("Force-stopping Instagram, then opening fresh...")
    d.app_start(PKG, stop=True)
    pause(3.5, 5.5)

    # Account switch (if requested) lives here, before we navigate to
    # Home. `switch_account` does its own Profile-tab tap internally
    # (the long-press surface needs to be Profile) and IG's redraw after
    # the swap usually lands somewhere other than Home — which is why
    # we explicitly tap Home next, regardless of whether we switched.
    if target_account:
        switch_account(d, target_account)
        pause()

    # New IG (2025+) puts `+` on the Home action bar, not on the Profile
    # screen. Make sure we're on Home before reaching for it.
    print("  → tap Home tab")
    if not tap_home_tab(d):
        raise RuntimeError(
            "Couldn't find the Home tab. Run `py modules/instagram/dump_ui.py` and grep ui_dump.xml "
            "for 'feed_tab' / 'Home' to find the new selector."
        )
    pause(0.5, 1.0)

    print("  → tap + (create) at top-left of Home")
    if not tap_create_button(d):
        raise RuntimeError(
            "Couldn't find the + create button. Run `py modules/instagram/dump_ui.py` and grep "
            "ui_dump.xml for 'action_bar_buttons_container_left' or 'Create' "
            "to find the new selector."
        )
    pause()

    print(f"  → tap {kind.capitalize()} in the creation sheet")
    if not tap_post_type(d, kind):
        raise RuntimeError(
            f"Couldn't find {kind.capitalize()!r} in the creation sheet. Run "
            "`py modules/instagram/dump_ui.py` and check ui_dump.xml for the row label."
        )
    pause()

    if kind == "reel":
        # If a previous attempt left an unfinished draft, IG interjects a
        # 'Keep editing / Start new video' modal here. Always discard the
        # draft so the gallery picker actually opens.
        if dismiss_reel_draft_modal(d):
            pause()

    if carousel:
        print(f"  → multi-select {len(image_path)} slides in gallery")
        select_carousel_items(d, len(image_path))
    else:
        print("  → tap latest thumbnail in gallery")
        if not tap_latest_gallery_item(d):
            raise RuntimeError(
                "Couldn't tap the latest thumbnail. Run `py modules/instagram/dump_ui.py` and check "
                "ui_dump.xml for the gallery grid item selector."
            )
    pause(0.6, 1.2)

    print("  → tap Next after gallery")
    if not tap_next(d):
        raise RuntimeError(
            "Couldn't find Next on the gallery screen. Check ui_dump.xml."
        )
    pause()

    if kind == "post":
        # Photo flow has a filter/edit screen between gallery and composer.
        # Reels skip it — gallery Next lands directly on the composer.
        print("  → tap Next after filter/edit")
        if not tap_next(d):
            raise RuntimeError(
                "Couldn't find Next on the filter/edit screen. Check ui_dump.xml."
            )
        pause()

    # Build the full caption (body + blank line + space-separated #tags)
    # and paste it all at once. Mimics a human composing in a notes app
    # and pasting whole — also less suspicious than a concentrated typing
    # burst across an entire caption.
    full_text = caption_body
    if hashtags:
        tag_line = " ".join(f"#{t}" for t in hashtags)
        full_text = f"{full_text}\n\n{tag_line}" if full_text else tag_line

    if full_text:
        print("  → focus caption field (placeholder 'Add a caption...')")
        if not d(textContains="caption").click_exists(timeout=10):
            raise RuntimeError(
                "Couldn't find the caption field. Check ui_dump.xml for the "
                "placeholder text of the input."
            )
        pause(0.5, 1.0)

        print(
            f"  → paste full caption ({len(full_text)} chars, body + "
            f"{len(hashtags)} hashtag(s))"
        )
        inject_text(d, full_text)
        pause(0.5, 1.0)

    # IG fires its hashtag autocomplete strip whenever the focused field
    # ends on (or contains) a `#tag`, regardless of whether the tag came
    # from the `hashtags` array or was embedded directly in `caption_body`.
    # The telegram-bot path takes that second route — it pastes the
    # source IG caption verbatim (which already has #tags inline) and
    # leaves the array empty. Detect either case.
    has_hashtags = bool(hashtags) or bool(re.search(r"(?<!\w)#\w+", full_text))
    if has_hashtags:
        # The last pasted `#tag` usually triggers IG's autocomplete strip
        # and it overlays Share. We wait briefly for it; if it shows, we
        # accept the top suggestion to close the strip. If it never
        # appears (faster paste, already-known tag, locale quirk, etc.) we
        # just proceed — Share should still be reachable.
        print("  → wait for hashtag autocomplete strip")
        if tap_first_hashtag_suggestion(d):
            pause()
        else:
            print("    (no autocomplete strip detected; proceeding to Share)")

    # Two IG versions diverge here.
    #
    # New (full-screen caption editor): tapping the caption field opens
    # an editor sheet with an 'OK' (or 'Done') button at the top right;
    # Share lives on the composer screen behind it. We must tap OK to
    # commit the caption and return to the composer. Pressing BACK to
    # dismiss the keyboard instead navigates the editor backwards —
    # which is the "got sent back to editing" symptom we keep hitting.
    #
    # Old (inline caption): no editor, no OK button — the soft keyboard
    # is what overlaps Share, and BACK is the only thing that clears it.
    #
    # Try OK first; if it isn't there we're on the old flow and fall
    # back to the BACK-keyboard-dismiss path.
    print("  → commit caption editor (OK top-right) or dismiss keyboard")
    if tap_caption_ok_if_present(d):
        pause()
    else:
        hide_keyboard_if_shown(d)
        pause(0.8, 1.3)

    print("  → tap Share (bottom of composer)")
    if not d(resourceId=f"{PKG}:id/share_button").click_exists(timeout=10):
        # Resource-id is the safer match (avoids hitting any other view
        # that happens to read 'Share') but fall back to text in case IG
        # renames the id in a future build.
        if not d(text="Share").click_exists(timeout=5):
            raise RuntimeError("Couldn't find Share. Check ui_dump.xml.")

    # Verify the tap actually published. After a real Share the composer
    # tears down and the `share_button` view disappears from the tree.
    # We poll for up to 15 s because the transition can take a couple of
    # seconds on slower networks (the composer stays up while IG queues
    # the upload). Sniffing for hint text like "Write a caption…" was
    # unreliable — that string lives on as the field's `hint=` attribute
    # even after the caption is filled.
    deadline = time.time() + 15.0
    shared = False
    final_xml = ""
    while time.time() < deadline:
        final_xml = d.dump_hierarchy()
        if 'resource-id="com.instagram.android:id/share_button"' not in final_xml:
            shared = True
            break
        time.sleep(0.5)
    if not shared:
        dump_path = os.path.join(
            os.path.dirname(__file__), "ui_dump_share_fail.xml"
        )
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(final_xml)
        raise RuntimeError(
            "Share tap didn't publish — share_button still on screen after 15s. "
            f"UI dumped to {dump_path}."
        )

    print("  → waiting for upload to finish...")
    # A carousel uploads every slide before IG lets go of it.
    extra = 3 * (len(image_path) - 1) if carousel else 0
    time.sleep(random.uniform(8, 12) + extra)


def upload_carousel(d, paths, caption_body, hashtags, target_account=None):
    """Post 2..CAROUSEL_MAX photos/videos as ONE carousel post, in the
    order given, through the phone (ADB) — Post tab → Select multiple →
    the slides → Next → Next → caption → Share."""
    paths = list(paths)
    if not 2 <= len(paths) <= CAROUSEL_MAX:
        raise ValueError(f"A carousel takes 2-{CAROUSEL_MAX} files, got {len(paths)}.")
    for p in paths:
        if not os.path.isfile(p):
            raise FileNotFoundError(p)
        if not p.lower().endswith(_CAROUSEL_EXT):
            raise ValueError(f"Not a photo/video IG takes in a carousel: {p}")
    upload_post(d, paths, caption_body, hashtags, kind="post",
                target_account=target_account)


def archive_post(posts_dir, image_path):
    """Move image + JSON sidecar into posts/posted/ so they don't re-upload."""
    archive_dir = os.path.join(posts_dir, "posted")
    os.makedirs(archive_dir, exist_ok=True)
    meta_path = os.path.splitext(image_path)[0] + ".json"
    shutil.move(image_path, os.path.join(archive_dir, os.path.basename(image_path)))
    if os.path.exists(meta_path):
        shutil.move(meta_path, os.path.join(archive_dir, os.path.basename(meta_path)))


def archive_carousel(posts_dir, paths, meta_path=None):
    """Move a carousel's slides + its `<stub>.json` into posts/posted/."""
    archive_dir = os.path.join(posts_dir, "posted")
    os.makedirs(archive_dir, exist_ok=True)
    for p in list(paths) + ([meta_path] if meta_path else []):
        if p and os.path.exists(p):
            shutil.move(p, os.path.join(archive_dir, os.path.basename(p)))


def main_carousel(posts_dir, account=None):
    paths, caption_body, hashtags, meta_path = find_next_carousel(posts_dir)
    if not paths:
        print(f"No carousels in: {posts_dir}")
        print("Queue one as <name>_1.jpg, <name>_2.jpg, … (+ optional <name>.json) and rerun.")
        return
    if len(paths) > CAROUSEL_MAX:
        raise SystemExit(f"{len(paths)} slides queued; IG takes at most {CAROUSEL_MAX}.")

    print(f"Next carousel: {len(paths)} slides — {', '.join(os.path.basename(p) for p in paths)}")
    preview = caption_body[:80].replace("\n", " ")
    print(f"Caption: {preview + ('...' if len(caption_body) > 80 else '') if caption_body else '(empty)'}")
    print(f"Hashtags: {', '.join('#' + h for h in hashtags) if hashtags else '(none)'}")

    print(f"Connecting to {DEVICE_ID}...")
    d = u2.connect(DEVICE_ID)
    print(f"Connected: {d.info.get('productName', 'unknown')}")

    upload_carousel(d, paths, caption_body, hashtags, target_account=account)
    archive_carousel(posts_dir, paths, meta_path)
    print(f"\nDone. Moved {len(paths)} slides → posts/posted/")


def main():
    import argparse

    ap = argparse.ArgumentParser(description="Post the next queued item to Instagram via ADB.")
    ap.add_argument("--carousel", action="store_true",
                    help="post the next queued carousel (<name>_1.jpg, <name>_2.jpg, … + <name>.json)")
    ap.add_argument("--account", help="IG username to switch to first (no @)")
    args = ap.parse_args()

    posts_dir = config.POSTS_DIR
    os.makedirs(posts_dir, exist_ok=True)

    if args.carousel:
        main_carousel(posts_dir, args.account)
        return

    image_path, caption_body, hashtags = find_next_post(posts_dir)
    if image_path is None:
        print(f"No images in: {posts_dir}")
        print("Drop .jpg/.png files in that folder (and optional same-name .json for caption + hashtags) and rerun.")
        return

    print(f"Next post: {os.path.basename(image_path)}")
    preview = caption_body[:80].replace("\n", " ")
    if len(caption_body) > 80:
        preview += "..."
    print(f"Caption: {preview if caption_body else '(empty)'}")
    print(f"Hashtags: {', '.join('#' + h for h in hashtags) if hashtags else '(none)'}")

    print(f"Connecting to {DEVICE_ID}...")
    d = u2.connect(DEVICE_ID)
    print(f"Connected: {d.info.get('productName', 'unknown')}")

    upload_post(d, image_path, caption_body, hashtags, target_account=args.account)
    archive_post(posts_dir, image_path)
    print(f"\nDone. Moved {os.path.basename(image_path)} → posts/posted/")


if __name__ == "__main__":
    main()
