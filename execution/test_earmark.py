#!/usr/bin/env python3
"""Golden tests for the earmark: the operator's money, measured not inferred.

Run from this directory:  python test_earmark.py

THE RULE: exclusion = min(marker, own cash), where own cash is the base-currency
balance less the base currency the BOT bought for itself and has not yet spent.
The bot's side is MEASURED from the fills ledger - the FX fill that bought it and
the stock fill that spent it are both recorded there.

Why it is not simply min(marker, balance): since Hong Kong went live the balance
has two sources, the operator's pass-through deposit and the bot's own funding,
and no rule that infers the split from the total can tell them apart. Capping
re-earmarked the bot's funding (NetLiq a slot light, a phantom drawdown against a
monotonic peak, and the funding path converting more USD); ratcheting latched on
whichever balance was sampled first - the 7 HKD residue before the GBP
conversion landed, or the bot's funding itself.

Both halves still matter. Without the cap, an 18,559 marker outlived its
withdrawal on 2026-08-31, NetLiq read 191,875 instead of 210,434 and the 8% kill
switch tripped on a drawdown that never happened.
"""
import json
import os
import pathlib
import tempfile

import earmark

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="mps-earmark-"))
earmark.MARKER_FILE = _TMP / "excluded_cash"
earmark.STATE_FILE = _TMP / "excluded_cash_state.json"
earmark.LEDGER = _TMP / "fills_ledger.jsonl"


def reset(marker_text=None):
    os.environ.pop("EXCLUDED_CASH", None)
    for f in (earmark.MARKER_FILE, earmark.STATE_FILE, earmark.LEDGER):
        if f.exists():
            f.unlink()
    if marker_text is not None:
        earmark.MARKER_FILE.write_text(marker_text)


def ledger(*rows):
    earmark.LEDGER.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def fx_in(day, hkd):                       # bot BOUGHT hkd (sold USD for it)
    return {"date": day, "sec_type": "CASH", "symbol": "USD", "side": "SLD",
            "qty": hkd / 7.84, "price": 7.84, "ccy": "HKD"}


def fx_out(day, hkd):                      # base given away (IB's own small sweeps)
    return {"date": day, "sec_type": "CASH", "symbol": "USD", "side": "BOT",
            "qty": hkd / 7.84, "price": 7.84, "ccy": "HKD"}


def stock_buy(day, hkd):                   # bot SPENT hkd on a SEHK entry
    return {"date": day, "sec_type": "STK", "symbol": "2269", "side": "BOT",
            "qty": 500, "price": hkd / 500.0, "ccy": "HKD"}


def t1_marker_parsing():
    reset()
    assert earmark.marker() == 0.0
    reset("23746\n")
    assert earmark.marker() == 23746.0
    for junk in ("", "abc\n", "-5\n", "nan\n", "inf\n", "0\n"):
        reset(junk)
        assert earmark.marker() == 0.0, junk
    reset("100\n")
    os.environ["EXCLUDED_CASH"] = "250"
    assert earmark.marker() == 250.0                # env overrides the file
    os.environ.pop("EXCLUDED_CASH")
    print("t1 marker parsing OK")


def t2_capped_at_the_operators_cash():
    reset()
    earmark.set_marker(23746, today="2026-09-30")
    assert earmark.effective(100000.0) == 23746.0   # plenty held: the whole marker
    assert earmark.effective(10000.0) == 10000.0    # only 10k here: that is all there is
    assert earmark.effective(0.0) == 0.0            # money gone: retires itself
    assert earmark.effective(-500.0) == 0.0         # an HKD debit excludes nothing
    assert earmark.marker() == 23746.0              # ...and the marker itself is untouched
    print("t2 capped at the operator's cash OK")


def t3_bot_funding_is_never_the_operators_money():
    # THE CASE THAT BROKE EVERY INFERRED RULE.
    reset()
    earmark.set_marker(14500, today="2026-09-30")
    ledger(fx_in("2026-09-30", 14936))              # bot buys HKD for a SEHK entry
    assert earmark.effective(14943.0) == 7.0        # only the 7 residue is the operator's
    ledger(fx_in("2026-09-30", 14936), stock_buy("2026-10-01", 14500))
    # 443 held = the bot's 436 of change + the operator's 7. Only the 7 is theirs.
    assert earmark.effective(443.0) == 7.0          # order fills, HKD spent
    # the deposit lands days later: it IS the operator's, and IS excluded
    assert earmark.effective(14943.0) == 14500.0
    print("t3 bot funding is never counted as the operator's OK")


