# brands/

One folder per brand, named exactly as its credentials file
(`credentials/brands/<name>.json`):

    brands/<name>/logo.png    transparent PNG, any size (the renderer scales
                              it to 180 px wide)
    brands/<name>/style.json  optional headline colors / font (see below)

A brand without a logo.png shows up disabled in the news bot's brand picker.

## style.json

Every key is optional; a missing file (or key) keeps the default look —
white text on a black@0.55 box in the repo-shipped bold font.

    {
      "background": "#C90A0A",   // headline banner box color
      "background_alpha": 1.0,   // 0..1; defaults to 1.0 once background is set
      "text": "#ffffff",         // headline color
      "font": "Verdana",         // absolute path, a file in this folder, or a
                                 // system font name/filename (arialbd.ttf)
      "font_size": 41            // px; wrapping width scales with it
    }

`background_alpha` only defaults to 0.55 for the built-in black box — an
explicitly configured color is drawn opaque unless you set the key yourself.
A malformed color, an out-of-range size or an unresolvable font raises at
render time rather than silently falling back, so typos surface in the news
bot's error reply.
## Wiring the accounts

The brand's platform accounts live in `credentials/brands/<name>.json` (one
file per brand, git-ignored). Copy `credentials/brands/_example.json`, name it
after the folder here, and keep only the platforms the brand actually has:

    credentials/brands/mirnews.json
    {
      "lang": "en",
      "group": "GMN",                          // picker family, see below
      "telegram":  {"channel": "@mir_news"},   // bot must be admin
      "youtube":   {"channel": "mirnews"},     // folder under credentials/youtube/
      "twitter":   {"consumer_key": "...", "secret_key": "...",
                    "bearer_token": "...", "access_token": "...",
                    "access_secret": "..."},
      "instagram": {"account": "mir.news", "user_id": "...",
                    "access_token": "IGAA...", "token_refreshed": "2026-09-04"}
    }

`"account"` is only needed when the platform handle differs from the brand
name. Check what loaded with `py shared/credentials.py --check`. Shared
services (OpenRouter, BulkFollows, SMTP, bot tokens) stay in `.env`; a brand
with no accounts renders and previews but publishes nowhere.

## Groups

`"group"` is the account family the brand belongs to — `GMN` or `JNN` today.
Both of the news bot's pickers OPEN COLLAPSED to one row per group with the
full list behind `⚙ Custom…`, so the common case ("this one goes to all of
GMN") is one tap instead of thirteen. Tapping a group fills it in; tapping a
full one clears it; `⚙ Custom…` expands to the per-account list showing
exactly what the group tick did, and anything you change by hand there
survives. Groups are listed alphabetically.

The as-is picker lists CHANNELS, not brands, so a destination is grouped by
whichever brand owns that handle: `@WsWire` in `TG_DESTINATIONS` is
`wswire.json`'s `telegram.channel`, so it rides with JNN. A channel no brand
claims stays reachable through `⚙ Custom…` and `All`. A brand with no
`"group"` is Custom-only, and with no group configured anywhere neither
picker collapses at all.

New group? Just put its name in the brand files — nothing else knows the
names (`modules/telegram/groups.py`).
