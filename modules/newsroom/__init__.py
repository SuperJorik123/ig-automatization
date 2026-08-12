"""
modules/newsroom — the client's WordPress -> Telegram news bot.

Lives on the client/wp-newsbot branch and is a separate product from the rest
of this tree: it shares an ancestor, not a runtime. Nothing here imports
modules/instagram, modules/youtube, modules/twitter or modules/telegram — the
pieces it needed from modules/telegram (the BulkFollows client, the order
wrappers, the send helpers, the OpenRouter client) were copied and adapted for
per-site configuration rather than imported, so a change on master can never
alter what a client channel posts.

Flow, per configured site:

    WordPress REST  ->  rewrite to a Telegram post  ->  publish to that site's
    channel  ->  BulkFollows orders (per-post views, per-channel bonus every
    5th post, delayed random reactions)

Entrypoint: modules/newsroom/main.py. Config: shared/config.py's NR_* block
plus one JSON file per site under modules/newsroom/sites/.
"""
