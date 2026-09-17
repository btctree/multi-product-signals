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

Safety: only SELLs, only for existing long positions, qty capped at held qty
less every SELL already working at IB or sent earlier in the same poll, and
nothing is sent while the working orders cannot be read.
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


def alert_sell_already_working(c, working):
    """Queue the alert for a SELL that found nothing left to sell. NEVER raises.

    working is (qty already on its way out, qty held, ib_symbol, size_known).
    Called only AFTER the DONE save - see main()."""
    try:
        out, held, sym, known = working
        size = f"{out:g}" if known else "an unreported quantity"
        text = (f"⚠️ PHONE SELL not sent: a sell of {size} {sym} is already working "
                f"(issue #{c['id']}), against {held:g} held - nothing was left to "
                f"sell.\n"
                f"Nothing new was sent and the command is marked done. If that "
                f"order is cancelled or expires unfilled, tap Sell again.")
        alerts.enqueue(f"cmd-{c['id']}", text)
    except Exception:
        pass


def alert_orders_unread(c, err):
    """Queue the alert for a SELL held back because the working orders could
    not be read. NEVER raises. once=True: the command stays pending and meets
    the same failure every 10 minutes until it is read or expires."""
    try:
        alerts.enqueue(
            f"cmd-orders-{c['id']}",
            f"⚠️ PHONE SELL waiting: {_describe(c)} (issue #{c['id']}) was NOT "
            f"sent - IB's working orders could not be read, and a sell sized "
            f"without them can sell shares an order is already selling.\n"
            f"Error: {str(err)[:300]}\n"
            f"It is retried every 10 minutes until it is {MAX_AGE_H} h old, then "
            f"dropped. This alert is sent once per command.", once=True)
    except Exception:
        pass


def _conid(contract):
    try:
        return int(getattr(contract, "conId", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _same_contract(a, b):
    """By conId when both carry one, else by IB symbol (never two blanks)."""
    ca, cb = _conid(a), _conid(b)
    if ca and cb:
        return ca == cb
    sa = str(getattr(a, "symbol", "") or "").upper()
    return bool(sa) and sa == str(getattr(b, "symbol", "") or "").upper()


def working_sells(ib):
    """[(contract, qty)] for every stock SELL still working at IB; qty None when
    IB did not say how many. RAISES when the book cannot be read - the web shim
    refuses to report an empty book it has not actually seen, and so must we.

    Filtered on ib_bot._WORKING_STATUS exactly as ib_bot's open_syms is:
    openTrades() carries the day's filled and cancelled rows too, and those are
    history, not shares still on their way out."""
    ib.reqAllOpenOrders()
    ib.sleep(2)
    out = []
    for t in ib.openTrades():
        if getattr(t.orderStatus, "status", "") not in ib_bot._WORKING_STATUS:
            continue
        if getattr(t.contract, "secType", "") == "CASH":
            continue                          # a conversion, not shares
        if str(getattr(t.order, "action", "")).upper() not in ("SELL", "S", "SLD"):
            continue
        try:
            q = abs(float(t.order.totalQuantity))
        except (TypeError, ValueError, AttributeError):
            q = 0.0
        out.append((t.contract, q if q > 0 else None))
    return out


def _on_its_way_out(contract, held, book, sent):
    """(shares of this position already being sold, size_known).

    Counts SELLs working at IB plus SELLs this poll has already sent. A working
    SELL whose size IB did not report is taken as covering everything held:
    under-selling costs the operator one more tap, over-selling opens a short
    nothing ever closes."""
    total = 0.0
    for oc, q in list(book) + list(sent):
        if not _same_contract(oc, contract):
            continue
        if q is None:
            return float(held), False
        total += q
    return total, True


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
    # Working SELLs, read at most once per poll, and what this poll has sent.
    # See the SELL branch below for why both exist.
    book, book_err, sent = None, None, []
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
            # A working SELL does not reduce the position until it fills, and
            # this path used to size on positions alone. So a 23:35 bot exit
            # resting PreSubmitted until the open, plus a morning tap on the
            # dashboard's Sell button under its own red SELL alert, would send a
            # second full-size market SELL; so would two taps in one poll. Both
            # fill at the open and the margin account ends short - and nothing
            # ever closes a short, because every exit path acts only on qty > 0
            # (review 2026-09-17, reproduced against a fake IB: with SELL 4 DELL
            # already working against 4 held, two taps sent SELL 4 twice more).
            #
            # The book is read BEFORE positions, and only once. An order that
            # fills in between then counts twice - once working, once gone from
            # the position - which under-sells. The other order would read the
            # position before the fill and the book after it, and sell the same
            # shares again. `sent` covers this poll's own orders, which the book
            # read before them cannot show.
            if book is None and book_err is None:
                try:
                    book = working_sells(ib)
                except Exception as e:
                    book_err = e
            if book_err is not None:
                # Send nothing and leave the command pending: the next poll
                # reads the book again. Nothing was placed, so there is no
                # DONE-save ordering to protect here.
                log(f"issue #{c['id']}: working orders unreadable "
                    f"({str(book_err)[:120]}) — nothing sent, retried next poll")
                alert_orders_unread(c, book_err)
                continue
            placed = False
            refusal = None                   # (qty, symbol, IB error) if IB refused
            working = None                   # set when a working sell covers it all
            sending = None
            for p in ib.positions():
                if p.position <= 0 or getattr(p.contract, "secType", "") == "CASH":
                    continue
                ysym = state.get("map", {}).get(p.contract.symbol, p.contract.symbol)
                if c["symbol"] not in (p.contract.symbol.upper(), ysym.upper()):
                    continue
                out, known = _on_its_way_out(p.contract, p.position, book, sent)
                avail = p.position - out
                if out > 0 and int(avail) <= 0:
                    working = (out, p.position, p.contract.symbol, known)
                    break
                # "SELL: SYM" and "SELL: SYM 0" still mean everything - now
                # everything not already being sold.
                qty = int(min(c["qty"] or avail, avail))
                if qty <= 0:
                    continue
                if out > 0:
                    log(f"  {out:g} {p.contract.symbol} already being sold — "
                        f"selling {qty} of the {p.position:g} held")
                p.contract.exchange = p.contract.exchange or "SMART"
                ib.qualifyContracts(p.contract)
                sending = (p.contract, qty)
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
            if working:
                log(f"issue #{c['id']}: a sell of {working[0]:g} {working[2]} is "
                    f"already working — nothing sent, marked done")
            elif not placed:
                log(f"issue #{c['id']}: no matching held position for {c['symbol']} — marked done")
            done.add(c["id"])
            # Persist BEFORE the next command. If a later command raises, the
            # orders already placed stay recorded; otherwise they re-execute on
            # every 10-minute poll for MAX_AGE_H, draining a position in slices.
            DONE.write_text(json.dumps(sorted(done)))
            # Counted whatever IB's verdict: an order refused on a timed-out
            # POST may still exist at IB, and counting it can only under-sell.
            if placed:
                sent.append(sending)
            # The alert goes AFTER that save, never between placeOrder and it:
            # anything raising in that window leaves the issue un-done and
            # re-executes the SELL on the next poll. The alert helpers cannot
            # raise either, so a failed alert cannot skip the commands after
            # this one or the publish below.
            if working:
                alert_sell_already_working(c, working)
            elif refusal or not placed:
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
