"""
shared/credentials.py — one JSON file per brand, holding everything that brand
owns.

`credentials/brands/<name>.json` (git-ignored) replaces the per-brand sprawl in
.env: adding a brand used to mean a `BRANDS` entry + four `BRAND_<NAME>_*`
lines + five `TWITTER_<NAME>_*` + three `IG_GRAPH_<NAME>_*`, spread over the
file. Now it is one file named after the brand, plus `brands/<name>/logo.png`:

    credentials/brands/wswire.json
    {
      "lang": "en",
      "group": "JNN",
      "telegram":  {"channel": "@wswire"},
      "youtube":   {"channel": "wswire"},
      "twitter":   {"consumer_key": "...", "secret_key": "...",
                    "bearer_token": "...", "access_token": "...",
                    "access_secret": "..."},
      "instagram": {"user_id": "...", "access_token": "...",
                    "token_refreshed": "2026-09-03"}
    }

"group" is the account family the news bot's pickers offer as one row (see
modules/telegram/groups.py); a brand without one is only reachable through
the picker's "Custom" list.

The FILENAME is the brand name — the same name as `brands/<name>/logo.png`, so
a brand is two paths and nothing else. Deleting a brand is deleting its file;
handing one brand's credentials to someone is sending one file, with no way to
leak a sibling's keys by accident.

SHARED services stay in .env — OpenRouter, BulkFollows, SMTP, the bot tokens,
the autopilot knobs are common to every brand and were never per-account.
These files are only ever about accounts a brand owns.

They are EXPANDED INTO os.environ by shared/config.py at import, so every
existing reader keeps working untouched: modules/twitter/poster.py and
modules/instagram/graph.py still do os.getenv("TWITTER_<ACCT>_..."), and the
BRANDS parser still reads BRANDS/BRAND_<NAME>_*. The JSON is a nicer front
door onto the same variables, not a second code path — which is also why it
is opt-in: no directory, nothing changes.

JSON wins over .env for the keys it defines (it is the source of truth for the
accounts it lists); BRANDS and IG_GRAPH_ACCOUNTS are MERGED, so a brand still
configured the old way in .env keeps working alongside a migrated one.

Brands load in FILENAME order (alphabetical), which is the order they appear
in the brand picker.

Unknown keys are an error, not a shrug: a "twiter" typo that silently drops
five credentials is exactly the failure these files exist to prevent.

CLI:
    py shared/credentials.py --check                 # what would be loaded
    py shared/credentials.py --import-env [--force]  # build them from .env
"""

import glob
import json
import os
import sys

# shared/credentials.py -> shared/ -> <repo root>. Computed here rather than
# imported from config.py, which imports THIS module.
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCOUNTS_DIR = os.path.join(ROOT_DIR, "credentials", "brands")

# json field -> env var suffix, for the two platforms whose secrets live here.
TWITTER_FIELDS = {
    "consumer_key": "CONSUMER_KEY",
    "secret_key": "SECRET_KEY",
    "bearer_token": "BEARER_TOKEN",
    "access_token": "ACCESS_TOKEN",
    "access_secret": "ACCESS_SECRET",
}
INSTAGRAM_FIELDS = {
    "access_token": "ACCESS_TOKEN",
    "user_id": "USER_ID",
    "token_refreshed": "TOKEN_REFRESHED",
}
# Facebook Pages. No token_refreshed twin: a Page token derived from a system
# user does not expire, so there is nothing for a daily refresh job to do.
FACEBOOK_FIELDS = {
    "access_token": "ACCESS_TOKEN",
    "page_id": "PAGE_ID",
}

_BRAND_KEYS = {"lang", "group", "notes", "telegram", "youtube", "twitter",
               "instagram", "facebook", "$schema", "_comment"}
_TELEGRAM_KEYS = {"channel"}
_YOUTUBE_KEYS = {"channel"}
_TWITTER_KEYS = set(TWITTER_FIELDS) | {"account"}
_INSTAGRAM_KEYS = set(INSTAGRAM_FIELDS) | {"account"}
_FACEBOOK_KEYS = set(FACEBOOK_FIELDS) | {"account"}

