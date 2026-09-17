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
Nothing here touches /root: every path is repointed before ib_bot is imported.
"""
import os
import tempfile
from pathlib import Path

os.environ.setdefault("IB_BACKEND", "web")
_TMP = Path(tempfile.mkdtemp(prefix="mps-fundbuf-"))
os.environ["MPS_EARMARK_DIR"] = str(_TMP)
os.environ["MPS_ORDERS_LEDGER"] = str(_TMP / "orders_ledger.jsonl")
os.environ["MPS_FX_LAST_GOOD"] = str(_TMP / "fx_last_good.json")
os.environ["MPS_CONID_CACHE"] = str(_TMP / "conid_cache.json")

import broker                                      # noqa: E402
import ib_bot                                      # noqa: E402
import ib_orders                                   # noqa: E402

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


if __name__ == "__main__":
    try:
        t1_buy_hkd_jpy_carries_3pct()
        t2_buy_eur_usd_carries_2pct()
        t3_sell_usd_hkd_unchanged()
        t4_sources_rank_by_value_not_raw_units()
        t5_hkd_is_still_never_a_source()
        t6_ensure_ccy_hk_entry_end_to_end()
    finally:
        reset()
    print("ALL FUND-BUFFER TESTS PASS")
