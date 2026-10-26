#!/usr/bin/env python3
"""Golden tests for the bot spending its OWN HKD (owner decision 2026-10-26).

Run from this directory:  python test_own_hkd.py

Until now the bot could never sell HKD: HK sale proceeds and the 3% funding
buffer's residue were stranded there for good. The owner approved a NARROW
change - never FX_CONVERT=1, which pays every shortfall from HKD with no pocket
or earmark check - and the reviewer added one more safety. Locked down here:

  * the bot's own HKD (the stamped pocket P) is a funding SOURCE for a
    USD/JPY/EUR order, worth min(P, HKD held - frozen earmark), less what
    working HK buys claim x BASE_FUND_BUFFER, less OWN_BASE_MARGIN - and 0
    unless the pocket is known, the earmark is frozen and every working order's
    cash was reserved. The operator's pot is never part of it;
  * that amount rides with the order as base_sell_cap, and _fx_order_pair and
    _fx_order each refuse any HKD sale above it (or with no cap at all);
  * a filled own sale shrinks P in-run, so the next candidate cannot sell the
    same money again - out of the pot; an unfilled one reserves its HKD;
  * --dry spends in memory only and writes nothing;
  * the reviewer's extra rule: inside a run whose pocket is UNKNOWN an HK buy
    spends no HKD at all - not provably the bot's - and is funded wholly by
    conversion; free cash then counts no HKD either.

Nothing here touches /root: every path is repointed before ib_bot is imported.
"""
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("IB_BACKEND", "web")
import testenv                                     # noqa: E402
_TMP = testenv.isolate("mps-ownhkd-")
os.environ.pop("EXCLUDED_CASH", None)
for _v in ("MAX_ORDER_BASE", "TARGET_POSITIONS", "FX_CONVERT", "FX_FUND_NONBASE",
           "BASE_FUND_BUFFER", "LIMIT_BUFFER"):
    os.environ.pop(_v, None)

import alerts                                      # noqa: E402
import broker                                      # noqa: E402
import earmark                                     # noqa: E402
import ib_bot                                      # noqa: E402
import ib_orders                                   # noqa: E402
testenv.assert_isolated()

BASE = ib_bot.BASE_CCY
M = 23746.0                                        # the month-end marker
HKD_PER = {"HKD": 1.0, "USD": 7.8, "JPY": 0.05, "EUR": 9.0}
RUN_AT = datetime(2026, 9, 16, 23, 35, 20, tzinfo=timezone.utc)
assert str(earmark.POCKET_FILE).startswith(str(_TMP)), earmark.POCKET_FILE


def rate(ib_, a, b):
    if a == b:
        return 1.0
    return HKD_PER[a] / HKD_PER[b] if (a in HKD_PER and b in HKD_PER) else 0.0


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
    for reg in (ib_bot._FX_COMMITTED, ib_bot._FX_PENDING_CCY, ib_bot._FX_PENDING,
                ib_bot._EARMARK_RUN, ib_bot._EXC_APPLIED, ib_bot._POCKET_RUN,
                ib_bot._RESERVE_OK, ib_bot._RUN_CONFIG, ib_bot._RATE_CACHE):
        reg.clear()
    del ib_bot.PLACED[:]


def in_run(p, exc=0.0, reserve_ok=True, committed=0.0):
    """The run-level registries as run() leaves them before the entries."""
    reset()
    ib_bot._POCKET_RUN.update(active=True, p=p, confirmed=p is not None)
    if exc is not None:
        ib_bot._EARMARK_RUN["base"] = exc
    if reserve_ok is not None:
        ib_bot._RESERVE_OK["ok"] = reserve_ok
    if committed:
        ib_bot._FX_COMMITTED[BASE] = committed


def cash(**c):
    return lambda ib_: dict(c)


class FxIB:
    """Fills (or leaves working) one conversion; records every order."""

    def __init__(self, status="Filled"):
        self.status, self.placed, self.qualify_calls = status, [], 0

    def qualifyContracts(self, *cs):
        self.qualify_calls += 1
        for c in cs:
            c.conId = 12087769
        return list(cs)

    def openTrades(self):
        return []

    def sleep(self, *a):
        pass

    def placeOrder(self, c, order):
        self.placed.append((order.action, order.totalQuantity,
                            getattr(c, "localSymbol", c.symbol)))
        t = broker.Trade(c, order)
        t.orderStatus = broker._OrderStatus(self.status)
        return t


