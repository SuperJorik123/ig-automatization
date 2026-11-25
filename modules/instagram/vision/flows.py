"""Flows for the vision driver. Today: a carousel with Instagram's own music.

    carousel_with_music(drv, paths, caption, account, track=None)

Dry run by default: it goes all the way to the final composer — slides
picked in order, music on, caption in — checks all three on that screen, and
STOPS before Share, leaving the screen for a human. Sharing needs
share=True AND confirm_share=True (run.py maps them to two flags), and a
share is NEVER retried: it is confirmed by the profile's post count, and an
unconfirmed share stops with "check the profile by hand".

The steps, as recorded on IG 457 (2026-11-25):
  cold start → Home → Profile (account check, post count) → Home → Create
  → gallery (POST) → Select multiple → tap slides 1..N, each confirmed by
  "Selected media number k" → Next → carousel editor → Audio → search the
  track → tap its row (preview) → the bar's select button → editor (default
  segment kept) → Next → [Sharing-posts info: OK] → composer → caption field
  → Caption editor → set_text → Done → composer: verify → stop / Share.
"""

import os
import re
from datetime import datetime

from modules.instagram.vision import screens as scr
from modules.instagram.vision.driver import Stop

PKG = scr.PKG
CAROUSEL_MAX = 20
SLIDE_EXT = (".jpg", ".jpeg", ".png", ".mp4", ".mov")
_UNAVAILABLE_RE = re.compile(r"not available|unavailable|can'?t (be )?(use|add)|isn'?t available", re.I)


def validate_slides(paths):
    paths = list(paths)
    if not 2 <= len(paths) <= CAROUSEL_MAX:
        raise ValueError(f"A carousel takes 2-{CAROUSEL_MAX} files, got {len(paths)}.")
    for p in paths:
        if not os.path.isfile(p):
            raise FileNotFoundError(p)
        if not p.lower().endswith(SLIDE_EXT):
            raise ValueError(f"Not a photo/video IG takes in a carousel: {p}")
    return paths


def full_caption(body, hashtags):
    body = body or ""
    tags = " ".join(f"#{t.lstrip('#')}" for t in (hashtags or []) if t)
    if tags:
        return f"{body}\n\n{tags}" if body else tags
    return body


def _norm(text):
    return re.sub(r"\s+", " ", text or "").strip()


# --------------------------------------------------------------------------- #
# finders — (label, fn(screen) -> node|None), most trusted first              #
# --------------------------------------------------------------------------- #

def _rid(rid):
    return (f"rid={rid}", lambda s: s.first(rid=rid))


def _desc(desc):
    return (f"desc={desc}", lambda s: s.first(desc=desc))


def _desc_prefix(prefix):
    return (f"desc^={prefix}", lambda s: s.first(desc_prefix=prefix))


def _text(text):
    return (f"text={text}", lambda s: s.first(text=text))


F_PROFILE_TAB = [_rid("profile_tab"), _desc("Profile")]
F_HOME_TAB = [_rid("feed_tab"), _desc("Home")]
F_CREATE = [_desc("Create"), _rid("action_bar_left_button"), _rid("action_bar_buttons_container_left")]
F_POST_DEST = [_rid("cam_dest_feed"), _text("POST")]
F_MULTI = [_rid("multi_select_slide_button_alt"), _rid("multi_select_slide_button"),
           _desc_prefix("Select multiple"), _text("Select")]
F_GALLERY_NEXT = [_rid("next_button_textview"), _text("Next"), _desc("Next")]
F_AUDIO = [_desc("Audio"), _text("Audio")]
F_SEARCH = [_rid("row_search_edit_text")]
F_SELECT_TRACK = [_rid("audio_bar_select_tap_target"), _desc_prefix("Select track")]
F_EDITOR_NEXT = [_rid("media_thumbnail_tray_button"), _text("Next"), _desc("Next")]
F_CAPTION = [_rid("caption_input_text_view"), _desc("Write a caption")]
F_CAPTION_DONE = [_rid("caption_done_button"), _desc("Done"), _rid("next_button_textview"), _text("OK")]
F_SHARE = [_rid("share_footer_button"), _rid("share_button"), _desc("Share"), _text("Share")]


# --------------------------------------------------------------------------- #
# steps                                                                       #
# --------------------------------------------------------------------------- #

