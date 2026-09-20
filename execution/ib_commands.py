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
nothing is sent while the working orders cannot be read, or the positions
cannot be read fresh. Both are read once per poll, book first, before any send.
Each command is a deliberate button press by the account owner.
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import alerts
import broker
import earmark
import ib_bot
from broker import IB, MarketOrder

# Overridable so tests never touch /root (the only hard-coded /root path left
# in this module - board review 2026-09-17, test isolation). Same default.
DONE = Path(os.environ.get("MPS_COMMANDS_DONE", "/root/commands_done.json"))
MAX_AGE_H = 48
# The repo is PUBLIC and issues are open to anyone, so the issue author is the
# only thing separating a stranger from a market SELL of a full position.
OWNER = "btctree"
# creator= is load-bearing, not a convenience. Issues on a PUBLIC repo can be
# opened by anyone and this reads the 30 NEWEST, so filtering by author only
# AFTER fetching meant a stranger opening 30 issues pushed the owner's SELL
# clean out of the window: the poller would never see it, log nothing unusual,
# and silently do nothing for the 48 hours until the command aged out. A denial
# of service on the emergency exit, needing no forged identity. GitHub now does
# the filtering, so the 30 are the owner's 30.
# Built from OWNER so the two can never drift apart, and the
# author_association check in fetch_commands STAYS: if this parameter were ever
# ignored or dropped, correctness must not depend on it.
ISSUES_URL = ("https://api.github.com/repos/btctree/multi-product-signals/"
              "issues?state=all&per_page=100&sort=created&direction=desc"
              "&creator=" + OWNER)
