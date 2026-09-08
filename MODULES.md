# Modules

## 1. Video Branding

You send a video, it comes back branded — a full processing pipeline: download, normalization, ffmpeg compositing, per-language rendering, upload.

- Send a link from Instagram or X, or upload a video directly. Links are resolved through an extractor-based downloader that pulls the file straight from the platform's CDN, with authenticated session handling for login-gated posts; direct uploads above Telegram's 20 MB API ceiling go through a separate large-file transport (MTProto)
- The video is normalized before branding: streams are probed for codec, resolution and rotation metadata, then scaled onto a 1080×1920 canvas over a blur-fill background generated from the video itself
- You add the headline; you pick the channels it's for
- Your logo and the headline are burned in through a single-pass ffmpeg filter graph — the headline is wrapped by measured pixel width per glyph (font metrics, not character counts) so lines never overflow the banner, and characters the font can't render are filtered out instead of showing up as boxes
- The headline is translated into each channel's language and the video re-rendered once per language, with translations cached so repeated languages don't re-bill
- Each variant is fully re-encoded with settings tuned per destination platform
- You either download the result or send it straight out — uploads go through each platform's own API client, chunked/resumable where the platform requires it

## 2. Post Design Engine

Your post style becomes a template the engine fills in — every measurement of the wsmirror frame reproduced programmatically in a raster pipeline.

- Send the photos and the headline
- The layout is picked from how many photos there are — or you choose. All geometry lives as measured constants taken off your reference designs
- The main photo is cover-fit cropped with focal-point steering so faces land where the frame expects them, not wherever the crop happens to fall
- Extra photos become the circular insets: anti-aliased masks, ring stroke, drop shadow — and the main subject is re-extracted on top of them by an ML background-removal model, with the alpha matte eroded and feathered so no halo shows around the cut
- The headline is uppercased, centered and auto-fitted: the font size steps down until the text fits the line limit — words are never dropped. Fonts and colors come from your brand's style config
- The finished image comes back for review or goes straight into publishing

Below is an example of the prototype's rendered output:

![Post creation example](postcreationdemo.jpg)

## 3. Social Media Publishing

TikTok, YouTube Shorts and X in one place — which in practice means three different publishing mechanisms, each with its own auth model, upload path and failure modes, unified behind one flow.

- You mark the finished video or image for the platforms you want
- Each platform gets its own formatting pass: captions and hashtags for TikTok; title/description split with length limits for YouTube; X's 280-character budget enforced with URL cost accounting (every link counts as 23 characters, regardless of its length)
- **TikTok is published through browser automation, not the API.** TikTok's posting API is gated behind an app review that can take weeks and be refused outright, so the upload is driven through the real creator interface with Selenium: a headless Chrome session logged in as your account, cookies persisted between runs so the login survives restarts, the file handed to the upload input directly, then the caption typed and the post confirmed. The page is waited on by element state rather than fixed sleeps, and every step is screenshotted so a failed post shows exactly where it stopped
- **YouTube and X publish over their official APIs** — resumable chunked sessions for video, with async-processing status polling where the API requires it, under per-account OAuth credentials
- Videos meant for Shorts are gated first: the file is probed (rotation-aware), and non-vertical sources are automatically re-rendered to 1080×1920 over a blurred backdrop so YouTube actually classifies them as Shorts instead of regular videos
- If a platform rejects an upload, it retries automatically with backoff; you can also re-trigger manually
- You get confirmation with the live links — read from the API response for YouTube and X, and off the finished upload screen for TikTok

## 4. Article Writer + WordPress

A post appears on Instagram, the article writes and publishes itself, and your Telegram channels get it automatically — an event-driven chain from source monitoring through LLM writing to WordPress publishing.

- The bot watches the Instagram pages you choose: a polling watcher with duplicate detection, built to survive Instagram changing its feed structure without notice
- Each new post is pulled apart — caption and media extracted into a structured story record
- The story is researched before writing: an LLM retrieval stage cross-references the post against other sources to gather the full details, not just what the caption says
- The article is written against a style profile derived from your existing published articles
- Publishing goes through the WordPress REST API: featured image uploaded through the media endpoint, category and tags assigned, post created live
- Your existing bot picks up the new article and posts it to the Telegram channels, exactly as it does now — that integration is not touched
- You get the published link — from Instagram post to live article to Telegram, without touching anything

## 5. Auto Subtitles

Spoken audio becomes burned-in captions, per channel, per language — a speech-recognition stage inserted ahead of the branding pass.

- The video's audio is transcribed by a speech-recognition model with word-level timestamps, so captions sync to the speech, not to guesses
- Captions are segmented and styled to your caption spec, and translated per channel through the same cached translation layer the branding uses
- Subtitles are burned in as their own render pass, sequenced before the logo and headline so branding lands on the subtitled video

## 6. Control Panel

One screen over the whole publishing machinery — needed once two or more publishing modules share the same queue and retry logic.

- Open the panel and see every post from the last days, one database record per post, per platform, per account, with status, timestamps and the actual error payloads
- Each row shows where it published and where it failed
- Failed uploads re-enter the queue on their own with attempt counting and backoff; every row also has a manual retry
- Server and error status visible in the same place: service liveness, last error, queue depth

## Support & Monitoring

The system watches itself, and problems get fixed before you see them — monitoring infrastructure plus ongoing maintenance of every external integration.

- Server, uptime, CPU and disk space are checked around the clock; on top of that, each worker process sends dead-man heartbeats from inside its own loop — so a process that's running but stuck still trips the alarm
- Any error or failed upload triggers an alert by email straight away: a logging-level hook mails every error and traceback the moment it happens
- Platform changes — TikTok, YouTube, X, Instagram or WordPress breaking their APIs — are fixed as part of the service. These platforms ship breaking changes without notice; extractor updates, auth-flow changes and endpoint migrations are handled as they land
- Backups run automatically, security updates applied
- You get a monthly report on what ran, what failed and what was fixed, generated from the post ledger and monitor state
- Response within 24h on working days
