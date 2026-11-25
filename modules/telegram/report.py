"""
modules/telegram/report.py — the /report command: every account's latest reel,
side by side.

For each brand, the newest Reel / Short on each platform it has an account on
— Instagram (Graph API, modules/instagram/graph.latest_reel), Facebook (Pages
API, modules/facebook/poster.latest_reel) and YouTube (public numbers via
yt-dlp, modules/youtube/stats.latest_short, needs the brand's @handle) — with
views (and the likes / comments / shares behind the engagement line). Read from the platforms, not from the bot's own logs, so a
reel posted by hand counts too.

`collect(brands)` is the blocking half (one thread per read, all in parallel;
a failed read becomes that line's ⚠️, never an exception). `render(results)`
is pure — Telegram HTML built to be read in one glance: total views, best /
most engaging / weakest reel in plain words, one monospace table of views
(account × platform, best account first; "!" = read failed, "·" = no account)
and a "needs fixing" list; split under the 4096-char limit between rows.

CLI (prints the report as plain text): py modules/telegram/report.py
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

PLATFORMS = (  # key, label, icon
    ("ig", "Instagram", "📸"),
    ("fb", "Facebook", "📘"),
    ("yt", "YouTube", "▶️"),
)
_LABEL = {k: lbl for k, lbl, _ in PLATFORMS}
_ICON = {k: icon for k, _, icon in PLATFORMS}
_SHORT = {"ig": "IG", "fb": "FB", "yt": "YT"}

MSG_LIMIT = 4000  # Telegram's cap is 4096; leave room for the HTML tags
MAX_WORKERS = 8


def _fetchers() -> dict:
    """platform -> (account) -> (data | None, why). Imported lazily so the
    pure half (render) is testable without the platform modules' env."""
    from modules.facebook import poster as fb
    from modules.instagram import graph as ig
    from modules.youtube import stats as yt
    return {"ig": ig.latest_reel, "fb": fb.latest_reel, "yt": yt.latest_short}


def _account(brand: dict, platform: str) -> str:
    return brand.get("yt_handle" if platform == "yt" else platform) or ""


def collect(brands: list, fetchers: dict | None = None) -> list:
    """[{brand, platform, data, error}] — one entry per (brand, platform the
    brand has an account on). A YouTube channel without a public handle gets
    error "no handle" without a read. Blocking; never raises."""
    fetchers = fetchers or _fetchers()
    jobs, out = [], []
    for b in brands:
        for p, _, _ in PLATFORMS:
            acct = _account(b, p)
            if acct:
                jobs.append((b["name"], p, acct))
            elif p == "yt" and b.get("yt"):
                out.append({"brand": b["name"], "platform": p, "data": None,
                            "error": "no handle"})

    def run(job):
        name, p, acct = job
        try:
            data, why = fetchers[p](acct)
        except Exception as exc:  # the fetchers never raise; belt and braces
            data, why = None, str(exc)
        return {"brand": name, "platform": p, "data": data,
                "error": "" if data else (why or "unavailable")}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        out.extend(pool.map(run, jobs))
    return out


# --------------------------------------------------------------------------- #
# formatting                                                                   #
# --------------------------------------------------------------------------- #

def fmt_num(n) -> str:
    """1234 -> 1.2K, 2500000 -> 2.5M, None -> —"""
    if n is None:
        return "—"
    n = float(n)
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            val = f"{n / div:.1f}".rstrip("0").rstrip(".")
            return f"{val}{suffix}"
    return str(int(n))


def _parse_time(stamp: str):
    if not stamp:
        return None
    try:
        t = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        # Meta: 2026-10-30T17:00:04+0000 (no colon in the offset)
        try:
            t = dt.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def age(stamp: str, now: dt.datetime) -> str:
    t = _parse_time(stamp)
    if t is None:
        return "?"
    s = max(0, (now - t).total_seconds())
    if s < 3600:
        return f"{int(s // 60)}m ago"
    if s < 86400:
        return f"{int(s // 3600)}h ago"
    return f"{int(s // 86400)}d ago"


def engagement(d: dict) -> float | None:
    """(likes + comments + shares + saves) / views, or None without views."""
    views = d.get("views")
    if not views:
        return None
    acts = sum(d.get(k) or 0 for k in ("likes", "comments", "shares", "saves"))
    return acts / views


