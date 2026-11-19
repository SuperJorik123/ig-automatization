# Radar — alerts-only OSINT plan (draft for the owner's review)

Status: **phases 1 + 2 built (2026-11-19), not deployed** — code in `modules/radar/`
(see CLAUDE.md). Owner's change to §5: no live window / digest — **every alert goes
out the moment it qualifies, any hour**; `RADAR_MAX_PER_HOUR` is only a runaway
guard (0 = off). Written 2026-11-19.

## 1. What it is, and what it is not

**It is** a process that watches many sources, notices when something worth a reel is
happening, and sends **one alert** to a private Telegram group — with the video when
there is one.

**It is not** a poster. The radar never publishes anything anywhere. Every reel
still goes through you: you look at the alert, and if you want it, you send the clip to
the news bot exactly as today (plan card → Render → Publish / 🇺🇸 US evening).

That keeps the 2026-10-06 rule (no unattended posting) intact **in spirit** — but the
rule as written also says the collector may not run. The radar is a new, always-on
process that reads Telegram channels the way the collector did, so **you have to
lift the rule for an alerts-only radar explicitly** before it is built. (Decision #1
in §10.)

## 2. The shape of it

```
 SOURCES                         every source returns the same "item":
 ─────────                       {source, time, text, media?, place?, link}
 Telegram channels (with video)
 Reddit (rising)                     │
 Hazard feeds (quakes, storms…)      ▼
 Calendar                         CLEAN UP      drop text-only, drop repeats of the
 later: Snap Map, scanners, X        │          same video (file size / hash), translate
                                     ▼
                                  GROUP         5 posts about the same robbery = ONE
                                     │          incident, not 5 alerts
                                     ▼
                                  SCORE         is it an incident? which category?
                                     │          where? is it rising? does it fit MY
                                     │          audience? (learned from your reels)
                                     ▼
                                  ALERT  →  private "Radar" Telegram group
                                     │          (only above the bar, with a volume cap)
                                     ▼
                                  YOU decide → forward the clip to the news bot
                                     │
                                     ▼
                                  LEARN         which alerts you used, how those reels
                                                did → the score gets better
```

## 3. Sources, in the order to build them

### Phase 1 — free, nothing new to buy

| Source | What it gives | How | Cost |
|---|---|---|---|
| **Telegram breaking channels** | The fastest raw video of incidents worldwide | A Telegram **user** session (Telethon, same library the bot already uses) listening to ~50-100 channels; **keeps posts with video**, skips text-only | free |
| **Reddit "rising"** | US street-level clips (fights, crashes, weather, freakouts) before they peak | Reddit's free API app (World Monitor found the no-login JSON blocked) on ~15 subs: PublicFreakout, CrazyFuckingVideos, Damnthatsinteresting, nextfuckinglevel, WeatherGifs, TikTokCringe, iamatotalpieceofshit, IdiotsInCars, ActualPublicFreakouts, news, worldnews, … | free |
| **Hazard feeds** | "Something big just happened at X" — the trigger to go look on Snap Map / TikTok | USGS earthquakes, US National Weather Service warnings (tornado, flash-flood emergency), NOAA hurricanes, GDACS world disasters | free, no key |
| **Calendar** | What will happen — games that end in riots, storms 3-5 days out, holidays (Black Friday fights, July 4 fireworks), trials, protests announced | A hand-kept list + the weather forecast feeds | free |

Starting channel list = World Monitor's breaking-news ones (BNONews, OSINTdefender,
ClashReport, AuroraIntel, nexta_tv, spectatorindex, LiveUAMap, …) **+ the channels
your old collector used** + US-focused ones we add together. You approve the list.

### Phase 2 — the "something is happening" brain

- **Grouping** posts about the same incident (same place + same kind + same hour, or
  the same video seen in several channels).
