#!/usr/bin/env python3
"""The earmark: base-currency cash that is NOT trading capital.

One number, in the base currency, set from the phone (ib_commands' EARMARK
command) or by hand, and read by everything that reports the investable pool or
sizes against it: ib_bot's net_liq and publisher, publish_web, and the Telegram
digest. This module exists so those four agree BY CONSTRUCTION - they each
carried their own copy of the arithmetic, and the copies had already drifted.

The operator's routine is a GBP deposit at each month end, converted to HKD in
the account and withdrawn days later. That money is not investable, so it must
not inflate NetLiq: position sizing is NetLiq/15 and the 8% kill switch measures
drawdown against it, on a peak that is monotonic and cash-flow-naive.

THE RULE: exclusion = min(marker, base cash held).

The cap is what stops a forgotten marker understating NetLiq forever. It is not
hypothetical: on 2026-08-31 an 18,559 marker outlived its withdrawal, NetLiq read
191,875 instead of 210,434, and the kill switch tripped on a drawdown that never
happened.

KNOWN LIMITATION - clear the marker when the money leaves.
Since 2026-09-12 the base-currency balance has two sources: the operator's
pass-through money and HKD the bot buys to fund a SEHK entry. While the marker
covers only the operator's money that is really there, the two never collide -
the cap stops at the marker, so the bot's funding stays in the pool. But whenever
the marker EXCEEDS the operator's own HKD, the cap reaches into whatever HKD is
there, which may be the bot's, and NetLiq is understated by that much. That
happens in two ordinary ways: the marker is set BEFORE the GBP converts and the
bot funds a SEHK entry in that window, or it is left set AFTER the withdrawal.
The publishers report the raw marker as earmark_marker, and the dashboard warns
whenever it is above the HKD held.

Four designs tried to close that automatically and each introduced a worse bug:
capping re-earmarked the funding, a down-only ratchet latched on whichever
balance was sampled first, arming on coverage armed on the bot's own money, and
measuring the bot's HKD from the fills ledger counted the operator's own GBP->HKD
conversion as the bot's - fills_capture sweeps every execution on the account and
cannot tell who initiated one, so the operator's conversion and the bot's are the
same row shape. Verified against the real ledger: that last one returned 0
instead of 18,559 for the whole of last month's window.

The honest fix is for the bot to STAMP its own conversions rather than anyone
inferring them afterwards; until then the rule above is the one that has run this
account for weeks, and the dashboard control makes clearing the marker two taps
instead of an SSH session.
"""
import os
from pathlib import Path

# Overridable so tests never touch /root.
DIR = Path(os.environ.get("MPS_EARMARK_DIR", "/root"))
MARKER_FILE = DIR / "excluded_cash"


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
        return 0.0                                   # NaN, inf, 0, negative: nothing marked
    return v


def effective(base_cash, persist=None):
    """How much to exclude, given the base-currency cash held right now.

    `persist` is accepted and ignored - it exists so callers written against an
    earlier, stateful version keep working.
    """
    m = marker()
    if m <= 0:
        return 0.0
    return min(m, max(0.0, float(base_cash or 0.0)))


def set_marker(amount, today=None):
    """Set the marker. Returns what was written. `today` is accepted and ignored."""
    try:
        amt = max(0.0, float(amount or 0))
    except (TypeError, ValueError):
        amt = 0.0
    if amt != amt or amt in (float("inf"), float("-inf")):
        amt = 0.0
    MARKER_FILE.write_text("%.2f\n" % amt)
    return amt
