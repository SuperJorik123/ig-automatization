# Video-first Instagram captions

**Date:** 2026-09-14
**Status:** approved, implementing

## The bug

A clip of a boxer published with a caption about Floyd Mayweather, who is not
the man in the video. The failure is structural, not a bad model day.

`modules/instagram/caption.py` takes the **headline** and nothing else. The
`:online` suffix on an OpenRouter model id is a search plugin that runs
**before** the model sees the prompt and builds its query from that prompt — so
the search fires off the headline, returns the most prominent story matching
those words, and the model writes the caption it was handed. Nothing in the
pipeline has ever looked at the media.

The second symptom is the same cause. The prompt says *"if the search returns
little, write ONE short paragraph and stop"*, so a story the search misses
comes back as the headline plus one paragraph restating the headline:

```
Bear beside elderly man goes unnoticced as he walks past

an eledrly man has walked past a bear standing besides him, fotage shows
apparently without noticing
```

What the post should have carried:

```
Elderly man doesn't notice a bear walking right beside him

An elderly man was walking down the street when a bear appeared just a few
feet away from him.

People nearby began shouting and warning him to turn around, but he initially
didn't hear them. Eventually, he realized what was happening, turned around
and spotted the bear.

He then quickly moved away from the animal as people continued warning him.

#bear #usa #wildlife #caughtoncamera #viralvideo #news
```

Note what that requires: the *sequence* of events, and the **audio** — nobody
can see that bystanders were shouting.

## The inversion

One call cannot fix this. Attaching the video to the existing `:online` call
leaves the search query built from the headline, which is the bug. The order
has to become two calls, the second one's query derived from the first one's
output:

```
media + headline  →  [vision call]  →  what the footage actually shows
                                        ↓
        that description + headline  →  [:online call]  →  final caption
```

## Decisions

| Question | Decision |
| --- | --- |
| Register | The model picks per post: narrate the footage when the clip **is** the story, keep the Reuters/AP register when it is a reported event the search corroborates. |
| Video input | The whole clip to a natively multimodal model, audio included. |
| Photo posts | Same inversion — the hero image goes to the same model as an image part. |
| Search role | Corroborate and add specifics only. It may never contradict the footage, and finding nothing costs the post nothing. |
| Operator caption | `caption:` reply, used as the **source** text and still rewritten per brand. |
| Preview | None. Generation stays at publish time, as today. |
| Headline | The vision pass also flags a headline the footage contradicts, early enough to re-type it before the banner is burned in. |
| Beyond IG | Nothing. Telegram, YouTube and X keep posting the headline. |

### Why the clip is base64'd and not hosted

OpenRouter takes video as a `video_url` content part, but for Gemini a plain
https URL works **only for YouTube links** (AI Studio); Vertex requires a
base64 data URL. A dummy YouTube channel was considered and rejected: Gemini
reads **public** videos only — not unlisted, not private — so every clip would
sit publicly on a channel before publication, which is the exposure that got
three YouTube accounts terminated in 2026-08 (see `YT_UPLOADS_ENABLED`). It
also costs 1600 quota units per upload (~6/day), inserts minutes of YouTube
processing into the render flow, and still uploads the whole file.

`shared/public_media.py` already serves files at a public https URL from the
VPS. It is useless for Gemini, but if a video-capable model on OpenRouter ever
accepts arbitrary https URLs the analysis copy is the same file — a one-line
swap.

## Components

### `shared/vision.py` — new

Media in, a plain description out. No Instagram knowledge in it, so the
headline check can use the same output.

**`describe(path, headline="", model=None) -> dict`**

Returns `{}` on any failure. Otherwise:

```python
{
  "summary":  "One line: what happens in this clip.",
  "beats":    ["...", "..."],   # the sequence, in order
  "audible":  "speech, shouting, commentary — '' when there is none",
  "setting":  "where it appears to be, '' when not inferable",
  "headline_ok":   True,
  "headline_note": "",          # why it doesn't match, when it doesn't
  "headline_suggestion": "",    # a better headline, when it doesn't
}
```

**ffmpeg pre-pass (video only).** `_analysis_copy(path) -> (path, cleanup)`
writes a small copy next to the source: scaled to fit 480px on the short side,
CRF-capped low bitrate, mono 64 kbps AAC audio **kept** (the shouting matters),
trimmed to the first `IG_VISION_MAX_S` seconds. A typical clip lands at 2-4 MB
against a 40 MB render. If the copy still exceeds `IG_VISION_MAX_MB` the pass
retries once at 360px and a harder CRF; still over, `describe` gives up and
returns `{}`.

Photos skip ffmpeg entirely and go as an `image_url` data part.

**The call.** One OpenRouter chat completion against `IG_VISION_MODEL`
(default `google/gemini-2.5-flash`), the media as a `video_url` / `image_url`
data URL plus a system prompt demanding JSON only. The response is parsed
defensively — fenced JSON unwrapped, missing keys defaulted, wrong types
coerced or dropped.

**Never raises.** Missing ffmpeg, missing API key, dead gateway, junk JSON,
an oversized file — all log and return `{}`, and every caller behaves exactly
as it does today with no footage.

### `modules/instagram/caption.py`

`expand(headline, footage=None, model=None)`. `footage` is the dict from
`vision.describe`; `None` or `{}` keeps today's behaviour byte for byte.

The footage-present system prompt changes three rules:

