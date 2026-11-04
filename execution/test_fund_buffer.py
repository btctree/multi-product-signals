#!/usr/bin/env python3
"""Golden tests: a funding conversion delivers shortfall x buffer, from the most
valuable balance.

Run from this directory:  python test_fund_buffer.py

Two defects in fund_from_nonbase, both seen on the live balances (HKD ~7,
USD 4,297, JPY 83,346, EUR 39):

  * The buffer rode only on the SOURCE amount. _fx_order_pair orders whichever
    side is the pair's base, so on a pair quoted with the TARGET first (EUR.USD,
    HKD.JPY, USD.JPY) it bought the bare shortfall: FX BUY 1539 EUR.USD on
    2026-09-12 was exactly 33 x 48.17 - 51 EUR held, no 2%, and HKD bought from
    JPY got no 3% - against the operator's "shortfall x 1.03" rule.
  * Sources were ranked by raw units in their own currency, so JPY 83,346
    (~HK$4.4k) was tried before USD 4,297 (~HK$33.5k).

Locked down: BUY HKD.JPY carries 3%, BUY EUR.USD carries 2%, SELL USD.HKD sends
what it always sent, the reservation and the in-run HKD pocket estimate follow
the buffered amount, sources rank by value in BASE_CCY with a missing rate
ranked last (never dropped), and HKD is still never a source.

A third, seen live on 2026-11-03: IB refused FX BUY 1,824 EUR.USD for
INSUFFICIENT CASH [-79.76 USD] with USD 2,065 held and a cost of USD 2,041. IB
wants about 5% over the quote plus the fee on a BUY of the pair's base. Locked
down: the checks allow for that on the BUY side and only there, the direction
table agrees with the side _fx_order_pair really sends, the sizing clamp and the
funding check agree at the boundary, and what is ordered and reserved is unchanged.
Nothing here touches /root: every path is repointed before ib_bot is imported.
"""
import os

os.environ.setdefault("IB_BACKEND", "web")
# Every /root default at a temp path before import (review 2026-09-17, test
# isolation: the alert spool and exit memo were still /root here). See testenv.
import testenv                                     # noqa: E402
_TMP = testenv.isolate("mps-fundbuf-")
os.environ["MPS_EARMARK_DIR"] = str(_TMP)

import broker                                      # noqa: E402
import ib_bot                                      # noqa: E402
import ib_orders                                   # noqa: E402
testenv.assert_isolated()

BASE = ib_bot.BASE_CCY                             # "HKD"
assert ib_bot.FX_CONVERT is False
assert abs(ib_bot.BASE_FUND_BUFFER - 1.03) < 1e-12

# HKD per 1 unit, close to the live fills; every pair is derived from this one
# table, so a rate and its inverse always agree.
HKD_PER = {BASE: 1.0, "USD": 7.8, "JPY": 1 / 20.25, "EUR": 9.047}
PAIRS = {frozenset((BASE, "JPY")): (15016098, "HKD.JPY"),
         frozenset(("USD", BASE)): (12345777, "USD.HKD"),
         frozenset(("EUR", "USD")): (12087792, "EUR.USD"),
         frozenset(("USD", "JPY")): (15016059, "USD.JPY")}


def rate(ib, a, b):
    return HKD_PER[a] / HKD_PER[b]


def pair_conid(a, b):
    return PAIRS.get(frozenset((a, b)), (None, None))


class Patch:
    def __init__(self, mod, **kw):
        self.mod, self.kw, self.old = mod, kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(self.mod, k)
            setattr(self.mod, k, v)
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            setattr(self.mod, k, v)
        reset()


def reset():
    ib_bot._RATE_CACHE.clear()
    ib_bot._STALE_RATES.clear()
    ib_bot._FX_PENDING.clear()
    ib_bot._FX_PENDING_CCY.clear()
    ib_bot._FX_COMMITTED.clear()
    ib_bot._POCKET_RUN.clear()
    del ib_bot.PLACED[:]


class FxIB:
    """Answers the real _fx_order: qualifies the pair, has no working orders,
    and reports every order with `status`."""

    def __init__(self, status="Filled"):
        self.status, self.placed = status, []

    def qualifyContracts(self, *c):
        for x in c:
            x.conId = pair_conid(x.symbol, x.currency)[0] or 1
        return list(c)

    def openTrades(self):
        return []

    def sleep(self, *a):
        pass

    def placeOrder(self, contract, order):
        self.placed.append((order.action, order.totalQuantity,
                            contract.symbol + "." + contract.currency))
        t = broker.Trade(contract, order)
        t.orderStatus.status = self.status
        return t


