"""
modules/telegram/translator.py — translate captions per destination language
via the Anthropic API (Claude).

Called by the publisher right before fan-out: each destination channel carries a
`lang`, and the caption is translated into that language. Results are cached per
language within a single publish (see publisher.py) so two same-language
channels don't pay twice.

Model defaults to claude-opus-4-8 (Anthropic's recommended default); override
with TRANSLATE_MODEL in .env — claude-haiku-4-5 is a cheaper/faster choice for
this simple task. No `temperature`/`thinking` params: they're rejected on
Opus 4.8, and omitting `thinking` keeps the call fast.
"""

import logging

import anthropic

from shared import config

log = logging.getLogger(__name__)

# One client per process. Reads the key from config (loaded from the repo .env).
_client = (
    anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    if config.ANTHROPIC_API_KEY
    else None
)

_SYSTEM = (
    "You are a professional translator for a news channel. Translate the user's "
    "message into {language}. Preserve the meaning, tone, and news register. Keep "
    "#hashtags, @mentions, URLs, emoji, and line breaks intact. Do not add "
    "commentary, notes, or quotation marks — output ONLY the translated text. If "
    "the text is already in {language}, return it unchanged."
)


def translate(text: str, target_lang: str, source_lang: str | None = None) -> str:
    """Translate `text` into `target_lang` (e.g. "ru", "es", "German").

    Returns `text` unchanged when translation isn't needed or possible:
      - empty text, or no target language,
      - target language equals the known source language,
      - the API key is missing, or the call fails / is refused.
    Never raises — a translation hiccup must not block publishing the post.
    """
    text = text or ""
    if not text.strip() or not target_lang:
        return text
    if source_lang and target_lang.strip().lower() == source_lang.strip().lower():
        return text
    if _client is None:
        log.warning("ANTHROPIC_API_KEY not set — posting %s untranslated", target_lang)
        return text

    try:
        resp = _client.messages.create(
            model=config.TRANSLATE_MODEL,
            max_tokens=4096,
            system=_SYSTEM.format(language=target_lang),
            messages=[{"role": "user", "content": text}],
        )
    except anthropic.APIError as exc:
        log.error("translation to %s failed: %s — posting untranslated", target_lang, exc)
        return text

    if resp.stop_reason == "refusal":
        log.warning("translation to %s refused by the model — posting untranslated", target_lang)
        return text

    out = "".join(b.text for b in resp.content if b.type == "text").strip()
    return out or text