def open_fresh(drv):
    """Wake + keep awake, cold-start IG, land on Home or Profile."""
    drv.step = "open"
    drv.dev.keep_awake()
    if drv.dev.is_locked():
        drv.stop("lockscreen", "The phone is locked — unlock it (no PIN on a bot phone) and rerun.")
    drv.log("  → cold-start Instagram")
    drv.dev.app_start(PKG, stop=True)
    drv.sleep(4.0)
    return drv.resolve(("home", "profile"), timeout=25)


def read_profile(drv):
    drv.step = "profile"
    s = drv.screen
    if s.name != "profile":
        s = drv.act("Profile tab", F_PROFILE_TAB, "profile")
    info = scr.profile_info(s)
    drv.log(f"    profile: @{info['account']} · {info['posts']} posts")
    return info


F_SWITCHER = [_rid("action_bar_username_container"), _rid("action_bar_title")]


def switch_account(drv, want):
    """Open the account switcher (tap the username on Profile), tap the row
    whose handle is exactly `want`, and land on its Profile. Stops when the
    account isn't logged in on this phone."""
    drv.step = "switch"
    drv.log(f"  → switch account to @{want}")
    drv.act("account switcher", F_SWITCHER, "account_switcher")

    seen = []

    def row(screen):
        if screen.name != "account_switcher":
            return None
        for r in scr.account_rows(screen):
            if r["handle"] not in seen:
                seen.append(r["handle"])
            if r["handle"].lower() == want:
                return r["node"]
        return None

    sheet = drv.screen.nodes
    top = min((n.bounds[1] for n in sheet if n.desc == "Dismiss"), default=600)
    node = drv.scroll_find(f"account row {want}", row, max_swipes=4, area=(0, top + 60, 1080, 2150))
    if node is None:
        drv.stop("account_not_logged_in", f"@{want} isn't in the account switcher on this phone "
                                          f"(seen: {', '.join(seen) or 'none'}). Log it in by hand first.")
    if node.flag("selected"):
        drv.dev.back()  # already active — close the sheet
        drv.sleep(1.0)
        return drv.resolve("profile")
    drv.tap(node.bounds, f"account row {want}")
    drv.sleep(3.0)
    return drv.resolve(("profile", "home"), timeout=25)


def ensure_account(drv, account, allow_switch=True):
    """Hard rule 3: the active account must be `account`, read off the
    Profile title. A different one is switched (when allowed) and then READ
    AGAIN — the post only goes on if the title now says `account`."""
    want = account.strip().lstrip("@").lower()
    info = read_profile(drv)
    if (info["account"] or "").lower() == want:
        return info
    if not allow_switch:
        drv.stop("wrong_account", f"Instagram is on @{info['account']}, the post is for @{want}.")
    switch_account(drv, want)
    info = read_profile(drv)
    if (info["account"] or "").lower() != want:
        drv.stop("wrong_account", f"Switched, but Instagram shows @{info['account']}, not @{want}.")
    drv.log(f"    now on @{info['account']}")
    return info


def open_gallery(drv):
    drv.step = "create"
    if drv.screen.name != "home":
        drv.act("Home tab", F_HOME_TAB, "home")
    s = drv.act("Create (+)", F_CREATE, "gallery", timeout=15)
    post = drv.find("POST destination", F_POST_DEST, s)
    if post is not None and not post.flag("selected"):
        s = drv.act("POST destination", F_POST_DEST, "gallery")
    return s


def _thumb_time(desc):
    m = re.search(r"created on (\w+ \d{1,2}, \d{4} \d{1,2}:\d\d)", desc)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%B %d, %Y %H:%M")
    except ValueError:
        return None