def fund(ib, ccy, short, balances, **kw):
    with Patch(ib_orders, fx_pair_conid=pair_conid), \
            Patch(ib_bot, cash_by_ccy=lambda ib_: dict(balances), fx_rate=rate,
                  confirm=lambda m: True, log=lambda *a: None):
        ib_bot._POCKET_RUN.update(active=True, p=kw.pop("pocket", 0.0))
        ok = ib_bot.fund_from_nonbase(ib, ccy, short, False, **kw)
        return ok, dict(ib_bot._FX_COMMITTED), ib_bot._POCKET_RUN.get("p")


def t1_buy_hkd_jpy_carries_3pct():
    # HKD 10,000 held for a HK$14,000 order: 4,000 short, JPY is the only source.
    ib = FxIB("Filled")
    ok, committed, pocket = fund(ib, BASE, 4000.0, {BASE: 10000.0, "JPY": 290000.0},
                                 buffer=ib_bot.BASE_FUND_BUFFER)
    assert ok is True
    assert ib.placed == [("BUY", 4120, "HKD.JPY")], ib.placed    # was BUY 4000
    # the in-run pocket grows by what was ordered - the buffered HKD
    assert pocket == 4120.0, pocket
    assert committed == {}, committed                            # filled: nothing reserved
    # accepted but unfilled: the JPY reserved is the buffered cost of those 4,120
    reset()
    ib = FxIB("Submitted")
    ok, committed, pocket = fund(ib, BASE, 4000.0, {BASE: 10000.0, "JPY": 290000.0},
                                 buffer=ib_bot.BASE_FUND_BUFFER)
    assert ok is False and ib.placed == [("BUY", 4120, "HKD.JPY")], ib.placed
    assert abs(committed["JPY"] - 4000.0 * 20.25 * 1.03) < 1e-6, committed
    assert abs(committed["JPY"] - 4120 * 20.25) < 1e-6         # the same conversion
    assert pocket == 0.0, "an unfilled conversion must not grow the pocket"
    print("t1 BUY HKD.JPY carries the 3% buffer; reservation and pocket agree OK")


def t2_buy_eur_usd_carries_2pct():
    # The live 2026-09-12 case: BAYN 33 x 48.17 = 1,589.6 EUR, 51 held.
    short = 33 * 48.17 - 51.0
    ib = FxIB("Filled")
    ok, committed, pocket = fund(ib, "EUR", short, {BASE: 7.0, "USD": 4297.0, "EUR": 51.0})
    assert ok is True
    assert ib.placed == [("BUY", round(short * 1.02), "EUR.USD")], ib.placed   # not 1539
    assert ib.placed[0][1] == 1569, ib.placed
    assert pocket == 0.0, "a conversion into EUR is not the HKD pocket"
    reset()
    ib = FxIB("Submitted")
    ok, committed, _ = fund(ib, "EUR", short, {BASE: 7.0, "USD": 4297.0, "EUR": 51.0})
    usd_per_eur = HKD_PER["EUR"] / HKD_PER["USD"]
    assert abs(committed["USD"] - short * usd_per_eur * 1.02) < 1e-6, committed
    print("t2 BUY EUR.USD carries the 2% buffer OK")


def t3_sell_usd_hkd_unchanged():
    # The SELL branch always carried the buffer: the same order as before.
    short = 14100.0 - 7.0
    ib = FxIB("Filled")
    ok, committed, pocket = fund(ib, BASE, short, {BASE: 7.0, "USD": 4558.0},
                                 buffer=ib_bot.BASE_FUND_BUFFER)
    before = int(round(short * (1 / 7.8) * 1.03))          # what the old code sent
    assert ok is True and ib.placed == [("SELL", before, "USD.HKD")], ib.placed
    assert before == 1861, before                           # 14,093 / 7.8 x 1.03
    assert abs(pocket - before * 7.8) < 1e-6 and pocket >= short * 1.03 - 7.8, pocket
    reset()
    ib = FxIB("Submitted")
    ok, committed, _ = fund(ib, BASE, short, {BASE: 7.0, "USD": 4558.0},
                            buffer=ib_bot.BASE_FUND_BUFFER)
    assert ib.placed == [("SELL", before, "USD.HKD")], ib.placed
    assert abs(committed["USD"] - short / 7.8 * 1.03) < 1e-6, committed
    print("t3 SELL USD.HKD sends exactly what it always sent OK")


