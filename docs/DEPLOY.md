# VPS deployment — the news pipeline, 24/7

The Telegram/YouTube news pipeline runs on a VPS as three systemd services.
The Instagram half stays on this PC: it drives a physical phone over ADB, so
there is nothing for it to talk to on a server.

| | |
| --- | --- |
| Host | `193.36.38.133` (root, Ubuntu 20.04) |
| App root | `/opt/ig-automatization2` |
| Python | `/opt/python3.12/bin/python3.12`, built from source — 20.04 is EOL and deadsnakes dropped it, so there is no packaged 3.11+. The venv is `/opt/ig-automatization2/.venv`. |
| ffmpeg | `/usr/local/bin/ffmpeg` — an **ffmpeg 8 static build** (BtbN), not the distro's 4.2. `shared/branding.py`'s row separator is a bare CR, which is an ffmpeg-8 drawtext behaviour; the apt build would render it differently. `/usr/local/bin` precedes `/usr/bin`, so the right binary wins. |
| Headline font | `/usr/local/share/fonts/segoeuib.ttf`, copied from Windows. `branding.FONT_CANDIDATES` finds it there; without it every clip falls back to the much heavier shipped DejaVu. |
| Timezone | `Europe/Bucharest`, matching `TIMEZONE` in `.env` — the weekly cleanup and `TG_FIRST_TICK` are local-clock. |
| Swap | 2 GB at `/swapfile` (1 vCPU / 2 GB RAM; 1080×1920 encodes need the headroom). |
| RAM sizing | Measured peaks: 4 services idle ~0.5 GB; branded video ~0.5 GB + ~0.39 GB per extra brand **within one ffmpeg pass**, capped by `BRAND_RENDER_BATCH` (default 4 → ~1.65 GB); one photo card **6.9 GB on `bria-rmbg`, 0.73 GB on `u2net`**. 2 GB needs `CARD_CUTOUT_MODEL=u2net` and `BRAND_RENDER_BATCH=2`; 4 GB runs the defaults; only 8 GB runs bria-rmbg cards. The `[monitor] Memory at 99%` mails come from these peaks — `psutil.virtual_memory().percent` is `MemAvailable`-based, so they are real pressure, not page cache. |

## Services

```
systemctl status  news-collector news-dispatcher news-bot
journalctl -u news-bot -f            # live log
systemctl restart news-bot
```

| Unit | Runs | Notes |
| ---- | ---- | ----- |
| `news-collector` | `modules/telegram/collector.py` | Needs a one-off interactive login (below) before it can be enabled. |
| `news-dispatcher` | `modules/telegram/dispatcher.py` | The only process that scores. |
| `news-bot` | `modules/telegram/news_bot.py` | Manual broadcaster + autopilot drip + weekly cleanup. |
| `newsroom-bot` | `modules/newsroom/main.py` | The client's WordPress→Telegram bot. Same checkout and venv as the three above since 2026-09-03 (it was the `client/wp-newsbot` branch and `/opt/wp-newsbot` before); its own token (`NR_BOT_TOKEN`), its own SQLite store under `modules/newsroom/data/`, its own BulkFollows key. A code push restarts it together with the others — the store makes restarts safe (seen articles are never reposted). |

All four are `Restart=always` and `WantedBy=multi-user.target`, so they come
back after a crash and after a reboot.

**One bot token allows exactly one poller.** Never run `news_bot.py` locally
while the VPS unit is up — both will fight over `getUpdates`.

## Sessions

Both Telethon sessions are logged in as `@viceB`.
`modules/telegram/data/bigfile.session` was copied from the PC and stayed
authorised. `collector.session` did not exist on the PC and was created on the
server by **QR login** — worth remembering, because the phone-code route is
painful over SSH and Telegram delivers those codes in-app, not by SMS:

```
py modules/telegram/mtproto.py --login --qr    # the built-in flow, draws the QR in the terminal
```

Then scan from Telegram → Settings → Devices → "Link Desktop Device". Tokens
expire in ~25 s, so scan the live terminal, never a screenshot. If a QR ever
has to cross a non-interactive channel, render it to a PNG and inline it into
the page as a `data:` URI — serving it as a separate `<img src>` gets cached by
the browser and every scan then hits an already-dead token ("invalid QR code").

## Monitoring (shared/monitoring)

Four layers, all inert until `ALERT_SMTP_HOST` + `ALERT_EMAIL_TO` are set in
`.env` (see `.env.example`, "Monitoring / alert email"):

1. **Error emails** — each service mails every logged ERROR/traceback as it
   happens (`errmail.install()` in each entrypoint; subject tag = process).