def select_slides(drv, n, pushed_at=None):
    """Multi-select the gallery's first n thumbnails as slides 1..n, each tap
    confirmed by IG's own "Selected media number k" before the next."""
    drv.step = "select"
    s = drv.screen
    thumbs = scr.gallery_thumbs(s)
    if len(thumbs) < n:
        drv.stop("too_many_slides", f"Only {len(thumbs)} thumbnails fit on screen, the carousel has {n}.")
    if pushed_at is not None:
        for i, t in enumerate(thumbs[:n], 1):
            when = _thumb_time(t["node"].desc)
            if when is None:
                drv.drift("thumb_date_unreadable", f"slide{i}", t["node"].desc[:80], capture=False)
            elif abs((when - pushed_at).total_seconds()) > 15 * 60:
                drv.stop("wrong_media", f"Thumbnail {i} was created {when:%Y-%m-%d %H:%M}, not just now — "
                                        "the pushed slides aren't at the top of the gallery.")
    if not scr.multi_select_on(s):
        toggle = drv.find("Select multiple", F_MULTI, s)
        if toggle is None:
            drv.stop("not_found", "No 'Select multiple' toggle in the gallery.")
        drv.log("  → Select multiple")
        drv.tap(toggle.bounds, "Select multiple")
        drv.sleep(1.0)
        s = drv.resolve("gallery")
        if not scr.multi_select_on(s):
            drv.stop("multi_select_off", "Tapped 'Select multiple' but the gallery isn't in multi-select.")

    for k in range(1, n + 1):
        for attempt in range(3):  # look, then up to two taps, each checked
            thumbs = scr.gallery_thumbs(drv.screen)
            t = thumbs[k - 1]
            if t["number"] == k:
                break
            if t["number"] is not None:
                drv.stop("selection_order", f"Slide {k} is picked as number {t['number']}.")
            if attempt == 2:
                drv.stop("select_failed", f"Slide {k} won't pick (two taps).")
            if attempt == 1:
                drv.drift("retry", f"slide{k}", "first tap on the thumbnail didn't register")
            drv.log(f"  → slide {k}")
            drv.tap(t["node"].bounds, f"slide {k}")
            # IG loads the slide's preview after a tap and drops taps that
            # come in meanwhile (Phase 2: slide 3 lost at a 0.5-1 s pace).
            drv.sleep(drv.rng.uniform(1.4, 2.0))
            drv.resolve("gallery")
    picked = [t["number"] for t in scr.gallery_thumbs(drv.screen)]
    extra = [p for p in picked[n:] if p]
    if picked[:n] != list(range(1, n + 1)) or extra:
        drv.stop("selection_order", f"Selection is {picked[:n]} (+{len(extra)} extra), expected 1..{n}.")
    drv.log(f"    {n} slides picked in order")


def music_library(rows, artist):
    """'full' when searching a famous `artist` returns tracks BY that artist
    (licensed music — a Creator account's library), 'royalty-free only'
    when it returns only other people's covers/RF tracks (what Business
    accounts get), None for no rows at all.

    The RF label itself is no proof: on 2026-11-25 two accounts showed the
    very same royalty-free covers WITHOUT the RF badge."""
    if not rows:
        return None
    want = artist.casefold()
    return "full" if any(want in r["artist"].casefold() for r in rows) else "royalty-free only"


def probe_library(drv, query):
    """Search `query` — a hugely popular ARTIST's name — in the open music
    picker and note which library this account gets in drv.notes."""
    if not drv.dev.set_text(f"{PKG}:id/row_search_edit_text", query):
        drv.stop("text_input", "Couldn't put the probe query into the music search box.")
    drv.sleep(3.0)
    s = drv.resolve(("music_picker", "music_preview"))
    rows = scr.track_rows(s)
    drv.notes["library"] = music_library(rows, query)
    drv.notes["library_probe"] = [f"{r['title']} — {r['artist']}"
                                  + ("" if "royalty-free" not in r["desc"] else " (RF)") for r in rows[:6]]
    drv.log(f"    music library for {query!r}: {drv.notes['library']}")