# ONE PAGE IS NOT A WINDOW, and creator= does not make it one. It keeps
# STRANGERS out, but the operator fills the page himself: every tap of Refresh
# and every pull-to-refresh on a token-bearing device opens an owner-authored
# REFRESH issue, and the live repo's last 100 owner issues are ALL "REFRESH" -
# 38 of them inside a single rolling 48 h window, against a 30-item page.
# A SELL held pending - because the book could not be read, or positions could
# not be flushed - has to survive MAX_AGE_H in that window. Pushed off the page
# it is never fetched, so it is never executed, never aged out, never added to
# DONE, and NEVER ALERTED: alert_orders_unread / alert_unrun can only fire for
# a command that is in the fetch. Silence, while the poll looks healthy.
# So read until the page runs OLDER than MAX_AGE_H, not until it is full.
MAX_PAGES = 5            # 500 owner issues; 5 GETs a poll stays inside 60/h anon
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

    working is (qty already on its way out, qty held, ib_symbol, size_known,
    refused) - refused is [(qty, issue_id)], this poll's own earlier sends of
    the same contract that IB REFUSED. Called only AFTER the DONE save - see
    main().

    Those refused sends still count toward what is on its way out (see the
    `sent` note in main()), but they are not a working order. When the book
    and the accepted sends alone would have left shares to sell, "a sell is
    already working" was false, and "wait until it is cancelled or expires" was
    wrong advice for a refusal that was final (review 2026-09-17, "Phone SELL
    netting against a same-poll order IB refused produces a false 'a sell is
    already working' alert"). The quantities withheld stay exactly the same;
    only the words change."""
    try:
        out, held, sym, known = working[:4]
        refused = list(working[4]) if len(working) > 4 else []
        refused_qty = sum(q for q, _ in refused)
        live = out - refused_qty                 # book + this poll's accepted sends
        if known and refused and int(held - live) > 0:
            issues = ", ".join(f"#{i}" for _, i in refused)
            tried = " + ".join(f"{q:g}" for q, _ in refused)
            if live > 0:
                lead = (f"a sell of {live:g} {sym} is working at IB, and an earlier "
                        f"tap in this poll (issue {issues}) tried to SELL {tried} "
                        f"{sym} and was refused by IB")
            else:
                lead = (f"an earlier tap in this poll (issue {issues}) tried to "
                        f"SELL {tried} {sym} and was refused by IB")
            text = (f"⚠️ PHONE SELL not sent (issue #{c['id']}): {lead} - against "
                    f"{held:g} held, that left nothing to sell.\n"
                    f"Nothing new was sent and the command is marked done. A "
                    f"refusal usually means no order exists, but one refused on a "
                    f"timed-out request may still exist at IB. Check IB's orders "
                    f"for {sym} before tapping Sell again, and tap again only if "
                    f"the refused order is not there.")
        else:
            size = f"{out:g}" if known else "an unreported quantity"
            text = (f"⚠️ PHONE SELL not sent: a sell of {size} {sym} is already "
                    f"working (issue #{c['id']}), against {held:g} held - nothing "
                    f"was left to sell.\n"
                    f"Nothing new was sent and the command is marked done. If that "
                    f"order is cancelled or expires unfilled, tap Sell again.")
        alerts.enqueue(f"cmd-{c['id']}", text)
    except Exception:
        pass


def alert_positions_unread(c, err):
    """Queue the alert for a SELL held back because the positions could not be
    read fresh. NEVER raises. once=True, as alert_orders_unread: the command
    stays pending and meets the same failure every 10 minutes."""
    try:
        alerts.enqueue(
            f"cmd-positions-{c['id']}",
            f"⚠️ PHONE SELL waiting: {_describe(c)} (issue #{c['id']}) was NOT "
            f"sent - IB's positions could not be refreshed, and a sell sized on "
            f"IB's cached positions can sell shares an order has already sold.\n"
            f"Error: {str(err)[:300]}\n"
            f"It is retried every 10 minutes until it is {MAX_AGE_H} h old, then "
            f"dropped. This alert is sent once per command.", once=True)
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
    history, not shares still on their way out.

    qty is the WHOLE order size, filled part included - the larger of the web
    shim's totalSize and totalQuantity (what is left to fill there; ib_async's
    totalQuantity is already the whole size). Counting only what was left let
    a partial fill sell twice: an exit of 100 with 40 filled showed 60 working,
    a positions read still at 100 left 40 "available", and the phone SELL of 40
    ended the account short 40 (review 2026-09-17). Counting the whole order
    over-counts the filled part instead, which can only under-sell."""
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
        q = 0.0
        for field in ("totalSize", "totalQuantity"):
            try:
                q = max(q, abs(float(getattr(t.order, field))))
            except (TypeError, ValueError, AttributeError):
                pass
        out.append((t.contract, q if q > 0 else None))
    return out


def fresh_positions(ib):
    """Positions to size a phone SELL on, read live. RAISES when they cannot be.

    The web API serves portfolio/<acct>/positions from a backend cache. The
    netting below relies on the positions read being NEWER than the book read:
    an order that fills in between then counts twice, which under-sells. A
    cached read breaks that. Every market open lands on a */10 poll, so a 23:35
    market-at-open exit that fills at 00:00:00 drops out of the 00:00 poll's
    book as Filled while cached positions still show the shares - and the phone
    SELL sold them a second time, leaving a short nothing ever closes (review
    2026-09-17, "Phone SELL netting counts on a cached positions read being
    newer than the order book read"). So the web path flushes the cache first
    and raises if it cannot; ib_async's positions are pushed live by the socket
    and have nothing to flush."""
    if broker.BACKEND != "web":
        return list(ib.positions())
    return list(ib.positions(fresh=True))


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


def _created(i):
    """The issue's creation time, as an aware UTC datetime."""
    return (datetime.strptime(i["created_at"], "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc))


def fetch_commands():
    req = urllib.request.Request(ISSUES_URL, headers={"User-Agent": "mps-vm"})
    with urllib.request.urlopen(req, timeout=30) as r:
        issues = json.load(r)
    # Keep paging while the oldest issue seen is still inside the window a
    # pending command must survive.
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_H)
    page = 1
    while (issues and len(issues) % 100 == 0 and page < MAX_PAGES
           and _created(issues[-1]) >= cutoff):
        page += 1
        req = urllib.request.Request("%s&page=%d" % (ISSUES_URL, page),
                                     headers={"User-Agent": "mps-vm"})
        with urllib.request.urlopen(req, timeout=30) as r:
            more = json.load(r)
        if not more:
            break
        issues += more
    if (issues and page >= MAX_PAGES and _created(issues[-1]) >= cutoff):
        log("WARNING: %d owner issues and the oldest is still inside %dh - a "
            "pending command may sit beyond the window" % (len(issues), MAX_AGE_H))
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
        age_h = (now - _created(i)).total_seconds() / 3600
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
    # Working SELLs and positions, each read at most once per poll, and what
    # this poll has sent. See the SELL branch below for why all three exist.
    # `refused` is the part of `sent` IB refused, as (contract, qty, issue id):
    # netting counts it all the same, but only the alert text may treat it
    # differently.
    book, book_err, sent, refused = None, None, [], []
    held, held_err = None, None
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
            #
            # That only holds if the positions read is live, and IB serves it
            # from a cache - so it is read through fresh_positions(), which
            # flushes the cache first, and a flush that fails sends nothing
            # (review 2026-09-17: a filled open exit, gone from the book, was
            # sold again off a cached position).
            #
            # Positions are read ONCE per poll too, right after the book and
            # before this poll sends anything, so every entry in `sent` postdates
            # the snapshot and is subtracted exactly once. A fresh read per
            # command double-counted a same-poll send that had already filled:
            # 100 held, SELL 60 filled at once, the next read showed 40, `sent`
            # took the 60 off again, and SELL 40 was refused as "already
            # working" with nothing working (final review 2026-09-17). A book
            # order that fills after the snapshot is still counted once: the
            # snapshot still holds its shares.
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
            if held is None and held_err is None:
                try:
                    held = fresh_positions(ib)
                except Exception as e:
                    held_err = e
            if held_err is not None:
                # Exactly the unreadable-book path: nothing placed, nothing
                # marked done, the next poll tries again with a fresh book.
                log(f"issue #{c['id']}: positions could not be read fresh "
                    f"({str(held_err)[:120]}) — nothing sent, retried next poll")
                alert_positions_unread(c, held_err)
                continue
            placed = False
            refusal = None                   # (qty, symbol, IB error) if IB refused
            working = None                   # set when a working sell covers it all
            sending = None
            for p in held:
                if p.position <= 0 or getattr(p.contract, "secType", "") == "CASH":
                    continue
                ysym = state.get("map", {}).get(p.contract.symbol, p.contract.symbol)
                if c["symbol"] not in (p.contract.symbol.upper(), ysym.upper()):
                    continue
                out, known = _on_its_way_out(p.contract, p.position, book, sent)
                avail = p.position - out
                if out > 0 and int(avail) <= 0:
                    working = (out, p.position, p.contract.symbol, known,
                               [(q, i) for oc, q, i in refused
                                if _same_contract(oc, p.contract)])
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
                # "on its way out", not "working": part of it may be a send IB
                # refused earlier in this poll (the alert below tells which).
                log(f"issue #{c['id']}: a sell of {working[0]:g} {working[2]} is "
                    f"already on its way out (working at IB or sent earlier this "
                    f"poll) — nothing sent, marked done")
            elif not placed:
                log(f"issue #{c['id']}: no matching held position for {c['symbol']} — marked done")
            done.add(c["id"])
            # Persist BEFORE the next command. If a later command raises, the
            # orders already placed stay recorded; otherwise they re-execute on
            # every 10-minute poll for MAX_AGE_H, draining a position in slices.
            DONE.write_text(json.dumps(sorted(done)))
            # Counted whatever IB's verdict: an order refused on a timed-out
            # POST may still exist at IB, and counting it can only under-sell.
            # The verdict is kept beside it for the alert text alone.
            if placed:
                sent.append(sending)
                if refusal:
                    refused.append((sending[0], sending[1], c["id"]))
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
