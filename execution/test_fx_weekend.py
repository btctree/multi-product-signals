#!/usr/bin/env python3
"""Golden tests: a weekend run can still SIZE orders, but never CONVERTS on memory.

Run from this directory:  python test_fx_weekend.py

2026-09-04, 09-05 and 09-12: IB's /iserver/exchangerate answered nothing while
the FX market was shut, fx_rate returned 0.0, and every Friday/Saturday run
skipped every non-HKD entry with "no USD/HKD rate to size order" - including US
stocks already paid for in USD. fx_rate now falls back to the last live rate
(FX_STALE_MAX_H), while conversions and the tax ledger use fx_rate_live, which
refuses a remembered rate.
"""
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("IB_BACKEND", "web")
# Every /root default at a temp path before import (review 2026-09-17, test
# isolation: the earmark, orders ledger and conid cache were still /root here).
import testenv                                      # noqa: E402
testenv.isolate("mps-fxweekend-")
import ib_bot                                       # noqa: E402
testenv.assert_isolated()

BASE = ib_bot.BASE_CCY
MEM = Path(tempfile.mkdtemp(prefix="fxmem_")) / "fx_last_good.json"
ib_bot.FX_LAST_GOOD = MEM                           # never touch /root in a test


class Patch:
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


def fresh(remember=True):
    ib_bot._RATE_CACHE.clear()
    ib_bot._STALE_RATES.clear()
    ib_bot._FX_COMMITTED.clear()
    ib_bot._FX_PENDING_CCY.clear()
    ib_bot._FX_REMEMBER = remember


def open_market(quotes):
    """_pair_mid replay: quotes maps 'USDHKD' -> mid; anything else unquotable."""
    return lambda ib, pair: ((object(), quotes[pair]) if pair in quotes else (None, None))


def shut_market(ib, pair):
    return None, None


def seed(rows):
    MEM.write_text(json.dumps(rows), encoding="utf-8")


def ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def t1_live_rate_is_remembered_then_used_when_shut():
    if MEM.exists():
        MEM.unlink()
    fresh()
    with Patch(_pair_mid=open_market({"USDHKD": 7.8})):
        assert ib_bot.fx_rate(None, "USD", BASE) == 7.8
    book = json.loads(MEM.read_text(encoding="utf-8"))
    assert book["USD/%s" % BASE]["rate"] == 7.8, book

    fresh()                                          # the next run: Saturday
    with Patch(_pair_mid=shut_market):
        assert ib_bot.fx_rate(None, "USD", BASE) == 7.8, "sizing must get a rate"
        assert ("USD", BASE) in ib_bot._STALE_RATES
        assert ib_bot.fx_rate_live(None, "USD", BASE) == 0.0, "never live on memory"
    print("t1 live rate remembered; a shut market sizes on it, live-only refuses OK")


def t2_too_old_or_missing_is_still_nothing():
    fresh()
    seed({"USD/%s" % BASE: {"rate": 7.8, "at": ago(ib_bot.FX_STALE_MAX_H + 1)}})
    with Patch(_pair_mid=shut_market):
        assert ib_bot.fx_rate(None, "USD", BASE) == 0.0
        assert ib_bot.fx_rate(None, "CHF", BASE) == 0.0      # never seen
    fresh()
    MEM.write_text("{not json", encoding="utf-8")           # a corrupt file
    with Patch(_pair_mid=shut_market):
        assert ib_bot.fx_rate(None, "USD", BASE) == 0.0
    print("t2 a stale-beyond-limit, unknown or corrupt memory gives no rate OK")


def t3_inverse_and_cross_are_marked_stale():
    fresh()
    seed({"%s/USD" % BASE: {"rate": 0.128, "at": ago(40)},
          "JPY/USD": {"rate": 0.0068, "at": ago(40)}})
    with Patch(_pair_mid=shut_market):
        assert abs(ib_bot.fx_rate(None, "USD", BASE) - 1 / 0.128) < 1e-9
        r = ib_bot.fx_rate(None, "JPY", BASE)                # via USD, both legs remembered
        assert abs(r - 0.0068 / 0.128) < 1e-9, r
        assert ("JPY", BASE) in ib_bot._STALE_RATES
        assert ib_bot.fx_rate_live(None, "JPY", BASE) == 0.0
    book = json.loads(MEM.read_text(encoding="utf-8"))
    assert "JPY/%s" % BASE not in book, "a rate built from memory must not be remembered"
    print("t3 inverted and USD-cross rates from memory are stale and not re-stored OK")


