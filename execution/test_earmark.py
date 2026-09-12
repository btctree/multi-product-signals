#!/usr/bin/env python3
"""Golden tests for the earmark: one shared definition of "not trading capital".

Run from this directory:  python test_earmark.py

THE RULE: exclusion = min(marker, base cash held) - the behaviour this account
has run for weeks, now in ONE module instead of four copies in ib_bot,
publish_web, daily_signal and ib_commands.

The cap is load-bearing: on 2026-08-31 an 18,559 marker outlived its withdrawal,
NetLiq read 191,875 instead of 210,434, and the 8% kill switch tripped on a
drawdown that never happened.

The known limitation is deliberate and documented in earmark.py: a marker left
set after a withdrawal reaches into whatever base cash is there, which since
Hong Kong went live may be HKD the bot bought to fund an entry. Four automatic
fixes for that each introduced a worse bug - the last one counted the operator's
own GBP->HKD conversion as the bot's, because fills_capture sweeps every
execution on the account and cannot tell who initiated one. t6 pins the
limitation so nobody "fixes" it by accident.
"""
import os
import pathlib
import tempfile

import earmark

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="mps-earmark-"))
earmark.MARKER_FILE = _TMP / "excluded_cash"


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


def t6_known_limitation_a_stale_marker_reaches_into_bot_cash():
    """PINNED, not a bug report. Do not "fix" this without reading earmark.py.

    A marker left set after the withdrawal caps against whatever base cash is
    there - which may be the bot's funding. Four automatic remedies each made
    something worse; the accepted answer is to clear the marker, which the
    dashboard control makes two taps.
    """
    reset()
    earmark.set_marker(23746)
    assert earmark.effective(7.0) == 7.0            # withdrawn, marker left set
    assert earmark.effective(14957.0) == 14957.0    # bot funds an entry: excluded too
    earmark.set_marker(0)                           # clearing it is the fix
    assert earmark.effective(14957.0) == 0.0
    print("t6 known limitation pinned OK")


if __name__ == "__main__":
    t1_marker_parsing(); t2_capped_at_the_cash_held(); t3_no_marker_excludes_nothing()
    t4_set_marker_writes_and_sanitises(); t5_the_operators_month()
    t6_known_limitation_a_stale_marker_reaches_into_bot_cash()
    print("ALL EARMARK TESTS PASS")
