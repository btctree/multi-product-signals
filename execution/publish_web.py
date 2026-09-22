#!/usr/bin/env python3
"""Publish bot_state.json (and the NetLiq series) from the IBKR Web API.

The phone dashboard reads data/bot_state.json out of the repo. That file is
normally written by ib_bot.py --publish-only over the TWS socket API, which died
on 2026-08-24 when IBKR forced passkey 2FA - so the dashboard has been frozen at
the 2026-08-24 07:20 UTC snapshot ever since, showing a stale 14-position book
with JBHT still in it.

This publisher does the same job over OAuth. It is READ-ONLY against the broker:
it imports ib_web (reads) and never ib_orders (writes), so it cannot place an
order even by accident. Its only side effects are writing repo files and pushing
them, exactly as publish_state() does.

Deliberately preserved from ib_bot.py's publish_state():
  - identical bot_state.json shape (dashboard depends on it)
  - CASH rows filtered out - FX balances are not investments
  - symbols mapped through state['map'] so keys stay byte-identical
  - netliq_history upsert-by-day, atomic write, and NEVER overwriting an
    unreadable file (that wipe destroyed the hand-entered flows once already)
  - the same git add/commit sequence, and the same push (gitpush.py: on a
    refusal it replays the commit onto origin/main and pushes again)

Run:  python3 publish_web.py            publish
      python3 publish_web.py --dry      print, write nothing
      python3 publish_web.py --backfill apply the one-time manual-trade backfill
"""
import json
import os
import subprocess
import sys
import datetime
from pathlib import Path

import day_parts
import earmark
import gitpush
import ib_web

REPO = Path(os.environ.get("MPS_REPO", "/root/multi-product-signals"))
DATA = REPO / "data"
STATE = REPO / "execution" / "state.json"
BACKFILL_MARK = Path(os.environ.get("MPS_BACKFILL_MARK", "/root/.mps_backfill_done"))

# Trades placed BY HAND while the gateway was down. IB's
# /iserver/account/trades?days=7 returns nothing for them, so the activity log
# would otherwise show a gap between 2026-08-21 and whenever automation resumes.
# Quantities and prices are the operator's, cross-checked against IB's own
# avg_cost on the resulting positions (NTAP 191.0511 and URI 1058.05 both
# confirm; URI's basis is higher than the 1057.05 fill because IB includes
# commission).
MANUAL_TRADES = [
    {"time": "2026-08-24 13:30 UTC", "action": "BUY", "qty": 9, "symbol": "NTAP",
     "limit": 191.05, "ccy": "USD",
     "reason": "entry signal, score 100 (order 2026-08-21; filled at Monday open)",
     "status": "ok", "error": "", "manual": True},
    {"time": "2026-08-26 14:00 UTC", "action": "SELL", "qty": 6, "symbol": "JBHT",
     "limit": 263.15, "ccy": "USD",
     "reason": "trailing stop 266.80 (placed by hand - gateway down)",
     "status": "ok", "error": "", "manual": True},
    {"time": "2026-08-26 14:05 UTC", "action": "BUY", "qty": 1, "symbol": "URI",
     "limit": 1057.05, "ccy": "USD",
     "reason": "entry signal, score 67 (placed by hand - gateway down)",
     "status": "ok", "error": "", "manual": True},
]


def log(*a):
    print(datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), *a)


def build(state):
    snap = ib_web.snapshot()
    smap = state.get("map", {})
    spos = state.get("pos", {})
    poss = []
    for p in snap["positions"]:
        if str(p.get("sec_type") or "").upper() == "CASH":
            continue                       # FX balances are not investments
        ib_sym = str(p["ib_symbol"])
        ysym = smap.get(ib_sym, ib_sym)
        st = spos.get(ysym, {})
        poss.append({"symbol": ysym, "ib_symbol": ib_sym, "qty": p["qty"],
                     "avg_cost": round(float(p["avg_cost"]), 4) if p["avg_cost"] else None,
                     "ccy": p["ccy"], "entry": st.get("entry"), "stop": st.get("stop")})
    cash = {k: round(v) for k, v in (snap["cash"] or {}).items() if abs(v) >= 1}
    return snap, poss, cash