# Masked in --check output; everything else is a handle, not a secret.
_SECRET_FIELDS = {"consumer_key", "secret_key", "bearer_token",
                  "access_token", "access_secret"}


class CredentialsError(Exception):
    """A brand file is unreadable, malformed, or has a key we don't know."""


def env_key(name: str) -> str:
    """Account name -> env-var infix: uppercased, non-alphanumerics to "_".
    The same rule modules/twitter/poster.py and modules/instagram/graph.py
    apply, so the vars this module writes are the ones they read."""
    return "".join(c if c.isalnum() else "_" for c in name).upper()


def brand_path(name: str, accounts_dir: str | None = None) -> str:
    return os.path.join(accounts_dir or ACCOUNTS_DIR, f"{name}.json")


def _check_keys(where: str, block, allowed: set) -> None:
    if not isinstance(block, dict):
        raise CredentialsError(f"{where}: expected an object, got "
                               f"{type(block).__name__}")
    unknown = sorted(set(block) - allowed)
    if unknown:
        raise CredentialsError(
            f"{where}: unknown key(s) {', '.join(unknown)} - expected one of "
            f"{', '.join(sorted(allowed))}")


def _put(out: dict, var: str, value) -> None:
    """Only non-empty strings land in the environment: a blank field in a
    brand file must not blow away a value .env still provides."""
    if isinstance(value, str) and value.strip():
        out[var] = value.strip()


def _merge_list(existing: str, incoming: list, key=lambda s: s) -> str:
    """Comma-joined env list with the JSON's entries authoritative: an entry
    whose key already appears in .env is REPLACED in place (order kept), the
    rest are appended. Lets a half-migrated .env keep working."""
    out = [e.strip() for e in (existing or "").split(",") if e.strip()]
    for item in incoming:
        for i, have in enumerate(out):
            if key(have).lower() == key(item).lower():
                out[i] = item
                break
        else:
            out.append(item)
    return ",".join(out)


def env_from_brands(brands: dict, existing=None) -> dict:
    """Pure: {brand name: brand document} -> {ENV_VAR: value}. `existing`
    supplies the current BRANDS / IG_GRAPH_ACCOUNTS to merge against
    (os.environ in production, a plain dict in tests). No I/O."""
    existing = existing or {}
    out, specs, ig_accounts, fb_accounts = {}, [], [], []
    for name, brand in brands.items():
        where = f"{name}.json"
        _check_keys(where, brand, _BRAND_KEYS)
        key = env_key(name)
        lang = (brand.get("lang") or "").strip()
        specs.append(f"{name}:{lang}" if lang else name)

        # Which family this brand belongs to (GMN / JNN). Both of the news
        # bot's pickers collapse behind it - modules/telegram/groups.py.
        _put(out, f"BRAND_{key}_GROUP", brand.get("group"))

        telegram = brand.get("telegram") or {}
        _check_keys(f"{where}: telegram", telegram, _TELEGRAM_KEYS)
        _put(out, f"BRAND_{key}_TG", telegram.get("channel"))

        youtube = brand.get("youtube") or {}
        _check_keys(f"{where}: youtube", youtube, _YOUTUBE_KEYS)
        if youtube:
            # The "channel" here names the credentials/youtube/<account>/
            # folder holding that channel's OAuth — Google's own files, which
            # stay on disk; the JSON only points at them.
            _put(out, f"BRAND_{key}_YT", youtube.get("channel") or name)

        twitter = brand.get("twitter") or {}
        _check_keys(f"{where}: twitter", twitter, _TWITTER_KEYS)
        if twitter:
            account = (twitter.get("account") or name).strip()
            _put(out, f"BRAND_{key}_TW", account)
            prefix = f"TWITTER_{env_key(account)}_"
            for field, var in TWITTER_FIELDS.items():
                _put(out, prefix + var, twitter.get(field))

        instagram = brand.get("instagram") or {}
        _check_keys(f"{where}: instagram", instagram, _INSTAGRAM_KEYS)
        if instagram:
            account = (instagram.get("account") or name).strip()
            _put(out, f"BRAND_{key}_IG", account)
            prefix = f"IG_GRAPH_{env_key(account)}_"
            for field, var in INSTAGRAM_FIELDS.items():
                _put(out, prefix + var, instagram.get(field))
            ig_accounts.append(account)

        facebook = brand.get("facebook") or {}
        _check_keys(f"{where}: facebook", facebook, _FACEBOOK_KEYS)
        if facebook:
            account = (facebook.get("account") or name).strip()
            _put(out, f"BRAND_{key}_FB", account)
            prefix = f"FACEBOOK_{env_key(account)}_"
            for field, var in FACEBOOK_FIELDS.items():
                _put(out, prefix + var, facebook.get(field))
            fb_accounts.append(account)

    if specs:
        out["BRANDS"] = _merge_list(existing.get("BRANDS", ""), specs,
                                    key=lambda s: s.split(":")[0])
    if ig_accounts:
        out["IG_GRAPH_ACCOUNTS"] = _merge_list(
            existing.get("IG_GRAPH_ACCOUNTS", ""), ig_accounts)
    if fb_accounts:
        out["FACEBOOK_ACCOUNTS"] = _merge_list(
            existing.get("FACEBOOK_ACCOUNTS", ""), fb_accounts)
    return out


