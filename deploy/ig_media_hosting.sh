#!/usr/bin/env bash
# deploy/ig_media_hosting.sh — one-shot VPS setup for Instagram media hosting.
#
# The Instagram Graph API fetches media from a public https URL (no upload),
# so the VPS serves one directory read-only behind a free DuckDNS hostname
# with a Let's Encrypt certificate. See docs/DEPLOY.md, "Instagram media
# hosting". Idempotent: re-running updates the pieces in place.
#
#   bash ig_media_hosting.sh <duckdns-subdomain> <duckdns-token> <email> [media-dir]
#
# Does, in order:
#   1. DuckDNS updater  /opt/duckdns/duck.sh  + cron (*/5 min and @reboot)
#   2. nginx + certbot (apt; certbot from snap when apt has none)
#   3. media dir (default /var/www/igmedia), root-owned 755
#   4. nginx vhost: only /m/ is served (alias to the dir, autoindex off)
#   5. Let's Encrypt cert via certbot --nginx, http->https redirect
#   6. prints the two .env lines to add
set -euo pipefail

SUB="${1:?duckdns subdomain (without .duckdns.org)}"
TOKEN="${2:?duckdns token}"
EMAIL="${3:?email for Lets Encrypt expiry notices}"
DIR="${4:-/var/www/igmedia}"
HOST="${SUB}.duckdns.org"

echo "== 1/6 DuckDNS updater for ${HOST}"
mkdir -p /opt/duckdns
cat > /opt/duckdns/duck.sh <<EOF
#!/usr/bin/env bash
# Tell DuckDNS this machine's current public IP (ip= blank = the caller's IP).
echo "\$(date -Is) \$(curl -s -k "https://www.duckdns.org/update?domains=${SUB}&token=${TOKEN}&ip=")" >> /opt/duckdns/duck.log
EOF
chmod 700 /opt/duckdns/duck.sh
# `crontab -l` exits 1 when there is no crontab yet and `grep -v` exits 1 on
# empty input — neither may abort the script under set -e / pipefail.
( { crontab -l 2>/dev/null || true; } | { grep -v '/opt/duckdns/duck.sh' || true; } ; \
  echo '*/5 * * * * /opt/duckdns/duck.sh >/dev/null 2>&1' ; \
  echo '@reboot /opt/duckdns/duck.sh >/dev/null 2>&1' ) | crontab -
/opt/duckdns/duck.sh
tail -1 /opt/duckdns/duck.log   # "... OK" expected
# Wait until the name resolves to us before certbot needs it.
MYIP="$(curl -s https://api.ipify.org || true)"
for i in $(seq 1 12); do
    R="$(getent hosts "${HOST}" | awk '{print $1}' | head -1 || true)"
    [ -n "$R" ] && [ "$R" = "$MYIP" ] && break
    echo "   waiting for ${HOST} -> ${MYIP} (now: ${R:-unresolved})"; sleep 5
done

echo "== 2/6 nginx + certbot"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq || true   # focal is EOL; a stale mirror must not stop the install
apt-get install -y -qq nginx >/dev/null
if ! command -v certbot >/dev/null; then
    if apt-get install -y -qq certbot python3-certbot-nginx >/dev/null 2>&1; then
        echo "   certbot from apt"
    else
        apt-get install -y -qq snapd >/dev/null
        snap install core >/dev/null 2>&1 || true
        snap install --classic certbot
        ln -sf /snap/bin/certbot /usr/bin/certbot
        echo "   certbot from snap"
    fi
fi
systemctl enable --now nginx >/dev/null

echo "== 3/6 media dir ${DIR}"
mkdir -p "${DIR}"
chown root:root "${DIR}"
chmod 755 "${DIR}"

echo "== 4/6 nginx vhost"
CONF=/etc/nginx/sites-available/igmedia
if [ ! -f "$CONF" ] || ! grep -q 'listen 443' "$CONF"; then
cat > "$CONF" <<EOF
# Instagram media hosting (deploy/ig_media_hosting.sh). Only /m/ exists.
server {
    listen 80;
    listen [::]:80;
    server_name ${HOST};

    location /m/ {
        alias ${DIR}/;
        autoindex off;
        add_header Cache-Control "no-store";
        add_header X-Content-Type-Options nosniff;
        types { image/jpeg jpg jpeg; video/mp4 mp4; text/plain txt; }
        default_type application/octet-stream;
    }
    location = /m/ { return 404; }
    location / { return 404; }
}
EOF
fi
ln -sf "$CONF" /etc/nginx/sites-enabled/igmedia
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx
if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
    ufw allow 'Nginx Full' >/dev/null || true
fi

echo "== 5/6 Lets Encrypt for ${HOST}"
certbot --nginx -d "${HOST}" --non-interactive --agree-tos -m "${EMAIL}" --redirect
# Renewal: the apt package ships certbot.timer, the snap ships its own timer.
if systemctl is-active --quiet certbot.timer || systemctl is-active --quiet snap.certbot.renew.timer \
   || [ -f /etc/cron.d/certbot ]; then
    echo "   renewal timer present"
else
    echo "   WARNING: no certbot timer found — add 'certbot renew' to cron"
fi
nginx -t && systemctl reload nginx

echo "== 6/6 probe"
echo probe > "${DIR}/probe.txt"
chmod 644 "${DIR}/probe.txt"
curl -sI "https://${HOST}/m/probe.txt" | head -1
curl -s -o /dev/null -w "   /m/ listing -> HTTP %{http_code} (must not be 200)\n" "https://${HOST}/m/"
rm -f "${DIR}/probe.txt"

cat <<EOF

Add to the VPS .env, then: systemctl restart news-bot

PUBLIC_MEDIA_DIR=${DIR}
PUBLIC_MEDIA_BASE_URL=https://${HOST}/m/
EOF
