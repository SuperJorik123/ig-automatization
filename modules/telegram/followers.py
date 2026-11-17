"""
modules/telegram/followers.py — the weekly follower report: how many
followers every account gained (or lost) on Instagram, Facebook, YouTube and
X over the last week.

No platform hands out "followers gained this week", so the bot keeps its own
history: once a day `collect(brands)` reads every account's CURRENT count and
news_bot stores it (queue_store.follower_counts, one row per day / brand /
platform). The report is today's count minus the stored count from 7 days
earlier — or from the closest older day when that one is missing (the bot was
down), and the header names the real span.

Readers (all never raise, all return (count | None, why)):
  IG  modules/instagram/graph.followers        (Graph API followers_count)
  FB  modules/facebook/poster.followers        (Page followers_count)
  YT  modules/youtube/stats.subscribers        (yt-dlp, public, ROUNDED by YouTube)
  X   modules/twitter/poster.followers         (v2 /users/me, a pay-per-use read)

`collect` is the blocking half; `render` is pure and laid out like /report:
the total, the best and worst account in plain words, one monospace table
(account × platform, change since the baseline; "!" = read failed, "·" = no
account, "new" = no count from back then), current totals, then what needs
fixing.

CLI (reads now, prints the current counts): py modules/telegram/followers.py
"""
import datetime as dt
import html
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from modules.telegram.report import MSG_LIMIT, fmt_num, short_error  # noqa: E402

PLATFORMS = (  # key, label, icon, brand field holding the account
    ("ig", "Instagram", "📸", "ig"),
    ("fb", "Facebook", "📘", "fb"),
    ("yt", "YouTube", "▶️", "yt_handle"),
    ("x", "X", "𝕏", "tw"),
)
_LABEL = {k: lbl for k, lbl, _, _ in PLATFORMS}
_ICON = {k: icon for k, _, icon, _ in PLATFORMS}
_SHORT = {"ig": "IG", "fb": "FB", "yt": "YT", "x": "X"}

NAME_W = 10
COL_W = 7
MAX_WORKERS = 8


def _readers() -> dict:
    """platform -> (account) -> (count | None, why). Imported lazily so the
    pure half is testable without the platform modules' env."""
    from modules.facebook import poster as fb
    from modules.instagram import graph as ig
    from modules.twitter import poster as tw
    from modules.youtube import stats as yt

    def ig_followers(account):
        n = ig.followers(account)   # logs its own reason; returns None
        return (n, "") if n is not None else (None, "can't read the count")
    return {"ig": ig_followers, "fb": fb.followers, "yt": yt.subscribers,
            "x": tw.followers}


def collect(brands: list, readers: dict | None = None) -> list:
    """[{brand, platform, count, error}] — one per (brand, platform the brand
    has an account on). A YouTube channel without a public @handle gets error
    "no handle" without a read. Blocking; never raises."""
    readers = readers or _readers()
    jobs, out = [], []
    for b in brands:
        for p, _, _, field in PLATFORMS:
            acct = (b.get(field) or "").strip()
            if acct:
                jobs.append((b["name"], p, acct))
            elif p == "yt" and b.get("yt"):
                out.append({"brand": b["name"], "platform": p, "count": None,
                            "error": "no handle"})

    def run(job):
        name, p, acct = job
        try:
            count, why = readers[p](acct)
        except Exception as exc:  # the readers never raise; belt and braces
            count, why = None, str(exc)
        return {"brand": name, "platform": p, "count": count,
                "error": "" if count is not None else (why or "unavailable")}

    if jobs:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            out.extend(pool.map(run, jobs))
    return out


def counts_of(results: list) -> dict:
    """{(brand, platform): count} for the reads that worked — what gets stored."""
    return {(r["brand"], r["platform"]): r["count"]
            for r in results if r["count"] is not None}


# --------------------------------------------------------------------------- #
# formatting                                                                   #
# --------------------------------------------------------------------------- #

def fmt_delta(n: int) -> str:
    """+1234 -> +1.2K, -3 -> −3, 0 -> 0"""
    if n == 0:
        return "0"
    return ("+" if n > 0 else "−") + fmt_num(abs(n))


def _day(stamp: str) -> str:
    return dt.date.fromisoformat(stamp).strftime("%d %b").lstrip("0")