2. **Heartbeats** — news_bot / collector / dispatcher each ping their own
   healthchecks.io check URL every 5 min from inside their own loop. Create
   three checks at healthchecks.io (grace ~15 min), paste the URLs into
   `HEALTHCHECK_URL_NEWSBOT/_COLLECTOR/_DISPATCHER`. Missing pings → email:
   this is the "server down" alarm, and it also fires on a crashed or hung
   process and a dead network. No pings are sent while the URL is blank.
3. **Machine + balance checks** — `shared/monitoring/checks.py` on a 5-minute
   systemd timer: CPU over `ALERT_CPU_PCT` (80), disk over `ALERT_DISK_PCT`
   (85, the filesystem holding the checkout) and memory over `ALERT_MEM_PCT`
   (90) each run; hourly, BulkFollows balance under `ALERT_BULKFOLLOWS_MIN`
   ($2) and OpenRouter credits under `ALERT_OPENROUTER_MIN` ($0.50). One
   email on crossing, a daily reminder while bad, an all-clear on recovery.
   A failing check (panel unreachable, bad key) alerts with the same shape.
   A condition counts as "alerted" only once the email actually left, so a
   crossing that met a dead mail server is retried next tick. The script's
   own crashes are mailed too (errmail tag `monitor`).
4. **Restart resilience** — already there: every unit is `Restart=always`; a
   crash-loop shows up as error emails + eventually silent heartbeats.

Timer units (one timer covers everything, the newsroom bot included — it
reads both BulkFollows keys from the same `.env`):

```
# /etc/systemd/system/news-monitor.service
[Unit]
Description=ig-automatization machine + balance checks

[Service]
Type=oneshot
WorkingDirectory=/opt/ig-automatization2
ExecStart=/opt/ig-automatization2/.venv/bin/python shared/monitoring/checks.py

# /etc/systemd/system/news-monitor.timer
[Unit]
Description=run news-monitor every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
```

```
systemctl daemon-reload && systemctl enable --now news-monitor.timer
```

The newsroom bot has its own heartbeat check (`HEALTHCHECK_URL_NEWSROOM`) and
its errors are mailed with tag `newsroom`; nothing else is per-product.

### Verifying monitoring after a deploy

Do these in order; each one proves the layer below it.

1. **Mailbox** — must print `test email SENT` and exit 0. Anything else is an
   SMTP problem (Gmail: app password, not the account password):

       .venv/bin/python shared/monitoring/checks.py --test

2. **Checks** — run once by hand, read the log lines, then look at the state
   file. Every leg logs its current value and floor. To force an alert
   without waiting for a real one, run with a floor you are already over,
   then again with the normal floor to get the all-clear:

       .venv/bin/python shared/monitoring/checks.py
       cat data/monitor_state.json
       ALERT_CPU_PCT=1 .venv/bin/python shared/monitoring/checks.py   # -> "CPU at N%" email
       .venv/bin/python shared/monitoring/checks.py                   # -> "CPU back under the limit"

3. **Timer** — the next run should be within 5 minutes and the last run's
   log lines should be in the journal:

       systemctl list-timers --all | grep monitor
       journalctl -u news-monitor.service -n 20

4. **Heartbeats** — after restarting the services each logs
   `heartbeat: pinging healthchecks every 300s`, and the healthchecks.io
   dashboard turns green within 5 minutes. Stop one service
   (`systemctl stop news-dispatcher`) and the "down" email must arrive once
   the check's grace period passes; start it again for the "up" email.

5. **Error emails** — each service logs `error emails on -> <address>` at
   startup. Trigger one on purpose, e.g. paste a broken Instagram URL into
   the trigger bot, and the traceback should land in the inbox with the
   process name in the subject.

## Instagram media hosting

Instagram's Graph API (`modules/instagram/graph.py`) has no upload: it fetches
`image_url` / `video_url` from a public https URL. The VPS therefore also runs
nginx, serving one directory read-only under a free DuckDNS hostname with a
Let's Encrypt certificate. `shared/public_media.py` copies each render into
that directory under an unguessable name for the seconds a publish takes, then
deletes it; leftovers older than an hour are swept when the news bot starts.

| | |
| --- | --- |
| Hostname | `<sub>.duckdns.org` — registered at duckdns.org (free, GitHub/Google login); the token shown there is what the updater sends. |
| Directory | `/var/www/igmedia` (`PUBLIC_MEDIA_DIR`) — root-owned, 755; files are written 644 so nginx (`www-data`) can read them. |
| URL | `https://<sub>.duckdns.org/m/<name>` (`PUBLIC_MEDIA_BASE_URL=https://<sub>.duckdns.org/m/`). Everything outside `/m/` is a 404; `/m/` itself has `autoindex off`, so the random file name is the whole access control. |
| Cert | certbot, nginx plugin, auto-renewed by the packaged `certbot.timer` (twice daily). |
| IP updater | `/opt/duckdns/duck.sh` from cron every 5 minutes (also `@reboot`), log in `/opt/duckdns/duck.log`. |

