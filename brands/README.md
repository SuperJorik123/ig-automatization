# brands/

One folder per brand, named exactly as in the `BRANDS` env var:

    brands/<name>/logo.png    transparent PNG, any size (the renderer scales
                              it to 180 px wide)
    brands/<name>/style.json  optional headline colors / font (see below)

A brand without a logo.png shows up disabled in the news bot's brand picker.

## style.json

Every key is optional; a missing file (or key) keeps the default look —
white text on a black@0.55 box in the repo-shipped bold font.

    {
      "background": "#C90A0A",   // headline banner box color
      "background_alpha": 1.0,   // 0..1; defaults to 1.0 once background is set
      "text": "#ffffff",         // headline color
      "font": "Verdana",         // absolute path, a file in this folder, or a
                                 // system font name/filename (arialbd.ttf)
      "font_size": 41            // px; wrapping width scales with it
    }

`background_alpha` only defaults to 0.55 for the built-in black box — an
explicitly configured color is drawn opaque unless you set the key yourself.
A malformed color, an out-of-range size or an unresolvable font raises at
render time rather than silently falling back, so typos surface in the news
bot's error reply.
Wire the brand's platform accounts in .env:

    BRANDS=mirnews:en,rusnews:ru
    BRAND_MIRNEWS_TG=@mir_news        # Telegram channel (bot must be admin)
    BRAND_MIRNEWS_YT=mirnews          # folder under credentials/youtube/
    BRAND_MIRNEWS_TW=mirnews          # TWITTER_<NAME>_* account name
