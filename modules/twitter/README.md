# Twitter / X module

**Status:** placeholder. No posting implemented yet.

## What already exists (elsewhere)

Downloading media *from* Twitter/X is already supported — see
`shared/reel_downloader.py` (`TWITTER_URL_RE`, `download_media`). The Telegram
bot uses it to pull a tweet's video and repost it to Instagram. That code stays
shared because it's a *source*, not a Twitter posting target.

## What goes here

`poster.py` — the future Twitter/X *publisher*. It stubs the same signature as
`modules/instagram/upload_post.py::upload_post(...)` so it can drop into the
same call sites (server.py, the Telegram bot) once built.
