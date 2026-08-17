"""
modules/newsroom/rewrite.py — a WordPress article into a Telegram post.

One OpenRouter call per article, built on the same client and the same
never-raises contract as modules/telegram/translator.py: a model hiccup must
degrade the post, not lose it. When the key is missing or the call fails, the
fallback is the article's own title and opening paragraph — thin, but true,
which is the property that matters for a client's news channel.

This module is the only part of the bot whose output is a matter of taste
rather than correctness, and the only one whose tuning is measured in weeks.
That is what `--sample` at the bottom is for: it prints source next to
generated post for the last N articles of a site, so the prompt can be judged
on fifty real examples instead of one at a time in production.

LENGTH is a hard constraint, not a preference. Telegram allows 4096 characters
in a message but only 1024 in a media CAPTION, and nearly every post here
carries the article's featured image. The link and its separator are appended
afterwards by publish.py, so the target below leaves room for a long URL.
"""

import argparse
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from openai import OpenAI, APIError  # noqa: E402

from shared import config  # noqa: E402

log = logging.getLogger(__name__)

_client = (
    OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=config.OPENROUTER_API_KEY,
    )
    if config.OPENROUTER_API_KEY
    else None
)

# Characters the generated post should stay under when the site does not say
# otherwise (`post_chars` in the site file). Telegram's caption ceiling is
# 1024; publish.py then appends "\n\n🔗 <url>", and a WordPress permalink with
# a long slug runs well past 100 characters — but the binding constraint is
# editorial: the client wants a short post, not the whole article.
TARGET_CHARS = 500


def _target(site: dict | None) -> int:
    """The site's length target, tolerating a malformed value the same way
    orders._rand_range does — a typo costs the site the default, not the run."""
    try:
        n = int((site or {}).get("post_chars") or 0)
    except (TypeError, ValueError):
        n = 0
    return n if n > 0 else TARGET_CHARS


_SYSTEM = (
    "You are the editor of a Telegram news channel. You are given one article "
    "from the channel's own website. Rewrite it as the channel's post about "
    "that article.\n\n"
    "FAITHFUL — every fact, name, number, date, quote and attribution in your "
    "post must appear in the source article. Invent nothing: no context the "
    "article does not give, no background, no consequence, no speculation, no "
    "detail that merely sounds plausible. If the article does not say it, it "
    "does not go in the post. This is the channel's credibility and it is the "
    "single rule that matters most.\n"
    "LEDE — open with the news itself, in the first sentence. No throat-"
    "clearing, no scene-setting, no rhetorical question, no 'Breaking:'.\n"
    "LENGTH — aim for about {aim} characters, shorter when the story is "
    "small; {target} is a HARD LIMIT past which the post is cut off "
    "mid-sentence. That budget fits the news plus one or two supporting facts "
    "— pick the facts that make it land and leave everything else for the "
    "click. Do not try to compress the whole article in.\n"
    "REGISTER — neutral, professional news style, the way a wire service "
    "writes. No hype, no editorialising, no exclamation marks, no emoji, no "
    "hashtags, no invented quotes, and never a quotation mark around words "
    "that were not quoted in the source.\n"
    "PLAIN TEXT — no Markdown, no HTML, no bold, no bullet characters, no "
    "links. Line breaks between paragraphs are fine and nothing else is.\n"
    "NO SIGN-OFF — do not add a call to action, a 'read more', a link, or any "
    "reference to the article, the website or the channel. The link is "
    "attached separately.\n\n"
    "Output ONLY the post text. No preamble, no notes, no surrounding "
    "quotation marks, no title line."
)


def _shorten(text: str, target: int) -> str:
    """Enforce the length target the model was asked for and routinely misses —
    models cannot count characters, so past a point this is the only guarantee.

    Cuts at the last sentence end inside the budget, so an over-long post loses
    its final fact instead of ending mid-sentence; a single monster sentence
    falls back to a word-boundary cut with an ellipsis."""
    if len(text) <= target:
        return text
    head = text[:target]
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "),
              head.rfind(".\n"), head.rfind("!\n"), head.rfind("?\n"))
    if head.endswith((".", "!", "?")):
        return head
    if cut > 0:
        return head[:cut + 1]
    spaced = head.rsplit(" ", 1)[0].rstrip(",;:—- ")
    return (spaced or head[:target - 1]) + "…"


