#!/bin/bash
# ⚠️ STALE REPO COPY (note added 2026-10-27) - NOT the script the VM runs.
# The live /root/monday_catchup.sh is the catch-up trading run, started by the
# "0 10" and "0 12" (London) crontab lines EVERY day: 10:00 London = 09:00 UTC
# in summer, 10:00 UTC in winter; the 12:00 run exits at once when the 10:00
# one succeeded (marker /root/ran_<date>, as below). Known to differ from this
# copy (see WIKI.md "The scheduled jobs"); its exact text is not in the repo:
#   * it runs ib_bot.py with IB_BACKEND=web (the OAuth Web API) and does NOT
#     wait for port 4001 - the "ss ... :4001" check below is from the IB
#     Gateway era and would stop every run today;
#   * its environment had no HK_ENABLED=1 on 2026-10-27 (the run's published
#     config showed hk_enabled false), so it places no new HK buys; the owner
#     is adding HK_ENABLED=1, as on the 23:35 line;
#   * like this copy, it pulls no new code: only the hourly ":25" line does.
# The owner will paste the live script so this copy can be refreshed. Do not
# install this file on the VM.
#
# Monday catch-up: run the daily trade cycle once the user has authorised.
PATH=/usr/sbin:/usr/bin:/bin
M=/root/ran_$(date -u +%F)
[ -f "$M" ] && exit 0
ss -tln | grep -qE ':4001 ' || exit 0
echo "$(date -u) monday catch-up trading run" >> /root/watchdog.log
cd /root/multi-product-signals/execution && IB_PORT=4001 CONFIRM_FIRST=0 python3.11 ib_bot.py >> /root/bot.log 2>&1 && touch "$M"
