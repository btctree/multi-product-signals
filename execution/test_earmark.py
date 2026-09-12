#!/usr/bin/env python3
"""Golden tests for the earmark: the cap, and the ratchet that stops it re-arming.

Run from this directory:  python test_earmark.py

The rule: the exclusion is the smallest of the marker, the base cash held, and
the last effective value. It falls as the money leaves and NEVER rises again
until the operator sets a new marker.

Both halves have cost real money. Without the CAP, an 18,559 marker outlived its
withdrawal on 2026-08-31, NetLiq read 191,875 instead of 210,434 and the 8% kill
switch tripped on a drawdown that never happened. Without the RATCHET, the cap
climbs with the balance - and since 2026-09-12 the bot BUYS HKD to fund a SEHK
entry, so a stale marker would re-earmark that funding: NetLiq a full position
slot light, a red day and a green bounce in the calendar, smaller position sizes,
and the dashboard's unearmarked-cash warning silenced exactly when it should
speak.
"""
import json
import os
import pathlib
import tempfile

import earmark

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="mps-earmark-"))
earmark.MARKER_FILE = _TMP / "excluded_cash"
earmark.STATE_FILE = _TMP / "excluded_cash_state.json"


def reset(marker_text=None):
    os.environ.pop("EXCLUDED_CASH", None)
    for f in (earmark.MARKER_FILE, earmark.STATE_FILE):
        if f.exists():
            f.unlink()
    if marker_text is not None:
        earmark.MARKER_FILE.write_text(marker_text)


def t1_marker_parsing():
    reset()
    assert earmark.marker() == 0.0                  # no file at all
    reset("23746\n")
    assert earmark.marker() == 23746.0
    reset("0\n")
    assert earmark.marker() == 0.0
    reset("  18559.50  \n")
    assert earmark.marker() == 18559.5
    for junk in ("", "abc\n", "-5\n", "nan\n", "inf\n"):
        reset(junk)
        assert earmark.marker() == 0.0, junk        # never a negative or a NaN exclusion
    reset("100\n")
    os.environ["EXCLUDED_CASH"] = "250"
    assert earmark.marker() == 250.0                # env overrides the file
    os.environ.pop("EXCLUDED_CASH")
    print("t1 marker parsing OK")


def t2_capped_at_the_cash_held():
    reset("23746\n")
    assert earmark.effective(100000.0) == 23746.0   # plenty held: the whole marker
    reset("23746\n")
    assert earmark.effective(10000.0) == 10000.0    # only 10k held: that is all we can exclude
    reset("23746\n")
    assert earmark.effective(0.0) == 0.0            # money gone: retires itself
    reset("23746\n")
    assert earmark.effective(-500.0) == 0.0         # an HKD debit excludes nothing
    print("t2 capped at the cash held OK")


def t3_ratchets_down_and_never_back_up():
    # THE FIX. The deposit leaves, then the bot buys HKD to fund a SEHK entry.
    reset("23746\n")
    assert earmark.effective(23746.0) == 23746.0    # deposit sitting there
    assert earmark.effective(0.0) == 0.0            # withdrawn
    assert earmark.effective(14950.0) == 0.0        # bot buys HKD -> NOT re-earmarked
    assert earmark.effective(23746.0) == 0.0        # and not even at the old level
    # a partial retirement ratchets to the lower value and stays there
    reset("20000\n")
    assert earmark.effective(20000.0) == 20000.0
    assert earmark.effective(5000.0) == 5000.0
    assert earmark.effective(20000.0) == 5000.0
    print("t3 ratchets down, never back up OK")


def t4_a_new_marker_starts_a_fresh_ratchet():
    reset("20000\n")
    assert earmark.effective(20000.0) == 20000.0
    assert earmark.effective(0.0) == 0.0            # withdrawn, ratchet at 0
    earmark.set_marker(23746)                       # next month's deposit
    assert earmark.marker() == 23746.0
    assert earmark.effective(23746.0) == 23746.0    # the new deposit IS excluded
    # the same is true of a marker edited by hand rather than set from the phone
    earmark.effective(0.0)
    earmark.MARKER_FILE.write_text("31000\n")
    assert earmark.effective(31000.0) == 31000.0
    print("t4 a new marker starts a fresh ratchet OK")


def t5_set_marker_writes_both_and_sanitises():
    reset()
    assert earmark.set_marker(23746.5) == 23746.5
    assert earmark.marker() == 23746.5
    d = json.loads(earmark.STATE_FILE.read_text())
    assert d["marker"] == 23746.5 and d["effective"] == 23746.5
    assert earmark.set_marker(0) == 0.0
    assert earmark.marker() == 0.0
    assert earmark.effective(50000.0) == 0.0        # cleared means cleared
    for junk in (-5, "abc", None, float("nan")):
        assert earmark.set_marker(junk) == 0.0, junk
    print("t5 set_marker writes both files and sanitises OK")


def t6_missing_or_corrupt_state_is_survivable():
    reset("23746\n")
    earmark.effective(23746.0)
    earmark.STATE_FILE.write_text("{ this is not json")
    assert earmark.effective(23746.0) == 23746.0    # corrupt ratchet -> plain cap
    earmark.STATE_FILE.unlink()
    assert earmark.effective(10000.0) == 10000.0
    # a read-only state dir must not break a run
    earmark.STATE_FILE = pathlib.Path("/nonexistent-dir-xyz/state.json")
    assert earmark.effective(23746.0) == 23746.0
    earmark.STATE_FILE = _TMP / "excluded_cash_state.json"
    print("t6 missing or corrupt state survivable OK")


def t7_persist_false_leaves_no_trace():
    # Reporting paths (the digest, _spendable_base) must not move the ratchet.
    reset("20000\n")
    assert earmark.effective(5000.0, persist=False) == 5000.0
    assert not earmark.STATE_FILE.exists(), "a read must not write the ratchet"
    assert earmark.effective(20000.0) == 20000.0    # so the real value is untouched
    print("t7 persist=False leaves no trace OK")


if __name__ == "__main__":
    t1_marker_parsing(); t2_capped_at_the_cash_held()
    t3_ratchets_down_and_never_back_up(); t4_a_new_marker_starts_a_fresh_ratchet()
    t5_set_marker_writes_both_and_sanitises(); t6_missing_or_corrupt_state_is_survivable()
    t7_persist_false_leaves_no_trace()
    print("ALL EARMARK TESTS PASS")
