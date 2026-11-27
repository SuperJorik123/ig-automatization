# JNN in groups of three — design

Date: 2026-11-25 · Status: awaiting owner approval

## Why

All eight JNN Instagram accounts post the same video within ~10 minutes (36 of the
last 40 clips went to 2-8 accounts, 13 to all 8). Instagram shows one or two of the
copies and buries the rest: @daily.news.co and @pioneerwire get a median of 2 views a
reel, Frontiva has been stuck at 476 followers for weeks. The co-founder posts one
video a day to all eight because the plan card opens with all of JNN ticked.

## The rule

**A video goes to at most one group of three accounts — never all eight.**

| Group | Accounts |
|---|---|
| JNN 1 ⭐ (most promising) | frontiva24, wswire, eur24news |
| JNN 2 | westorn24, worldbrief, altenews |
| JNN 3 | dailynews, pioneerwire, atlasnews |

vestra24 (on hold) and europamonitor (Telegram only) stay out of the groups (Custom only).
altenews has a logo in `brands/JNN/altenews/`, but locally its credentials file sits in
`credentials/brands_disabled/` — so the bot doesn't see it there. Check the VPS at deploy:
its file must be in `credentials/brands/` with `"group": "JNN 2"`.

## Behaviour

- **Single post:** the plan card opens with **JNN 1** ticked (today: all of JNN). The
  co-founder's routine doesn't change — Render, Publish. To send a video elsewhere he
  taps the JNN 2 / JNN 3 row (the group rows already exist, one tap each).
- **GMN is not touched** — same group, same behaviour, in single posts and bundles.
- **Bundle (JNN accounts):** every video goes to **at most 3 accounts**, so a bundle of
  V videos fills `min(3 × V, accounts)` JNN accounts, and **JNN 1 gets as many
  different videos as possible**. The copies are dealt round-robin (v1, v2, …, vV, v1,
  v2, …) onto the accounts in group order JNN 1 → JNN 2 → JNN 3; inside a group, which
  account gets which of its videos is random (and still avoids a video the account
  already posted, per the reel log):

  | Videos | JNN 1 | JNN 2 | JNN 3 |
  |---|---|---|---|
  | 1 | v1 v1 v1 | — | — |
  | 2 | v1 v2 v1 | v2 v1 v2 | — |
  | 3 | v1 v2 v3 | v1 v2 v3 | v1 v2 v3 |
  | 4 | v1 v2 v3 | v4 v1 v2 | v3 v4 v1 |
  | 9 | v1 v2 v3 | v4 v5 v6 | v7 v8 v9 |

  v1 is the first video sent. Videos past the ninth wait for the next bundle, as extras
  do today.
- **More than three accounts ticked by hand** (⚙ Custom): allowed, with a warning on
  the card — `⚠️ same video on 8 accounts — Instagram hides copies` — so an exceptional
  story can still go wide on purpose.

## Approaches considered

1. **Groups as data (chosen).** Set `"group"` in each `credentials/brands/<name>.json`
   to `JNN 1` / `JNN 2` / `JNN 3` and change `branded.DEFAULT_GROUP`. The collapsed
   group rows, the picker and `groups.py` work unchanged; the brand's logo folder stays
   `brands/JNN/<name>/` (`config._brand_folder` lets the credentials group win and
   searches the folders for the logo). Smallest change, no new concept.
2. A separate `"tier"` field next to `"group"`: keeps "JNN" as the family name but adds
   a second grouping concept to config, the picker and the tests. Rejected — more code
   for the same buttons.
3. A daily round / rotation (videos queued until three are in, or auto-rotating
   groups): rejected by the owner — the co-founder finds a video and posts it at once.

## Changes

- `credentials/brands/*.json` on the VPS (git-ignored — an ops edit, listed in the
  deploy steps), and `_example.json`'s comment.
- `modules/telegram/branded.py`: `DEFAULT_GROUP = "JNN 1"`; the >3-accounts warning
  line on the plan card.
- `modules/telegram/bundle.py` + `news_bot._render_bundle`: assignment by group —
  round-robin copies (≤ 3 per video) dealt onto the JNN accounts in group order, as in
  the table above; non-JNN-numbered accounts (GMN) keep today's assignment.
  `BUNDLE_ACCOUNTS` keeps working as the pause list (a paused account just drops out
  of its group).
- Tests: `tests/test_branded.py`, `tests/test_groups.py`, `tests/test_news_bot_bundle.py`.
- `CLAUDE.md`: the group rows / bundle paragraphs.

## Left to the builder

- Group names with a space (`JNN 1`) must survive the `b:g:<i>` callbacks (they index,
  so they should) and the Brands summary line.
- The working tree has uncommitted changes in `branded.py`, `bundle.py`, `news_bot.py`
  (the schedule-card work) — build on top of them, don't revert them.

## Not in scope

The X radar source (designed separately, after this); AI-suggested videos for the
other groups (later, once the radar feeds the news bot).
