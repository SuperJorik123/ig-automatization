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
2-8 minute gap (BUNDLE_GAP_MIN_S..MAX_S), and never more than
BUNDLE_MAX_COPIES (2) copies of the same video inside BUNDLE_COPY_WINDOW_S
(30 min) — a slot for a video that would break that waits until it is
allowed, so a one-video bundle (ten copies) takes about two and a half hours
and a ten-video bundle about forty minutes.
"""

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
              gap=(120, 480), max_copies: int = 2, window: int = 1800,
              first: int = 0) -> list:
    """[(seconds from now, account, video)], soonest first.

    One post at a time with a random `gap` between them; never more than
    `max_copies` posts of one video in any `window` seconds — a video that
    would break that is deferred, and when only such videos are left the
    clock jumps to the moment the oldest of them leaves the window. Among the
    videos allowed, the one with the most copies still to go is posted next
    (ties random), which spreads every video across the whole run."""
    rng = rng or random.Random()
    left = {}
    for account, video in assignment.items():
        left.setdefault(video, []).append(account)
    for accs in left.values():
        rng.shuffle(accs)
    posted = {v: [] for v in left}          # video -> its slot times
    # Whole seconds throughout: a float clock jumped to `oldest + window`
    # can land a hair inside the window and never get out of it.
    out, t = [], int(first)

    def allowed(video, at):
        recent = [x for x in posted[video] if at - x < window]
        return len(recent) < max_copies

    while any(left.values()):
        ready = [v for v, a in left.items() if a and allowed(v, t)]
        if not ready:
            # The earliest moment any pending video is allowed again.
            t = min(sorted(x for x in posted[v] if t - x < window)[
                -max_copies] + window for v, a in left.items() if a)
            continue
        most = max(len(left[v]) for v in ready)
        video = rng.choice([v for v in ready if len(left[v]) == most])
        account = left[video].pop()
        out.append((t, account, video))
        posted[video].append(t)
        t += int(rng.uniform(*gap))
    return out


def check_timetable(slots: list, max_copies: int = 2, window: int = 1800) -> bool:
    """True when no video has more than `max_copies` slots in any `window`."""
    by_video = {}
    for at, _, video in slots:
        by_video.setdefault(video, []).append(at)
    for times in by_video.values():
        times.sort()
        for i in range(len(times) - max_copies):
            if times[i + max_copies] - times[i] < window:
                return False
    return True


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


def line_keyboard(n_headlines: int = 0, chosen=None) -> InlineKeyboardMarkup:
    rows = []
    if n_headlines:
        rows.append([InlineKeyboardButton(
            f"{'● ' if i == chosen else ''}{i + 1}", callback_data=f"b:h:{i}")
            for i in range(n_headlines)])
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
        [InlineKeyboardButton("🚀 Publish", callback_data=f"{NS}:pub:{bundle_id}")],
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


def report_text(rows: list, since_label: str) -> str:
    """The weekly Telegram report, plain text."""
    if not rows:
        return f"📊 Weekly report ({since_label}): no reels logged."
    lines = [f"📊 Weekly report ({since_label}) — {len(rows)} reels"]
    lines.append("\nPer account (median views · ≥10k · ≥50k · follows):")
    for acc, s in sorted(account_stats(rows).items(),
                         key=lambda kv: -(kv[1]["median"] or 0)):
        med = f"{s['median']:,}" if s["median"] is not None else "—"
        lines.append(f"  {acc}: {med} · {s['hits10k']} · {s['hits50k']} · "
                     f"{s['follows']:+d} ({s['posts']} posts)")
    spread = bundle_spread(rows)
    if spread:
        lines.append("\nPer bundle (best · median · best/median):")
        for b, (best, med, ratio) in sorted(spread.items()):
            lines.append(f"  #{b}: {best:,} · {med:,} · ×{ratio if ratio else '—'}")
    comp = copies_comparison(rows)
    if comp:
        lines.append("\nViews per copy by copies of the video:")
        for k in ("1-2", "3", "4+"):
            if k in comp:
                mean, n = comp[k]
                lines.append(f"  on {k} accounts: {mean:,} avg ({n} reels)")
    return "\n".join(lines)


REMINDER = ("🛡 Weekly check: open Settings → Account status on every "
            "Instagram account and look for removed content, restrictions or "
            "a 'not eligible to be recommended' warning.")


def even_split_ok(assignment: dict) -> bool:
    """True when the copy counts differ by at most one (for tests/logs)."""
    c = list(copies(assignment).values())
    return not c or max(c) - min(c) <= 1
