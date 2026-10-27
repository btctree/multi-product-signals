#!/bin/bash
# The catch-up trading run - the LIVE /root/monday_catchup.sh, as
# `sudo cat /root/monday_catchup.sh` printed it on 2026-10-27 at about 19:00
# UTC in Oracle Cloud Shell (transcribed from the screen). Below this header
# is that text line for line, plus ONE marked line the owner added on the VM
# after that reading (the APPLIED block), so the copy matches the live script
# as it is since 2026-10-27 20:51 UTC. Refresh this copy after any change on
# the VM. From Cloud Shell, where the key ~/mp_vm_key lives:
#     ssh -i ~/mp_vm_key opc@<vm-ip> 'sudo cat /root/monday_catchup.sh'
# The "0 10" and "0 12" London crontab lines run it EVERY day (see
# crontab.reference): 10:00 London = 09:00 UTC in summer, 10:00 UTC in
# winter; the 12:00 run exits at once when the 10:00 one succeeded (the
# marker below). It does NOT git pull: it runs the checkout the hourly :25
# line last reset to origin/main. A reference copy, not an install file.
#
# Monday catch-up: run the daily trade cycle once the user has authorised.
PATH=/usr/sbin:/usr/bin:/bin
M=/root/ran_$(date -u +%F)
[ -f "$M" ] && exit 0
# gateway port check removed: the Web API has no local listener
export IB_BACKEND=web
# >>> APPLIED on the VM 2026-10-27 20:51 UTC (after the ~19:00 reading above):
# >>> the owner added this one line as line 8 of /root/monday_catchup.sh,
# >>> verified (bash -n OK; the old script is kept as
# >>> /root/monday_catchup.sh.bak), so the catch-up may place new HK buys like
# >>> the 23:35 line. First run with it: 2026-10-28 10:00 UTC.
export HK_ENABLED=1
echo "$(date -u) monday catch-up trading run" >> /root/watchdog.log
cd /root/multi-product-signals/execution && IB_PORT=4001 CONFIRM_FIRST=0 python3.11 ib_bot.py >> /root/bot.log 2>&1 && touch "$M"
