#!/usr/bin/env python3
"""Golden tests for funding an order denominated in the BASE currency (HKD).

Run from this directory:  python test_hkd_funding.py

The mandate these lock down:
  * the bot NEVER sells HKD - not as an FX source, not as an order side;
  * the bot MAY buy HKD, because an HK stock settles in HKD and IB would
    otherwise book a negative HKD balance and charge margin interest on it;
  * it buys only the SHORTFALL for the order at hand, plus BASE_FUND_BUFFER
    (3%), using HKD already held first.
"""
import os

os.environ.setdefault("IB_BACKEND", "web")
import ib_bot                                     # noqa: E402

BASE = ib_bot.BASE_CCY                            # "HKD"


class FakeIB:
    def __init__(self, qualify_raises=False):
        self.qualify_calls = 0
        self.qualify_raises = qualify_raises

    def qualifyContracts(self, *a):
        self.qualify_calls += 1
        if self.qualify_raises:
            raise AssertionError("reached order construction")
        return []


class Patch:
    """Swap module attributes for the duration of a block."""

    def __init__(self, **kw):
        self.kw, self.old = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(ib_bot, k)
            setattr(ib_bot, k, v)
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            setattr(ib_bot, k, v)
        ib_bot._FX_COMMITTED.clear()
        ib_bot._FX_PENDING_CCY.clear()
        ib_bot._EARMARK_RUN.clear()


def t1_spendable_base_respects_earmark_and_commitments():
    with Patch(cash_by_ccy=lambda ib: {BASE: 20000.0, "USD": 500.0},
               _excluded_cash=lambda: 0.0):
        assert ib_bot._spendable_base(None) == 20000.0
    with Patch(cash_by_ccy=lambda ib: {BASE: 20000.0},
               _excluded_cash=lambda: 5000.0):
        assert ib_bot._spendable_base(None) == 15000.0      # earmark is not trading capital
    with Patch(cash_by_ccy=lambda ib: {BASE: 20000.0},
               _excluded_cash=lambda: 0.0):
        ib_bot._FX_COMMITTED[BASE] = 3000.0
        assert ib_bot._spendable_base(None) == 17000.0      # another order claimed it
        ib_bot._FX_COMMITTED.clear()
    with Patch(cash_by_ccy=lambda ib: {BASE: 20000.0},
               _excluded_cash=lambda: 30000.0):             # stale marker, money gone
        assert ib_bot._spendable_base(None) == 0.0          # capped, never negative
    print("t1 spendable base cash OK")


def t2_enough_base_cash_places_without_converting():
    calls = []
    with Patch(cash_by_ccy=lambda ib: {BASE: 20000.0},
               _excluded_cash=lambda: 0.0,
               fund_from_nonbase=lambda *a, **k: calls.append((a, k)) or True):
        assert ib_bot.ensure_ccy(None, BASE, 14508.0, False) is True
    assert calls == [], "must not convert when the cash is already there"
    print("t2 sufficient HKD converts nothing OK")


def t3_short_base_cash_buys_only_the_shortfall_plus_3pct():
    calls = []

    def rec(ib, ccy, short, dry, buffer=1.02):
        calls.append({"ccy": ccy, "short": short, "buffer": buffer})
        return True

    with Patch(cash_by_ccy=lambda ib: {BASE: 7.0, "USD": 4558.0},
               _excluded_cash=lambda: 0.0, fund_from_nonbase=rec):
        assert ib_bot.ensure_ccy(None, BASE, 14100.0, False) is True
    assert len(calls) == 1, calls
    c = calls[0]
    assert c["ccy"] == BASE
    assert abs(c["short"] - (14100.0 - 7.0)) < 1e-9, c        # the shortfall, no more
    assert c["buffer"] == 1.03, c                             # operator's 3%
    assert abs(ib_bot.BASE_FUND_BUFFER - 1.03) < 1e-9
    print("t3 shortfall only, 3%% buffer OK")


def t4_failed_conversion_skips_the_order():
    with Patch(cash_by_ccy=lambda ib: {BASE: 7.0},
               _excluded_cash=lambda: 0.0,
               fund_from_nonbase=lambda *a, **k: False):
        assert ib_bot.ensure_ccy(None, BASE, 14100.0, False) is False
    print("t4 unfunded order is skipped OK")


def t5_never_sells_base_as_an_fx_source():
    # _fx_order_pair: BASE as SOURCE is refused outright...
    assert ib_bot._fx_order_pair(None, BASE, "USD", 100.0, 10.0, False) is False
    # ...and BASE as DESTINATION is allowed through to order construction.
    ib = FakeIB()
    import ib_orders
    old = ib_orders.fx_pair_conid
    try:
        ib_orders.fx_pair_conid = lambda a, b: (123, "USD.HKD")
        seen = {}

        def fake_fx_order(ib_, base, quote, side, qty, dry, why, **kw):
            seen.update(base=base, quote=quote, side=side, why=why)
            return True

        with Patch(_fx_order=fake_fx_order):
            assert ib_bot._fx_order_pair(ib, "USD", BASE, 1800.0, 14100.0, False) is True
        # USD.HKD with USD as base: acquiring HKD means SELLING USD
        assert seen == {"base": "USD", "quote": "HKD", "side": "SELL",
                        "why": "USD->HKD"}, seen
    finally:
        ib_orders.fx_pair_conid = old
    print("t5 base may be bought, never sold, as an FX pair OK")


