# JNN groups of three — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** JNN splits into numbered groups (JNN 1/2/3); a card opens on JNN 1; a bundle puts each video on ≤ 3 JNN accounts with JNN 1 getting the most different videos; GMN unchanged.

**Architecture:** Groups stay data (`"group"` in `credentials/brands/*.json`). A *numbered* group (`<name> <n>`) is a priority tier — `groups.tier_order` lists them. `bundle.assign` deals round-robin copies onto tier accounts in tier order and keeps today's even split for every other account.

**Tech Stack:** Python, python-telegram-bot, pytest.

**Spec:** `docs/superpowers/specs/2026-11-25-jnn-groups-design.md`

## Global Constraints

- GMN (and any un-numbered group) behaves exactly as today.
- In a bundle a video goes to at most `bundle.COPIES_PER_VIDEO = 3` tier accounts.
- Copies dealt round-robin v1, v2, …, vV, v1, … onto tier accounts in tier order (JNN 1 → 2 → 3); v1 = first video joined.
- Inside one group, which account gets which of that group's videos is random and avoids reel-log history.
- Card default: `JNN 1`; if no `JNN 1` exists (credentials not edited yet) fall back to `JNN`, then every usable brand.
- Warning line when > 3 tier-group brands are selected: `⚠️ same video on N accounts — Instagram hides copies`.

## Review Focus

- Credentials not yet edited (all `"JNN"`): card default must stay JNN, bundle must behave as today — tests in Task 2/3.
- A paused account inside a group: the group just has fewer slots; the deal continues to the next account in order — Task 2 test.
- More videos than tier accounts: extras carried, none dropped — Task 2 test.
- Mixed GMN + JNN tiers in BUNDLE_ACCOUNTS: GMN still gets every account filled — Task 2 test.
- Group name with a space (`JNN 1`) in the Brands summary line and `b:g:<i>` — Task 3 test.

---

### Task 1: `groups.tier_order`

**Files:** Modify `modules/telegram/groups.py`; Test `tests/test_groups.py`

**Interfaces — Produces:** `tier_number(name: str) -> int | None` (`"JNN 1"` → 1, `"GMN"` → None); `tier_order(brands: list, names: list) -> list[list[str]]` — the numbered groups' members among `names`, groups by number ascending, members in `names` order.

- [ ] Tests: `tier_number("JNN 2") == 2`, `tier_number("GMN") is None`, `tier_number("JNN") is None`; `tier_order` on brands in groups JNN 2/JNN 1/GMN returns `[[JNN 1 members], [JNN 2 members]]`, GMN absent, a name not in `names` absent.
- [ ] Implement, run `py -m pytest tests/test_groups.py -q`, commit.

### Task 2: tiered `bundle.assign`

**Files:** Modify `modules/telegram/bundle.py`, `modules/telegram/news_bot.py` (`_render_bundle`, `_reshuffle_bundle`); Test `tests/test_bundle.py`, `tests/test_news_bot_bundle.py`

**Interfaces — Consumes:** `groups.tier_order`. **Produces:** `assign(videos, accounts, history=None, rng=None, tiers=None) -> ({account: video}, carried)` — `tiers` a list of account lists; accounts in a tier use the tiered deal, the rest the old even split. Carried = videos neither part used.

- [ ] Tests (`tests/test_bundle.py`, tiers T = [[a1,a2,a3],[b1,b2,b3],[c1,c2,c3]]): 1 video → only T[0], 3 copies; 2 videos → 6 accounts, T[0] holds both videos (2+1), T[2] empty, each video 3 copies; 3 videos → every group has v1,v2,v3; 4 videos → T[0] = {v1,v2,v3} as a multiset, every video ≤ 3; 10 videos → 9 used, v10 carried; paused a2 → T[0] = 2 accounts and the deal continues into T[1]; history avoidance inside a group; tiers + legacy accounts → legacy accounts each get one video.
- [ ] news_bot passes `tiers=groups.tier_order(config.BRANDS, accounts)` in both call sites; news_bot test: brands in JNN 1/2/3, 2 videos → 6 accounts in plan.
- [ ] Run `py -m pytest tests/test_bundle.py tests/test_news_bot_bundle.py -q`, commit.

### Task 3: card default + warning

**Files:** Modify `modules/telegram/branded.py`; Test `tests/test_branded.py`

**Interfaces — Produces:** `DEFAULT_GROUPS = ("JNN 1", "JNN")`; `plan_lines(...)` appends the warning when > 3 selected brands are in numbered groups.

- [ ] Tests: default_plan picks JNN 1 when it exists, JNN when only JNN exists; warning present for 4 tier brands, absent for 3 and for 6 GMN brands; `brands_summary` shows `JNN 1 (3)`.
- [ ] Implement, run `py -m pytest tests -q`, commit.

### Task 4: docs + ops

- [ ] CLAUDE.md: groups.py / bundle.py / branded rows; `credentials/brands/_example.json` comment.
- [ ] Deploy note: on the VPS set `"group"` per spec and move `altenews.json` back into `credentials/brands/` if disabled.
