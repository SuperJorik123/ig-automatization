"""
modules/telegram/bundle.py — bundle mode's pure half: spreading several videos
across EVERY active account, the publish timetable, and what the bundle
messages say. Offline-testable, like branded.py / schedule.py; news_bot owns
Telegram, the renders and the database.

WHY BUNDLES. The ten Instagram accounts post the same videos, and identical
videos land wildly differently per account (1M views on one, nothing on the
rest; a 10-follower account out-performing 500-follower ones). Every copy is
its own random test audience, so every account posting is another chance for
a video to take off. A bundle takes V videos and gives EVERY active account
exactly one of them, each video on as many accounts as the split allows.

ASSIGNMENT (`assign`): with A accounts, the first A videos are split as
evenly as possible (each gets floor(A/V) or ceil(A/V) accounts — 3 videos on
10 accounts is 4/3/3), WHICH account gets WHICH video is random and re-drawn
every time, and an account is never handed a video its history says it has
already published, whenever another valid assignment exists. Videos past A
are carried over to the next bundle — one account never posts two bundle
videos at once.

TIMETABLE (`timetable`): the posts go out one after another with a random
1-3 minute gap (BUNDLE_GAP_MIN_S..MAX_S), the videos interleaved — ten posts
take ~20 minutes however many videos they are. (The old cap of 2 copies of
one video per 30 min was removed 2026-11-25, owner: it stretched a 3-video
bundle to ~45 min and a 1-video one to ~2.5 h.)
"""

import html
import random
import statistics

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


# --------------------------------------------------------------------------- #
# assignment                                                                  #
# --------------------------------------------------------------------------- #


def split_sizes(n_videos: int, n_accounts: int) -> list:
    """How many accounts each of the first min(V, A) videos gets: as even as
    possible, the larger shares first. [] when either side is empty."""
    v = min(n_videos, n_accounts)
    if v <= 0:
        return []
    base, extra = divmod(n_accounts, v)
    return [base + 1 if i < extra else base for i in range(v)]


def _matching(videos: list, sizes: dict, accounts: list, history: dict,
              rng: random.Random):
    """A random assignment respecting the sizes with as few history clashes as
    possible: a bipartite matching (account -> video slot) found by augmenting
    paths over the clash-free edges first, in random order, then completed
    with whatever is left. Returns {account: video}."""
    slots = [v for v in videos for _ in range(sizes[v])]
    rng.shuffle(slots)
    order = list(accounts)
    rng.shuffle(order)
    owner = [None] * len(slots)          # slot index -> account

    def ok(account, slot):
        return slots[slot] not in history.get(account, ())

    def augment(account, seen):
        cand = list(range(len(slots)))
        rng.shuffle(cand)
        for s in cand:
            if s in seen or not ok(account, s):
                continue
            seen.add(s)
            if owner[s] is None or augment(owner[s], seen):
                owner[s] = account
                return True
        return False

    for account in order:
        augment(account, set())
    placed = set(a for a in owner if a is not None)
    free = [s for s, a in enumerate(owner) if a is None]
    for account in order:
        if account not in placed:
            owner[free.pop()] = account     # no clean slot exists: take any
    return {a: slots[s] for s, a in enumerate(owner)}


def assign(videos: list, accounts: list, history: dict | None = None,
           rng: random.Random | None = None) -> tuple[dict, list]:
    """({account: video}, carried videos).

    `videos` in the order they joined the bundle; `accounts` the ACTIVE ones
    (a paused account is simply not in the list); `history` {account: set of
    videos it has already published}. Every account gets exactly one video;
    the first len(accounts) videos are used and the rest are carried over.
    Pure but for `rng` (default: a fresh, unseeded one)."""
    rng = rng or random.Random()
    history = history or {}
    accounts = list(dict.fromkeys(accounts))
    used, carried = list(videos[:len(accounts)]), list(videos[len(accounts):])
    if not used or not accounts:
        return {}, list(videos)
    shares = split_sizes(len(used), len(accounts))
    # Which video gets the larger share is random too.
    order = list(used)
    rng.shuffle(order)
    sizes = dict(zip(order, shares))
    return _matching(used, sizes, accounts, history, rng), carried


def copies(assignment: dict) -> dict:
    """{video: how many accounts got it}."""
    out = {}
    for video in assignment.values():
        out[video] = out.get(video, 0) + 1
    return out


def clashes(assignment: dict, history: dict) -> list:
    """Accounts handed a video they already published (only when unavoidable)."""
    return [a for a, v in assignment.items() if v in history.get(a, ())]


