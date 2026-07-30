# brands/

One folder per brand, named exactly as in the `BRANDS` env var:

    brands/<name>/logo.png    transparent PNG, any size (the renderer scales
                              it to 180 px wide)

A brand without a logo.png shows up disabled in the news bot's brand picker.
Wire the brand's platform accounts in .env:

    BRANDS=mirnews:en,rusnews:ru
    BRAND_MIRNEWS_TG=@mir_news        # Telegram channel (bot must be admin)
    BRAND_MIRNEWS_YT=mirnews          # folder under credentials/youtube/
    BRAND_MIRNEWS_TW=mirnews          # TWITTER_<NAME>_* account name