def t4_sources_rank_by_value_not_raw_units():
    # Both balances can cover HK$3,000: JPY needs ~62,573, USD ~396.
    live = {BASE: 7.0, "USD": 4297.0, "JPY": 83346.0}
    picked = []
    with Patch(ib_bot, cash_by_ccy=lambda ib_: dict(live), fx_rate=rate, log=lambda *a: None,
               _fx_order_pair=lambda ib_, s, d, qs, qd, dry: picked.append((s, d, qs, qd)) or True):
        assert ib_bot.fund_from_nonbase(None, BASE, 3000.0, False, buffer=1.03) is True
    assert [p[0] for p in picked] == ["USD"], picked               # was JPY
    s, d, qs, qd = picked[0]
    assert d == BASE and abs(qd - 3090.0) < 1e-9 and abs(qs - 3090.0 / 7.8) < 1e-9, picked

    # A source whose ranking rate is missing is tried LAST, not dropped: USD has
    # no USD->HKD rate for ranking, JPY is too small, and USD still pays.
    def gappy(ib_, a, b):
        return 0.0 if (a, b) == ("USD", BASE) else rate(ib_, a, b)
    picked = []
    order = []
    with Patch(ib_bot, cash_by_ccy=lambda ib_: {BASE: 7.0, "USD": 4297.0, "JPY": 50000.0},
               fx_rate=gappy, log=lambda *a: order.append(" ".join(map(str, a))),
               _fx_order_pair=lambda ib_, s, d, qs, qd, dry: picked.append(s) or True):
        assert ib_bot.fund_from_nonbase(None, BASE, 3000.0, False, buffer=1.03) is True
    assert picked == ["USD"], picked
    assert any(l.startswith("  JPY 50,000 short") for l in order), order   # JPY tried first
    # ...and a ranking lookup that RAISES ranks that source last the same way
    def boom(ib_, a, b):
        if (a, b) == ("JPY", BASE):
            raise RuntimeError("rate service down")
        return rate(ib_, a, b)
    picked = []
    with Patch(ib_bot, cash_by_ccy=lambda ib_: {BASE: 7.0, "USD": 10.0, "JPY": 83346.0},
               fx_rate=boom, log=lambda *a: None,
               _fx_order_pair=lambda ib_, s, d, qs, qd, dry: picked.append(s) or True):
        assert ib_bot.fund_from_nonbase(None, BASE, 3000.0, False, buffer=1.03) is True
    # USD 10 cannot pay, and the conversion itself asks rate(HKD, JPY), which
    # boom answers - so JPY, ranked last, is still reached
    assert picked == ["JPY"], picked
    print("t4 sources rank by BASE value; a missing rate ranks last, never dropped OK")


def t5_hkd_is_still_never_a_source():
    # The most valuable balance by far is HKD. It is still not a source, for any
    # target, and the refusals at the pair and the order are untouched.
    picked = []
    with Patch(ib_bot, cash_by_ccy=lambda ib_: {BASE: 9999999.0, "USD": 4297.0, "JPY": 83346.0},
               fx_rate=rate, log=lambda *a: None,
               _fx_order_pair=lambda ib_, s, d, qs, qd, dry: picked.append(s) or True):
        assert ib_bot.fund_from_nonbase(None, "EUR", 100.0, False) is True
        assert ib_bot.fund_from_nonbase(None, BASE, 100.0, False, buffer=1.03) is True
    assert picked == ["USD", "USD"], picked
    with Patch(ib_bot, cash_by_ccy=lambda ib_: {BASE: 9999999.0}, fx_rate=rate,
               log=lambda *a: None,
               _fx_order_pair=lambda *a: picked.append("reached") or True):
        assert ib_bot.fund_from_nonbase(None, "USD", 100.0, False) is False
    assert "reached" not in picked
    with Patch(ib_bot, log=lambda *a: None):
        assert ib_bot._fx_order_pair(None, BASE, "USD", 100.0, 10.0, False) is False

    class Refuse:
        def qualifyContracts(self, *a):
            raise AssertionError("reached order construction")
    with Patch(ib_bot, log=lambda *a: None):
        assert ib_bot._fx_order(Refuse(), "USD", BASE, "BUY", 100, False, "HKD->USD") is False
        assert ib_bot._fx_order(Refuse(), BASE, "JPY", "SELL", 100, False, "HKD->JPY") is False
    print("t5 HKD is never a funding source, whatever it is worth OK")


