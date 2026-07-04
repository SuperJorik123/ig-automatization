# NewsCompany — Social Media Automation Platform

A modular automation platform for managing news distribution across multiple social media accounts and platforms at scale. Built for a news company operating accounts across different regions and languages.

---

## What This Is

A PC-side orchestration layer + Android driver that lets a small team:
- Run **10–500 social accounts** across Instagram, Telegram, Twitter, and Facebook from one place
- Post the same news story to every account simultaneously, **translated into each account's language**
- Eventually: ingest news from hundreds of external sources and route it intelligently to the right channels at the right frequency

The human workload collapses to: *drop content → review → it goes everywhere*.

---

## Platform Modules

| Module | Status | Purpose |
|--------|--------|---------|
| `modules/telegram/` | **In progress** | News broadcast to 5 channels (manual + auto) |
| `modules/instagram/` | Functional | Post photos/reels to phone-managed IG accounts via ADB |
| `modules/twitter/` | Stub | Posting not implemented; downloading from X works |
| `modules/facebook/` | Stub | Not started |

---

## Current Focus: Telegram Broadcast

### The 5 channels

| Channel | Language | Audience |
|---------|----------|----------|
| @channel_us_1 | English | US — East Coast / general |
| @channel_us_2 | English | US — regional |
| @channel_us_3 | English | US — specific niche |
| @channel_eu | English | Europe |
| @channel_ru | Russian | Russia |

> Replace the @usernames above with the real ones once confirmed.

### How it works (current)

The **manual broadcast**: an operator posts a photo/video/album with a caption (or plain text) into the control Telegram group. The bot replies with a channel picker; the operator selects which channels get it and confirms. No scheduling — nothing posts until a human confirms.

```
Operator posts in the control group (TELEGRAM_CHAT_ID)
  → bot replies with a channel picker (toggle per channel, All/None)
  → operator taps "▶ Post to selected"
  → translator.py (OpenRouter) translates the caption per channel language
  → publisher.py fans out to the selected channels
  → per-channel ✅/❌ status appears in the group
```

**Files:**
- `modules/telegram/news_bot.py` — the bot process; watches the control group, shows the picker, publishes
- `modules/telegram/publisher.py` — translate-per-language + fan-out
- `modules/telegram/translator.py` — OpenRouter translation; fails gracefully (posts original on error)
- `modules/telegram/collector.py` — Telethon user client; watches source channels and queues posts (future smart-filter input; nothing drains the queue yet)
- `modules/telegram/queue_store.py` — SQLite queue + per-source cursor

### Current state of the manual broadcast

**Built, needs live-channel testing.** The group-picker flow, translation, and fan-out are implemented end to end.

**What still needs testing:**
- End-to-end run against the live 5 channels with real media (photo, video, album)
- Translation fallback (a failed OpenRouter call should post the original text, not drop the post)
- Confirming bot has admin/post rights in all 5 channels

---

## Next Step: Smart Filter (Designed, Not Built)

Once the manual broadcast is stable, the next layer is **automated ingestion** — pulling news from external sources and distributing it intelligently.

### Concept

```
External sources (RSS, Telegram channels, APIs)
  → collector.py ingests + deduplicates
  → importance scorer ranks each item
  → regional router assigns to relevant channel(s)
  → quota manager applies the percentage budget
  → publisher.py posts (with translation)
```

### Importance scoring

Every incoming item gets a score (0–100). Score determines:
- Whether it gets posted at all
- Whether it can exceed the channel's daily quota

Factors that drive score up:
- Topic relevance to the channel's region
- Source credibility / reach
- Recency
- Keywords (war, election, disaster, economy — high; celebrity milestones — low)

A **hard-override threshold** exists: items scoring above a configurable ceiling (e.g. 85) always post regardless of quota. This handles "Germany declares war on Poland"-level stories.

### Regional routing

Each channel has a regional profile (e.g. `us-east`, `europe`, `russia`). A story is routed to a channel only if its geographic tags overlap with the channel's profile.

### Percentage quota

