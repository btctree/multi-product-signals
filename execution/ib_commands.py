"""Executes SELL commands sent from the phone dashboard.

The dashboard's Sell button opens a GitHub issue titled "SELL: <SYMBOL> <QTY>"
(using the same device-stored token as the add-product flow). This poller (VM
cron, every 10 min) reads recent issues WITHOUT auth (public repo), places the
sell on IB for positions actually held, remembers processed issue ids, and
republishes bot_state.json so the phone reflects it within minutes.

Safety: only SELLs, only for existing long positions, qty capped at held qty.
Each command is a deliberate button press by the account owner.
"""
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import ib_bot
from ib_async import IB, MarketOrder

ISSUES_URL = ("https://api.github.com/repos/btctree/multi-product-signals/"
              "issues?state=all&per_page=30&sort=created&direction=desc")
DONE = Path("/root/commands_done.json")
MAX_AGE_H = 48


def log(*a):
    print(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), *a, flush=True)


def fetch_commands():
    req = urllib.request.Request(ISSUES_URL, headers={"User-Agent": "mps-vm"})
    with urllib.request.urlopen(req, timeout=30) as r:
        issues = json.load(r)
    out = []
    now = datetime.now(timezone.utc)
    for i in issues:
        m = re.match(r"^(SELL|REFRESH):?\s*([A-Za-z0-9.^=\-]+)?(?:\s+(\d+(?:\.\d+)?))?",
                     i.get("title", ""))
        if not m:
            continue
        age_h = (now - datetime.strptime(i["created_at"], "%Y-%m-%dT%H:%M:%SZ")
                 .replace(tzinfo=timezone.utc)).total_seconds() / 3600
        if age_h > MAX_AGE_H:
            continue
        out.append({"id": i["number"], "kind": m.group(1).lower(),
                    "symbol": (m.group(2) or "").upper(),
                    "qty": float(m.group(3)) if m.group(3) else None})
    return out


def main():
    cmds = fetch_commands()
    done = set(json.loads(DONE.read_text())) if DONE.exists() else set()
    todo = [c for c in cmds if c["id"] not in done]
    if not todo:
        return
    log(f"{len(todo)} sell command(s) to execute")
    ib = IB()
    # SAME clientId as the nightly bot: IB only lets the ORIGINATING client
    # cancel an order, and we must be able to cancel the bot's resting trail
    # stops before a phone sell. If the bot happens to be mid-run, connect
    # raises (326 client id in use) and the outer handler skips this cycle —
    # the 10-minute cron simply retries.
    ib.connect(ib_bot.HOST, ib_bot.PORT, clientId=ib_bot.CLIENT_ID, timeout=25)
    state = ib_bot.load_state()
    try:
        for c in todo:
            if c["kind"] == "refresh":
                log(f"issue #{c['id']}: refresh — capturing live account state")
                done.add(c["id"])
                continue                     # publish at the end does the capture
            placed = False
            for p in ib.positions():
                if p.position <= 0 or getattr(p.contract, "secType", "") == "CASH":
                    continue
                ysym = state.get("map", {}).get(p.contract.symbol, p.contract.symbol)
                if c["symbol"] not in (p.contract.symbol.upper(), ysym.upper()):
                    continue
                qty = int(min(c["qty"] or p.position, p.position))
                if qty <= 0:
                    continue
                p.contract.exchange = p.contract.exchange or "SMART"
                ib.qualifyContracts(p.contract)
                # kill the bot's resting trail stops first, CONFIRMED — a phone
                # sell and a resting stop together would both fill (double-sell)
                ib.reqAllOpenOrders()
                ib.sleep(1)
                stops = [t for t in ib.trades()
                         if (getattr(t.order, "orderRef", "") or "")
                         .startswith(ib_bot.TRAIL_TAG)
                         and t.contract.symbol == p.contract.symbol
                         and t.orderStatus.status not in ib_bot._STOP_DEAD]
                try:
                    if stops:
                        ib_bot.confirm_cancelled(ib, stops, "phone sell")
                except Exception as e:
                    # not confirmed dead (or it FILLED meanwhile): selling now
                    # risks a double-fill. Skip; the issue stays un-done so the
                    # next 10-min poll retries against fresh state.
                    log(f"!! {p.contract.symbol}: {e} — phone sell SKIPPED")
                    placed = None
                    break
                trade = ib.placeOrder(p.contract, MarketOrder("SELL", qty))
                ib.sleep(2)
                status, err = ib_bot._order_verdict(trade)
                if status == "REJECTED":
                    log(f"!! phone sell REJECTED {p.contract.symbol} — {err[:120]}")
                ib_bot.PLACED.append({
                    "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                    "action": "SELL", "qty": qty, "symbol": p.contract.symbol,
                    "limit": "MKT", "ccy": p.contract.currency,
                    "reason": "sell button (your phone)",
                    "status": status, "error": err[:160]})
                log(f"SELL {qty} {p.contract.symbol} placed (issue #{c['id']})")
                placed = True
                break
            if placed is None:
                continue                     # cancel unconfirmed — retry next poll
            if not placed:
                log(f"issue #{c['id']}: no matching held position for {c['symbol']} — marked done")
            done.add(c["id"])
        DONE.write_text(json.dumps(sorted(done)))
        ib_bot.publish_state(ib, state, ib_bot.net_liq(ib))
    finally:
        ib.disconnect()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"skipped ({e})")
        sys.exit(0)