def _fallback(article: dict, target: int = TARGET_CHARS) -> str:
    """What ships when the model is unavailable. The article's title and its
    opening paragraph: dull, but every word of it came from the source."""
    title = (article.get("title") or "").strip()
    body = (article.get("body") or "").strip()
    first = next((p.strip() for p in body.split("\n\n") if p.strip()), "")
    out = "\n\n".join(p for p in (title, first) if p)
    return out[:target].rstrip()


def _prompt(article: dict) -> str:
    """The article as the model sees it. Title and body are labelled so the
    model does not mistake the headline for the first sentence of the body.

    The body is truncated: a 20 000-character longread costs tokens on every
    poll, and the news is in the top of a news article by construction."""
    body = (article.get("body") or "").strip()
    return (
        f"TITLE: {(article.get('title') or '').strip()}\n\n"
        f"ARTICLE:\n{body[:6000]}"
    )


def to_telegram(article: dict, site: dict | None = None) -> str:
    """`article` (wp.normalise's shape) as a Telegram post body, without the
    link — publish.py appends that after trimming to Telegram's limit.

    Never raises. Returns the title-plus-lede fallback when there is no API
    key, when the call fails, or when the model returns nothing."""
    site = site or {}
    if not (article.get("body") or article.get("title")):
        return ""

    target = _target(site)
    if _client is None:
        log.warning("OPENROUTER_API_KEY not set — posting the article's own lede")
        return _fallback(article, target)

    # The aim sits well under the cap: models track an aim point far better
    # than a ceiling, and "about 375" is what actually lands posts under 500.
    system = _SYSTEM.format(target=target, aim=max(1, target * 3 // 4))
    hint = (site.get("rewrite_hint") or "").strip()
    if hint:
        # Per-channel tone, appended rather than interpolated into the rules
        # above so a hint can never dislodge the FAITHFUL constraint.
        system += f"\n\nCHANNEL NOTE: {hint}"

    try:
        resp = _client.chat.completions.create(
            model=config.NR_REWRITE_MODEL,
            # Low, like the translator's: this is wire copy, and temperature is
            # exactly the knob that invents a detail the article never had.
            temperature=0.3,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": _prompt(article)},
            ],
        )
    except APIError as exc:
        log.error("rewrite failed for %s: %s — posting the article's own lede",
                  article.get("url"), exc)
        return _fallback(article, target)
    except Exception as exc:  # transport, auth, malformed response
        log.error("rewrite failed for %s: %s — posting the article's own lede",
                  article.get("url"), exc)
        return _fallback(article, target)

    try:
        out = (resp.choices[0].message.content or "").strip()
    except (AttributeError, IndexError, TypeError):
        out = ""
    if not out:
        return _fallback(article, target)
    return _shorten(out, target)


# --------------------------------------------------------------------------- #
# Eyeball harness                                                             #
# --------------------------------------------------------------------------- #


def _sample(site_name: str, n: int) -> int:
    """Print source next to generated post for a site's last N articles.

    Reads live from the site and calls the model, but posts nothing and touches
    no database — this is the tool the prompt gets tuned with before anything
    goes near a client's channel."""
    from modules.newsroom import wp

    site = next((s for s in config.NR_SITES if s["name"] == site_name), None)
    if not site:
        known = ", ".join(s["name"] for s in config.NR_SITES) or "none configured"
        print(f"unknown site {site_name!r} — known: {known}")
        return 1

    for article in wp.fetch_recent(site, limit=n)[:n]:
        post = to_telegram(article, site)
        print("=" * 78)
        print(f"{article['published_at']}  {article['url']}")
        print(f"media: {article['media_type'] or 'none'}")
        print("-" * 78)
        print("SOURCE:", (article["body"] or "")[:600].replace("\n", " "))
        print("-" * 78)
        print(f"POST ({len(post)} chars):")
        print(post)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", metavar="SITE",
                    help="print source vs generated post for a site's recent articles")
    ap.add_argument("-n", type=int, default=10, help="how many articles (default 10)")
    args = ap.parse_args()
    if not args.sample:
        ap.print_help()
        return 1
    return _sample(args.sample, args.n)


if __name__ == "__main__":
    raise SystemExit(main())