def t6_ensure_ccy_hk_entry_end_to_end():
    # ensure_ccy -> fund_from_nonbase -> _fx_order_pair -> _fx_order, only the
    # IB edges stubbed: HK$14,100 order, HK$10,000 of the bot's own HKD spendable.
    reset()
    ib = FxIB("Filled")
    with Patch(ib_orders, fx_pair_conid=pair_conid), \
            Patch(ib_bot, cash_by_ccy=lambda ib_: {BASE: 10000.0, "JPY": 290000.0},
                  fx_rate=rate, confirm=lambda m: True, log=lambda *a: None):
        ib_bot._POCKET_RUN.update(active=True, p=10000.0)
        ib_bot._EARMARK_RUN["base"] = 0.0
        assert ib_bot.ensure_ccy(ib, BASE, 14100.0, False) is True
        assert ib.placed == [("BUY", 4223, "HKD.JPY")], ib.placed    # 4,100 x 1.03
        assert ib_bot._POCKET_RUN["p"] == 14223.0, ib_bot._POCKET_RUN
        ib_bot._EARMARK_RUN.clear()
    print("t6 an HK entry short HK$4,100 buys HK$4,223 from JPY end to end OK")


def t7_the_direction_table_matches_the_side_really_sent():
    # Every conversion among the five currencies, on the pair IB really lists,
    # through the real _fx_order_pair. The table and the order must agree: the
    # table decides the check, the pair decides the order.
    listed = ("EUR.GBP", "EUR.USD", "EUR.HKD", "EUR.JPY", "GBP.USD", "GBP.HKD",
              "GBP.JPY", "USD.HKD", "USD.JPY", "HKD.JPY")
    table = {frozenset(s.split(".")): (i + 1, s) for i, s in enumerate(listed)}
    ccys = ("EUR", "GBP", "USD", "HKD", "JPY")
    seen = 0
    for src in ccys:
        for dst in ccys:
            if src == dst:
                continue
            sent = []

            def order(ib_, base, quote, side, qty, dry, why, **kw):
                sent.append(side)
                return True
            with Patch(ib_orders, fx_pair_conid=lambda a, b: table[frozenset((a, b))]), \
                    Patch(ib_bot, _fx_order=order, log=lambda *a: None):
                cap = {"base_sell_cap": 1000.0} if src == BASE else {}
                assert ib_bot._fx_order_pair(None, src, dst, 100.0, 10.0, False, **cap)
            assert sent == [("BUY" if ib_bot._fx_buys_pair_base(src, dst) else "SELL")], (
                src, dst, sent)
            seen += 1
    assert seen == 20, seen
    assert ib_bot._fx_buys_pair_base("USD", "EUR") is True      # the 3 Nov refusal
    assert ib_bot._fx_buys_pair_base("EUR", "USD") is False     # 466 of 485 filled, 4 Nov
    # A currency outside the table takes the stricter check, whichever way round.
    assert ib_bot._fx_buys_pair_base("CHF", "USD") is True
    assert ib_bot._fx_buys_pair_base("USD", "CHF") is True
    print("t7 the direction table agrees with the side sent on all 20 conversions OK")