def render(results: list, then: dict | None, then_day: str | None,
           today: str) -> list:
    """Telegram-HTML messages for `collect`'s results against the counts
    stored on `then_day` (None = no history yet: the table shows today's
    counts and says when the first comparison comes)."""
    if not results:
        return ["👥 <b>Followers</b>\n\nNo account has Instagram, Facebook, "
                "YouTube or X connected."]
    then = then or {}
    plats = [p for p, *_ in PLATFORMS if any(r["platform"] == p for r in results)]
    ok = [r for r in results if r["count"] is not None]
    compare = bool(then_day) and then_day < today

    def delta(r):
        key = (r["brand"], r["platform"])
        return r["count"] - then[key] if key in then else None

    out = []
    if compare:
        days = (dt.date.fromisoformat(today) - dt.date.fromisoformat(then_day)).days
        out.append(f"👥 <b>Followers this week</b> · {_day(then_day)} → {_day(today)}"
                   + ("" if days == 7 else f" ({days} days)"))
        moved = [r for r in ok if delta(r) is not None]
        total = sum(delta(r) for r in moved)
        word = "gained" if total >= 0 else "lost"
        out += ["", f"<b>{fmt_delta(total)} followers {word} in total</b>"]
        out.append("   " + " · ".join(
            f"{_ICON[p]} {fmt_delta(sum(delta(r) for r in moved if r['platform'] == p))}"
            for p in plats))
        if moved:
            out.append("")
            best = max(moved, key=delta)
            if delta(best) > 0:
                out.append(f"🏆 Grew most: <b>{html.escape(best['brand'])}</b> on "
                           f"{_LABEL[best['platform']]} — {fmt_delta(delta(best))}")
            losers = [r for r in moved if delta(r) < 0]
            if losers:
                worst = min(losers, key=delta)
                out.append(f"📉 Lost most: <b>{html.escape(worst['brand'])}</b> on "
                           f"{_LABEL[worst['platform']]} — {fmt_delta(delta(worst))}")
            flat = [r for r in moved if delta(r) == 0]
            if flat:
                out.append(f"😐 No change: {len(flat)} of {len(moved)} accounts")
    else:
        out.append(f"👥 <b>Followers now</b> · {_day(today)}")
        first = dt.date.fromisoformat(today) + dt.timedelta(days=7)
        out += [f"<i>First count saved — the weekly change starts on "
                f"{first.strftime('%d %b').lstrip('0')}.</i>"]

    # The table: change per account and platform (or today's count without
    # history), the accounts that grew most first.
    by_brand: dict[str, dict] = {}
    for r in results:
        by_brand.setdefault(r["brand"], {})[r["platform"]] = r

    def cell(r):
        if r is None:
            return "·"
        if r["count"] is None:
            return "!"
        if not compare:
            return fmt_num(r["count"])
        d = delta(r)
        return "new" if d is None else fmt_delta(d)

    def score(name):
        rows = [r for r in by_brand[name].values() if r["count"] is not None]
        if compare:
            return sum(delta(r) or 0 for r in rows)
        return sum(r["count"] for r in rows)

    ranked = sorted(by_brand, key=lambda n: (-score(n), n))
    name_w = min(NAME_W, max(len(n) for n in ranked))

    def name(n):
        return n if len(n) <= name_w else n[:name_w - 1] + "…"
    header = " " * name_w + "".join(_SHORT[p].rjust(COL_W) for p in plats)
    rows = [name(n).ljust(name_w)
            + "".join(cell(by_brand[n].get(p)).rjust(COL_W) for p in plats)
            for n in ranked]

    tail = []
    if compare:
        tail += ["", "Followers now: " + " · ".join(
            f"{_ICON[p]} {fmt_num(sum(r['count'] for r in ok if r['platform'] == p))}"
            for p in plats)]
    if "yt" in plats:
        tail.append("<i>YouTube rounds its public count, so a small week can "
                    "show 0.</i>")
    broken = [r for r in results if r["count"] is None]
    if broken:
        tail += ["", f"⚠️ <b>Needs fixing ({len(broken)})</b> — the ! in the table"]
        tail += [f"• {html.escape(r['brand'])} {_SHORT[r['platform']]}: "
                 f"{html.escape(short_error(r['error']))}"
                 for r in sorted(broken, key=lambda r: (r["brand"], r["platform"]))]

    def table(chunk):
        return "<pre>" + html.escape("\n".join([header] + chunk)) + "</pre>"

    pages, head, chunk = [], "\n".join(out) + "\n\n", []
    for row in rows:
        if chunk and len(head) + len(table(chunk + [row])) > MSG_LIMIT:
            pages.append(head + table(chunk))
            head, chunk = "", []
        chunk.append(row)
    cur = head + table(chunk)
    for line in tail:
        if len(cur) + 1 + len(line) > MSG_LIMIT:
            pages.append(cur)
            cur = line
        else:
            cur += "\n" + line
    pages.append(cur)
    return pages


def main() -> None:
    from shared import config
    today = dt.date.today().isoformat()
    for msg in render(collect(config.BRANDS), None, None, today):
        print(html.unescape(re.sub(r"<[^>]+>", "", msg)))
        print()


if __name__ == "__main__":
    main()
