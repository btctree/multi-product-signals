"""Executes SELL commands sent from the phone dashboard.

The dashboard's Sell button opens a GitHub issue titled "SELL: <SYMBOL> <QTY>"
(using the same device-stored token as the add-product flow). This poller (VM
cron, every 10 min) reads recent issues WITHOUT auth (public repo), places the
sell on IB for positions actually held, remembers processed issue ids, and
republishes bot_state.json so the phone reflects it within minutes.

Also EARMARK: the operator's month-end routine is a GBP deposit converted to
HKD and withdrawn days later, and that pass-through money is not trading
capital - left unmarked it inflates NetLiq, so the bot sizes positions (NetLiq /
15) off money that is about to leave, and ratchets the kill-switch peak against
it. The marker is the base-currency number ib_bot._excluded_cash() reads.

Safety: only SELLs, only for existing long positions, qty capped at held qty.
Each command is a deliberate button press by the account owner.
"""
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import alerts
import earmark
import ib_bot
from broker import IB, MarketOrder

ISSUES_URL = ("https://api.github.com/repos/btctree/multi-product-signals/"
              "issues?state=all&per_page=30&sort=created&direction=desc")
DONE = Path("/root/commands_done.json")
MAX_AGE_H = 48
# The repo is PUBLIC and issues are open to anyone, so the issue author is the
# only thing separating a stranger from a market SELL of a full position.
OWNER = "btctree"
# Must match the WHOLE title (fullmatch). A prefix match treats "SELL: NVDA when
# it hits 200" as an immediate full-position sell, because the trailing words
# leave qty unparsed and qty=None means "sell everything".
# Accepts exactly what docs/index.html sends: "REFRESH", "SELL: SYM",
# "SELL: SYM QTY", "EARMARK: AMOUNT".
# EARMARK takes a REQUIRED amount: an optional one would make a stray "EARMARK"
# title mean zero, silently un-marking money that is still waiting to leave.
CMD_RE = re.compile(
    r"(?:(SELL)(?::\s*|\s+)([A-Za-z0-9.^=\-]{1,15})(?:\s+(\d+(?:\.\d+)?))?"
    r"|(REFRESH)"
    r"|(EARMARK)(?::\s*|\s+)(\d+(?:\.\d+)?))\s*"
)


# What the current poll set out to run. `done` is the same set main() adds to,
# so the __main__ handler - where an IB or session failure lands - can name the
# commands that exception cut short. Before this they retried every 10 minutes
# for MAX_AGE_H and were then dropped by fetch_commands, with only a 'skipped'
# log line ever written: a SELL tap could expire unexecuted and unannounced.
_POLL = {"todo": [], "done": set()}


def log(*a):
    print(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), *a, flush=True)


def _describe(c):
    if c.get("kind") == "sell":
        return f"SELL {c.get('symbol')} {int(c['qty']) if c.get('qty') else '(all)'}"
    if c.get("kind") == "earmark":
        return f"EARMARK {c.get('amount')}"
    return "REFRESH"


def alert_sell_outcome(c, refusal):
    """Queue the alert for a SELL that did not sell. NEVER raises.

    refusal is (qty, ib_symbol, ib_error) when IB refused the order, or None
    when no held position matched. Called only AFTER the DONE save - see main().
    """
    try:
        if refusal:
            qty, sym, err = refusal
            text = (f"⚠️ PHONE SELL REFUSED by IB: SELL {qty} {sym} "
                    f"(issue #{c['id']})\n"
                    f"IB said: {str(err or '')[:600] or '(no message)'}\n"
                    f"Nothing was sold. The command is marked done and will not "
                    f"retry - tap Sell again if you still want out.")
        else:
            text = (f"⚠️ PHONE SELL did nothing: no held position matches "
                    f"{c['symbol']} (issue #{c['id']}). Nothing was sold and the "
                    f"command is marked done.")
        alerts.enqueue(f"cmd-{c['id']}", text)
    except Exception:
        pass


def alert_unrun(err):
    """Queue one alert per command this poll could not run. NEVER raises.

    once=True per issue: the command stays un-done and is retried every 10
    minutes, and every retry that fails the same way lands here again."""
    try:
        for c in _POLL["todo"]:
            if c["id"] in _POLL["done"]:
                continue
            alerts.enqueue(
                f"cmd-unrun-{c['id']}",
                f"⚠️ Phone command did not complete: {_describe(c)} "
                f"(issue #{c['id']})\n"
                f"Error: {str(err)[:300]}\n"
                f"It is retried every 10 minutes until it is {MAX_AGE_H} h old, "
                f"then dropped. This alert is sent once per command.", once=True)
    except Exception:
        pass


def fetch_commands():
    req = urllib.request.Request(ISSUES_URL, headers={"User-Agent": "mps-vm"})
    with urllib.request.urlopen(req, timeout=30) as r:
        issues = json.load(r)
    out = []
    now = datetime.now(timezone.utc)
    for i in issues:
        m = CMD_RE.fullmatch(i.get("title", ""))
        if not m:
            continue
        login = (i.get("user") or {}).get("login")
        assoc = i.get("author_association")
        if login != OWNER or assoc != "OWNER":
            log(f"REJECTED command issue #{i.get('number')} {i.get('title')!r} "
                f"from {login!r} (author_association={assoc!r}) — not the repo owner")
            continue
        age_h = (now - datetime.strptime(i["created_at"], "%Y-%m-%dT%H:%M:%SZ")
                 .replace(tzinfo=timezone.utc)).total_seconds() / 3600
        if age_h > MAX_AGE_H:
            continue
        kind = "sell" if m.group(1) else ("refresh" if m.group(4) else "earmark")
        out.append({"id": i["number"], "kind": kind,
                    "symbol": (m.group(2) or "").upper(),
                    "qty": float(m.group(3)) if m.group(3) else None,
                    "amount": float(m.group(6)) if m.group(6) else None})
    return out