class Refuse:
    """Raises if order construction is reached - proof a guard did NOT stop it."""
    qualify_calls = 0

    def qualifyContracts(self, *a):
        Refuse.qualify_calls += 1
        raise AssertionError("reached order construction")


def t1_own_hkd_is_zero_unless_every_condition_holds():
    assert ib_bot.OWN_BASE_FUND is True and ib_bot.FX_CONVERT is False
    held = {BASE: 38253.0, "USD": 100.0}            # pot 23,746 + the bot's 14,500 + 7
    with Patch(ib_bot, cash_by_ccy=cash(**held)):
        in_run(14500.0, exc=M)
        assert ib_bot._own_base_spendable(None) == 14400.0          # 14,500 - margin 100
        in_run(None, exc=M)
        assert ib_bot._own_base_spendable(None) == 0.0              # pocket unknown
        in_run(14500.0, exc=None)
        assert ib_bot._own_base_spendable(None) == 0.0              # earmark not frozen
        in_run(14500.0, exc=M, reserve_ok=False)
        assert ib_bot._own_base_spendable(None) == 0.0              # a claim unreserved
        in_run(14500.0, exc=M, reserve_ok=None)
        assert ib_bot._own_base_spendable(None) == 0.0              # no verdict at all
        in_run(14500.0, exc=M)
        ib_bot._POCKET_RUN["active"] = False
        assert ib_bot._own_base_spendable(None) == 0.0              # outside a run
        in_run(14500.0, exc=M)
        with Patch(ib_bot, OWN_BASE_FUND=False):
            in_run(14500.0, exc=M)
            assert ib_bot._own_base_spendable(None) == 0.0          # the rollback flag
        in_run(20000.0, exc=M)                    # P above H - E: H - E bounds it
        assert ib_bot._own_base_spendable(None) == 38253.0 - M - 100.0
        in_run(-250.0, exc=M)                     # an unstamped sweep charged to P
        assert ib_bot._own_base_spendable(None) == 0.0
        # working HK buys keep their HKD AND their stamp-duty/fee headroom
        in_run(14500.0, exc=M, committed=1000.0)
        assert abs(ib_bot._own_base_spendable(None) - (14500.0 - 1030.0 - 100.0)) < 1e-9
        in_run(14500.0, exc=M, committed=14170.5)  # a 100 x 141 buy working
        assert ib_bot._own_base_spendable(None) == 0.0
    print("t1 own HKD: min(P, H - E) - committed x 1.03 - 100, and 0 unless pocket, "
          "freeze and reservations are all known OK")


def t2_the_pot_is_never_offered_as_a_source():
    # Marker 0: nothing is earmarked, so H - E is the whole 38,253 - and still
    # only P (14,500, less the margin) may be sold. The pot is not the bot's.
    picked = []

    def rec(ib_, src, dst, qty_src, qty_dst, dry, **kw):
        picked.append((src, dst, round(qty_src), round(qty_dst), kw))
        return True
    with Patch(ib_bot, cash_by_ccy=cash(HKD=38253.0, USD=100.0), fx_rate=rate,
               _fx_order_pair=rec):
        in_run(14500.0, exc=0.0)
        assert ib_bot._own_base_spendable(None) == 14400.0
        # USD 2,000 short needs 2,000 x 7.8 x 1.02 = 15,912 HKD: more than the
        # bot's own, so NOTHING is converted - the pot is not dipped into
        assert ib_bot.fund_from_nonbase(None, "USD", 2000.0, False) is False
        assert picked == [], picked
        # USD 1,000 short needs 7,956: inside the cap, which travels with it
        assert ib_bot.fund_from_nonbase(None, "USD", 1000.0, False) is True
    assert picked == [(BASE, "USD", 7956, 1020, {"base_sell_cap": 14400.0})], picked
    # an HKD order never sells HKD to buy HKD
    del picked[:]
    with Patch(ib_bot, cash_by_ccy=cash(HKD=38253.0), fx_rate=rate, _fx_order_pair=rec):
        in_run(14500.0, exc=0.0)
        assert ib_bot.fund_from_nonbase(None, BASE, 1000.0, False, buffer=1.03) is False
    assert picked == [], picked
    print("t2 only the bot's own HKD is offered, capped - never the operator's pot OK")


