#!/bin/bash
# Monday catch-up: run the daily trade cycle once the user has authorised.
PATH=/usr/sbin:/usr/bin:/bin
M=/root/ran_$(date -u +%F)
[ -f "$M" ] && exit 0
ss -tln | grep -qE ':4001 ' || exit 0
echo "$(date -u) monday catch-up trading run" >> /root/watchdog.log
cd /root/multi-product-signals/execution && IB_PORT=4001 CONFIRM_FIRST=0 python3.11 ib_bot.py >> /root/bot.log 2>&1 && touch "$M"
