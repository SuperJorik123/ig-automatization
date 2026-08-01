# Branded clips — per-brand logo + headline overlay

**Date:** 2026-07-31
**Status:** implemented

## Goal

Send one clean HD clip + a headline to the news bot and get back one branded
variant per brand: the brand's logo burned in top-right, the headline burned in
as a lower-third banner — same font, same size, same position every time. The
files come back into the control group, then a second picker decides which of
them actually get published (Telegram channel, YouTube channel, Twitter
account) through the existing publishers. Nothing publishes without an
explicit selection.

## Flow

Stage 1 — brand & render (in the control group):

1. Operator sends a video; the caption is the headline. No caption → the bot
   replies asking for the headline (a plain-text reply to the bot's message
   supplies it, same reply mechanic the manual picker already uses).
2. Because the manual broadcaster also triggers on any video, the bot's first
   inline message is a two-button gate: **Post as-is** (existing manual flow,
   unchanged) / **Brand it** (this flow).
3. **Brand it** → brand picker: one toggle per configured brand, all on by
   default, plus **Render**.
4. On Render, per selected brand: translate the headline to the brand's
   language (`translator.translate`, cached per lang like the publishers do),
   render the branded variant, send it back into the group labeled with the
   brand name. The clean original stays on disk until the flow ends.

Stage 2 — publish:

5. After the last variant is delivered the bot posts the publish picker: one
   toggle per brand-destination pair (`mirnews → TG`, `mirnews → YT`,
   `mirnews → X`, `rusnews → TG`, …). Only pairs for rendered brands appear;
   YT pairs are hidden when the clip exceeds the 180 s Shorts cap
   (`shorts_format.MAX_SHORT_S`). All pairs default **off** — publishing is
   opt-in per pair.
6. **Publish** → each selected pair uploads that brand's *branded* file:
   - TG: send the video to the brand's channel, caption = translated headline,
     then the same post-publish path as the manual picker (BulkFollows
     per-post order + durable every-5th-post channel counter via
     `reactions.record_posts`).
   - YT: `modules/youtube/uploader.upload_short` with the brand's account
     (the file is already vertical 1080x1920 — no second `ensure_short`
     render). Title/description split like `youtube/publisher`.
   - X: `modules/twitter/poster.post_media` with the brand's account.
7. Results reported in one summary reply; temp files deleted; **Cancel** at
   any stage deletes them too. Files live in `tg_data/media/` with a
   `brand_*` prefix so the existing startup sweep collects orphans.

## Brands

One logo per brand; a brand fans out to its platform accounts.

```
brands/<name>/logo.png          transparent PNG, any size (renderer scales it)
```

`.env`, same conventions as the existing account vars (name uppercased,
non-alphanumerics → `_`):

```
BRANDS=mirnews:en,rusnews:ru          # name:lang, comma-separated
BRAND_MIRNEWS_TG=-1001234567890       # Telegram channel chat id   (optional)
BRAND_MIRNEWS_YT=mirnews              # YouTube account name       (optional)
BRAND_MIRNEWS_TW=mirnews              # Twitter account name       (optional)
```

`shared/config.py` parses this into `BRANDS = [{name, lang, tg, yt, tw}]`.
A brand with a missing platform var simply has no pair for that platform in
the stage-2 picker. A brand whose `brands/<name>/logo.png` is missing is shown
disabled in the brand picker with the reason. Instagram is out of scope — its
pipeline is phone-driven and takes photos, not branded shorts.

## Renderer — `shared/branding.py`

Pure ffmpeg/ffprobe, no Telegram, no network. Sits in `shared/` because it is
platform-agnostic (the same file is posted everywhere).

```
render_branded(video_path, headline, logo_path, out_path) -> out_path
```

One ffmpeg pass per brand:

1. **Canvas** — reuse `shorts_format.probe` (rotation-aware). Vertical input
   scales to 1080x1920; square/horizontal gets the existing blur-fill
   treatment (same filter chain as `ensure_short`). Every variant is
   Shorts-safe by construction.
2. **Logo** — `overlay` top-right, fixed geometry: logo scaled to 180 px wide
   (aspect kept), 40 px margin from the top and right edges.
3. **Headline** — `drawtext` lower third: bold white text over a
   semi-transparent black box (`box=1, boxcolor=black@0.55`, generous
   `boxborderw` padding), fontsize 64, text block anchored at y ≈ 72 % of
   frame height. Font ships in the repo at `assets/fonts/` (one bold weight
   with full Latin + Cyrillic coverage, e.g. Montserrat Bold) so "always the
   same font" holds on any machine — no system-font lookup.
4. **Wrapping** — `drawtext` gets the text via `textfile=` (a temp file),
   which both renders embedded newlines and sidesteps ffmpeg filter-escaping
   of quotes/colons in real headlines. Python pre-wraps with `textwrap` to
   ≤ 3 lines (~24 chars/line at this size); anything longer is truncated with
   an ellipsis rather than shrinking the font — the design stays fixed.
5. **Encode** — same profile as `shorts_format`: libx264 veryfast, crf 21,
   yuv420p, aac 128k, `+faststart`. Output `<stem>_<brand>.mp4` next to the
   source. Failure removes the partial file and raises with ffmpeg's stderr
   tail.

Renders run sequentially per brand via `asyncio.to_thread` so the bot's event
loop never blocks. Four brands on a sub-minute clip is seconds of work.

## news_bot integration

`news_bot.py` gains one flow, reusing the existing pending-state pattern:

- The video handler branches into the as-is/brand gate before the old picker.
- Brand-flow state (mode, headline, selected brands, rendered paths, selected
  pairs) lives in the same in-memory pending dict as the manual picker, keyed
  by the prompt message. Callback data is namespaced (`brand:...`) so the two
  keyboards can't collide.
- A render or publish failure for one brand/pair is caught, reported in the
  summary, and never blocks the others — same "one leg must not kill the
  post" rule the manual flow already follows.
- Manual branded posts are not queue items, so `queue_store` is untouched;
  the TG leg still goes through `reactions.record_posts` (which handles its
  own durable counter) so branded posts count toward the every-5th-post
  channel order like any other publish.

## Module map

```
NEW  shared/branding.py            the renderer (probe, wrap, filter graph, encode)
NEW  brands/<name>/logo.png        per-brand logos (checked in)
NEW  assets/fonts/<font>.ttf       the one headline font (checked in)
MOD  shared/config.py              + BRANDS parsing
MOD  modules/telegram/news_bot.py  + as-is/brand gate, brand picker, render step,
                                     publish picker, summary + cleanup
NEW  tests/test_branding.py        offline: wrap logic, filter-graph strings,
                                     config parsing; a real-render test that
                                     skips when ffmpeg is absent
```

## Error handling

- Missing logo file → brand disabled in the picker, not a crash.
- ffmpeg/ffprobe missing or failing → that brand's render reported failed,
  others proceed; stderr tail in the reply.
- Publish failure (TG/YT/TW) → reported per pair in the summary, others
  proceed.
- Restart mid-flow → pending state is in-memory (accepted, same as the manual
  picker today); orphaned `brand_*` files are removed by the startup sweep.
- Headline text is never interpolated into the ffmpeg filter string
  (`textfile=` only).

## Testing

`tests/test_branding.py`, offline like the rest of the suite: headline
wrapping/truncation boundaries, filter-graph construction for vertical vs
horizontal input, `BRANDS` env parsing including missing-platform and
missing-logo cases. One integration test renders a generated 1-second clip
end-to-end and asserts the output geometry via `probe`; it is skipped when
ffmpeg is not on PATH.
