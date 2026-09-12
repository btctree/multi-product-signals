#!/usr/bin/env python3
"""Golden tests for the SEHK market rules: board lots and the spread table.

Run from this directory:  python test_hk_market.py

Both tables come from HKEX itself - data/hk_board_lots.json is built from the
List of Securities, and hk_tick() is the Second Schedule, Part A. IB is not the
authority on either: its sizeIncrement is an order-ticket step on some venues,
and its minTick is the lowest band of a tiered ladder, which is a legal price
increment only below HK$0.25.
"""
import os

os.environ.setdefault("IB_BACKEND", "web")
import ib_bot                                     # noqa: E402


class CD:
    def __init__(self, size=None, minSize=None):
        self.sizeIncrement, self.minSize = size, minSize


class FakeIB:
    def __init__(self, result=None):
        self.result, self.calls = result if result is not None else [], 0

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


def t1_board_lots_come_from_hkex():
    # Verified against HKEX's own List of Securities.
    for sym, lot in (("2359.HK", 100), ("2269.HK", 500), ("1810.HK", 200),
                     ("0700.HK", 100), ("9988.HK", 100)):
        assert ib_bot.hk_board_lot(sym) == lot, sym
    assert len(ib_bot._HK_LOTS) > 2500, len(ib_bot._HK_LOTS)
    # the code may arrive padded, unpadded, or bare
    assert ib_bot.hk_board_lot("2269") == 500
    assert ib_bot.hk_board_lot("02269.HK") == 500
    assert ib_bot.hk_board_lot("0000.HK") is None      # not a listed code
    assert ib_bot.hk_board_lot("nonsense") is None
    print("t1 board lots from HKEX OK")


def t2_hkex_beats_ib_when_they_disagree():
    # The case that motivated all of this: IB reporting its generic 100 for a
    # stock whose real lot is 500 would have sent a 300-share odd lot.
    fresh()
    ib = FakeIB([CD(100)])
    assert ib_bot.lot_size(ib, C("2269", "HKD", 1)) == 500
    fresh()
    # IB silent: HKEX still knows the answer, so the entry is NOT skipped
    assert ib_bot.lot_size(FakeIB(RuntimeError("no details")), C("2359", "HKD", 2)) == 100
    fresh()
    # a code HKEX does not list: unknown, and the caller skips
    assert ib_bot.lot_size(FakeIB([CD(100)]), C("0000", "HKD", 3)) == 0
    print("t2 HKEX overrides IB OK")


def t3_other_markets_untouched():
    fresh()
    assert ib_bot.lot_size(FakeIB(RuntimeError("x")), C("5301", "JPY", 4)) == 100
    fresh()
    assert ib_bot.lot_size(FakeIB([CD(200)]), C("6971", "JPY", 5)) == 200
    fresh()
    f = FakeIB([CD(100)])
    assert ib_bot.lot_size(f, C("DXCM", "USD", 6)) == 1 and f.calls == 0
    print("t3 JP and US lot rules unchanged OK")


def t4_spread_table_matches_the_second_schedule():
    # Second Schedule Part A, band by band, at each boundary and just above it.
    for price, tick in ((0.01, 0.001), (0.25, 0.001), (0.2501, 0.005),
                        (10.0, 0.005), (10.01, 0.01), (20.0, 0.01),
                        (20.01, 0.02), (50.0, 0.02), (50.01, 0.05),
                        (100.0, 0.05), (100.01, 0.1), (200.0, 0.1),
                        (200.01, 0.2), (500.0, 0.2), (500.01, 0.5),
                        (1000.0, 0.5), (1000.01, 1.0), (2000.0, 1.0),
                        (2000.01, 2.0), (5000.0, 2.0), (5000.01, 5.0),
                        (9995.0, 5.0), (9999.0, 5.0)):
        assert ib_bot.hk_tick(price) == tick, (price, ib_bot.hk_tick(price))
    print("t4 HKEX spread table OK")


def t5_live_limits_are_legal_prices():
    # What the bot would actually send for today's two HK signals. Before this,
    # the limit was snapped with IB's lowest band and 189.744 would have gone
    # out - not a legal increment anywhere near HK$188.
    for price, expect in ((188.8, 189.7), (47.0, 47.24)):
        raw = price * (1 + ib_bot.LIMIT_BUFFER)
        tick = max(0.001, ib_bot.hk_tick(raw))
        lim = ib_bot.snap_to_tick(raw, tick)
        assert abs(lim - expect) < 1e-9, (price, lim, expect)
        assert abs(round(lim / tick) - lim / tick) < 1e-6, (lim, tick)   # a legal multiple
        assert lim >= price, (lim, price)          # a BUY limit must not undercut the signal
    print("t5 live HK limit prices are legal OK")