def t4_the_full_month():
    reset()
    earmark.set_marker(23746, today="2026-09-30")
    assert earmark.effective(7.0) == 7.0            # marked before the GBP converts
    assert earmark.effective(23753.0) == 23746.0    # conversion lands
    ledger(fx_in("2026-10-01", 14950))              # bot funds a SEHK entry
    assert earmark.effective(38703.0) == 23746.0    # deposit still excluded, funding not
    ledger(fx_in("2026-10-01", 14950), stock_buy("2026-10-02", 14500))
    assert earmark.effective(24203.0) == 23746.0    # order fills
    assert earmark.effective(457.0) == 7.0          # withdrawn: 450 of the 457 is the bot's change
    ledger(fx_in("2026-10-01", 14950), stock_buy("2026-10-02", 14500),
           fx_in("2026-10-03", 14900))              # bot funds another entry
    assert earmark.effective(15357.0) == 7.0        # still only the 7 residue is theirs
    print("t4 a full month, step by step OK")


def t5_only_activity_since_the_marker_counts():
    reset()
    ledger(fx_in("2026-08-01", 20000))              # last month's funding, long spent
    earmark.set_marker(14500, today="2026-09-30")
    assert earmark.effective(14507.0) == 14500.0    # not attributed to the bot
    print("t5 only activity since the marker counts OK")


def t6_a_hand_set_marker_still_caps():
    # No set_at recorded -> nothing is measured -> the plain cap, the old rule.
    reset("14500\n")
    assert earmark.effective(14507.0) == 14500.0
    assert earmark.effective(7.0) == 7.0            # still retires itself
    print("t6 a hand-set marker still caps OK")


def t7_a_new_marker_starts_a_new_episode():
    reset()
    earmark.set_marker(20000, today="2026-08-31")
    ledger(fx_in("2026-09-01", 14900))
    assert earmark.effective(14907.0) == 7.0        # September: bot money
    earmark.set_marker(23746, today="2026-09-30")   # October's deposit
    assert earmark.effective(23753.0) == 23746.0    # September's FX no longer counts
    print("t7 a new marker starts a new episode OK")


def t8_survives_a_missing_or_broken_ledger():
    reset()
    earmark.set_marker(14500, today="2026-09-30")
    assert earmark.effective(14507.0) == 14500.0    # no ledger file at all
    earmark.LEDGER.write_text("{not json\n\n[]\n")
    assert earmark.effective(14507.0) == 14500.0
    ledger({"date": "2026-09-30", "sec_type": "CASH", "ccy": "HKD",
            "side": "SLD", "qty": "abc", "price": None})
    assert earmark.effective(14507.0) == 14500.0    # unparseable row ignored
    print("t8 survives a missing or broken ledger OK")


def t9_small_ib_sweeps_out_of_base_are_counted():
    # IB makes its own tiny conversions OUT of HKD; they reduce the bot's pile.
    reset()
    earmark.set_marker(14500, today="2026-09-30")
    ledger(fx_in("2026-09-30", 10000), fx_out("2026-09-30", 200))
    assert abs(earmark.effective(20000.0) - 10200.0) < 0.01   # 20000 - (10000-200)
    print("t9 sweeps out of base are counted OK")


def t10_set_marker_sanitises_and_clears():
    reset()
    assert earmark.set_marker(23746.5, today="2026-09-30") == 23746.5
    assert earmark.marker() == 23746.5
    d = json.loads(earmark.STATE_FILE.read_text())
    assert d["marker"] == 23746.5 and d["set_at"] == "2026-09-30"
    assert earmark.set_marker(0, today="2026-10-05") == 0.0
    assert earmark.effective(50000.0) == 0.0        # cleared means cleared
    for junk in (-5, "abc", None, float("nan")):
        assert earmark.set_marker(junk, today="2026-10-05") == 0.0, junk
    print("t10 set_marker sanitises and clears OK")


if __name__ == "__main__":
    t1_marker_parsing(); t2_capped_at_the_operators_cash()
    t3_bot_funding_is_never_the_operators_money(); t4_the_full_month()
    t5_only_activity_since_the_marker_counts(); t6_a_hand_set_marker_still_caps()
    t7_a_new_marker_starts_a_new_episode(); t8_survives_a_missing_or_broken_ledger()
    t9_small_ib_sweeps_out_of_base_are_counted(); t10_set_marker_sanitises_and_clears()
    print("ALL EARMARK TESTS PASS")
