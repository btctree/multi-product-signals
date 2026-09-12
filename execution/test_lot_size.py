#!/usr/bin/env python3
"""Golden tests for lot_size() - the exchange board-lot rule - and the entry block.

Run from this directory:  python test_lot_size.py

Hong Kong's own rules (lots from HKEX, the spread table, the live limit prices)
live in test_hk_market.py. What this file locks down:
  * a board-lot venue whose lot cannot be established makes lot_size return 0
    (UNKNOWN) so the caller SKIPS rather than sizing an odd lot;
  * Japan may fall back to 100 - TSE has been flat 100 shares since Oct 2018;
  * the US and Europe never consult IB at all, because its sizeIncrement is an
    order-ticket step there and reading it as a lot once rounded every US order
    to zero;
  * HK entries are switched off, and no other market is affected.
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


def t1_jp_lot_read_from_ib():
    fresh()
    assert ib_bot.lot_size(FakeIB([CD(100)]), C("5301", "JPY", 1)) == 100
    assert ib_bot.lot_size(FakeIB([CD(200)]), C("6971", "JPY", 2)) == 200
    fresh()
    assert ib_bot.lot_size(FakeIB([CD(None, 100)]), C("7733", "JPY", 3)) == 100
    print("t1 JP lot read from IB OK")


def t2_unknown_lot_reports_zero():
    # THE RULE: never guess on a board-lot venue. A HK code HKEX does not list
    # is unknown however confidently IB answers - and the caller skips it.
    fresh()
    assert ib_bot.lot_size(FakeIB([CD(100)]), C("0000", "HKD", 4)) == 0
    fresh()
    assert ib_bot.lot_size(FakeIB(RuntimeError("no details")), C("0000", "HKD", 5)) == 0
    print("t2 unknown lot reports 0 OK")


def t3_jp_fallback_unchanged():
    fresh()
    assert ib_bot.lot_size(FakeIB(RuntimeError("x")), C("5301", "JPY", 6)) == 100
    fresh()
    assert ib_bot.lot_size(FakeIB([CD(1)]), C("7733", "JPY", 7)) == 100
    fresh()
    assert ib_bot.lot_size(FakeIB([]), C("4208", "JPY", 8)) == 100
    print("t3 JP 100-share fallback unchanged OK")


def t4_us_never_consults_ib():
    fresh()
    f = FakeIB([CD(100)])                    # IB says 100 for every US stock - ignore it
    assert ib_bot.lot_size(f, C("DXCM", "USD", 9)) == 1
    assert f.calls == 0, "US path must not call reqContractDetails"
    fresh()
    assert ib_bot.lot_size(FakeIB([CD(100)]), C("MC", "EUR", 10)) == 1
    print("t4 US/EU single share unchanged OK")


def t5_known_lot_is_cached():
    fresh()
    c = C("5301", "JPY", 11)
    assert ib_bot.lot_size(FakeIB([CD(100)]), c) == 100
    f = FakeIB(RuntimeError("must not be called"))
    assert ib_bot.lot_size(f, c) == 100 and f.calls == 0
    # the HK table is authoritative and cached too
    fresh()
    c2 = C("2269", "HKD", 12)
    assert ib_bot.lot_size(FakeIB([CD(500)]), c2) == 500
    f2 = FakeIB(RuntimeError("must not be called"))
    assert ib_bot.lot_size(f2, c2) == 500 and f2.calls == 0
    print("t5 known lot cached OK")


def t6_unknown_is_not_cached():
    # A transient failure must not stick for the rest of the run.
    fresh()
    c = C("0000", "HKD", 13)
    assert ib_bot.lot_size(FakeIB(RuntimeError("transient")), c) == 0
    assert ("lot", 13) not in ib_bot._TICK_CACHE
    print("t6 unknown is not cached OK")


def t7_live_hk_signals_size_to_zero():
    # 2026-09-12: NetLiq 217,629 HKD / 15 slots = 14,508 per position, against
    # the REAL HKEX lots.
    per_pos = 217629.0 / 15
    for sym, price in (("2359.HK", 188.8), ("2269.HK", 47.0)):
        lot = ib_bot.hk_board_lot(sym)
        shares = int(per_pos / price)
        assert (shares // lot) * lot == 0, sym       # one lot exceeds the slot
        assert lot * price > per_pos, sym
    print("t7 both live HK signals size to zero OK")


def t8_hk_entries_are_blocked_for_now():
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
    t1_jp_lot_read_from_ib(); t2_unknown_lot_reports_zero()
    t3_jp_fallback_unchanged(); t4_us_never_consults_ib()
    t5_known_lot_is_cached(); t6_unknown_is_not_cached()
    t7_live_hk_signals_size_to_zero(); t8_hk_entries_are_blocked_for_now()
    print("ALL LOT TESTS PASS")