def t6_fx_order_still_refuses_to_sell_base():
    # BUY USD.HKD pays HKD -> refused before any contract is built.
    ib = FakeIB(qualify_raises=True)
    assert ib_bot._fx_order(ib, "USD", BASE, "BUY", 100.0, False, "HKD->USD") is False
    assert ib.qualify_calls == 0, "refusal must come before order construction"
    # SELL USD.HKD receives HKD -> allowed past the guard (blows up later, which
    # is how we know it got through).
    try:
        ib_bot._fx_order(ib, "USD", BASE, "SELL", 100.0, False, "USD->HKD")
        reached = ib.qualify_calls > 0
    except AssertionError:
        reached = True
    assert reached, "buying HKD must not be refused by the sell-base guard"
    print("t6 sell-base guard unchanged, buy-base allowed OK")


def t7_funding_base_never_draws_on_base():
    picked = []

    def rec_pair(ib, src, dst, qty_src, qty_dst, dry):
        picked.append((src, dst))
        return True

    with Patch(cash_by_ccy=lambda ib: {BASE: 99999.0, "USD": 4558.0, "JPY": 83346.0},
               fx_rate=lambda ib, a, b: {"USD": 0.1274, "JPY": 19.0}.get(b, 1.0),
               _fx_order_pair=rec_pair):
        assert ib_bot.fund_from_nonbase(None, BASE, 14100.0, False, buffer=1.03) is True
    assert picked, "should have converted something"
    srcs = [p[0] for p in picked]
    assert BASE not in srcs, srcs          # the transfer pot is never a source
    assert picked[0][1] == BASE
    print("t7 base funded only from non-base balances OK")


def t8_non_base_path_unchanged():
    # A USD or JPY order still takes the old route with the old 2% buffer.
    calls = []

    def rec(ib, ccy, short, dry, buffer=1.02):
        calls.append({"ccy": ccy, "short": short, "buffer": buffer})
        return True

    with Patch(cash_by_ccy=lambda ib: {BASE: 0.0, "JPY": 1000.0},
               fx_rate=lambda ib, a, b: 0.0528,      # HKD per JPY
               fund_from_nonbase=rec):
        assert ib_bot.ensure_ccy(None, "JPY", 17900.0, False) is True
    assert len(calls) == 1 and calls[0]["ccy"] == "JPY"
    assert calls[0]["buffer"] == 1.02, calls        # unchanged for non-base
    need_jpy = 17900.0 / 0.0528
    assert abs(calls[0]["short"] - (need_jpy - 1000.0)) < 1e-6, calls
    print("t8 non-base funding unchanged OK")


def t9_earmark_is_frozen_for_the_run():
    # Board finding: the cap min(marker, cash) RISES as the bot buys HKD, so a
    # re-derived earmark re-classifies freshly converted HKD as earmarked and
    # the next candidate converts all over again. run() freezes it instead.
    with Patch(cash_by_ccy=lambda ib: {BASE: 14947.0}, _excluded_cash=lambda: 18559.0):
        # unfrozen (outside a run): the old, self-re-arming behaviour
        assert ib_bot._spendable_base(None) == 0.0
        # frozen at the start of the run, when only HKD 7 was held
        ib_bot._EARMARK_RUN["base"] = 7.0
        assert ib_bot._spendable_base(None) == 14940.0     # the bought HKD stays spendable
        ib_bot._FX_COMMITTED[BASE] = 14580.0
        assert ib_bot._spendable_base(None) == 360.0       # and the placed order is still reserved
    print("t9 earmark frozen per run OK")


def t10_no_second_conversion_while_one_is_working():
    # Board finding: _fx_already_working matches the PAIR, so an in-flight
    # USD.HKD did not stop a second conversion into HKD from JPY.
    tried = []
    with Patch(cash_by_ccy=lambda ib: {BASE: 7.0, "USD": 4558.0, "JPY": 83346.0},
               fx_rate=lambda ib, a, b: {"USD": 0.1274, "JPY": 19.0}.get(b, 1.0),
               _fx_order_pair=lambda *a, **k: tried.append(a[1:3]) or True):
        ib_bot._FX_PENDING_CCY.add(BASE)
        assert ib_bot.fund_from_nonbase(None, BASE, 4000.0, False, buffer=1.03) is False
        assert tried == [], tried
        ib_bot._FX_PENDING_CCY.clear()
        assert ib_bot.fund_from_nonbase(None, BASE, 4000.0, False, buffer=1.03) is True
        assert tried, "should convert once nothing is in flight"
    print("t10 no double conversion into the base currency OK")


if __name__ == "__main__":
    t1_spendable_base_respects_earmark_and_commitments()
    t2_enough_base_cash_places_without_converting()
    t3_short_base_cash_buys_only_the_shortfall_plus_3pct()
    t4_failed_conversion_skips_the_order()
    t5_never_sells_base_as_an_fx_source()
    t6_fx_order_still_refuses_to_sell_base()
    t7_funding_base_never_draws_on_base()
    t8_non_base_path_unchanged()
    t9_earmark_is_frozen_for_the_run()
    t10_no_second_conversion_while_one_is_working()
    print("ALL HKD FUNDING TESTS PASS")