# --------------------------------------------------------------------------- #
# timetable                                                                   #
# --------------------------------------------------------------------------- #


def timetable(assignment: dict, rng: random.Random | None = None,
              gap=(60, 180), first: int = 0) -> list:
    """[(seconds from now, account, video)], soonest first.

    One post at a time with a random `gap` between them. The video with the
    most copies still to go is posted next (ties random; never the same video
    twice in a row while another is left), which spreads every video across
    the whole run."""
    rng = rng or random.Random()
    left = {}
    for account, video in assignment.items():
        left.setdefault(video, []).append(account)
    for accs in left.values():
        rng.shuffle(accs)
    out, t, last = [], int(first), None
    while any(left.values()):
        ready = [v for v, a in left.items() if a]
        if len(ready) > 1 and last in ready:
            ready.remove(last)
        most = max(len(left[v]) for v in ready)
        video = rng.choice([v for v in ready if len(left[v]) == most])
        account = left[video].pop()
        out.append((t, account, video))
        last = video
        t += int(rng.uniform(*gap))
    return out


# --------------------------------------------------------------------------- #
# what the messages say                                                       #
# --------------------------------------------------------------------------- #

# Callback namespace: "u:<verb>:<bundle id>[:<arg>]" — DB-routed by bundle id
# so every button works after a restart. Per-video lines carry the plan
# card's own "b:" namespace instead (they live in _pending like a card).
NS = "u"


def video_line(n: int, video: dict) -> str:
    """One video in a bundle message: number, headline, what it still needs."""
    head = (video.get("text") or "").strip()
    if not (video.get("info") or "").strip():
        return f"{n}. ⏳ waiting for info: — {head or '(no headline yet)'}"
    if video.get("headline_task_running"):
        return f"{n}. ✍️ writing headlines… — {head}"
    star = " ⭐" if video.get("headline_pick") is not None else ""
    return f"{n}. {head or '(no headline yet)'}{star}"


def line_text(bundle_id: int, n_videos: int, n: int, video: dict) -> str:
    """The short message a video gets when it joins (or starts) a bundle."""
    return "\n".join([
        f"📦 Bundle #{bundle_id}: {n_videos} video{'s' if n_videos != 1 else ''}",
        video_line(n, video),
        "↩️ reply `info: …` here — the AI headlines pick themselves "
        "(tap 1-5 to change)",
    ])


def line_keyboard(n_headlines: int = 0, chosen=None,
                  preview: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if n_headlines:
        rows.append([InlineKeyboardButton(
            f"{'● ' if i == chosen else ''}{i + 1}", callback_data=f"b:h:{i}")
            for i in range(n_headlines)])
    if preview:
        # Same verb as the plan card's (branded.PREVIEW_EDIT_BUTTON).
        rows.append([InlineKeyboardButton("▶ Preview edit",
                                          callback_data="b:pv")])
    rows.append([InlineKeyboardButton("🎬 Render bundle", callback_data="b:bgo"),
                 InlineKeyboardButton("🗑 Remove this one", callback_data="b:bx")])
    return InlineKeyboardMarkup(rows)


def open_text(bundle_id: int, videos: list) -> str:
    """/bundle on an open bundle: every video and what it still needs."""
    lines = [f"📦 Bundle #{bundle_id}: {len(videos)} video"
             f"{'s' if len(videos) != 1 else ''} (open)"]
    lines += [video_line(i + 1, v) for i, v in enumerate(videos)]
    if not videos:
        lines.append("(empty — send a video to add it)")
    return "\n".join(lines)


def open_keyboard(bundle_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎬 Render bundle", callback_data=f"{NS}:go:{bundle_id}"),
        InlineKeyboardButton("✕ Close bundle", callback_data=f"{NS}:x:{bundle_id}")]])


def fmt_offset(seconds: int, start) -> str:
    """A slot as a local clock time, given the timetable's start datetime."""
    import datetime as _dt
    return (start + _dt.timedelta(seconds=seconds)).strftime("%H:%M")