def t6_digest_reads_the_same_table():
    # daily_signal keeps its own loader on purpose (it imports nothing from
    # ib_bot and runs under an interpreter with no broker library), so the FILE
    # has to be the shared source of truth. Check the two agree.
    import daily_signal
    for sym in ("2359.HK", "2269.HK", "1810.HK"):
        assert daily_signal.hk_board_lot(sym) == ib_bot.hk_board_lot(sym), sym
    assert daily_signal.hk_board_lot("0000.HK") is None
    print("t6 digest and bot read the same lots OK")


def t7_reits_are_in_the_table():
    # HKEX files REITs under their own Category, so a build that kept only
    # "Equity" dropped all eleven - including 0823.HK Link REIT, which is in
    # this product's live universe. The table is the sole authority for HKD, so
    # a missing code is a permanent refusal, not a fallback.
    assert ib_bot.hk_board_lot("0823.HK") == 100          # Link REIT
    assert ib_bot.hk_board_lot("0405.HK") == 1000         # Yuexiu REIT
    assert ib_bot.hk_board_lot("2778.HK") == 1000         # Champion REIT
    assert len(ib_bot._HK_LOTS) >= 2793, len(ib_bot._HK_LOTS)
    print("t7 REITs present OK")


def t8_place_sends_a_legal_hk_limit():
    # The hookup itself: deleting place()'s hk_tick line must fail a test.
    sent = {}

    class Trade:
        pass

    class IB:
        def reqContractDetails(self, c):
            return []                      # min_tick falls back to 0.01

        def placeOrder(self, contract, order):
            sent["lim"] = order.lmtPrice
            sent["qty"] = order.totalQuantity
            return Trade()

        def sleep(self, n):
            pass

    old = (ib_bot.live_base_price, ib_bot._order_verdict, ib_bot.CONFIRM_FIRST)
    try:
        ib_bot.live_base_price = lambda ib, c, fallback: fallback
        ib_bot._order_verdict = lambda t: ("Submitted", "")
        ib_bot.CONFIRM_FIRST = False
        for price, expect, tick in ((188.8, 189.7, 0.1), (47.0, 47.24, 0.02),
                                    (8.0, 8.04, 0.005)):
            fresh()
            sent.clear()
            ib_bot.place(IB(), C("2359", "HKD", 90), "BUY", 100, price, False)
            assert abs(sent["lim"] - expect) < 1e-9, (price, sent.get("lim"), expect)
            # and it is a legal SEHK increment for that price band
            assert abs(round(sent["lim"] / tick) - sent["lim"] / tick) < 1e-6, sent
        # a US order must be unaffected by any of this
        fresh()
        sent.clear()
        ib_bot.place(IB(), C("DXCM", "USD", 91), "BUY", 20, 83.03, False)
        assert abs(sent["lim"] - 83.45) < 1e-9, sent      # 83.03 * 1.005, tick 0.01
    finally:
        (ib_bot.live_base_price, ib_bot._order_verdict, ib_bot.CONFIRM_FIRST) = old
    print("t8 place() sends legal HK limits OK")


def t9_digest_replays_the_bot_rules():
    import daily_signal
    old = os.environ.get("HK_ENABLED")
    try:
        os.environ["HK_ENABLED"] = "0"
        lot, problem = daily_signal.entry_lot("2269.HK", "HKD")
        assert lot == 0 and "switched off" in problem, (lot, problem)
        os.environ["HK_ENABLED"] = "1"
        assert daily_signal.entry_lot("2269.HK", "HKD") == (500, None)
        assert daily_signal.entry_lot("0823.HK", "HKD") == (100, None)
        lot, problem = daily_signal.entry_lot("0000.HK", "HKD")
        assert lot == 0 and "no HKEX board lot" in problem, (lot, problem)
        # other markets keep the old behaviour
        assert daily_signal.entry_lot("4208.T", "JPY") == (100, None)
        assert daily_signal.entry_lot("DXCM", "USD") == (1, None)
    finally:
        if old is None:
            os.environ.pop("HK_ENABLED", None)
        else:
            os.environ["HK_ENABLED"] = old
    print("t9 digest replays the bot's HK rules OK")


if __name__ == "__main__":
    t1_board_lots_come_from_hkex(); t2_hkex_beats_ib_when_they_disagree()
    t3_other_markets_untouched(); t4_spread_table_matches_the_second_schedule()
    t5_live_limits_are_legal_prices(); t6_digest_reads_the_same_table()
    t7_reits_are_in_the_table(); t8_place_sends_a_legal_hk_limit()
    t9_digest_replays_the_bot_rules()
    print("ALL HK MARKET TESTS PASS")