def short_error(why: str) -> str:
    """An API error trimmed to what an operator can act on."""
    w = (why or "").strip()
    low = w.lower()
    if w == "no handle":
        return 'no @handle set — add "handle" to the youtube block'
    if "missing" in low and "credential" in low:
        return "not connected"
    if "404" in w or "not found" in low or "does not exist" in low:
        return "channel not found"
    if "permission" in low or "(#10)" in w or "(#200)" in w:
        return "token lacks permission"
    if "expired" in low or "(code 190" in w or "session has been invalidated" in low:
        return "token expired"
    w = re.sub(r"\s+", " ", w)
    return w[:90] + ("…" if len(w) > 90 else "")


NAME_W = 10   # brand column of the table; longer names are cut with "…"
COL_W = 6     # one number column: "12.4K" plus a space
FRESH_S = 6 * 3600  # a reel younger than this isn't called the weakest yet


def _views(r: dict):
    return (r["data"] or {}).get("views")


def _cell(r: dict | None) -> str:
    """One table cell: views, "!" for a failed read, "·" for no account."""
    if r is None:
        return "·"
    if not r["data"]:
        return "!"
    return fmt_num(_views(r))


def _where(r: dict) -> str:
    return f"<b>{html.escape(r['brand'])}</b> on {_LABEL[r['platform']]}"


def render(results: list, now: dt.datetime | None = None) -> list:
    """Telegram-HTML messages (usually one) for `collect`'s results.

    Built to be read in one glance, top to bottom: the total, the three
    things worth knowing (best, most engaging, weakest), one table of views —
    an account per row, a platform per column, best account first — and what
    needs fixing. Likes / comments / shares stay out of it: views decide the
    ranking and the engagement line covers the rest."""
    now = now or dt.datetime.now(dt.timezone.utc)
    stamp = now.astimezone().strftime("%d %b, %H:%M")
    title = f"📊 <b>Latest reels</b> · {stamp}"
    if not results:
        return [f"{title}\n\nNo account has Instagram, Facebook or YouTube "
                "connected."]

    ok = [r for r in results if r["data"]]
    viewed = [r for r in ok if _views(r) is not None]
    plats = [p for p, _, _ in PLATFORMS if any(r["platform"] == p for r in results)]

    out = [title, "<i>views of each account's newest reel</i>", ""]
    total = sum(_views(r) for r in viewed)
    out.append(f"👁 <b>{fmt_num(total)} views in total</b>")
    out.append("   " + " · ".join(
        f"{_ICON[p]} {fmt_num(sum(_views(r) for r in viewed if r['platform'] == p))}"
        for p in plats))

    if viewed:
        out.append("")
        best = max(viewed, key=_views)
        out.append(f"🏆 Best: {_where(best)} — {fmt_num(_views(best))} views")
        engaged = [r for r in viewed if _views(r) >= 100
                   and engagement(r["data"])]
        if engaged:
            top = max(engaged, key=lambda r: engagement(r["data"]))
            out.append(f"🔥 Most engaging: {_where(top)} — "
                       f"{engagement(top['data']) * 100:.1f}% liked, commented "
                       "or shared")
        settled = [r for r in viewed if r is not best and (
            (t := _parse_time(r["data"].get("posted_at"))) is None
            or (now - t).total_seconds() >= FRESH_S)]
        if len(viewed) > 2 and settled:
            low = min(settled, key=_views)
            out.append(f"🐢 Weakest: {_where(low)} — {fmt_num(_views(low))} "
                       f"views ({age(low['data'].get('posted_at'), now)})")

    # The table: one row per account, most total views first.
    by_brand: dict[str, dict] = {}
    for r in results:
        by_brand.setdefault(r["brand"], {})[r["platform"]] = r
    ranked = sorted(by_brand, key=lambda n: (
        -sum(_views(r) or 0 for r in by_brand[n].values()), n))
    name_w = min(NAME_W, max(len(n) for n in ranked))

    def name(n):
        return n if len(n) <= name_w else n[:name_w - 1] + "…"
    header = " " * name_w + "".join(_SHORT[p].rjust(COL_W) for p in plats)
    rows = [name(n).ljust(name_w)
            + "".join(_cell(by_brand[n].get(p)).rjust(COL_W) for p in plats)
            for n in ranked]

    broken = [r for r in results if not r["data"]]
    tail = []
    if broken:
        tail = ["", f"⚠️ <b>Needs fixing ({len(broken)})</b> — the ! in the table"]
        tail += [f"• {html.escape(r['brand'])} {_SHORT[r['platform']]}: "
                 f"{html.escape(short_error(r['error']))}"
                 for r in sorted(broken, key=lambda r: (r["brand"], r["platform"]))]

    return paginate(out, [("", header, rows)], tail)