def add_music(drv, track, artist=None, probe=None):
    """Search `track` in the editor's Audio picker and put it on with the
    default segment. Stops when it isn't found or IG says it's unavailable
    (Business accounts get only the royalty-free library). `probe` = a
    query searched first to record the account's music library."""
    drv.step = "music"
    drv.act("Audio", F_AUDIO, ("music_picker", "music_preview"))
    search = drv.find("music search", F_SEARCH)
    if search is None:
        drv.stop("not_found", "No search box in the music picker.")
    drv.tap(search.bounds, "music search")
    drv.sleep(1.0)
    drv.resolve(("music_picker", "music_preview"))
    if probe:
        probe_library(drv, probe)
    if not drv.dev.set_text(f"{PKG}:id/row_search_edit_text", track):
        drv.stop("text_input", "Couldn't put the track name into the music search box.")
    drv.log(f"  → search music: {track!r}")
    drv.sleep(2.5)
    drv.resolve(("music_picker", "music_preview"))

    want, want_artist = track.casefold().strip(), (artist or "").casefold().strip()

    def match(screen):
        rows = scr.track_rows(screen)
        exact = [r for r in rows if r["title"].casefold() == want
                 and (not want_artist or want_artist in r["artist"].casefold())]
        loose = [r for r in rows if want in r["desc"].casefold()
                 and (not want_artist or want_artist in r["desc"].casefold())]
        hit = (exact or loose or [None])[0]
        return hit["node"] if hit else None

    row = drv.scroll_find(f"track {track}", match, max_swipes=4, area=(0, 950, 1080, 2000))
    if row is None:
        drv.stop("track_not_found", f"No track matching {track!r}" + (f" by {artist}" if artist else "") +
                 " in the music search.")
    drv.log(f"  → preview {row.desc[13:80]!r}")
    drv.tap(row.bounds, "track row")
    drv.sleep(2.0)
    s = drv.resolve(("music_preview", "music_picker"), timeout=10)
    texts = " ".join(n.text or n.desc for n in s.nodes)
    if s.name != "music_preview":
        if _UNAVAILABLE_RE.search(texts):
            drv.stop("track_unavailable", f"{track!r} isn't available on this account "
                                          "(Business accounts only get the royalty-free library).")
        drv.stop("not_found", "Tapped the track but no preview bar with a select button appeared.")
    drv.act("select track", F_SELECT_TRACK, "editor", timeout=12)


def to_composer(drv):
    drv.step = "composer"
    return drv.act("Next (editor)", F_EDITOR_NEXT, "composer", timeout=20)


def write_caption(drv, text):
    drv.step = "caption"
    s = drv.act("caption field", F_CAPTION, ("caption_editor", "composer"))
    if not drv.dev.set_text(f"{PKG}:id/caption_input_text_view", text):
        drv.stop("text_input", "Couldn't put the caption into the caption field.")
    drv.sleep(1.0)
    s = drv.look()
    if s.name == "caption_editor":
        s = drv.act("Done (caption)", F_CAPTION_DONE, "composer")
    return s


def verify_composer(drv, caption, track=None, artist=None):
    drv.step = "verify"
    s = drv.resolve("composer")
    got = scr.caption_text(s)
    if _norm(got) != _norm(scr.dump_form(caption)):
        drv.stop("caption_mismatch", f"The composer's caption reads {got!r}, expected {caption!r}.")
    music = scr.composer_track(s)
    if track:
        if not music or music[0].casefold() != track.casefold() or (
            artist and artist.casefold() not in music[1].casefold()
        ):
            drv.stop("music_mismatch", f"The composer's music is {music}, expected {track!r}.")
    elif music:
        drv.stop("music_mismatch", f"Music {music} is on the post but none was asked for.")
    drv.log(f"    composer ok: caption ✓, music {music or 'none'} ✓")
    return s


def share_and_confirm(drv, before_posts, timeout_s=180):
    """Tap Share ONCE, then confirm through the profile's post count. Never
    retries: an unconfirmed share stops and says so."""
    drv.step = "share"
    drv.log("  → SHARE")
    node = drv.find("Share", F_SHARE)
    if node is None:
        drv.stop("not_found", "No Share button on the composer.")
    drv.tap(node.bounds, "Share")
    drv.sleep(5.0)
    deadline = drv.clock() + timeout_s
    while drv.clock() < deadline:
        s = drv.look()
        drv.guard(s)
        if s.name == "composer":
            drv.sleep(3.0)
            continue
        if s.name != "profile":
            tab = drv.find("Profile tab", F_PROFILE_TAB, s)
            if tab is not None:
                drv.tap(tab.bounds, "Profile tab")
                drv.sleep(3.0)
                continue
        if s.name == "profile":
            info = scr.profile_info(s)
            if info["posts"] is not None and before_posts is not None and info["posts"] == before_posts + 1:
                drv.capture("shared_profile", note="post count +1")
                drv.log(f"    ✅ posted: {before_posts} → {info['posts']} posts")
                return info
            # pull to refresh the profile, then read again
            drv.dev.swipe(540, 700, 545, 1500, 0.4)
        drv.sleep(6.0)
    drv.stop("share_unconfirmed", "Share was tapped once but the post count didn't go up. "
                                  "Do NOT rerun — check the profile by hand first.")


