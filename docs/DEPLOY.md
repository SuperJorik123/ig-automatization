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