def main():
    cmds = fetch_commands()
    done = set(json.loads(DONE.read_text())) if DONE.exists() else set()
    todo = [c for c in cmds if c["id"] not in done]
    # OLDEST FIRST. The API is queried newest-first and EARMARK is last-write-
    # wins on a single file, so processing in arrival order let a typo overwrite
    # the correction sent to fix it: tap 200000, notice, tap 20000, and the file
    # ended at 200000 while the log printed the right value first. Issue numbers
    # are monotonic, so sorting by id is the operator's own order.
    todo.sort(key=lambda c: c["id"])
    _POLL["todo"], _POLL["done"] = todo, done
    if not todo:
        return
    log(f"{len(todo)} sell command(s) to execute")
    ib = IB()
    ib.connect(ib_bot.HOST, ib_bot.PORT, clientId=ib_bot.CLIENT_ID + 4, timeout=25)
    state = ib_bot.load_state()
    try:
        for c in todo:
            if c["kind"] == "earmark":
                # Write the number only. The CAP - min(marker, base cash held,
                # less the bot's own stamped HKD when that is known) - stays
                # where it already lives, in earmark.exclusion() via
                # ib_bot.net_liq() and the publishers, so a marker larger than
                # the balance cannot understate NetLiq and trip the kill switch
                # the way a stale one did on 2026-08-31. A typo therefore costs
                # nothing worse than excluding every base-currency dollar held
                # that is not the bot's own.
                amt = earmark.set_marker(c["amount"])
                log(f"issue #{c['id']}: earmark set to {amt:,.2f} {ib_bot.BASE_CCY}"
                    f" — excluded from NetLiq, position sizing and the dashboard")
                done.add(c["id"])
                DONE.write_text(json.dumps(sorted(done)))
                continue                     # publish at the end shows it
            if c["kind"] == "refresh":
                log(f"issue #{c['id']}: refresh — capturing live account state")
                done.add(c["id"])
                # Persist HERE too. This `continue` jumped over the only write,
                # which sits at the bottom of the sell branch, so a poll holding
                # only refreshes never recorded them: `done` died with the
                # process and the same issue was reprocessed every 10 minutes
                # until MAX_AGE_H expired it - about 288 times per tap.
                # Measured effect: from 2026-08-31 publish_state ran on nearly
                # every poll (126, 138, 142, 143, 140 commits/day against a
                # */10 maximum of 144, versus under 40/day before), holding an
                # IB brokerage session almost continuously. That is why the
                # hourly publish_web run kept losing ssodh/init with 410 Gone.
                DONE.write_text(json.dumps(sorted(done)))
                continue                     # publish at the end does the capture
            placed = False
            refusal = None                   # (qty, symbol, IB error) if IB refused
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
                trade = ib.placeOrder(p.contract, MarketOrder("SELL", qty))
                ib.sleep(2)
                # Read the verdict. This path used to record no status at all,
                # so a sell IB REJECTED looked identical to one it accepted -
                # the row rendered blank on the dashboard and the command was
                # marked done either way, leaving the operator believing a tap
                # had sold something when nothing had moved. The command still
                # gets marked done on a rejection (see the note below on
                # re-execution draining a position in slices), but the failure
                # is now visible instead of silent.
                status, err = ib_bot._order_verdict(trade)
                status = ib_bot._stock_status(status)
                ib_bot.PLACED.append({
                    "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                    "action": "SELL", "qty": qty, "symbol": p.contract.symbol,
                    "limit": "MKT", "ccy": p.contract.currency,
                    "reason": "sell button (your phone)",
                    "status": status, "error": err[:160]})
                if status == "REJECTED":
                    log(f"  !! SELL REJECTED: {qty} {p.contract.symbol} — {err[:140]}")
                    # A tuple and nothing else: this line sits between placeOrder
                    # and the DONE save, where nothing may raise.
                    refusal = (qty, p.contract.symbol, err)
                log(f"SELL {qty} {p.contract.symbol} {status} (issue #{c['id']})")
                placed = True
                break
            if not placed:
                log(f"issue #{c['id']}: no matching held position for {c['symbol']} — marked done")
            done.add(c["id"])
            # Persist BEFORE the next command. If a later command raises, the
            # orders already placed stay recorded; otherwise they re-execute on
            # every 10-minute poll for MAX_AGE_H, draining a position in slices.
            DONE.write_text(json.dumps(sorted(done)))
            # The alert goes AFTER that save, never between placeOrder and it:
            # anything raising in that window leaves the issue un-done and
            # re-executes the SELL on the next poll. alert_sell_outcome cannot
            # raise either, so a failed alert cannot skip the commands after
            # this one or the publish below.
            if refusal or not placed:
                alert_sell_outcome(c, refusal)
        ib_bot.publish_state(ib, state, ib_bot.net_liq(ib))
    finally:
        ib.disconnect()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"skipped ({e})")
        alert_unrun(e)
        sys.exit(0)
