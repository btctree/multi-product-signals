#!/usr/bin/env python3
"""The earmark: base-currency cash that is NOT trading capital.

One number, in the base currency, set from the phone (ib_commands' EARMARK
command) or by hand, and read by everything that reports the investable pool or
sizes against it: ib_bot's net_liq and publisher, publish_web, and the Telegram
digest. This module exists so those four agree BY CONSTRUCTION - they each
carried their own copy of the same arithmetic, and the copies drifted.

The operator's routine is a GBP deposit at each month end, converted to HKD and
withdrawn days later. That money sits in the account but is not investable, so
it must not inflate NetLiq: position sizing is NetLiq/15 and the kill switch
measures drawdown against it.

WHY AN EFFECTIVE VALUE IS STORED, NOT JUST CAPPED
The exclusion is capped at the base cash actually held, so a marker left set
after the money was withdrawn retires itself rather than understating NetLiq
forever. That cap is not hypothetical: on 2026-08-31 an 18,559 marker outlived
the withdrawal, NetLiq read 191,875 instead of 210,434, and the 8% kill switch
tripped on a drawdown that never happened, freezing entries.

But a cap that tracks the balance RISES as well as falls - and since 2026-09-12
the bot BUYS base currency to fund a SEHK entry, so the balance now rises for
reasons that have nothing to do with the operator's pass-through money. A rising
cap re-earmarks the capital the bot just converted: NetLiq drops by a full
position slot, the calendar books a red day and a green bounce when the order
fills, sizing shrinks, and the dashboard's "not earmarked" warning goes quiet
exactly when it should speak.

So the exclusion only ever ratchets DOWN. effective() is the smallest of the
marker, the base cash held, and the last effective value; it is persisted when
it falls, and reset the moment the marker itself changes. A stale marker still
retires itself - money leaving lowers the balance and the exclusion follows it
down for good - while base currency ARRIVING can never raise it again.

A new deposit therefore needs a new marker, which is what the dashboard control
is for, and what the unearmarked-cash warning on the Positions page nudges.
"""
import json
import os
from pathlib import Path

# Overridable so tests never touch /root.
DIR = Path(os.environ.get("MPS_EARMARK_DIR", "/root"))
MARKER_FILE = DIR / "excluded_cash"                 # the operator's number
STATE_FILE = DIR / "excluded_cash_state.json"       # {"marker": m, "effective": e}


def marker():
    """The raw number the operator set, or 0.0 if nothing is marked.

    EXCLUDED_CASH wins over the file so a one-off run can override it without
    touching the operator's state.
    """
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
        return 0.0                                   # NaN, inf, 0 and negatives: nothing marked
    return v


def _read_state():
    try:
        d = json.loads(STATE_FILE.read_text())
        return (float(d.get("marker", 0)), float(d.get("effective", 0)))
    except Exception:
        return (None, None)


def _write_state(m, e):
    try:
        STATE_FILE.write_text(json.dumps({"marker": round(m, 2),
                                          "effective": round(e, 2)}))
    except Exception:
        pass                                          # advisory only - never fail a run over it


def effective(base_cash, persist=True):
    """How much to exclude, given the base-currency cash held right now.

    min(marker, cash held, last effective). Ratchets down only; a change of
    marker starts a fresh ratchet.
    """
    m = marker()
    if m <= 0:
        return 0.0
    cap = min(m, max(0.0, float(base_cash or 0.0)))
    prev_m, prev_e = _read_state()
    if prev_m is not None and abs(prev_m - m) < 0.005 and prev_e is not None:
        cap = min(cap, max(0.0, prev_e))              # the ratchet
    if persist and (prev_m is None or abs(prev_m - m) >= 0.005
                    or prev_e is None or abs(prev_e - cap) >= 0.005):
        _write_state(m, cap)
    return cap


def set_marker(amount):
    """Set the marker and START A FRESH RATCHET. Returns what was written."""
    try:
        amt = max(0.0, float(amount or 0))
    except (TypeError, ValueError):
        amt = 0.0
    if amt != amt or amt in (float("inf"), float("-inf")):
        amt = 0.0
    MARKER_FILE.write_text("%.2f\n" % amt)
    _write_state(amt, amt)
    return amt
