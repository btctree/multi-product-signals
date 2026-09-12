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
    """(marker, effective, armed) or (None, None, False) when there is no usable state.

    Every field must be present and numeric. A half-written or hand-edited object
    missing "effective" used to read as 0 and pin the exclusion at zero for good.
    """
    try:
        d = json.loads(STATE_FILE.read_text())
        if not isinstance(d, dict) or "marker" not in d or "effective" not in d:
            return (None, None, False)
        m, e = float(d["marker"]), float(d["effective"])
        if m != m or e != e:                          # NaN
            return (None, None, False)
        return (m, e, bool(d.get("armed")))
    except Exception:
        return (None, None, False)


def _write_state(m, e, armed):
    try:
        STATE_FILE.write_text(json.dumps({"marker": round(m, 2),
                                          "effective": round(e, 2),
                                          "armed": bool(armed)}))
    except Exception:
        pass                                          # advisory only - never fail a run over it


def _marker_is_newer_than_state():
    """True when the marker file was touched after the state was written.

    set_marker() resets the ratchet, but the marker can also be edited by hand
    (echo > /root/excluded_cash), and re-marking the SAME amount as last month
    would otherwise leave last month's retired floor in force and exclude
    nothing at all.
    """
    try:
        return MARKER_FILE.stat().st_mtime > STATE_FILE.stat().st_mtime + 1e-6
    except Exception:
        return False


def effective(base_cash, persist=True):
    """How much to exclude, given the base-currency cash held right now.

    min(marker, cash held) while the marked money has not yet all arrived, and
    from the moment it HAS arrived once, additionally floored by the ratchet:
    the exclusion can then only fall.

    The arming step matters because the deposit lands as GBP and is converted to
    HKD afterwards, while the marker is in HKD. Ratcheting from the first sample
    would latch on the pre-conversion balance - mark 14,500 while 7 HKD is held,
    let the hourly publisher observe it, and the exclusion would be pinned at 7
    for the rest of the month: the whole deposit counted as investable, fifteen
    oversized slots, and a kill-switch peak inflated by money that then leaves.
    """
    m = marker()
    if m <= 0:
        return 0.0
    cash = max(0.0, float(base_cash or 0.0))
    cap = min(m, cash)
    prev_m, prev_e, armed = _read_state()
    fresh = (prev_m is None or abs(prev_m - m) >= 0.005
             or _marker_is_newer_than_state())
    if fresh:
        armed, prev_e = False, None
    armed_before = armed
    if not armed and cash >= m - 0.005:
        armed = True                                  # the marked money is all here
    # The floor only counts if it was recorded while ARMED. Applying it on the
    # arming call itself would hand back the pre-conversion balance as the
    # ceiling - the very latch this guard exists to prevent.
    val = min(cap, max(0.0, prev_e)) if (armed_before and prev_e is not None) else cap
    if persist and (fresh or armed != bool(_read_state()[2])
                    or prev_e is None or abs(prev_e - val) >= 0.005):
        _write_state(m, val, armed)
    return val


def set_marker(amount):
    """Set the marker and START A FRESH RATCHET. Returns what was written."""
    try:
        amt = max(0.0, float(amount or 0))
    except (TypeError, ValueError):
        amt = 0.0
    if amt != amt or amt in (float("inf"), float("-inf")):
        amt = 0.0
    MARKER_FILE.write_text("%.2f\n" % amt)
    # armed=False: the money may not have arrived yet (the deposit lands as GBP
    # and is converted afterwards), and the ratchet must not latch on the
    # pre-conversion balance. effective() arms it once the marker is covered.
    _write_state(amt, amt, False)
    return amt
