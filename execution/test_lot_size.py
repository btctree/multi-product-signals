#!/usr/bin/env python3
"""Golden tests for lot_size() - the exchange board-lot rule.

Run from this directory:  python test_lot_size.py

The rule these lock down: a board-lot venue whose lot we cannot READ must make
lot_size return 0 (UNKNOWN) so the caller skips. Japan may fall back to 100
because TSE has been a flat 100 shares since October 2018. Hong Kong may not:
HKEX sets the lot per stock (2359 = 100, 2269 = 500, 1810 = 200, verified
against HKEX's own List of Securities), so a guess of 100 sizes 2269 at 300
shares - an odd lot, which SEHK's continuous market will not auto-match.
"""
import os

os.environ.setdefault("IB_BACKEND", "web")     # import without a live socket
import ib_bot                                   # noqa: E402


class CD:                                       # a fake contractDetails row
    def __init__(self, size=None, minSize=None):
        self.sizeIncrement, self.minSize = size, minSize


class FakeIB:
    def __init__(self, result):
        self.result, self.calls = result, 0

    def reqContractDetails(self, c):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class C:
    def __init__(self, symbol, currency, conId):
        self.symbol, self.currency, self.conId = symbol, currency, conId


def fresh():
    ib_bot._TICK_CACHE.clear()


def t1_hk_lot_from_ib():
    fresh()
    assert ib_bot.lot_size(FakeIB([CD(500)]), C("2269", "HKD", 1)) == 500
    assert ib_bot.lot_size(FakeIB([CD(100)]), C("2359", "HKD", 2)) == 100
    print("t1 HK lot read from IB OK")


def t2_hk_unknown_is_zero():
    # THE FIX: each of these returned 1 before, and sized an odd lot.
    fresh()
    assert ib_bot.lot_size(FakeIB(RuntimeError("no details")), C("2269", "HKD", 3)) == 0
    fresh()
    assert ib_bot.lot_size(FakeIB([]), C("2359", "HKD", 4)) == 0
    fresh()
    assert ib_bot.lot_size(FakeIB([CD(1)]), C("2359", "HKD", 5)) == 0
    fresh()
    assert ib_bot.lot_size(FakeIB([CD(None, None)]), C("2269", "HKD", 6)) == 0
    print("t2 HK unknown lot reports 0 OK")


def t3_jp_fallback_unchanged():
    fresh()
    assert ib_bot.lot_size(FakeIB(RuntimeError("x")), C("5301", "JPY", 7)) == 100
    fresh()
    assert ib_bot.lot_size(FakeIB([CD(1)]), C("7733", "JPY", 8)) == 100
    fresh()
    assert ib_bot.lot_size(FakeIB([CD(200)]), C("6971", "JPY", 9)) == 200
    print("t3 JP 100-share fallback unchanged OK")


def t4_us_never_consults_ib():
    # IB reports sizeIncrement 100 for US stocks too; reading it as a lot once
    # rounded every US order to zero. The US path must not call IB at all.
    fresh()
    f = FakeIB([CD(100)])
    assert ib_bot.lot_size(f, C("DXCM", "USD", 10)) == 1
    assert f.calls == 0, "US path must not call reqContractDetails"
    fresh()
    assert ib_bot.lot_size(FakeIB([CD(100)]), C("MC", "EUR", 11)) == 1
    print("t4 US/EU single share unchanged OK")


def t5_unknown_not_cached():
    fresh()
    c = C("2269", "HKD", 12)
    assert ib_bot.lot_size(FakeIB(RuntimeError("transient")), c) == 0
    assert ib_bot.lot_size(FakeIB([CD(500)]), c) == 500      # next call may succeed
    print("t5 unknown is not cached OK")


def t6_known_is_cached():
    fresh()
    c = C("2359", "HKD", 13)
    assert ib_bot.lot_size(FakeIB([CD(100)]), c) == 100
    f = FakeIB(RuntimeError("must not be called"))
    assert ib_bot.lot_size(f, c) == 100 and f.calls == 0
    print("t6 known lot cached OK")


def t7_live_hk_signals_size_to_zero():
    # 2026-09-12: NetLiq 217,629 HKD / 15 slots = 14,508 per position.
    per_pos = 217629.0 / 15
    for sym, price, lot in (("2359.HK", 188.8, 100), ("2269.HK", 47.0, 500)):
        shares = int(per_pos / price)
        assert (shares // lot) * lot == 0, sym       # one lot exceeds the slot
        assert lot * price > per_pos, sym
    # ...and with the old fallback of 1, both would have gone out as odd lots
    assert int(per_pos / 47.0) == 308 and 308 % 500 != 0
    assert int(per_pos / 188.8) == 76 and 76 % 100 != 0
    print("t7 both live HK signals size to zero OK")


def t8_hk_entries_are_blocked_for_now():
    # HK entries are off until the board lot comes from HKEX and HK limits snap
    # to the HKEX spread table. Every other market must be unaffected, and the
    # switch must lift it.
    from contracts import currency_of
    assert ib_bot.HK_ENABLED is False, "HK must ship switched OFF"
    for sym in ("2359.HK", "2269.HK", "0700.HK"):
        assert ib_bot.entry_blocked_reason(sym, currency_of(sym)), sym
    for sym in ("DXCM", "4208.T", "MC.PA", "BP.L", "ETH-USD"):
        assert ib_bot.entry_blocked_reason(sym, currency_of(sym)) is None, sym
    old = ib_bot.HK_ENABLED
    try:
        ib_bot.HK_ENABLED = True
        assert ib_bot.entry_blocked_reason("2269.HK", "HKD") is None
    finally:
        ib_bot.HK_ENABLED = old
    print("t8 HK entries blocked, other markets untouched OK")


if __name__ == "__main__":
    t1_hk_lot_from_ib(); t2_hk_unknown_is_zero(); t3_jp_fallback_unchanged()
    t4_us_never_consults_ib(); t5_unknown_not_cached(); t6_known_is_cached()
    t7_live_hk_signals_size_to_zero(); t8_hk_entries_are_blocked_for_now()
    print("ALL LOT TESTS PASS")