def load_brand(path: str) -> dict:
    """One brand file -> its document. Malformed RAISES — a typo'd credentials
    file must fail at startup, loudly, not ship a fan-out with that brand
    silently missing."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        raise CredentialsError(f"{path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise CredentialsError(f"{path}: expected a JSON object at the top level")
    return doc


def load(accounts_dir: str | None = None) -> dict:
    """Every credentials/brands/*.json -> {brand name: document}, in filename
    order. Missing directory = {} (the layout is opt-in)."""
    accounts_dir = accounts_dir or ACCOUNTS_DIR
    if not os.path.isdir(accounts_dir):
        return {}
    brands = {}
    for path in sorted(glob.glob(os.path.join(accounts_dir, "*.json"))):
        name = os.path.basename(path)[:-len(".json")]
        # _foo.json / foo.example.json are scratch files, not brands.
        if name.startswith((".", "_")) or name.endswith(".example"):
            continue
        brands[name] = load_brand(path)
    return brands


def apply(accounts_dir: str | None = None, env=None) -> list:
    """Expand every brand file into `env` (os.environ by default). Returns the
    brand names it loaded — [] when there is no directory."""
    env = os.environ if env is None else env
    brands = load(accounts_dir)
    if not brands:
        return []
    for var, value in env_from_brands(brands, env).items():
        env[var] = value
    return list(brands)


def _write(doc: dict, path: str) -> None:
    """Temp file + rename, so a crash mid-write can't leave half a file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def update_instagram_token(account: str, token: str, refreshed: str,
                           accounts_dir: str | None = None) -> bool:
    """Write a freshly refreshed Instagram token back into the owning brand's
    file. Returns False when no brand file claims the account — the caller then
    falls back to rewriting the .env line, so a half-migrated setup still
    refreshes."""
    accounts_dir = accounts_dir or ACCOUNTS_DIR
    for name, brand in load(accounts_dir).items():
        instagram = brand.get("instagram") or {}
        if not instagram:
            continue
        if (instagram.get("account") or name).lower() != account.lower():
            continue
        instagram["access_token"] = token
        instagram["token_refreshed"] = refreshed
        _write(brand, brand_path(name, accounts_dir))
        return True
    return False


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def _mask(value: str) -> str:
    return f"{value[:4]}...{value[-4:]} ({len(value)})" if len(value) > 12 else "set"


def _from_env(env) -> dict:
    """Rebuild {brand name: document} from the flat .env variables — the
    migration path off the old layout."""
    brands = {}
    for spec in (env.get("BRANDS") or "").split(","):
        spec = spec.strip()
        if not spec:
            continue
        parts = [p.strip() for p in spec.split(":")]
        name, key = parts[0], env_key(parts[0])
        brand = {}
        if len(parts) > 1 and parts[1]:
            brand["lang"] = parts[1]

        group = (env.get(f"BRAND_{key}_GROUP") or "").strip()
        if group:
            brand["group"] = group

        channel = (env.get(f"BRAND_{key}_TG") or "").strip()
        if channel:
            brand["telegram"] = {"channel": channel}

        yt = (env.get(f"BRAND_{key}_YT") or "").strip()
        if yt:
            brand["youtube"] = {"channel": yt}

        # BRAND_<NAME>_TW names the credential set. When it is MISSING, fall
        # back to the brand's own name: TWITTER_<BRAND>_* keys sitting in .env
        # with no BRAND_*_TW line pointing at them are real credentials the
        # picker never offered, and dropping them here would lose them for
        # good once the .env lines go. An explicit line is kept even with no
        # credentials behind it — it may point at an account configured
        # elsewhere.
        explicit = (env.get(f"BRAND_{key}_TW") or "").strip()
        tw = explicit or name
        block = {} if tw.lower() == name.lower() else {"account": tw}
        prefix = f"TWITTER_{env_key(tw)}_"
        for field, var in TWITTER_FIELDS.items():
            value = (env.get(prefix + var) or "").strip()
            if value:
                block[field] = value
        if explicit or block.keys() - {"account"}:
            brand["twitter"] = block

        explicit = (env.get(f"BRAND_{key}_IG") or "").strip()
        ig = explicit or name
        block = {} if ig.lower() == name.lower() else {"account": ig}
        prefix = f"IG_GRAPH_{env_key(ig)}_"
        for field, var in INSTAGRAM_FIELDS.items():
            value = (env.get(prefix + var) or "").strip()
            if value:
                block[field] = value
        if explicit or block.keys() - {"account"}:
            brand["instagram"] = block

        brands[name] = brand
    return brands


def _print_check(accounts_dir: str) -> int:
    brands = load(accounts_dir)
    if not brands:
        print(f"no {accounts_dir}/*.json - brands come from .env only")
        return 0
    print(f"{accounts_dir}: {len(brands)} brand file(s)")
    for name, brand in brands.items():
        group = (brand.get("group") or "").strip()
        print(f"  {name}.json ({brand.get('lang') or 'raw'}"
              f"{', ' + group if group else ''})")
        slots = []
        for platform in ("telegram", "youtube", "twitter", "instagram",
                         "facebook"):
            block = brand.get(platform) or {}
            if not block:
                continue
            bits = [f"{k}={_mask(str(v)) if k in _SECRET_FIELDS else v}"
                    for k, v in block.items()]
            slots.append(f"    {platform:<9} {', '.join(bits)}")
        print("\n".join(slots) or "    - no platforms configured")
    env = env_from_brands(brands, os.environ)
    print(f"\nexpands to {len(env)} env var(s); BRANDS={env.get('BRANDS', '')}")
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--import-env" in argv:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(ROOT_DIR, ".env"))
        brands = _from_env(os.environ)
        existing = [n for n in brands if os.path.exists(brand_path(n))]
        if existing and "--force" not in argv:
            print(f"already there: {', '.join(existing)} - pass --force to "
                  f"overwrite", file=sys.stderr)
            return 1
        for name, brand in brands.items():
            _write(brand, brand_path(name))
        print(f"wrote {len(brands)} file(s) to {ACCOUNTS_DIR}: "
              f"{', '.join(brands) or '-'}")
        print("Check them with --check, then remove the migrated "
              "BRAND_*/TWITTER_*/IG_GRAPH_* lines from .env.")
        return 0
    try:
        return _print_check(ACCOUNTS_DIR)
    except CredentialsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
