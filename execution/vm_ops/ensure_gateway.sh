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
if pgrep -x java >/dev/null; then
  echo "$(date -u) java up, port down - waiting" >> /root/watchdog.log
else
  echo "$(date -u) gateway down -> restarting" >> /root/watchdog.log
  pkill -9 Xvfb 2>/dev/null; sleep 2; rm -f /tmp/.X1-lock /tmp/.X11-unix/X1
  nohup /root/start_gateway.sh >> /root/gateway.log 2>&1 &
fi
