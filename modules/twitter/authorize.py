"""
modules/twitter/authorize.py — one-time login that mints the ACCESS token pair.

The 2026 console (console.x.com) hands out Consumer Key / Secret Key / Bearer
Token / Client ID / Client Secret — but NOT the OAuth 1.0a Access Token +
Secret the poster signs its requests with. This script runs the PIN ("oob")
flow to create them, once per account:

    py modules/twitter/authorize.py --account mirnews

It reads TWITTER_<ACCOUNT>_CONSUMER_KEY / _SECRET_KEY from .env, prints an
authorization URL, you open it IN A BROWSER LOGGED IN AS THAT ACCOUNT and
approve, X shows a 7-digit PIN, you paste the PIN here, and the script prints
the two .env lines to add (or appends them itself with --write).

Do this AFTER the app's permissions are set to Read and Write — tokens
inherit the permissions that existed when they were minted, and a read-only
token can never post.
"""

import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shared import config  # noqa: E402,F401  (loads the repo-root .env)

import tweepy  # noqa: E402

from modules.twitter.poster import _env_prefix, _get_account_env  # noqa: E402

ENV_PATH = os.path.join(config.ROOT_DIR, ".env")


def main() -> int:
    ap = argparse.ArgumentParser(description="Mint the OAuth 1.0a access token pair for one account")
    ap.add_argument("--account", required=True,
                    help="account name keying the TWITTER_<ACCOUNT>_* env vars")
    ap.add_argument("--write", action="store_true",
                    help="append the two lines to the repo-root .env instead of just printing them")
    args = ap.parse_args()

    consumer_key = _get_account_env(args.account, "CONSUMER_KEY")
    secret_key = _get_account_env(args.account, "SECRET_KEY", "CONSUMER_SECRET")

    auth = tweepy.OAuth1UserHandler(consumer_key, secret_key, callback="oob")
    url = auth.get_authorization_url()
    print(f"\n1. Open this URL in a browser logged in as @{args.account}:\n\n   {url}\n")
    print("2. Click Authorize; X shows a 7-digit PIN.")
    pin = input("3. Enter the PIN here: ").strip()

    access_token, access_secret = auth.get_access_token(pin)
    prefix = _env_prefix(args.account)
    lines = (f"{prefix}ACCESS_TOKEN={access_token}\n"
             f"{prefix}ACCESS_SECRET={access_secret}\n")

    if args.write:
        with open(ENV_PATH, "a", encoding="utf-8") as fh:
            fh.write("\n" + lines)
        print(f"\nAppended to {ENV_PATH}. Restart any running bot to pick it up.")
    else:
        print(f"\nAdd these two lines to {ENV_PATH}:\n\n{lines}")

    print("Verify with:")
    print(f"   py modules/twitter/poster.py some.jpg --caption test --account {args.account}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
