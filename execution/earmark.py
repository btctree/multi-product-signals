#!/usr/bin/env python3
"""The earmark: base-currency cash that is NOT trading capital.

One number, in the base currency, set from the phone (ib_commands' EARMARK
command) or by hand, and read by everything that reports the investable pool or
sizes against it: ib_bot's net_liq and publisher, publish_web, and the Telegram
digest. This module exists so those four agree BY CONSTRUCTION - they each
carried their own copy of the arithmetic, and the copies drifted.

The operator's routine is a GBP deposit at each month end, converted to HKD in
the account and withdrawn days later. That money is not investable, so it must
not inflate NetLiq: position sizing is NetLiq/15 and the 8% kill switch measures
drawdown against it, on a peak that is monotonic and cash-flow-naive.

WHY THE EXCLUSION IS NOT SIMPLY min(marker, base cash held)
That was the rule until 2026-09-12, and the cap half of it is necessary: a
marker left set after the money was withdrawn understates NetLiq forever. It did
exactly that on 2026-08-31 - an 18,559 marker outlived its withdrawal, NetLiq
read 191,875 instead of 210,434, and the kill switch tripped on a drawdown that
never happened.

What broke it is that the base-currency balance stopped having ONE meaning. Since
Hong Kong went live the bot BUYS HKD to fund a SEHK entry, so the balance is now
the operator's pass-through money PLUS the bot's own funding, and a cap taken
against the total cannot tell them apart. Every rule that tried to infer the
split from the balance alone failed somewhere: capping re-earmarked the bot's
funding, and ratcheting latched on whichever balance happened to be sampled
first - the residue before the GBP conversion landed, or the funding itself.

So this does not infer. Both sides of the bot's HKD are RECORDED: the FX fill
that bought it and the stock fill that spent it are rows in
data/fills_ledger.jsonl, which every one of these programs can read. The bot's
own base currency is therefore measured, not guessed:

    own cash  = base cash held - (base bought by the bot - base it has spent)
    exclusion = min(marker, own cash)

A stale marker still retires itself, because the operator's money leaving lowers
own cash. The bot's funding is never excluded, because it was never counted as
the operator's. And there is no ratchet, no arming flag and no history to latch
on: the answer depends only on the marker, the balance and the ledger, so all
four programs compute the same number at any moment.
"""
import json
import os
from pathlib import Path

# Overridable so tests never touch /root.
DIR = Path(os.environ.get("MPS_EARMARK_DIR", "/root"))
MARKER_FILE = DIR / "excluded_cash"                 # the operator's number
STATE_FILE = DIR / "excluded_cash_state.json"       # {"marker": m, "set_at": "YYYY-MM-DD"}

# The ledger lives beside the code, in the repo the VM checks out.
LEDGER = Path(__file__).resolve().parent.parent / "data" / "fills_ledger.jsonl"
BASE_CCY = os.environ.get("IB_BASE_CCY", "HKD")


def marker():
    """The raw number the operator set, or 0.0 if nothing is marked."""
    raw = os.environ.get("EXCLUDED_CASH")
    if raw is None:
        try:
            raw = MARKER_FILE.read_text().strip()
        except Exception:
            return 0.0
    try:
        v = float(str(raw).strip() or 0)
    except (TypeError, ValueError):
        return 0.0
    if v != v or v in (float("inf"), float("-inf")) or v <= 0:
        return 0.0                                   # NaN, inf, 0, negative: nothing marked
    return v


def _state():
    try:
        d = json.loads(STATE_FILE.read_text())
        if not isinstance(d, dict):
            return {}
        return d
    except Exception:
        return {}


def set_marker(amount, today=None):
    """Set the marker, stamped with the day it was set. Returns what was written.

    The stamp is what makes the bot's own base currency measurable: only FX and
    fills FROM THAT DAY ON belong to the episode being marked.
    """
    try:
        amt = max(0.0, float(amount or 0))
    except (TypeError, ValueError):
        amt = 0.0
    if amt != amt or amt in (float("inf"), float("-inf")):
        amt = 0.0
    MARKER_FILE.write_text("%.2f\n" % amt)
    day = today or __import__("datetime").date.today().isoformat()
    try:
        STATE_FILE.write_text(json.dumps({"marker": round(amt, 2), "set_at": day}))
    except Exception:
        pass                                          # advisory - never fail a run over it
    return amt


def _set_at():
    """The day the current marker was set, or None when it is not recorded.

    None means "no start date", and bot_base() then measures nothing - the
    exclusion falls back to the plain cap, which is the pre-2026-09-12 rule.
    """
    d = _state()
    m = marker()
    try:
        if abs(float(d.get("marker", -1)) - m) < 0.005:
            s = d.get("set_at")
            return s if isinstance(s, str) and len(s) >= 10 else None
    except (TypeError, ValueError):
        pass
    return None


def bot_base(since, ledger=None):
    """Base currency the BOT bought and has not yet spent, on or after `since`.

    Measured from the fills ledger, both sides:
      + an FX fill that ACQUIRED base currency  (CASH row priced in base, sold)
      - an FX fill that gave base currency away (CASH row priced in base, bought)
      - a stock bought in base currency         (STK row priced in base, bought)

    Never negative: the bot cannot hold less than none of its own money.
    """
    if not since:
        return 0.0
    path = Path(ledger) if ledger else LEDGER
    net = 0.0
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                when = str(r.get("ts") or r.get("date") or "")[:10]
                if not when or when < since:
                    continue
                if str(r.get("ccy") or "").upper() != BASE_CCY:
                    continue
                try:
                    amt = float(r.get("qty") or 0) * float(r.get("price") or 0)
                except (TypeError, ValueError):
                    continue
                side = str(r.get("side") or "").upper()
                sec = str(r.get("sec_type") or "").upper()
                if sec == "CASH":
                    # Priced in base: SLD gave the other currency away and
                    # RECEIVED base; BOT paid base away.
                    net += amt if side == "SLD" else -amt
                elif side == "BOT":
                    net -= amt                        # spent base on a stock
    except Exception:
        return 0.0                                    # unreadable ledger: measure nothing
    return max(0.0, net)


def effective(base_cash, persist=None, ledger=None):
    """How much to exclude, given the base-currency cash held right now.

    min(marker, the operator's OWN base cash) - the balance less whatever the
    bot bought for itself and has not spent. `persist` is accepted and ignored:
    this is a pure function of the marker, the balance and the ledger, so there
    is no state for a caller to move.
    """
    m = marker()
    if m <= 0:
        return 0.0
    cash = max(0.0, float(base_cash or 0.0))
    own = max(0.0, cash - bot_base(_set_at(), ledger))
    return min(m, own)