One-shot setup, run **on the VPS** (nginx + certbot + DuckDNS cron + vhost;
idempotent, safe to re-run):

```
scp deploy/ig_media_hosting.sh root@193.36.38.133:/tmp/
ssh root@193.36.38.133 'bash /tmp/ig_media_hosting.sh <sub> <duckdns-token> you@example.com'
```

Then add to the VPS `.env` (the script prints these two lines at the end):

```
PUBLIC_MEDIA_DIR=/var/www/igmedia
PUBLIC_MEDIA_BASE_URL=https://<sub>.duckdns.org/m/
```

and `systemctl restart news-bot`. The bot's startup log line
`instagram (graph api): … | media hosting: …` confirms both settings were
read. Verify the vhost end to end:

```
echo hi > /var/www/igmedia/probe.txt
curl -sI https://<sub>.duckdns.org/m/probe.txt | head -1     # HTTP/1.1 200
curl -sI https://<sub>.duckdns.org/m/                        # 403 / 404, never a listing
rm /var/www/igmedia/probe.txt
```

The IG tokens themselves live in `credentials/brands/<name>.json`, under the
brand's `instagram` block (`access_token`, `user_id`, `token_refreshed`); the
news bot refreshes any token older than 7 days once a day and writes the new
one straight back into that file, so the VPS copy is the live one — when
pasting a fresh token by hand also set `"token_refreshed": "YYYY-MM-DD"`.
Accounts still configured the old way in `.env`
(`IG_GRAPH_<ACCOUNT>_ACCESS_TOKEN` + `_TOKEN_REFRESHED`) keep working and get
their `.env` line rewritten instead.

### credentials/brands/ holds SECRETS the deploy does not carry

Per-brand accounts (Telegram channel, Twitter keys, Instagram token, YouTube
channel) live in `credentials/brands/<name>.json`, one file per brand, all
git-ignored and — like `.env` — excluded from the deploy tarball below,
because the VPS copies are the LIVE ones: the bot writes refreshed IG tokens
into them. Adding or re-keying a brand on this PC therefore does NOT reach the
VPS on the next push. Send the files deliberately, and restart the services
(one brand, or the whole directory):

```
scp credentials/brands/wswire.json     root@193.36.38.133:/opt/ig-automatization2/credentials/brands/
ssh root@193.36.38.133 'chmod 600 /opt/ig-automatization2/credentials/brands/*.json     && systemctl restart news-collector news-dispatcher news-bot'
```

Careful in the other direction too: the VPS copy is the live one for IG
tokens, so copying a stale local file over it costs that account's token
(paste a fresh one, or run `py modules/instagram/graph.py --account <n>
--refresh` on the VPS afterwards). Only the brand you actually changed needs
sending, which is the point of one file per brand. Check either side with
`py shared/credentials.py --check` — it masks the secrets.

## Pushing new code

Secrets and runtime state are git-ignored, so a plain `git pull` never touches
them. From this PC, tar the tree without them and drop it over the old one:

```
tar czf app.tar.gz --exclude=.git --exclude=__pycache__ --exclude=node_modules \
    --exclude=.env --exclude='credentials/*' \
    --exclude='posts/*' --exclude='modules/telegram/data/*' .
scp app.tar.gz root@193.36.38.133:/tmp/
ssh root@193.36.38.133 'cd /opt/ig-automatization2 && tar xzf /tmp/app.tar.gz \
    && .venv/bin/pip install -q -r requirements.txt \
    && systemctl restart news-collector news-dispatcher news-bot'
```

Excluding `modules/telegram/data/` is the important part — it holds the live
SQLite queue and the Telethon session. `.env` and `credentials/` are excluded
for the same reason: the VPS copies are the live ones (the news bot rewrites
Instagram tokens into them daily), so shipping this PC's copies over them
costs you whatever the bot refreshed since you last pulled them down. Push
either one by hand, deliberately, when you actually changed it.

Gotcha when running these from Git Bash on Windows: it rewrites anything that
looks like a Unix path into a Windows one, so `scp app.tar.gz root@host:/tmp/`
silently targets `C:/Users/.../tmp`. Prefix the command with
`MSYS_NO_PATHCONV=1`.
