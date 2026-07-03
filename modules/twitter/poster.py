"""
modules/twitter/poster.py — Twitter/X posting (NOT IMPLEMENTED).

Placeholder that mirrors modules.instagram.upload_post.upload_post so a future
implementation can slot into the same call sites. Note: pulling media *from*
Twitter/X already works today via shared.reel_downloader (TWITTER_URL_RE); this
module is only about *publishing* to Twitter/X.
"""


def upload_post(d, media_path, caption_body, hashtags, kind="post", target_account=None):
    """Publish `media_path` to Twitter/X. Not implemented yet — see the
    Instagram poster (modules/instagram/upload_post.py) for the reference flow."""
    raise NotImplementedError("Twitter/X posting is not implemented yet.")