1. **Footage is ground truth.** The search may add a place, a date, a name, an
   official response — only where it clearly matches what the media shows.
   Anything contradicting the footage is dropped. Finding nothing is a valid
   outcome and costs the post nothing: the footage alone is enough material.
2. **Two registers, model picks.** Search corroborates a reported event →
   the existing wire-service prose. The clip is the story → narrate it: what
   happens, what the people around do, how it ends. The bear caption above
   goes into the prompt verbatim as the model of that mode.
3. **Never restate the headline.** The first paragraph must advance the story.
   This is what produced the thin duplicate, and with footage in hand there is
   always material. Hashtags may include `#caughtoncamera` / `#viralvideo` on
   a footage-led post; the relevance-first rule is unchanged.

`plan`, `rephrase`, `pick_hashtags`, `with_brand_tag`, `seed_for` and the whole
per-account layer are untouched — they operate on the shared caption text and
do not care how it was produced.

### `modules/telegram/news_bot.py`

**Headline check.** Tapping *Brand it* / *Create post* (`verb in ("brand",
"card")`) starts a background task: fetch the media locally, then
`vision.describe`. The result is memoised on `state["footage"]`, the task
handle on `state["vision_task"]`. When it lands, the brand-picker message is
re-rendered through `_brand_prompt_text`, which grows:

```
👁 An elderly man walks along a street as a bear crosses behind him; bystanders shout.
⚠️ headline may not match — suggested: "Elderly man doesn't notice a bear walking right beside him"
```

The warning lines appear only when `headline_ok` is false. `_do_render` and
`_do_render_card` await the task before rendering (it is normally finished);
a failed or timed-out task leaves `state["footage"]` empty and changes nothing.

Downloading at the gate tap rather than at render is the one behavioural
change: the file was going to be fetched anyway, and the size check that
already runs on that tap means nothing new can fail there.

**`caption:` replies.** In `on_message`, a reply to an open picker whose text
starts with `caption:` (case-insensitive, optional space) sets
`state["caption"]` instead of `state["text"]`. A plain reply still replaces the
headline — today's behaviour is unchanged. The picker shows the stored caption
under a `✍️` marker.

**`_ig_captions(source_text, pairs, footage=None, manual="")`.** With `manual`
set, it is used as the shared caption verbatim and **no vision or search call
is made at all**; the per-brand `plan` / `rephrase` / `pick_hashtags` layer runs
exactly as it does for a generated caption, so each account still gets its own
voice, angle, hashtags and brand tag. Otherwise `footage` is passed into
`expand`.

### `shared/config.py`

```
IG_VISION_MODEL    default google/gemini-2.5-flash
IG_VISION_ENABLED  default 1          — kill switch, off = today's behaviour
IG_VISION_MAX_S    default 120        — seconds of clip analysed
IG_VISION_MAX_MB   default 18         — give-up ceiling for the analysis copy
```

## Data flow

```
operator posts a clip + headline
        │
        ├─ taps "Brand it" ──► background: fetch file ──► vision.describe ──► state["footage"]
        │                                                        │
        │                             brand picker updates with 👁 / ⚠️ + suggestion
        │                                                        │
        │        (optional) operator replies with a new headline, or "caption: ..."
        │
        ├─ taps Render ──► await vision task ──► branding/photo_card renders per brand
        │
        └─ taps Publish ──► _ig_captions
                               ├─ manual caption present? ──► use verbatim as the source
                               └─ else expand(headline, footage) ──► :online search on the
                                                                     footage description
                               └─ per brand: rephrase (voice + angle + lang), pick_hashtags,
                                             with_brand_tag
```

## Failure modes

Every leg degrades to what ships today.

| Failure | Result |
| --- | --- |
| ffmpeg missing / pre-pass fails | `describe` → `{}`, expansion runs headline-only |
| Vision call fails or returns junk | `{}`, same |
| `IG_VISION_ENABLED=0` | No vision call, same |
| Vision task still running at render | Awaited with a timeout, then treated as `{}` |
| Expansion fails | Bare headline with its brand tag, as today |
| A single per-brand rewrite fails | That brand falls back to the shared caption, as today |

## Cost

| Leg | When | Cost |
| --- | --- | --- |
| Vision | Once per branded post at gate tap — **including posts later cancelled** | ~$0.005-0.01 |
| Search expansion | At publish, only when an IG pair is selected | ~$0.04 (unchanged) |
| Per-account rewrite | Per IG account | ~$0.0004 (unchanged) |
| Hand-written `caption:` | — | Nothing: no vision, no search |

## Testing

Offline, no network, in the existing `tests/` style.

- `tests/test_vision.py` — scripted fake client: well-formed JSON, fenced JSON,
  junk, missing keys, an API exception, `IG_VISION_ENABLED=0`, no API key. The
  ffmpeg pre-pass on a two-second clip generated by ffmpeg itself, skipped when
  ffmpeg is absent. Oversized-output give-up path with a stubbed encoder.
- `tests/test_ig_caption.py` — the footage-present and footage-absent prompt
  branches, and that `footage={}` sends the identical request the current code
  sends.
- `tests/test_news_bot_caption.py` — pure helpers only: `caption:` parsing,
  `_brand_prompt_text` with and without a footage warning, and `_ig_captions`
  with a manual caption making no model call.

## Out of scope

Autopilot, the as-is picker, the collector/dispatcher path, and any change to
what Telegram, YouTube or X publish.
