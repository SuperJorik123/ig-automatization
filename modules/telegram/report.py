"""
modules/telegram/report.py — the /report command: every account's latest reel,
side by side.

For each brand, the newest Reel / Short on each platform it has an account on
— Instagram (Graph API, modules/instagram/graph.latest_reel), Facebook (Pages
API, modules/facebook/poster.latest_reel) and YouTube (public numbers via
yt-dlp, modules/youtube/stats.latest_short, needs the brand's @handle) — with
views, likes, comments, shares/saves where the platform gives them, and an
engagement rate. Read from the platforms, not from the bot's own logs, so a
reel posted by hand counts too.

`collect(brands)` is the blocking half (one thread per read, all in parallel;
a failed read becomes that line's ⚠️, never an exception). `render(results)`
is pure — Telegram HTML messages, split under the 4096-char limit on account
boundaries, accounts ordered by total views.

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


def _metrics(d: dict) -> str:
    parts = [f"👁 {fmt_num(d.get('views'))}", f"❤️ {fmt_num(d.get('likes'))}",
             f"💬 {fmt_num(d.get('comments'))}"]
    if d.get("shares") is not None:
        parts.append(f"🔁 {fmt_num(d['shares'])}")
    if d.get("saves") is not None:
        parts.append(f"🔖 {fmt_num(d['saves'])}")
    er = engagement(d)
    if er is not None:
        parts.append(f"ER {er * 100:.1f}%")
    return " · ".join(parts)


def _line(r: dict, now: dt.datetime) -> str:
    p = r["platform"]
    d = r["data"]
    if not d:
        return f"{_ICON[p]} {_SHORT[p]} · ⚠️ {html.escape(short_error(r['error']))}"
    link = f'<a href="{html.escape(d["url"], quote=True)}">{_SHORT[p]}</a>' \
        if d.get("url") else _SHORT[p]
    return f"{_ICON[p]} {link} · {age(d.get('posted_at'), now)} · {_metrics(d)}"


def _sum(rows: list, key: str):
    vals = [r["data"].get(key) for r in rows if r["data"]]
    vals = [v for v in vals if v is not None]
    return sum(vals) if vals else None


def render(results: list, now: dt.datetime | None = None) -> list:
    """Telegram-HTML messages (usually one) for `collect`'s results."""
    now = now or dt.datetime.now(dt.timezone.utc)
    stamp = now.astimezone().strftime("%d %b %Y, %H:%M")
    ok = [r for r in results if r["data"]]
    head = [f"📊 <b>Latest-reel report</b> · {stamp}"]
    counts = " · ".join(
        f"{_SHORT[p]} {sum(1 for r in ok if r['platform'] == p)}/"
        f"{sum(1 for r in results if r['platform'] == p)}"
        for p, _, _ in PLATFORMS if any(r["platform"] == p for r in results))
    if not results:
        return ["\n".join(head + ["", "No account has Instagram, Facebook or "
                                  "YouTube connected."])]
    head.append(f"<i>Newest reel on every account · read {counts}</i>")

    # Totals per platform, then across all of them.
    head += ["", "<b>Totals</b>"]
    for p, label, icon in PLATFORMS:
        rows = [r for r in ok if r["platform"] == p]
        if not rows:
            continue
        tot = {k: _sum(rows, k) for k in ("views", "likes", "comments",
                                         "shares", "saves")}
        head.append(f"{icon} {label} · {_metrics(tot)}")
    if len({r["platform"] for r in ok}) > 1:
        tot = {k: _sum(ok, k) for k in ("views", "likes", "comments")}
        head.append(f"Σ All · 👁 {fmt_num(tot['views'])} · ❤️ "
                    f"{fmt_num(tot['likes'])} · 💬 {fmt_num(tot['comments'])}")

    # Best and weakest single reel by views, and best engagement.
    viewed = [r for r in ok if r["data"].get("views") is not None]
    if viewed:
        head.append("")
        best = max(viewed, key=lambda r: r["data"]["views"])
        head.append(f"🏆 Most views: <b>{html.escape(best['brand'])}</b> "
                    f"{_LABEL[best['platform']]} — {fmt_num(best['data']['views'])}")
        engaged = [r for r in viewed if engagement(r["data"]) is not None
                   and r["data"]["views"] >= 100]
        if engaged:
            top = max(engaged, key=lambda r: engagement(r["data"]))
            head.append(f"🔥 Best engagement: <b>{html.escape(top['brand'])}</b> "
                        f"{_LABEL[top['platform']]} — "
                        f"{engagement(top['data']) * 100:.1f}%")
        if len(viewed) > 2:
            low = min(viewed, key=lambda r: r["data"]["views"])
            head.append(f"🐢 Fewest views: <b>{html.escape(low['brand'])}</b> "
                        f"{_LABEL[low['platform']]} — {fmt_num(low['data']['views'])}")

    # Per account, most total views first; accounts with nothing readable last.
    by_brand: dict[str, list] = {}
    for r in results:
        by_brand.setdefault(r["brand"], []).append(r)
    order = {k: i for i, (k, _, _) in enumerate(PLATFORMS)}

    def brand_views(name):
        return sum(r["data"].get("views") or 0 for r in by_brand[name] if r["data"])

    blocks = []
    ranked = sorted(by_brand, key=lambda n: (-brand_views(n), n))
    for i, name in enumerate(ranked, 1):
        rows = sorted(by_brand[name], key=lambda r: order[r["platform"]])
        total = brand_views(name)
        title = f"<b>{i}. {html.escape(name)}</b>" + (
            f" — {fmt_num(total)} views" if total else "")
        blocks.append("\n".join([title] + [_line(r, now) for r in rows]))

    messages, cur = [], "\n".join(head) + "\n\n<b>By account</b>"
    for block in blocks:
        if len(cur) + 2 + len(block) > MSG_LIMIT:
            messages.append(cur)
            cur = block
        else:
            cur += "\n\n" + block
    messages.append(cur)
    return messages


def main() -> None:
    from shared import config
    for msg in render(collect(config.BRANDS)):
        print(html.unescape(re.sub(r"<[^>]+>", "", msg)))
        print()


if __name__ == "__main__":
    main()