def paginate(head: list, tables: list, tail: list) -> list:
    """Telegram pages under MSG_LIMIT: the `head` lines, then each table
    ((caption line or "", header row, rows) — a monospace <pre>), then the
    `tail` lines. Split only between table rows (a page that continues a
    table repeats its header) and between tail lines, so no page ever cuts a
    row in half."""
    def pre(header, chunk):
        return "<pre>" + html.escape("\n".join([header] + chunk)) + "</pre>"

    pages, cur = [], "\n".join(head)
    for caption, header, rows in tables:
        prefix = ("\n\n" if cur else "") + (caption + "\n" if caption else "")
        chunk = []
        for row in rows:
            if chunk and len(cur) + len(prefix) + len(pre(header, chunk + [row])) > MSG_LIMIT:
                pages.append(cur + prefix + pre(header, chunk))
                cur, prefix, chunk = "", "", []
            chunk.append(row)
        if cur and len(cur) + len(prefix) + len(pre(header, chunk)) > MSG_LIMIT:
            pages.append(cur)
            cur, prefix = "", (caption + "\n" if caption else "")
        cur += prefix + pre(header, chunk)
    for line in tail:
        if cur and len(cur) + 1 + len(line) > MSG_LIMIT:
            pages.append(cur)
            cur = line
        else:
            cur = cur + "\n" + line if cur else line
    pages.append(cur)
    return pages


# --------------------------------------------------------------------------- #
# /report — every reel of the last 24 h                                        #
# --------------------------------------------------------------------------- #

WINDOW_H = 24


def _recent_fetchers() -> dict:
    """platform -> (account, since) -> (list of reels | None, why)."""
    from modules.facebook import poster as fb
    from modules.instagram import graph as ig
    from modules.youtube import stats as yt
    return {"ig": ig.recent_reels, "fb": fb.recent_reels, "yt": yt.recent_shorts}


def collect_recent(brands: list, since: dt.datetime,
                   fetchers: dict | None = None) -> list:
    """[{brand, platform, reels, error}] — every reel each account posted at
    or after `since`, one entry per (brand, platform it has an account on).
    `reels` is [] when the account posted none, None when the read failed.
    Blocking; never raises."""
    fetchers = fetchers or _recent_fetchers()
    jobs, out = [], []
    for b in brands:
        for p, _, _ in PLATFORMS:
            acct = _account(b, p)
            if acct:
                jobs.append((b["name"], p, acct))
            elif p == "yt" and b.get("yt"):
                out.append({"brand": b["name"], "platform": p, "reels": None,
                            "error": "no handle"})

    def run(job):
        name, p, acct = job
        try:
            reels, why = fetchers[p](acct, since)
        except Exception as exc:  # the fetchers never raise; belt and braces
            reels, why = None, str(exc)
        return {"brand": name, "platform": p, "reels": reels,
                "error": "" if reels is not None else (why or "unavailable")}

    if jobs:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            out.extend(pool.map(run, jobs))
    return out


def _sum_views(reels) -> int:
    return sum(r.get("views") or 0 for r in reels or ())


