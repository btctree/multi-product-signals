#!/usr/bin/env python3
"""Golden tests for the earmark: one shared definition of "not trading capital".

Run from this directory:  python test_earmark.py

THE FALLBACK RULE: exclusion = min(marker, base cash held) - the behaviour this
account has run for weeks, now in ONE module instead of four copies in ib_bot,
publish_web, daily_signal and ib_commands. It is still the rule whenever the
bot's stamped HKD pocket is not known (earmark.effective, and exclusion() with
pocket None); t1-t5 pin it unchanged.

The cap is load-bearing: on 2026-08-31 an 18,559 marker outlived its withdrawal,
NetLiq read 191,875 instead of 210,434, and the 8% kill switch tripped on a
drawdown that never happened.

t6 USED to pin the known limitation - a marker above the operator's own HKD
reaching into the bot's funding - "so nobody fixes it by accident". It was fixed
on purpose, on the operator's instruction (2026-09-17), by the stamped pocket:
the bot's own HKD is identified by the "mps-" order_ref IB echoes on its own
executions, not inferred from row shape (the fourth failed remedy). t6 now pins
BOTH halves deliberately: the limitation is closed when the pocket is known, and
the fallback still behaves exactly as before when it is not. The pocket's own
arithmetic and scenarios live in test_bot_pocket.py.
"""
import os
import pathlib
import tempfile

import earmark

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="mps-earmark-"))
earmark.MARKER_FILE = _TMP / "excluded_cash"
earmark.ANCHOR_FILE = _TMP / "earmark_anchor"          # never /root from a test
earmark.POCKET_FILE = _TMP / "earmark_pocket.json"


def reset(marker_text=None):
    os.environ.pop("EXCLUDED_CASH", None)
    if earmark.MARKER_FILE.exists():
        earmark.MARKER_FILE.unlink()
    if marker_text is not None:
        earmark.MARKER_FILE.write_text(marker_text)


def t1_marker_parsing():
    reset()
    assert earmark.marker() == 0.0                  # no file at all
    reset("23746\n")
    assert earmark.marker() == 23746.0
    reset("  18559.50  \n")
    assert earmark.marker() == 18559.5
    for junk in ("", "abc\n", "-5\n", "nan\n", "inf\n", "0\n"):
        reset(junk)
        assert earmark.marker() == 0.0, junk        # never a negative or NaN exclusion
    reset("100\n")
    os.environ["EXCLUDED_CASH"] = "250"
    assert earmark.marker() == 250.0                # env overrides the file
    os.environ.pop("EXCLUDED_CASH")
    print("t1 marker parsing OK")


def t2_capped_at_the_cash_held():
    reset("23746\n")
    assert earmark.effective(100000.0) == 23746.0   # plenty held: the whole marker
    assert earmark.effective(23746.0) == 23746.0    # exactly covered
    assert earmark.effective(10000.0) == 10000.0    # only 10k here: that is all there is
    assert earmark.effective(0.0) == 0.0            # money gone: retires itself
    assert earmark.effective(-500.0) == 0.0         # an HKD debit excludes nothing
    assert earmark.marker() == 23746.0              # the marker itself is never rewritten
    print("t2 capped at the cash held OK")


def t3_no_marker_excludes_nothing():
    reset()
    assert earmark.effective(50000.0) == 0.0
    reset("0\n")
    assert earmark.effective(50000.0) == 0.0
    print("t3 no marker excludes nothing OK")


def t4_set_marker_writes_and_sanitises():
    reset()
    assert earmark.set_marker(23746.5) == 23746.5
    assert earmark.marker() == 23746.5
    assert earmark.effective(30000.0) == 23746.5
    assert earmark.set_marker(0) == 0.0             # the phone's "clear"
    assert earmark.effective(30000.0) == 0.0
    for junk in (-5, "abc", None, float("nan"), float("inf")):
        assert earmark.set_marker(junk) == 0.0, junk
    print("t4 set_marker writes and sanitises OK")


def t5_the_operators_month():
    reset()
    earmark.set_marker(23746)
    assert earmark.effective(7.0) == 7.0            # marked before the GBP converts
    assert earmark.effective(23753.0) == 23746.0    # conversion lands: deposit excluded
    assert earmark.effective(38696.0) == 23746.0    # bot funds a SEHK entry: ITS money stays in
    assert earmark.effective(23753.0) == 23746.0    # order fills
    earmark.set_marker(0)                           # withdrawn, and cleared from the phone
    assert earmark.effective(7.0) == 0.0
    print("t5 the operator's month OK")


def t6_stale_marker_no_longer_reaches_into_a_known_bot_pocket():
    """REWRITTEN ON PURPOSE (was: t6_known_limitation_..., which pinned the bug).

    Whenever the marker exceeds the operator's own HKD, the plain cap reaches into
    whatever base cash is there - which may be the bot's funding. Two ordinary
    ways in: left set after the withdrawal, or set before the GBP converts.
    With the bot's stamped pocket P known, exclusion() takes P out of the HKD
    held before capping, so the bot's funding stays in the pool. Without it
    (stamping unconfirmed, no anchor, canary) effective() is still the rule, and
    the old numbers are pinned below so the fallback cannot drift either.
    """
    reset()
    earmark.set_marker(23746)
    # withdrawn, marker left set
    assert earmark.exclusion(7.0, 0.0) == 7.0
    # bot funds an entry (14,950 of the 14,957 is its own, stamped): stays in
    assert earmark.exclusion(14957.0, 14950.0) == 7.0
    # ...the fallback, unchanged: the old limitation, still what runs unconfirmed
    assert earmark.exclusion(14957.0, None) == 14957.0
    assert earmark.effective(14957.0) == 14957.0
    earmark.set_marker(0)                           # clearing it still works
    assert earmark.exclusion(14957.0, 14950.0) == 0.0
    assert earmark.effective(14957.0) == 0.0
    # ...and the other way in: marked before the GBP converts
    reset()
    earmark.set_marker(23746)
    assert earmark.exclusion(7.0, 0.0) == 7.0       # deposit not converted yet
    assert earmark.exclusion(14957.0, 14950.0) == 7.0     # bot funds an entry first: not excluded
    assert earmark.exclusion(38703.0, 14950.0) == 23746.0  # conversion lands: capped at the marker
    assert earmark.effective(38703.0) == 23746.0
    # The clamp: a pocket above the balance (drift) or below zero (an unstamped
    # sweep charged to it) can neither exclude more than is held nor go negative.
    assert earmark.exclusion(500.0, 90000.0) == 0.0
    assert earmark.exclusion(500.0, -300.0) == 500.0
    assert earmark.exclusion(-50.0, 100.0) == 0.0
    assert earmark.bot_share(500.0, 90000.0) == 500.0
    assert earmark.bot_share(500.0, -300.0) == 0.0
    assert earmark.bot_share(500.0, None) is None
    print("t6 stale marker no longer reaches into a known bot pocket; fallback pinned OK")


if __name__ == "__main__":
    t1_marker_parsing(); t2_capped_at_the_cash_held(); t3_no_marker_excludes_nothing()
    t4_set_marker_writes_and_sanitises(); t5_the_operators_month()
    t6_stale_marker_no_longer_reaches_into_a_known_bot_pocket()
    print("ALL EARMARK TESTS PASS")
