"""
modules/telegram/translator.py — translate captions per destination language
via OpenRouter (openrouter.ai — OpenAI-compatible API gateway).

Called by the publisher right before fan-out: each destination channel carries a
`lang`, and the caption is translated into that language. Results are cached per
language within a single publish (see publisher.py) so two same-language
channels don't pay twice.

Model defaults to openai/gpt-4o-mini (cheap, fast, accurate for news
translation). Override with TRANSLATE_MODEL in .env — any OpenRouter model id
works.
"""

import logging

from openai import OpenAI, APIError

from shared import config

log = logging.getLogger(__name__)

_client = (
    OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=config.OPENROUTER_API_KEY,
    )
    if config.OPENROUTER_API_KEY
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
      - the API key is missing, or the call fails.
    Never raises — a translation hiccup must not block publishing the post.
    """
    text = text or ""
    if not text.strip() or not target_lang:
        return text
    if source_lang and target_lang.strip().lower() == source_lang.strip().lower():
        return text
    if _client is None:
        log.warning("OPENROUTER_API_KEY not set — posting %s untranslated", target_lang)
        return text

    try:
        resp = _client.chat.completions.create(
            model=config.TRANSLATE_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM.format(language=target_lang)},
                {"role": "user", "content": text},
            ],
        )
    except APIError as exc:
        log.error("translation to %s failed: %s — posting untranslated", target_lang, exc)
        return text

    out = (resp.choices[0].message.content or "").strip()
    return out or text
