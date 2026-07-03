# Facebook module

**Status:** placeholder. Nothing implemented — no posting, no downloading.

## What goes here

`poster.py` — the future Facebook *publisher*. It stubs the same signature as
`modules/instagram/upload_post.py::upload_post(...)` so it can drop into the
same call sites once built. If Facebook media downloading is ever needed, add
a matching URL regex + branch to `shared/reel_downloader.py` (yt-dlp already
supports many Facebook URLs).