def t3_a_filled_own_sale_shrinks_the_pocket_in_the_same_run():
    ib = FxIB("Filled")
    with Patch(ib_bot, cash_by_ccy=cash(HKD=14507.0, USD=100.0), fx_rate=rate,
               confirm=lambda m: True), \
            Patch(ib_orders, fx_pair_conid=lambda a, b: (12087769, "USD.HKD")):
        in_run(14500.0, exc=7.0)
        assert ib_bot.fund_from_nonbase(ib, "USD", 1000.0, False) is True
        # BUY USD.HKD (USD is the pair's base): pays ~7,956 HKD for 1,020 USD
        assert ib.placed == [("BUY", 1020, "USDHKD")], ib.placed
        assert abs(ib_bot._POCKET_RUN["p"] - (14500.0 - 7956.0 * 1.01)) < 1e-6
        assert "bot's own HKD" in ib_bot.PLACED[-1]["reason"], ib_bot.PLACED[-1]
        # the next candidate sees what is left - the cash read here still shows
        # the old balance, as an unsettled read would, and P bounds it anyway
        left = ib_bot._own_base_spendable(None)
        assert abs(left - (14500.0 - 7956.0 * 1.01 - 100.0)) < 1e-6, left
        ib_bot._FX_PENDING_CCY.clear()        # (a filled order leaves no flight)
        assert ib_bot.fund_from_nonbase(ib, "USD", 1000.0, False) is False
        assert len(ib.placed) == 1, "the same HKD was sold twice: %s" % ib.placed
    print("t3 a filled own sale shrinks P at once; a second sale cannot reach the pot OK")


def t4_an_unfilled_own_sale_reserves_its_hkd():
    ib = FxIB("Submitted")
    with Patch(ib_bot, cash_by_ccy=cash(HKD=14507.0, USD=100.0), fx_rate=rate,
               confirm=lambda m: True), \
            Patch(ib_orders, fx_pair_conid=lambda a, b: (12087769, "USD.HKD")):
        in_run(14500.0, exc=7.0)
        assert ib_bot.fund_from_nonbase(ib, "USD", 1000.0, False) is False
        assert ib_bot._POCKET_RUN["p"] == 14500.0              # nothing left yet
        assert abs(ib_bot._FX_COMMITTED[BASE] - 7956.0) < 1e-6  # but its HKD is claimed
        assert "USD" in ib_bot._FX_PENDING_CCY
        left = ib_bot._own_base_spendable(None)
        assert abs(left - max(0.0, 14500.0 - 7956.0 * 1.03 - 100.0)) < 1e-6, left
        # ...and it is what the pocket file reports as pending to every publisher
        ib_bot._write_pocket_file()
        body = json.loads(earmark.POCKET_FILE.read_text(encoding="utf-8"))
        assert body["p"] == 14500.0 and body["pending"] == 7956.0, body
    earmark.POCKET_FILE.unlink()
    print("t4 an unfilled own sale reserves its HKD and shows as pending OK")