def summary_text(bundle_id: int, headlines: dict, slots: list, start,
                 platforms: list, notes=(), scheduled: bool = False) -> str:
    """The one message after Render: which video goes to which account, when.

    `headlines` {video id: headline}; `slots` the timetable; `start` the
    datetime offsets count from (local tz); `notes` extra lines (carried
    videos, quota roll-overs, render failures)."""
    order = list(dict.fromkeys(v for _, _, v in sorted(slots)))
    by_video = {v: [] for v in order}
    for at, account, video in sorted(slots):
        by_video[video].append(f"{account} {fmt_offset(at, start)}")
    n_acc = len(slots)
    share = " / ".join(str(len(by_video[v])) for v in order)
    head = (f"📦 Bundle #{bundle_id} {'scheduled' if scheduled else 'ready'}: "
            f"{len(order)} video{'s' if len(order) != 1 else ''} → {n_acc} "
            f"account{'s' if n_acc != 1 else ''} ({share})")
    lines = [head]
    for i, v in enumerate(order, 1):
        lines.append(f"video {i} — {headlines.get(v, '?')}")
        lines.append("   " + " · ".join(by_video[v]))
    if slots:
        lines.append(f"🕒 {fmt_offset(min(s[0] for s in slots), start)}–"
                     f"{fmt_offset(max(s[0] for s in slots), start)} · on "
                     + " · ".join(platforms))
    lines += list(notes)
    return "\n".join(lines)


def ready_keyboard(bundle_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Publish", callback_data=f"{NS}:pub:{bundle_id}"),
         # The whole timetable, starting at the next free US-evening slot
         # (schedule.next_us_slot) instead of BUNDLE_FIRST_POST_S from now.
         InlineKeyboardButton("🇺🇸 US evening", callback_data=f"{NS}:us:{bundle_id}")],
        [InlineKeyboardButton("🔀 Reshuffle", callback_data=f"{NS}:rs:{bundle_id}"),
         InlineKeyboardButton("✕ Cancel", callback_data=f"{NS}:x:{bundle_id}")]])


def scheduled_keyboard(bundle_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "✕ Cancel what hasn't gone out", callback_data=f"{NS}:xs:{bundle_id}")]])


def retry_keyboard(sched_id: int, account: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        f"🔁 Retry {account}", callback_data=f"s:{sched_id}:r")]])


# --------------------------------------------------------------------------- #
# the weekly report                                                           #
# --------------------------------------------------------------------------- #


def _views(row: dict):
    """The best view count a logged reel has: 72h, else 24h, else None."""
    for key in ("insights_72h", "insights_24h"):
        data = row.get(key) or {}
        if isinstance(data.get("views"), (int, float)):
            return data["views"]
    return None


def account_stats(rows: list) -> dict:
    """{account: {posts, median, hits10k, hits50k, follows}} over logged reels.
    `follows` is the follower delta from post time to the latest snapshot —
    the Graph API has no per-Reel follows metric."""
    out = {}
    for r in rows:
        s = out.setdefault(r["account"], {"posts": 0, "views": [], "follows": 0})
        s["posts"] += 1
        v = _views(r)
        if v is not None:
            s["views"].append(v)
        after = r.get("followers_72h") or r.get("followers_24h")
        if after is not None and r.get("followers_at_post") is not None:
            s["follows"] += after - r["followers_at_post"]
    for s in out.values():
        views = s.pop("views")
        s["median"] = int(statistics.median(views)) if views else None
        s["hits10k"] = sum(v >= 10_000 for v in views)
        s["hits50k"] = sum(v >= 50_000 for v in views)
    return out


def bundle_spread(rows: list) -> dict:
    """{bundle id: (best views, median views, best/median ratio)} — how much
    the same bundle's results differed across accounts."""
    by = {}
    for r in rows:
        v = _views(r)
        if r.get("bundle_id") is not None and v is not None:
            by.setdefault(r["bundle_id"], []).append(v)
    out = {}
    for b, views in by.items():
        med = statistics.median(views)
        out[b] = (max(views), int(med),
                  round(max(views) / med, 1) if med else None)
    return out


def copies_comparison(rows: list) -> dict:
    """{"1-2": mean views per copy, "3": …, "4+": …} by how many accounts a
    video was posted on — does spreading a video wide cost each copy?"""
    groups = {}
    for r in rows:
        v = _views(r)
        if v is None or not r.get("copy_count"):
            continue
        key = ("1-2" if r["copy_count"] <= 2 else
               "3" if r["copy_count"] == 3 else "4+")
        groups.setdefault(key, []).append(v)
    return {k: (int(statistics.mean(v)), len(v)) for k, v in groups.items()}


REPORT_NAME_W = 10   # account column, like /report's table
SETTLED_POSTS = 2    # an account needs this many reels to be called weakest