def abandon_creation(drv, max_backs=8):
    """Back out of a half-made post to Home/Profile, choosing Discard on the
    way. Used by `run.py --discard` and nothing else."""
    drv.step = "discard"
    for _ in range(max_backs):
        s = drv.look()
        drv.guard(s)
        if s.name in ("home", "profile"):
            return s
        if s.is_popup:
            btn = scr.popup_button(s)
            if btn is None:
                drv.stop("unknown_popup", f"Unknown popup while backing out ({s.name}).")
            drv.tap(btn.bounds, f"dismiss {s.name}")
        else:
            drv.dev.back()
        drv.sleep(1.2)
    drv.stop("unexpected_screen", f"Still on '{drv.screen.name}' after {max_backs} backs.")


def push_slides(dev, paths, sleep=None):
    """Put the slides on the phone so the gallery shows them in carousel
    order (same trick as upload_post.push_carousel_to_phone: last slide
    first, >1 s apart, so slide 1 is the newest = top-left). Returns the
    remote paths so `remove_slides` can take them off again."""
    import time

    sleep = sleep or time.sleep
    stamp = int(time.time())
    remote = []
    for i, path in reversed(list(enumerate(paths, 1))):
        ext = os.path.splitext(path)[1].lower() or ".jpg"
        dest = f"/sdcard/DCIM/Camera/igv_{stamp}_{i:02d}{ext}"
        dev.push(path, dest)
        dev.shell(f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file://{dest}")
        remote.append(dest)
        if i > 1:
            sleep(1.2)
    return remote


def remove_slides(dev, remote):
    """Delete pushed slides from the phone and from the gallery index."""
    for dest in remote or []:
        if not dest.startswith("/sdcard/DCIM/Camera/igv_"):
            continue  # only ever files this module pushed
        dev.shell(f"rm -f '{dest}'")
        # MediaStore records /storage/emulated/0/…, not /sdcard/…: match the name.
        name = dest.rsplit("/", 1)[1]
        dev.shell(f"content delete --uri content://media/external/file --where \"_data LIKE '%/{name}'\"")
        dev.shell(f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file://{dest}")


def phone_now(dev):
    """The phone's local wall-clock time (thumbnail dates are in it)."""
    out = dev.shell('date "+%Y-%m-%d %H:%M:%S"')
    out = getattr(out, "output", out if isinstance(out, str) else "") or ""
    try:
        return datetime.strptime(out.strip()[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def carousel_with_music(drv, paths, caption, account, track=None, artist=None,
                        share=False, confirm_share=False, push=None, allow_switch=True, probe=None):
    """The whole flow. Returns a dict summary; raises Stop on anything off.
    `push(dev, paths)` puts the files on the phone in gallery order and
    returns their remote paths (default: push_slides); they are kept in
    drv.notes["pushed"] — even when the flow stops — for remove_slides.
    `probe` = a music query searched first to note the account's library."""
    paths = validate_slides(paths)
    if share and not confirm_share:
        raise ValueError("share=True needs confirm_share=True as well.")
    if push is None:
        push = push_slides

    open_fresh(drv)
    info = ensure_account(drv, account, allow_switch=allow_switch)

    drv.step = "push"
    pushed_at = phone_now(drv.dev)
    drv.log(f"  → push {len(paths)} slides")
    drv.notes["pushed"] = push(drv.dev, paths) or []
    drv.sleep(1.5)

    drv.act("Home tab", F_HOME_TAB, "home")
    open_gallery(drv)
    select_slides(drv, len(paths), pushed_at)
    drv.step = "gallery_next"
    drv.act("Next (gallery)", F_GALLERY_NEXT, "editor", timeout=15)
    if track:
        add_music(drv, track, artist, probe=probe)
    to_composer(drv)
    write_caption(drv, caption)
    verify_composer(drv, caption, track, artist)
    drv.capture("final_composer", note="dry run stop" if not share else "about to share")
    result = {"account": info["account"], "posts_before": info["posts"], "slides": len(paths),
              "track": track, "shared": False, "drift": list(drv.events),
              "library": drv.notes.get("library")}
    if not share:
        drv.log("  ■ DRY RUN — stopped on the composer before Share. Check the phone.")
        return result
    after = share_and_confirm(drv, info["posts"])
    result.update(shared=True, posts_after=after["posts"])
    return result