def render_recent(results: list, now: dt.datetime | None = None,
                  hours: int = WINDOW_H) -> list:
    """Telegram-HTML pages for `collect_recent`: total views of every reel
    posted in the window, the best / most engaging / weakest reel, accounts
    that posted nothing, one table of views (account x platform: the sum of
    that account's reels there, plus how many reels), then what needs fixing.
    Views are as of now — a reel posted an hour ago has had an hour."""
    now = now or dt.datetime.now(dt.timezone.utc)
    stamp = now.astimezone().strftime("%d %b, %H:%M")
    since = (now - dt.timedelta(hours=hours)).astimezone().strftime("%d %b, %H:%M")
    title = f"📊 <b>Reels of the last {hours} h</b> · {stamp}"
    if not results:
        return [f"{title}\n\nNo account has Instagram, Facebook or YouTube "
                "connected."]

    plats = [p for p, _, _ in PLATFORMS if any(r["platform"] == p for r in results)]
    reels = [dict(x, brand=r["brand"], platform=r["platform"])
             for r in results if r["reels"] for x in r["reels"]]
    viewed = [x for x in reels if x.get("views") is not None]

    plural = "s" if len(reels) != 1 else ""
    out = [title, f"<i>views so far of every reel posted since {since}</i>", ""]
    out.append(f"👁 <b>{fmt_num(_sum_views(reels))} views</b> on {len(reels)} reel{plural}")
    out.append("   " + " · ".join(
        f"{_ICON[p]} {fmt_num(_sum_views(x for x in reels if x['platform'] == p))}"
        f" ({sum(1 for x in reels if x['platform'] == p)})" for p in plats))

    def where(x):
        link = f' · <a href="{html.escape(x["url"])}">open</a>' if x.get("url") else ""
        return (f"<b>{html.escape(x['brand'])}</b> on {_LABEL[x['platform']]}",
                f"({age(x.get('posted_at'), now)}){link}")

    if viewed:
        out.append("")
        best = max(viewed, key=lambda x: x["views"])
        w, end = where(best)
        out.append(f"🏆 Best reel: {w} — {fmt_num(best['views'])} views {end}")
        engaged = [x for x in viewed if x["views"] >= 100 and engagement(x)]
        if engaged:
            top = max(engaged, key=engagement)
            w, end = where(top)
            out.append(f"🔥 Most engaging: {w} — {engagement(top) * 100:.1f}% "
                       f"liked, commented or shared {end}")
        settled = [x for x in viewed if x is not best and (
            (t := _parse_time(x.get("posted_at"))) is None
            or (now - t).total_seconds() >= FRESH_S)]
        if len(viewed) > 2 and settled:
            low = min(settled, key=lambda x: x["views"])
            w, end = where(low)
            out.append(f"🐢 Weakest: {w} — {fmt_num(low['views'])} views {end}")

    by_brand: dict[str, dict] = {}
    for r in results:
        by_brand.setdefault(r["brand"], {})[r["platform"]] = r
    idle = sorted(n for n, rows in by_brand.items()
                  if all(r["reels"] == [] for r in rows.values()))
    if idle:
        out.append(f"😴 Nothing posted: {html.escape(', '.join(idle))}")

    def cell(r):
        if r is None:
            return "·"
        if r["reels"] is None:
            return "!"
        if not r["reels"]:
            return "—"
        return fmt_num(_sum_views(r["reels"]))

    ranked = sorted(by_brand, key=lambda n: (
        -sum(_sum_views(r["reels"]) for r in by_brand[n].values()), n))
    name_w = min(NAME_W, max(len(n) for n in ranked))

    def name(n):
        return n if len(n) <= name_w else n[:name_w - 1] + "…"
    header = (" " * name_w + "".join(_SHORT[p].rjust(COL_W) for p in plats)
              + "#".rjust(4))
    rows = [name(n).ljust(name_w)
            + "".join(cell(by_brand[n].get(p)).rjust(COL_W) for p in plats)
            + str(sum(len(r["reels"] or ()) for r in by_brand[n].values())).rjust(4)
            for n in ranked]

    tail = ["<i>— = no reel in the window · # = reels posted</i>"]
    broken = [r for r in results if r["reels"] is None]
    if broken:
        tail += ["", f"⚠️ <b>Needs fixing ({len(broken)})</b> — the ! in the table"]
        tail += [f"• {html.escape(r['brand'])} {_SHORT[r['platform']]}: "
                 f"{html.escape(short_error(r['error']))}"
                 for r in sorted(broken, key=lambda r: (r["brand"], r["platform"]))]
    return paginate(out, [("", header, rows)], tail)


def main() -> None:
    from shared import config
    if "--latest" in sys.argv:
        pages = render(collect(config.BRANDS))
    else:
        now = dt.datetime.now(dt.timezone.utc)
        pages = render_recent(collect_recent(
            config.BRANDS, now - dt.timedelta(hours=WINDOW_H)), now)
    for msg in pages:
        print(html.unescape(re.sub(r"<[^>]+>", "", msg)))
        print()


if __name__ == "__main__":
    main()
