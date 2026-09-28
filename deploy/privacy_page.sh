#!/usr/bin/env bash
# deploy/privacy_page.sh — publish deploy/site/privacy.html on the media vhost.
#
# Meta requires a Privacy Policy URL and a User data deletion URL for the app;
# both point at this one page:
#   https://<host>/privacy
#   https://<host>/privacy#data-deletion
# Run on the VPS AFTER deploy/ig_media_hosting.sh (it adds to that vhost).
# Idempotent: re-run it after editing the page.
#
#   bash deploy/privacy_page.sh [host]     # host default news-automation.duckdns.org
set -euo pipefail

HOST="${1:-news-automation.duckdns.org}"
SRC="$(cd "$(dirname "$0")" && pwd)/site/privacy.html"
DIR=/var/www/igsite
CONF=/etc/nginx/sites-available/igmedia
SNIP=/etc/nginx/snippets/igsite.conf

[ -f "$SRC" ]  || { echo "missing $SRC" >&2; exit 1; }
[ -f "$CONF" ] || { echo "missing $CONF — run ig_media_hosting.sh first" >&2; exit 1; }

echo "== 1/3 page -> ${DIR}"
mkdir -p "$DIR"
chmod 755 "$DIR"
install -m 644 "$SRC" "$DIR/privacy.html"

echo "== 2/3 nginx snippet + include"
mkdir -p "$(dirname "$SNIP")"
cat > "$SNIP" <<EOF
# Privacy policy page (deploy/privacy_page.sh).
location = /privacy {
    alias ${DIR}/privacy.html;
    default_type text/html;
    charset utf-8;
    add_header Cache-Control "public, max-age=300";
    add_header X-Content-Type-Options nosniff;
}
location = /privacy/     { return 301 /privacy; }
location = /privacy.html { return 301 /privacy; }
EOF
if ! grep -q 'snippets/igsite.conf' "$CONF"; then
    # Goes next to the catch-all 404, i.e. inside the https server block that
    # certbot left the original locations in.
    if ! grep -q 'location / { return 404; }' "$CONF"; then
        echo "no 'location / { return 404; }' line in $CONF — add 'include $SNIP;' by hand" >&2
        exit 1
    fi
    cp "$CONF" "$CONF.bak.$(date +%Y%m%d-%H%M%S)"
    sed -i 's|^\(\s*\)location / { return 404; }|\1include snippets/igsite.conf;\n&|' "$CONF"
fi
nginx -t
systemctl reload nginx

echo "== 3/3 probe"
curl -s -o /dev/null -w "   /privacy -> HTTP %{http_code}\n" "https://${HOST}/privacy"
curl -s -o /dev/null -w "   /m/      -> HTTP %{http_code} (must not be 200)\n" "https://${HOST}/m/"
cat <<EOF

Meta app → App settings → Basic:
  Privacy Policy URL:      https://${HOST}/privacy
  User data deletion URL:  https://${HOST}/privacy#data-deletion
EOF
