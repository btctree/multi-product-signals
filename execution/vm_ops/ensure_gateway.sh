#!/bin/bash
PATH=/usr/sbin:/usr/bin:/bin
# port up -> healthy; clear any pending-reauth flag
if ss -tln | grep -qE ':4001 '; then
  rm -f /root/reauth_pending
  exit 0
fi
# weekly reauth in progress: do NOT auto-restart (pushes only at scheduled times)
if [ -f /root/reauth_pending ]; then
  exit 0
fi
# NIGHT HOLD (23:30-07:00 London): never cold-restart at night — a cold start can
# demand 2FA and ping the user's phone while they sleep. The 07:05 London check
# sends one civilised push instead, and the 10:00 catch-up trades after approval.
# (Synced from the LIVE VM copy 2026-08-02 — the repo had lagged this addition;
# ib_bot's zombie heal depends on this hold existing.)
H=$(TZ=Europe/London date +%H%M)
if [ "$H" -ge 2330 ] || [ "$H" -lt 0700 ]; then
  echo "$(date -u) night hold: gateway down, waiting for morning" >> /root/watchdog.log
  exit 0
fi
if pgrep -x java >/dev/null; then
  echo "$(date -u) java up, port down - waiting" >> /root/watchdog.log
else
  echo "$(date -u) gateway down -> restarting" >> /root/watchdog.log
  pkill -9 Xvfb 2>/dev/null; sleep 2; rm -f /tmp/.X1-lock /tmp/.X11-unix/X1
  nohup /root/start_gateway.sh >> /root/gateway.log 2>&1 &
fi
