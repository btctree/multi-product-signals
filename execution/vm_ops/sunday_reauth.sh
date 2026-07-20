#!/bin/bash
# Weekly re-auth on OUR schedule: fresh login -> 2FA push to phone (if needed).
PATH=/usr/sbin:/usr/bin:/bin
touch /root/reauth_pending
echo "$(date -u) weekly reauth: fresh login started" >> /root/watchdog.log
pkill -9 java 2>/dev/null; pkill -9 Xvfb 2>/dev/null; sleep 2
rm -f /tmp/.X1-lock /tmp/.X11-unix/X1
nohup /root/start_gateway.sh >> /root/gateway.log 2>&1 &