def main():
    dry = "--dry" in sys.argv
    do_backfill = "--backfill" in sys.argv

    state = json.loads(STATE.read_text(encoding="utf-8"))
    snap, poss, cash = build(state)
    nl = float(snap["netliq"])
    # Earmarked cash is not trading capital. execution/earmark.py is the one shared
    # rule, used identically by ib_bot, this publisher and the digest; this file
    # used to carry its own copy of the read and the cap, and the copies drifted.
    # This publisher sweeps no executions (and must never import ib_orders), so
    # it takes the bot's own HKD from the last live run's pocket file, net of HK
    # buys still working: min(marker, HKD held - bot's HKD). A missing, stale or
    # unconfirmed file gives exactly the old min(marker, HKD held). Between runs
    # the file lags the balance, and every bot-side lag (a working buy, a sale, a
    # conversion that fills after the run) reads as pot: over-excluded, the safe
    # direction - see earmark.published_pocket.
    exc, bot_hkd = earmark.publisher_exclusion(float((cash or {}).get("HKD", 0) or 0))
    if exc:
        log("excluding %s HKD earmarked cash (NetLiq %s -> %s)"
            % (f"{exc:,.0f}", f"{nl:,.0f}", f"{nl - exc:,.0f}"))
        nl -= exc

    out = DATA / "bot_state.json"
    prev = {}
    if out.exists():
        try:
            prev = json.loads(out.read_text(encoding="utf-8"))
        except Exception:
            prev = {}

    # Scrubbed on every publish: rows already in the public file carry the live
    # account number (failed-order errors from 2026-09-01..03), and this hourly
    # rewrite is what cleans them - see ib_web.redact.
    act = ib_web.scrub(list(prev.get("activity") or []))
    if do_backfill and not BACKFILL_MARK.exists():
        have = {(a.get("time"), a.get("symbol"), a.get("action")) for a in act}
        added = 0
        for t in MANUAL_TRADES:
            if (t["time"], t["symbol"], t["action"]) not in have:
                act.append(t)
                added += 1
        act.sort(key=lambda a: str(a.get("time")))
        log("backfilled %d manual trade(s) into the activity log" % added)
        if not dry:
            BACKFILL_MARK.write_text(datetime.datetime.now(datetime.timezone.utc).isoformat())

    snapshot = {
        "updated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "netliq": round(nl), "base_ccy": "HKD", "cash": cash,
        "excluded_cash": round(exc),          # already netted out of netliq
        "earmark_marker": round(earmark.marker()),   # raw, display only - see ib_bot
        # the bot's own HKD left in the pool, or null (plain cap) - see ib_bot
        "earmark_bot_hkd": (None if bot_hkd is None else round(bot_hkd)),
        "positions": poss, "activity": act[-100:],
    }

    log("netliq %s | %d positions | cash %s | %d activity rows"
        % (round(nl), len(poss), cash, len(snapshot["activity"])))
    for p in sorted(poss, key=lambda x: str(x["symbol"])):
        log("   %-8s %8g @ %-10s stop %s" % (p["symbol"], p["qty"], p["avg_cost"], p["stop"]))
    if dry:
        log("--dry: nothing written")
        return 0

    out.write_text(json.dumps(snapshot, indent=1))

    # NetLiq series: upsert today. An UNREADABLE file must be left alone for
    # human repair - rewriting it would destroy the backfilled series and the
    # hand-entered deposit/withdrawal flows the P&L Calendar depends on.
    try:
        hp = DATA / "netliq_history.json"
        hist = {"series": [], "flows": []}
        if hp.exists():
            hist = json.loads(hp.read_text(encoding="utf-8"))
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        ser = [e for e in hist.get("series", []) if e.get("d") != today]
        # "exc" = the earmark this nl was published NET of. Without it a day
        # whose earmark moved looks like trading P&L to any reader that chains
        # the series (the dashboard's vs-S&P card). Additive: older rows read 0.
        ser.append({"d": today, "nl": round(nl), "exc": round(exc)})
        hist["series"] = sorted(ser, key=lambda e: e["d"])
        tmp = hp.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(hist, indent=1))
        os.replace(tmp, hp)
    except Exception as e:
        log("  !! netliq history NOT updated (%s) - existing file left untouched" % e)

    # What that day's number was MADE of: every position's value, the cash in
    # each currency and the rate each was valued at, so the calendar can break a
    # day down instead of just restating it. Best-effort by design - a breakdown
    # is a nice-to-have and must never cost a publish. The rates come from the
    # ledger IB returns anyway; mkt_value is in the position's own currency.
    try:
        led = ib_web.ledger()
        rates = day_parts.rates_from_ledger(led, base_ccy=str(snap.get("base_ccy") or "HKD"))
        # snap's rows carry IB's ticker and mkt_value; poss carries the mapped
        # symbol the whole site uses. Join them so the row has both.
        mv = {str(p.get("ib_symbol")): p for p in snap["positions"]}
        merged = []
        for p in poss:
            q = mv.get(str(p.get("ib_symbol")), {})
            merged.append(dict(p, mkt_value=q.get("mkt_value"),
                               sec_type=q.get("sec_type")))
        row = day_parts.build_row(merged, cash, rates, nl, exc=exc,
                                  base_ccy=str(snap.get("base_ccy") or "HKD"))
        n = day_parts.upsert(str(DATA / "day_parts.json"), row)
        log("  day parts: %d positions priced, %d days on file"
            % (len(row["pos"]), n))
    except Exception as e:
        log("  !! day parts NOT updated (%s) - the calendar falls back to totals" % e)

    for cmd in (["add", "data/bot_state.json", "data/netliq_history.json",
                 "data/day_parts.json"],
                ["-c", "user.email=bot@vm", "-c", "user.name=ib-bot",
                 "commit", "-m", "bot: state update (web api) [skip ci]"]):
        r = subprocess.run(["git", "-C", str(REPO)] + cmd,
                           capture_output=True, text=True, timeout=90)
        if r.returncode != 0:
            log("  publish '%s' stopped: %s"
                % (cmd[0] if cmd[0] != "-c" else "commit",
                   (r.stderr or r.stdout).strip()[:120]))
            return 1
    # Not a bare push: origin can move between the :25 reset and here (an
    # operator deploy), and a refused commit was thrown away by the next reset.
    # gitpush replays it onto origin and retries, and never leaves the checkout
    # mid-rebase (board review 2026-09-21). It has logged why when it fails.
    if not gitpush.push_with_retry(REPO, log, attempts=3, timeout=90):
        return 1
    log("  dashboard updated and pushed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log("FAILED: %r" % e)
        sys.exit(1)