def _name(n: str, width: int) -> str:
    return n if len(n) <= width else n[:width - 1] + "…"


def report_text(rows: list, since_label: str) -> str:
    """The Monday report as Telegram HTML, laid out like /report
    (modules/telegram/report.py): the total, a few one-line highlights, ONE
    monospace table (an account per row, best first), then the bundle and
    copy comparisons. Sent with parse_mode=HTML."""
    from modules.telegram.report import fmt_num   # pure; no platform imports

    title = f"📊 <b>Reels this week</b> · {html.escape(since_label)}"
    if not rows:
        return f"{title}\n\nNo reels logged."

    stats = account_stats(rows)
    views = [v for v in (_views(r) for r in rows) if v is not None]
    follows = sum(s["follows"] for s in stats.values())

    out = [title, f"<i>{len(rows)} Instagram reels the bot published · "
                  "views at 72 h (24 h if younger)</i>", ""]
    if views:
        out.append(f"👁 <b>{fmt_num(sum(views))} views in total</b> · "
                   f"median {fmt_num(int(statistics.median(views)))} a reel")
        hits10 = sum(v >= 10_000 for v in views)
        hits50 = sum(v >= 50_000 for v in views)
        out.append(f"   🔟 {hits10} past 10K · 🚀 {hits50} past 50K")
    else:
        out.append("👁 <b>No view counts yet</b> — insights arrive 24 h after posting")
    out.append(f"👥 {'+' if follows >= 0 else '−'}{fmt_num(abs(follows))} "
               "followers after posting")

    ranked = sorted(stats, key=lambda a: (-(stats[a]["median"] or -1), a))
    measured = [a for a in ranked if stats[a]["median"] is not None]
    if measured:
        out.append("")
        best = measured[0]
        out.append(f"🏆 Best account: <b>{html.escape(best)}</b> — median "
                   f"{fmt_num(stats[best]['median'])} over {stats[best]['posts']} reels")
        top = max((r for r in rows if _views(r) is not None), key=_views)
        where = (f" (bundle #{top['bundle_id']})" if top.get("bundle_id") is not None
                 else "")
        out.append(f"⭐ Best reel: <b>{html.escape(top['account'])}</b> — "
                   f"{fmt_num(_views(top))} views{where}")
        settled = [a for a in measured[1:] if stats[a]["posts"] >= SETTLED_POSTS]
        if len(measured) > 2 and settled:
            low = settled[-1]
            out.append(f"🐢 Weakest: <b>{html.escape(low)}</b> — median "
                       f"{fmt_num(stats[low]['median'])} over {stats[low]['posts']} reels")

    w = min(REPORT_NAME_W, max(len(a) for a in ranked))
    table = [" " * w + "reels".rjust(6) + "median".rjust(8) + "10K+".rjust(6)
             + "follows".rjust(8)]
    for a in ranked:
        s = stats[a]
        f = s["follows"]
        table.append(_name(a, w).ljust(w) + str(s["posts"]).rjust(6)
                     + fmt_num(s["median"]).rjust(8) + str(s["hits10k"]).rjust(6)
                     + (("+" if f > 0 else "−" if f < 0 else "") + fmt_num(abs(f))).rjust(8))
    out += ["", "<pre>" + html.escape("\n".join(table)) + "</pre>"]

    spread = bundle_spread(rows)
    if spread:
        out += ["", "📦 <b>Bundles</b> — same video, different accounts"]
        lines = [f"#{b}".ljust(5) + f"best {fmt_num(best)}".ljust(11)
                 + f"median {fmt_num(med)}".ljust(13) + f"×{ratio if ratio else '—'}"
                 for b, (best, med, ratio) in sorted(spread.items())]
        out.append("<pre>" + html.escape("\n".join(lines)) + "</pre>")
    comp = copies_comparison(rows)
    if comp:
        out += ["", "🔁 <b>Views per copy</b> by how many accounts posted the video",
                "   " + " · ".join(f"{k} accounts: {fmt_num(comp[k][0])} ({comp[k][1]})"
                                   for k in ("1-2", "3", "4+") if k in comp)]
    return "\n".join(out)


REMINDER = ("🛡 Weekly check: open Settings → Account status on every "
            "Instagram account and look for removed content, restrictions or "
            "a 'not eligible to be recommended' warning.")


def even_split_ok(assignment: dict) -> bool:
    """True when the copy counts differ by at most one (for tests/logs)."""
    c = list(copies(assignment).values())
    return not c or max(c) - min(c) <= 1