def t8_the_hold_reproduces_the_refusal():
    # IB's own number, 2026-11-03: -79.76 USD with USD 2,065 held, BUY 1,824 EUR.USD.
    assert ib_bot.IB_FX_BUY_HOLD == 1.05 and ib_bot.IB_FX_FEE_BASE == 16.0
    cost = 1824 * 1.11893
    assert abs((2065.0 - cost * 1.05 - 2.0) - (-79.76)) < 0.5
    # ...and the ~2% a LYNX help page gives does not: a real number to reject.
    assert abs((2065.0 - cost * 1.02 - 2.0) - (-79.76)) > 50
    # The sum the funding code does, on those balances, says no before IB can.
    usd = {"USD": 7.8463, "EUR": 7.8463 * 1.11893, BASE: 1.0}

    def r(ib_, a, b):
        return usd[a] / usd[b]
    log = []
    with Patch(ib_bot, cash_by_ccy=lambda ib_: {BASE: 0.0, "USD": 2065.0, "EUR": 485.0},
               fx_rate=r, log=lambda *a: log.append(" ".join(map(str, a))),
               _fx_order_pair=lambda *a, **k: log.append("ORDER SENT") or True):
        assert ib_bot.fund_from_nonbase(None, "EUR", 1788.15, False) is False
    assert "ORDER SENT" not in log and any("hold on a BUY-side" in l for l in log), log
    # A missing rate drops only the fee; the hold still stands.
    def boom(ib_, a, b):
        raise RuntimeError("rate service down")
    with Patch(ib_bot, fx_rate=boom):
        assert ib_bot._fx_cash_needed(None, "USD", "EUR", 1000.0) == 1050.0
    # The SELL side is exactly what it always was, both ways round.
    assert ib_bot._fx_cash_needed(None, "EUR", "USD", 1000.0) == 1000.0
    assert ib_bot._fx_payable_base("EUR", "USD", 1000.0) == 1000.0
    assert ib_bot._fx_payable_base("USD", "EUR", 10.0) == 0.0        # never negative
    print("t8 the hold reproduces IB's -79.76; SELL conversions are untouched OK")


def t9_the_clamp_and_the_check_agree_at_the_boundary():
    # _fundable_base says what one pass can pay; fund_from_nonbase must then pay
    # it. A gap either way is a bug: too wide and the check refuses every order the
    # clamp sized, too narrow and IB refuses instead. BUY-side and SELL-side pairs.
    cases = [("USD", "EUR", 2065.0), ("JPY", "USD", 300000.0), ("JPY", BASE, 300000.0),
             ("USD", "JPY", 2065.0), ("EUR", "USD", 900.0), ("USD", BASE, 2065.0)]
    for src, dst, have in cases:
        bal = {BASE: 0.0, src: have}
        with Patch(ib_bot, cash_by_ccy=lambda ib_, b=bal: dict(b), fx_rate=rate,
                   log=lambda *a: None, _spendable_base=lambda *a: 0.0,
                   _fx_order_pair=lambda *a, **k: True):
            f = ib_bot._fundable_base(None, dst, HKD_PER[dst])
            short = f / HKD_PER[dst]
            buf = ib_bot.BASE_FUND_BUFFER if dst == BASE else 1.02
            assert ib_bot.fund_from_nonbase(None, dst, short * 0.9995, False, buffer=buf), (src, dst)
            assert not ib_bot.fund_from_nonbase(None, dst, short * 1.001, False, buffer=buf), (src, dst)
    # The hold costs only the BUY side: the same dollars buy less EUR, not less JPY.
    with Patch(ib_bot, cash_by_ccy=lambda ib_: {BASE: 0.0, "USD": 2065.0}, fx_rate=rate,
               _spendable_base=lambda *a: 0.0):
        eur = ib_bot._fundable_base(None, "EUR", HKD_PER["EUR"])
        jpy = ib_bot._fundable_base(None, "JPY", HKD_PER["JPY"])
    v = 2065.0 * HKD_PER["USD"]
    assert abs(jpy - v / (1.02 * 1.0002)) < 1e-6, jpy
    assert abs(eur - (v - 16.0) / 1.05 / (1.02 * 1.0002)) < 1e-6, eur
    print("t9 the sizing clamp and the funding check agree on both sides OK")


if __name__ == "__main__":
    try:
        t1_buy_hkd_jpy_carries_3pct()
        t2_buy_eur_usd_carries_2pct()
        t3_sell_usd_hkd_unchanged()
        t4_sources_rank_by_value_not_raw_units()
        t5_hkd_is_still_never_a_source()
        t6_ensure_ccy_hk_entry_end_to_end()
        t7_the_direction_table_matches_the_side_really_sent()
        t8_the_hold_reproduces_the_refusal()
        t9_the_clamp_and_the_check_agree_at_the_boundary()
    finally:
        reset()
    print("ALL FUND-BUFFER TESTS PASS")
