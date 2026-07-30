"""
modules/telegram/translator.py — translate captions per destination language
via OpenRouter (openrouter.ai — OpenAI-compatible API gateway).

Called by the publisher right before fan-out: each destination channel carries a
`lang`, and the caption is translated into that language. Results are cached per
language within a single publish (see publisher.py) so two same-language
channels don't pay twice.

Model defaults to google/gemini-2.5-flash — picked over gpt-4o-mini and gpt-4o
on a side-by-side of real wire copy: mini translates too literally (сказал for
"said", ungrammatical case) and 4o rewrites #hashtags into the target language,
which breaks tag consistency across channels. Override with TRANSLATE_MODEL in
.env — any OpenRouter model id (provider/model) works.
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
    "You are a native {language} news editor. Render the user's message in "
    "{language} for a {language}-language news channel.\n\n"
    "FAITHFUL — every fact, name, number, date, quote and attribution in the "
    "source appears in your output. Add nothing: no context, no explanation, no "
    "adjectives, no commentary. Remove nothing, soften nothing, and never change "
    "who said or did what.\n"
    "NATURAL — write it as a {language} journalist would write it, not as a "
    "translator would. Idiomatic word order, standard {language} spelling of "
    "names and places. It must not read as translated.\n"
    "NO REDUNDANT FRAMING — the source was written to orient a foreign reader; "
    "your audience is not one. Drop nationality and affiliation glosses your "
    "readers plainly do not need: \"Russia's Dmitry Medvedev said\" is simply "
    "\"Dmitry Medvedev said\", rendered in {language}. Keep a gloss only when the "
    "person or body is genuinely unfamiliar to your audience, or when the "
    "affiliation is itself the news.\n"
    "REGISTER — neutral, professional news style. Translate quoted speech as "
    "quoted speech; never paraphrase inside quotation marks.\n"
    "FORMAT — keep #hashtags, @mentions, URLs, emoji and line breaks exactly as "
    "they are.\n\n"
    "Output ONLY the {language} text. No preamble, no notes, no surrounding "
    "quotation marks. If the message is already in {language}, return it "
    "unchanged."
)


def translate(text: str, target_lang: str, source_lang: str | None = None,
              model: str | None = None) -> str:
    """Translate `text` into `target_lang` (e.g. "ru", "es", "German").
    `model` overrides TRANSLATE_MODEL — used for side-by-side model comparison.

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
            model=model or config.TRANSLATE_MODEL,
            # Faithfulness over flourish — near-greedy keeps the model from
            # embellishing wire copy.
            temperature=0.2,
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