- **Spike detection** (World Monitor's recipe): a word or place suddenly appearing
  ≥ 3× its normal rate over the last 7 days, within 2 hours. "Lyon" normally twice a
  day, suddenly 15 times in an hour → alert, even before any video exists.
- **Speed**: how fast an incident gains new posts. Rising beats big.

### Phase 3 — harder or paid sources (each a separate decision)

| Source | Why | Catch |
|---|---|---|
| **Snap Map** | The "woman filming on the same street" clips, worldwide | Unofficial web data; may break any day; against Snapchat's terms. One-evening test first. |
| **Police scanner audio** (Broadcastify) → speech-to-text → keywords | US street crime minutes after the 911 call | Broadcastify's terms limit automated use; speech-to-text costs per hour of audio. Pick 3-5 big cities. |
| **X/Twitter** | Where many clips appear first | The search API is paid per read; the cost has to be measured first |
| **TikTok** | Where originals often land first | No usable official access; scraping is fragile |

## 4. Scoring — how it decides what reaches you

Two layers, cheapest first:

1. **Rules (free)**: has video? → big bonus. Fresh (< 1 h)? → bonus. Several channels
   at once? → bonus. Category on your "never" list (e.g. war footage you don't post)?
   → drop.
2. **One cheap AI call per video item** (not per text post): what category it is
   (robbery / fight / fire / crash / weather / rescue / protest / politics / animal…),
   where (city, country), is the speech English, is it too graphic to post, a 1-line
   description. Uses the same OpenRouter account with a **daily spend cap** setting.

Then **"fits my audience"** — the part that comes from the audience work (category
tags on every reel you post + their follows/shares per view). At the start this is
just your list of wanted categories; once ~50 reels are tagged, the radar ranks
categories by what actually brought followers.

## 5. What an alert looks like

```
🔥 FIRE · Bushwick, Brooklyn (US) · 12 min ago
Warehouse fire, flames through the roof, crowd filming from the street.
📈 rising — 4 channels in 15 min · 🗣 English speech
[video attached]
🔗 BNONews · t.me/… · reddit.com/…

[👀 Watch]   [✕ Not interesting ▾]
```

- **The video comes attached**, so the normal path is: you **forward it to the news
  bot's group** — that's already a plan card today, no new code in the news bot.
  (A one-tap "Send to news bot" button can come later; bots can't see other bots'
  messages, so it needs a small handover through the database.)
- **👀 Watch**: alert me again only if this incident grows (more videos, better angle).
- **✕ Not interesting** with a reason (boring / old / already everywhere / not my
  audience / too graphic) — that's how the radar learns your taste.

**Volume**: a cap per hour (start: 6) and modes —
- **Live** during your hunting window (e.g. 21:00-01:00 Chisinau): alerts as they come.
- **Digest** outside it: one message every few hours with the top 5.
- **Evening briefing** at the start of the hunt: what's happening now + what's
  coming (storms, games, holidays) in the next days.

## 6. Where it runs and how it's built

- **Own process, own service** on the VPS: `radar` (systemd), **own bot token**, own
  private "Radar" group (a **supergroup** from day one — the lesson of the lite bot).
  It doesn't touch `news-bot`, `newsroom-bot` or `render-bot`, so a radar crash or
  restart never affects posting.
- **A separate Telegram account for listening.** Reading 100 channels around the
  clock is the kind of thing Telegram rate-limits or bans; your main account (@viceB)
  is the one the bots use for big files and must not be put at risk. A spare SIM /
  number for the radar.
- Code in `modules/radar/`: one file per source (each returns items), `cluster.py`,
  `score.py`, `alert.py`, its own small database. Tests offline like the rest.
- **Memory**: the VPS has 2 GB and already runs 3 bots and renders. The radar must
  stay small (target < 150 MB) and never download videos it isn't going to alert on.

## 7. Things that can go wrong

| Risk | What we do |
|---|---|
| Alert flood → you mute it | Hard cap per hour, digest mode, "not interesting" learning |
| The listening account gets banned | Separate account; join channels slowly; obey Telegram's wait times |
| Graphic / illegal content in alerts | AI flags it; "too graphic" category hidden by default |
| Copyright | Same as today — credit the filmer in the pinned comment; prefer finding the original (`tools/find_original`) |
| Costs creep | Daily spend cap; AI only on video items; paid sources only after a measured test |
| Unofficial sources break (Snap Map) | Each source is independent; one breaking doesn't stop the others; the radar names a dead source in the group once |

## 8. How we know it works

After 2 weeks, measured from the radar's own log:
- how many alerts you **used** (target: at least 1 in 5),
- how many minutes **ahead** of the big pages the alert came,
- how reels from radar clips do vs. your other reels (follows per 1000 views).

## 9. Build order

1. **Audience tagging** (category on every reel + weekly "what converts") — tells the
   radar what to hunt. Small, inside the existing bot.
2. **Radar skeleton**: Telegram channels (video only) → AI label → alert with video,
   rules-only score, volume cap. *The first useful version.*
3. Reddit rising + hazard feeds + evening briefing.
4. Grouping + spike detection + 👀 Watch.
5. "Not interesting" learning + the 2-week measurement.
6. Phase-3 sources, one at a time, each after a small test: Snap Map → scanners → X.

## 10. Decisions needed from you

1. **Lift the "collector must not run" rule for an alerts-only radar?** (It never posts.)
2. **A spare Telegram account** for the listening session — can you get a number?
3. **Your hunting window** (when alerts come live) and the **cap per hour**.
4. **Categories** you want / never want (e.g. war footage? politics? animals?).
5. **World or mostly US?** — affects channels, subs and the score's US bonus.
6. **Monthly budget** for AI labelling (and later X / speech-to-text).
7. **The channel list** — I draft it, you strike what you don't want.
