#!/bin/sh
# Restart systemd units only once the news bot is idle.
#
# news_bot.py keeps modules/telegram/data/busy in existence exactly while it is
# rendering, sending a download back, counting down to a publish or
# publishing (scheduled posts included). Restarting under any of those cuts a
# post in half — some platforms get it, the rest never do — so a deploy runs
# this instead of a bare `systemctl restart`:
#
#     sh deploy/restart_when_idle.sh news-bot newsroom-bot
#
# Waits up to MAX_WAIT_S (default 3600) and then gives up WITHOUT restarting
# (exit 1): the new code is on disk and the next run picks it up. Scheduled
# posts don't need protecting here — they live in the DB and are re-armed on
# startup; one that comes due during the restart fires right after it.

APP_DIR=${APP_DIR:-/opt/ig-automatization2}
BUSY="$APP_DIR/modules/telegram/data/busy"
MAX_WAIT_S=${MAX_WAIT_S:-3600}

waited=0
while [ -e "$BUSY" ]; do
    if [ "$waited" -ge "$MAX_WAIT_S" ]; then
        echo "news-bot still busy after ${waited}s ($(cat "$BUSY" 2>/dev/null)) — NOT restarting"
        exit 1
    fi
    if [ $((waited % 60)) -eq 0 ]; then
        echo "news-bot busy ($(cat "$BUSY" 2>/dev/null)) — waiting…"
    fi
    sleep 5
    waited=$((waited + 5))
done

echo "news-bot idle — restarting: $*"
exec systemctl restart "$@"
