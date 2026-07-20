#!/bin/bash
# If the Sunday-morning auth was approved, all good; otherwise send a new push.
PATH=/usr/sbin:/usr/bin:/bin
if ss -tln | grep -qE ':4001 '; then
  rm -f /root/reauth_pending
  echo "$(date -u) reauth check: authorised OK" >> /root/watchdog.log
else
  echo "$(date -u) reauth check: NOT authorised -> sending new push" >> /root/watchdog.log
  /root/sunday_reauth.sh
fi