Each channel runs a daily budget split:
- **60% local** — stories tagged to the channel's primary region
- **40% international** — stories of broad global relevance

The quota resets daily. When a channel's local bucket is full, incoming local stories queue or drop (unless they hit the hard-override threshold). Same for international.

### Deduplication

Stories from different sources about the same event are clustered. The cluster is posted once (the best version). Subsequent updates ("X hours later…") are posted only if they add material new information — detected by semantic similarity against what's already been posted to that channel.

> **This module is not yet started.** Design above is the intended architecture.

---

## Future: Instagram Distribution

Instagram is intentionally lower-volume than Telegram. The design goal:

- **1–2 posts per day per account**
- Only items that score above a high threshold (importance ≥ 70, rough target)
- Captions rewritten for Instagram tone (engaging, punchy, with hashtags) — separate from Telegram's more neutral news style
- One IG account per regional language, mirroring the Telegram channel structure

The current `modules/instagram/upload_post.py` already drives the phone via ADB/uiautomator2 for photo and reel posting. The missing piece is plugging it into the smart filter output.

**Not started yet — depends on smart filter being live.**

---

## Future: Video Processing

For video content, a processing pipeline will sit between download and publish:
- Resize/crop to platform specs (9:16 for Reels/Stories, 16:9 for YouTube)
- Burn-in subtitles / captions from the translated text
- Overlay: channel logo, lower-third with source attribution
- Intro/outro bumper per channel brand

Likely toolchain: `ffmpeg` for processing, triggered automatically before publish.

**Not started. Requires video pipeline design.**

---

## Future: Twitter / Facebook

Both stubs exist in `modules/twitter/poster.py` and `modules/facebook/poster.py`. They mirror the Instagram upload signature so they can drop into the same call sites once implemented.

Twitter/X downloading already works (via `shared/reel_downloader.py` + yt-dlp).

**No timeline yet.**

---

## Architecture

```
shared/
  config.py            .env loading, device ID, account lists
  reel_downloader.py   yt-dlp wrapper (IG + Twitter URLs → local file)

modules/
  telegram/
    collector.py       Telethon user client — watches source channels, fills queue
    news_bot.py        Bot process — manual DMs + daily auto-post job
    publisher.py       Translate-per-language + fan-out
    translator.py      Claude API translation (fails gracefully)
    queue_store.py     SQLite queue + per-source cursor
    telegram_bot.py    Separate: IG trigger bot (paste URL → post to IG)

  instagram/
    upload_post.py     ADB + uiautomator2 driver for IG post/reel flow
    main.py            Reels scroll loop (engagement automation)
    human_swipe.py     Realistic swipe model

  twitter/             Stub
  facebook/            Stub

server.py              FastAPI backend (:8000) — UI uploads, queue management
ui/                    Angular frontend (:4200) — queue new posts manually
posts/                 Shared media queue (NNN.jpg/mp4 + NNN.json)
posts/posted/          Archive after successful post
```

---

## Running the System

Full setup and run commands: see `START.md`.

Telegram-specific setup (credentials, channel IDs, bot privacy mode): see `modules/telegram/README.md`.

---

## Immediate Priorities

1. **Wire up live Telegram channels** — fill `.env` with real bot token, control group ID, channel list
2. **End-to-end test the manual broadcast** — post a photo + caption in the control group, pick channels, verify it lands with correct translations
3. **Stress test translation fallback** — confirm that a Claude API failure doesn't drop the post, just posts the original
4. **Design the importance scorer** — define scoring rubric, pick data sources, prototype

---

## Tech Stack

| Layer | Tech |
|-------|------|
| Backend | Python 3, FastAPI, uvicorn |
| Frontend | Angular 21, TypeScript |
| Android driver | uiautomator2, ADB |
| Media download | yt-dlp |
| Telegram bot | python-telegram-bot (Bot API) |
| Telegram collector | Telethon (MTProto / user client) |
| Translation | OpenRouter (anthropic/claude-haiku-4-5 default; any model id works) |
| Queue | SQLite (via queue_store.py) |
| Config | python-dotenv (.env at repo root) |