def t5_the_order_level_guards_refuse_anything_beyond_the_cap():
    # _fx_order: BUY USD.HKD pays HKD - refused with no cap, above the cap, or
    # when the source it names is not HKD, before any contract is built
    Refuse.qualify_calls = 0
    for kw in ({}, {"src_ccy": BASE, "src_qty": 800.0},
               {"src_ccy": BASE, "src_qty": 800.0, "base_sell_cap": 700.0},
               {"src_ccy": "USD", "src_qty": 800.0, "base_sell_cap": 900.0},
               {"src_ccy": BASE, "src_qty": 0.0, "base_sell_cap": 900.0}):
        assert ib_bot._fx_order(Refuse(), "USD", BASE, "BUY", 100, False, "x", **kw) is False, kw
        assert ib_bot._fx_order(Refuse(), BASE, "JPY", "SELL", 100, False, "x", **kw) is False, kw
    assert Refuse.qualify_calls == 0
    # within the cap the guard lets it through - to order construction
    for base_ccy, quote_ccy, side in (("USD", BASE, "BUY"), (BASE, "JPY", "SELL")):
        try:
            ib_bot._fx_order(Refuse(), base_ccy, quote_ccy, side, 100, False, "x",
                             src_ccy=BASE, src_qty=800.0, base_sell_cap=900.0)
            reached = False
        except AssertionError:
            reached = True
        assert reached, "an own sale inside its cap was refused"
    # _fx_order_pair: HKD as the source needs a cap that covers it
    def no_pairs(a, b):
        raise AssertionError("reached the pair lookup")
    with Patch(ib_orders, fx_pair_conid=no_pairs):
        assert ib_bot._fx_order_pair(None, BASE, "USD", 50000.0, 10.0, False,
                                     base_sell_cap=49900.0) is False
        assert ib_bot._fx_order_pair(None, BASE, "USD", 100.0, 10.0, False) is False
    print("t5 _fx_order and _fx_order_pair refuse an HKD sale with no cap or beyond it OK")


def t6_dry_spends_the_pocket_in_memory_and_writes_nothing():
    ib = FxIB("Filled")
    assert not earmark.POCKET_FILE.exists()
    with Patch(ib_bot, cash_by_ccy=cash(HKD=14507.0, USD=100.0), fx_rate=rate,
               confirm=lambda m: True), \
            Patch(ib_orders, fx_pair_conid=lambda a, b: (12087769, "USD.HKD")):
        in_run(14500.0, exc=7.0)
        assert ib_bot.fund_from_nonbase(ib, "USD", 1000.0, True) is True
        assert ib.placed == [] and ib_bot.PLACED == [], (ib.placed, ib_bot.PLACED)
        assert abs(ib_bot._POCKET_RUN["p"] - (14500.0 - 7956.0 * 1.01)) < 1e-6
    assert not earmark.POCKET_FILE.exists(), "--dry wrote the pocket file"
    print("t6 --dry spends the pocket in memory only and writes nothing OK")


def t7_hk_buys_spend_no_unprovable_hkd_when_the_pocket_is_unknown():
    earmark.set_marker(0.0)
    asked = []

    def rec(ib_, ccy, short, dry, buffer=1.02):
        asked.append((ccy, round(short, 2), buffer))
        return True
    pot = {BASE: 22441.77, "USD": 10000.0}         # the operator's pot, unmarked
    with Patch(ib_bot, cash_by_ccy=cash(**pot), fund_from_nonbase=rec):
        # in a run, pocket unknown: nothing is provably the bot's
        in_run(None, exc=0.0)
        assert ib_bot._spendable_base(None) == 0.0
        assert ib_bot.ensure_ccy(None, BASE, 14100.0, False) is True
        assert asked == [(BASE, 14100.0, 1.03)], asked     # the WHOLE order converted
        # outside a run (tests, one-off tools): the old formula, unchanged
        reset()
        assert ib_bot._spendable_base(None) == 22441.77
        # pocket KNOWN at 0 - all of it the operator's: nothing spent either
        del asked[:]
        in_run(0.0, exc=0.0)
        assert ib_bot._spendable_base(None) == 0.0
        assert ib_bot.ensure_ccy(None, BASE, 14100.0, False) is True
        assert asked == [(BASE, 14100.0, 1.03)], asked
    # ...and the sizing counts none of it while the pocket is unknown
    with Patch(ib_bot, cash_by_ccy=cash(**pot), fx_rate=rate):
        in_run(None, exc=0.0)
        fc, parts = ib_bot._free_cash_base(None)
    assert fc == 78000.0 and parts == ["USD 78,000"], (fc, parts)
    print("t7 pocket unknown: an HK buy spends no HKD, converts the whole order; "
          "free cash counts no HKD OK")


