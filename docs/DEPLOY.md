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
| `newsroom-bot` | `modules/newsroom/main.py` | The client's WordPress→Telegram bot (`client/wp-newsbot` branch). Runs from its **own checkout** `/opt/wp-newsbot` with its own venv and `.env` (the `NR_*` vars + `OPENROUTER_API_KEY`), so pushing master code never restarts it. Deploy it by tarring the `client/wp-newsbot` tree (exclude `posts/`, `ui/`, media) over `/opt/wp-newsbot`. |

All three are `Restart=always` and `WantedBy=multi-user.target`, so they come
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
   systemd timer: CPU over `ALERT_CPU_PCT` (80) each run; hourly, BulkFollows
   balance under `ALERT_BULKFOLLOWS_MIN` ($2) and OpenRouter credits under
   `ALERT_OPENROUTER_MIN` ($0.50). One email on crossing, a daily reminder
   while bad, an all-clear on recovery. A failing check (panel unreachable,
   bad key) alerts with the same shape.
4. **Restart resilience** — already there: every unit is `Restart=always`; a
   crash-loop shows up as error emails + eventually silent heartbeats.

Timer units (master checkout — mirror with `/opt/wp-newsbot` paths and the
name `newsroom-monitor` for the client checkout):

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

**Both checkouts share this VPS**, so per-machine and per-account legs run in
only one of them: the newsroom checkout's `.env` sets `ALERT_CPU_PCT=0` and
`ALERT_OPENROUTER_MIN=0` — its timer then only watches the client's
`NR_BULKFOLLOWS_API_KEY` balance, keeping the client's panel key out of the
master checkout's `.env`.

## Pushing new code

Secrets and runtime state are git-ignored, so a plain `git pull` never touches
them. From this PC, tar the tree without them and drop it over the old one:

```
tar czf app.tar.gz --exclude=.git --exclude=__pycache__ --exclude=node_modules \
    --exclude='posts/*' --exclude='modules/telegram/data/*' .
scp app.tar.gz root@193.36.38.133:/tmp/
ssh root@193.36.38.133 'cd /opt/ig-automatization2 && tar xzf /tmp/app.tar.gz \
    && .venv/bin/pip install -q -r requirements.txt \
    && systemctl restart news-collector news-dispatcher news-bot'
```

Excluding `modules/telegram/data/` is the important part — it holds the live
SQLite queue and the Telethon session.

Gotcha when running these from Git Bash on Windows: it rewrites anything that
looks like a Unix path into a Windows one, so `scp app.tar.gz root@host:/tmp/`
silently targets `C:/Users/.../tmp`. Prefix the command with
`MSYS_NO_PATHCONV=1`.