def t4_dry_run_never_writes_the_memory():
    if MEM.exists():
        MEM.unlink()
    fresh(remember=False)
    with Patch(_pair_mid=open_market({"USDHKD": 7.8})):
        assert ib_bot.fx_rate(None, "USD", BASE) == 7.8
    assert not MEM.exists(), "--dry wrote the rate memory"
    print("t4 --dry does not write the rate memory OK")


def t5_weekend_usd_entry_proceeds_when_usd_is_held():
    # ensure_ccy for a US stock already covered by USD cash: the only thing it
    # needs is a rate to express the HKD budget in USD.
    fresh()
    seed({"USD/%s" % BASE: {"rate": 7.8, "at": ago(38)}})
    with Patch(_pair_mid=shut_market, FX_CONVERT=False, FX_FUND_NONBASE=True,
               cash_by_ccy=lambda ib: {BASE: 0.0, "USD": 4297.0}):
        assert ib_bot.ensure_ccy(None, "USD", 14000.0, False) is True
    print("t5 weekend US entry funded by USD cash goes ahead OK")


def t6_weekend_never_converts_on_a_remembered_rate():
    fresh()
    seed({"USD/%s" % BASE: {"rate": 7.8, "at": ago(38)},
          "JPY/USD": {"rate": 0.0068, "at": ago(38)},
          "%s/JPY" % BASE: {"rate": 18.9, "at": ago(38)}})
    tried = []
    with Patch(_pair_mid=shut_market, FX_CONVERT=False, FX_FUND_NONBASE=True,
               cash_by_ccy=lambda ib: {BASE: 0.0, "USD": 100.0, "JPY": 83346.0},
               _fx_order_pair=lambda *a, **k: tried.append(a[1:3]) or True):
        # USD entry short of USD: would fund from JPY - must not on memory
        assert ib_bot.ensure_ccy(None, "USD", 14000.0, False) is False
        # HKD entry: would buy HKD from JPY/USD - must not on memory either
        assert ib_bot.fund_from_nonbase(None, BASE, 4000.0, False, buffer=1.03) is False
    assert tried == [], tried
    # Review 2026-09-16: with FX_CONVERT on, convert_into's USD-cross leg asked
    # fx_rate_live(USD, USD) == 1.0 and placed a market USD.HKD BUY on memory.
    fresh()
    seed({"USD/%s" % BASE: {"rate": 7.8, "at": ago(20)}})
    sent = []
    with Patch(_pair_mid=shut_market, FX_CONVERT=True,
               cash_by_ccy=lambda ib: {BASE: 200000.0, "USD": 0.0},
               _fx_order=lambda ib, b, q, side, qty, *a, **k: sent.append((side, b, q)) or True):
        assert ib_bot.ensure_ccy(None, "USD", 14000.0, False) is False
    assert sent == [], sent
    print("t6 no conversion is ever placed on a remembered rate OK")


def t7_callers_wired_the_right_way_round():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ib_bot.py"),
               encoding="utf-8").read()
    # sizing may use memory ...
    assert "rate = fx_rate(ib, ccy, BASE_CCY) if ccy != BASE_CCY else 1.0" in src
    # ... money and tax may not
    assert "rate = fx_rate_live(ib, ccy, src)" in src
    assert 'usd_per_ccy = fx_rate_live(ib, ccy, "USD")' in src
    assert 'fills_capture.capture(ib, lambda ccy: fx_rate_live(ib, ccy, "GBP"))' in src
    assert "_FX_REMEMBER = not dry" in src
    print("t7 sizing reads memory; conversions, funding and tax read live only OK")


if __name__ == "__main__":
    try:
        t1_live_rate_is_remembered_then_used_when_shut()
        t2_too_old_or_missing_is_still_nothing()
        t3_inverse_and_cross_are_marked_stale()
        t4_dry_run_never_writes_the_memory()
        t5_weekend_usd_entry_proceeds_when_usd_is_held()
        t6_weekend_never_converts_on_a_remembered_rate()
        t7_callers_wired_the_right_way_round()
    finally:
        fresh(remember=False)
    print("ALL FX-WEEKEND TESTS PASS")