def t8_free_cash_counts_the_bots_own_hkd_and_any_hkd_debt():
    with Patch(ib_bot, cash_by_ccy=cash(HKD=14507.0, USD=1000.0), fx_rate=rate):
        in_run(14500.0, exc=7.0)
        fc, parts = ib_bot._free_cash_base(None)
        assert fc == 7800.0 + 14400.0, (fc, parts)
        assert parts == ["USD 7,800", "own HKD 14,400"], parts
    with Patch(ib_bot, cash_by_ccy=cash(HKD=-500.0, USD=1000.0), fx_rate=rate):
        in_run(None, exc=0.0)
        fc, parts = ib_bot._free_cash_base(None)
        assert fc == 7300.0 and parts == ["HKD owed -500", "USD 7,800"], (fc, parts)
    # a working order's claim is netted before anything is counted - also in a
    # currency the account holds none of: that is money owed when it fills
    with Patch(ib_bot, cash_by_ccy=cash(USD=1000.0), fx_rate=rate):
        in_run(None, exc=0.0)
        ib_bot._FX_COMMITTED["USD"] = 400.0
        assert ib_bot._free_cash_base(None)[0] == 600.0 * 7.8
        ib_bot._FX_COMMITTED["JPY"] = 20000.0                 # a JPY buy, no JPY held
        fc, parts = ib_bot._free_cash_base(None)
        assert abs(fc - (600.0 * 7.8 - 20000.0 * 0.05)) < 1e-9, (fc, parts)
        ib_bot._FX_COMMITTED["CHF"] = 50.0                    # owed, and no CHF rate
        try:
            ib_bot._free_cash_base(None)
            raise AssertionError("an unvalued claim must stop the sizing")
        except ValueError as e:
            assert "CHF" in str(e), e
    print("t8 free cash counts the bot's own HKD (pocket known) and any HKD debt OK")


def t9_one_funding_pass_counts_own_hkd_as_one_source():
    with Patch(ib_bot, cash_by_ccy=cash(HKD=14507.0, USD=100.0, JPY=20000.0),
               fx_rate=rate):
        in_run(14500.0, exc=7.0)
        f = ib_bot._fundable_base(None, "USD", 7.8)
        assert abs(f - (780.0 + 14400.0 / (1.02 * 1.0002))) < 1e-6, f
        # pocket unknown: JPY (HK$1,000) is the only other source
        in_run(None, exc=7.0)
        f = ib_bot._fundable_base(None, "USD", 7.8)
        assert abs(f - (780.0 + 1000.0 / (1.02 * 1.0002))) < 1e-6, f
        # a conversion into USD already working: no second one this run
        in_run(14500.0, exc=7.0)
        ib_bot._FX_PENDING_CCY.add("USD")
        assert ib_bot._fundable_base(None, "USD", 7.8) == 780.0
        # an HKD order: own HKD is its own cash; others buy it at 1.03
        in_run(14500.0, exc=7.0)
        f = ib_bot._fundable_base(None, BASE, 1.0)
        assert abs(f - (14500.0 + 1000.0 / (1.03 * 1.0002))) < 1e-6, f
    print("t9 one funding pass: own cash + the largest single source, own HKD included OK")


class RunIB:
    """A whole run: 14 US holdings, HKD 14,507 (the bot's 14,500 + 7), USD 500.
    Conversions fill; stock orders are accepted."""

    def __init__(self):
        self.placed = []

    def accountValues(self):
        return [broker.AccountValue("NetLiquidation", "200000", BASE),
                broker.AccountValue("CashBalance", "14507", BASE),
                broker.AccountValue("CashBalance", "500", "USD")]

    def positions(self):
        return [broker.Position(broker.Contract("H%02d" % k, "STK", "USD"), 1, 10.0)
                for k in range(14)]

    def reqAllOpenOrders(self):
        pass

    def openTrades(self):
        return []

    def sleep(self, *a):
        pass

    def qualifyContracts(self, *cs):
        for c in cs:
            if c.secType == "CASH":
                c.conId = 12087769
        return list(cs)

    def reqTickers(self, c):
        return [broker.Ticker()]

    def placeOrder(self, c, order):
        self.placed.append((order.action, order.totalQuantity, c.symbol, c.secType))
        t = broker.Trade(c, order)
        t.orderStatus = broker._OrderStatus("Filled" if c.secType == "CASH" else "Submitted")
        return t

    def disconnect(self):
        pass


def t10_a_whole_run_funds_a_us_buy_from_the_bots_own_hkd():
    earmark.set_marker(M)
    d = Path(tempfile.mkdtemp(prefix="run-", dir=str(_TMP)))
    state = d / "state.json"
    state.write_text('{"map": {}, "pos": {}}', encoding="utf-8")
    alerts.DIR = d / "outbox"
    signals = {"generated": "2026-09-16", "actions": [
        {"symbol": "NEWA", "action": "BUY", "price": 100.0, "score": 90, "stop": 88.0}]}
    ib, lines = RunIB(), []
    sweep = {"active": True, "p": 14500.0, "confirmed": True, "anchor": "2026-09-01 00:00"}
    with Patch(ib_bot, STATE=state, IB=lambda: ib, connect_or_heal=lambda *a, **k: None,
               get_json=lambda url: signals, _now_utc=lambda: RUN_AT,
               _sweep_pocket=lambda ib_, dry: dict(sweep),
               publish_state=lambda *a: None, fx_rate=rate, confirm=lambda m: True,
               min_tick=lambda ib_, c: 0.01, ADOPT=d / "adopt.json",
               EXIT_ATTEMPTS=d / "exit_attempts.json", FILLS_LEDGER=d / "fills.jsonl",
               log=lambda *a: lines.append(" ".join(str(x) for x in a))), \
            Patch(ib_orders, fx_pair_conid=lambda a, b: (12087769, "USD.HKD")):
        ib_bot.run(dry=False)
    # the earmark froze at min(M, H - P) = 7; own HKD 14,400; USD 3,900 -> one
    # free slot of HK$18,300, cut to what one pass can pay, 3,900 + 14,400/1.0202
    assert any("free cash 18,300 HKD (USD 3,900, own HKD 14,400) / 1 free slot(s)" in l
               for l in lines), lines
    fundable = 3900.0 + 14400.0 / (1.02 * 1.0002)
    shares = int(fundable / 1.005 / 7.8 / 100.0)                      # 22
    short = shares * 100.0 - 500.0                                    # 1,700 USD
    assert ib.placed == [("BUY", round(short * 1.02), "USD", "CASH"),
                         ("BUY", shares, "NEWA", "STK")], ib.placed
    assert any("funding USD from HKD" in l and "the bot's own HKD, at most 14,400" in l
               for l in lines), lines
    # P shrank by the sale, and the pocket file says so to every publisher
    body = json.loads(earmark.POCKET_FILE.read_text(encoding="utf-8"))
    want = 14500.0 - short * 7.8 * 1.02 * 1.01
    assert abs(body["p"] - round(want, 2)) < 0.01 and body["pending"] == 0.0, (body, want)
    earmark.POCKET_FILE.unlink()
    earmark.set_marker(0.0)
    print("t10 a whole run: a US buy funded from the bot's own HKD, P shrinks, pot untouched OK")


if __name__ == "__main__":
    try:
        t1_own_hkd_is_zero_unless_every_condition_holds()
        t2_the_pot_is_never_offered_as_a_source()
        t3_a_filled_own_sale_shrinks_the_pocket_in_the_same_run()
        t4_an_unfilled_own_sale_reserves_its_hkd()
        t5_the_order_level_guards_refuse_anything_beyond_the_cap()
        t6_dry_spends_the_pocket_in_memory_and_writes_nothing()
        t7_hk_buys_spend_no_unprovable_hkd_when_the_pocket_is_unknown()
        t8_free_cash_counts_the_bots_own_hkd_and_any_hkd_debt()
        t9_one_funding_pass_counts_own_hkd_as_one_source()
        t10_a_whole_run_funds_a_us_buy_from_the_bots_own_hkd()
    finally:
        reset()
    print("ALL OWN-HKD TESTS PASS")
