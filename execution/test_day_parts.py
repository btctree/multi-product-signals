#!/usr/bin/env python3
"""Golden tests for the per-day P&L breakdown (day_parts.py + the backfill).

Run from this directory:  python test_day_parts.py

Tapping a day in the P&L Calendar claims to say what that day's number was MADE
of. A breakdown that is merely plausible is worse than none at all: the owner
uses it to decide whether a red day was one position or the whole book. The
traps this locks down are the ones that actually bit during the build:

  * SYMBOL IDENTITY. fills_ledger records IB's ticker ("DBK"); positions are
    published under the site's ("DBK.DE"). Without the join, a 46-share
    purchase appeared as a +13,543 HKD gain with a matching -13,553
    "unexplained" - the single most misleading number the card could show.
  * THE BASE CURRENCY IS 1. Left as a free parameter, the rate fit returned
    HKD = 0.9867 and quietly swallowed the model error into a rate that cannot
    be anything but 1.
  * CLOSES CARRY FORWARD. A Tokyo holiday leaves a holding with no bar that
    date. It is still worth its last close - that is how the account itself is
    valued - and dropping the day instead cost 17 of 56 days on the first run.
  * AN UNREADABLE FILE IS A REPAIR JOB. Same rule as netliq_history: never
    silently rewrite days already recorded.
"""
import io
import json
import os
import sys
import tempfile

os.environ.setdefault("IB_BACKEND", "web")
import testenv                                      # noqa: E402
testenv.isolate("mps-dayparts-")

import day_parts                                    # noqa: E402
import backfill_day_parts as bf                     # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def t1_rates_from_ledger():
    led = {
        "BASE": {"exchangerate": 1, "netliquidationvalue": 216201},   # not a currency
        "USD": {"exchangerate": 7.8534, "cashbalance": 4297},
        "JPY": {"exchangerate": 0.050866},
        "EUR": {"exchangerate": "not a number"},
        "XXX": {"exchangerate": 0},                                   # unusable
        "junk": "not a dict",
    }
    r = day_parts.rates_from_ledger(led, base_ccy="HKD")
    assert "BASE" not in r, "the BASE row is the whole account, not a currency"
    assert r["USD"] == 7.8534 and r["JPY"] == 0.050866
    assert "EUR" not in r and "XXX" not in r, "an unusable rate must be absent, not 0"
    assert r["HKD"] == 1.0, "the base currency is 1 by definition"
    print("t1 ledger rates: BASE skipped, base pinned to 1, junk dropped OK")


def t2_build_row_joins_ib_and_site_symbols():
    rows = [
        {"symbol": "DBK.DE", "ib_symbol": "DBK", "qty": 46, "ccy": "EUR", "mkt_value": 1547.4},
        {"symbol": "NVDA", "ib_symbol": "NVDA", "qty": 20, "ccy": "USD", "mkt_value": 4380.0},
        {"symbol": "HKD.JPY", "ib_symbol": "HKD.JPY", "qty": 1, "ccy": "HKD",
         "mkt_value": 5, "sec_type": "CASH"},                     # an FX balance
        {"symbol": "GONE", "ib_symbol": "GONE", "qty": 0, "ccy": "USD", "mkt_value": 0},
        {"symbol": "NORATE", "ib_symbol": "NORATE", "qty": 3, "ccy": "SEK", "mkt_value": 90},
    ]
    rates = {"EUR": 8.83, "USD": 7.85, "HKD": 1.0}
    row = day_parts.build_row(rows, {"USD": 4297, "EUR": 0.4}, rates, 216201, exc=1234)
    assert row["ib"] == {"DBK": "DBK.DE"}, \
        "only a DIFFERING IB ticker is recorded, and it must be recorded"
    assert row["pos"]["DBK.DE"] == [46.0, round(1547.4 * 8.83, 2)]
    assert "HKD.JPY" not in row["pos"], "an FX balance is cash, not a holding"
    assert "GONE" not in row["pos"], "a closed position is not carried at zero"
    assert row["pos"]["NORATE"] == [3.0, None], \
        "no rate must read as UNPRICED, not as zero value"
    assert row["cash"] == {"USD": 4297}, "dust below 1 unit is not cash"
    assert row["nl"] == 216201 and row["exc"] == 1234 and row["src"] == "live"
    print("t2 row keys by site symbol, keeps IB's beside it, drops FX and dust OK")


def t3_upsert_replaces_the_day_and_stays_bounded():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "day_parts.json")
        r1 = day_parts.build_row([], {}, {"HKD": 1.0}, 100)
        day_parts.upsert(p, r1, day="2026-09-19")
        r2 = day_parts.build_row([], {}, {"HKD": 1.0}, 200)
        n = day_parts.upsert(p, r2, day="2026-09-19")
        doc = json.loads(io.open(p, encoding="utf-8").read())
        assert n == 1 and doc["days"]["2026-09-19"]["nl"] == 200, \
            "the last write of a day is the day's record"

        for i in range(1, 8):
            day_parts.upsert(p, r1, day="2026-08-%02d" % i, keep_days=3)
        doc = json.loads(io.open(p, encoding="utf-8").read())
        assert len(doc["days"]) == 3, "the file must stay bounded"
        # 2026-09-19 was written first but sorts LAST, so it is the one row
        # pruning must never reach - that is the point of pruning by date.
        assert sorted(doc["days"]) == ["2026-08-06", "2026-08-07", "2026-09-19"], \
            "pruning drops the OLDEST days, never the newest"

        io.open(p, "w", encoding="utf-8").write("{ this is not json")
        try:
            day_parts.upsert(p, r1, day="2026-09-20")
            raise AssertionError("a damaged file must RAISE, not be overwritten")
        except ValueError:
            pass
        assert io.open(p, encoding="utf-8").read().startswith("{ this is not"), \
            "the damaged file must be left in place for repair"
    print("t3 upsert replaces the day, prunes the oldest, refuses to clobber damage OK")


def t4_mark_at_carries_the_last_close_forward():
    px = {"2026-09-14": 10.0, "2026-09-15": 11.0, "2026-09-18": 12.0}
    assert bf.mark_at(px, "2026-09-15") == 11.0
    assert bf.mark_at(px, "2026-09-16") == 11.0, \
        "a holiday does not make a holding unpriced - it is worth its last close"
    assert bf.mark_at(px, "2026-09-19") == 12.0
    assert bf.mark_at(px, "2026-09-13") is None, \
        "before the first published bar is genuinely unknown"
    assert bf.mark_at({}, "2026-09-15") is None
    print("t4 closes carry forward over holidays, None before the first bar OK")


def t5_fit_recovers_known_rates():
    """Synthesise an account with rates we choose, then see if the fit finds them.

    Two currencies plus the base, prices that move, a position that changes
    size: netliq is computed EXACTLY, so any error in the recovered rates is
    the fitter's own.
    """
    true = {"USD": 7.85, "JPY": 0.0509}
    px, days = {"AAA": {}, "BBB": {}}, {}
    for i in range(20):
        d = "2026-08-%02d" % (i + 1)
        px["AAA"][d] = 100.0 + i          # USD name
        px["BBB"][d] = 2000.0 - 3 * i     # JPY name
        qa, qb = (10 + i % 3), 100
        cash = {"USD": 500.0, "JPY": 40000.0, "HKD": 250.0}
        nl = (qa * px["AAA"][d] * true["USD"] + qb * px["BBB"][d] * true["JPY"]
              + cash["USD"] * true["USD"] + cash["JPY"] * true["JPY"] + cash["HKD"])
        days[d] = {"netliq": nl, "excluded_cash": 0, "cash": cash,
                   "positions": [{"symbol": "AAA", "ccy": "USD", "qty": qa},
                                 {"symbol": "BBB", "ccy": "JPY", "qty": qb}]}
    rates, resid = bf.fit_rates(days, px, {})
    assert rates["HKD"] == 1.0, "the base currency must be pinned, not fitted"
    for c, v in true.items():
        assert abs(rates[c] - v) / v < 1e-6, \
            "fit missed %s: %r vs %r" % (c, rates[c], v)
    assert max(abs(r) for _, r, _ in resid) < 1e-6, "an exact system must fit exactly"
    print("t5 rate fit recovers known rates and pins the base OK")


def t6_the_dashboard_joins_the_two_symbol_books():
    """The join has to exist in the page, or the breakdown lies by 13,543 HKD."""
    page = io.open(os.path.join(ROOT, "docs", "index.html"),
                   encoding="utf-8").read().replace(" ", "")
    assert "constsym=(b.ib||{})[raw]||(a.ib||{})[raw]||raw;" in page, \
        "the day sheet no longer maps IB's ticker onto the published symbol"
    assert "if(f.sec_type==='CASH')return;" in page, \
        "a currency conversion must not be treated as a holding"
    assert "f.source==='estimate'" in page, \
        "seed fills must still be recognised (and counted, not skipped)"
    # A reconstructed day's rate-fit error must be NAMED, not left sitting inside
    # "unexplained". On 2026-09-03 it was -1,797 of the -1,734 the owner was
    # looking at, and he asked what the number was - which is the point: a
    # figure nobody can account for is worse than a bigger one that says what it is.
    assert "constrecon=(typeofb.fit===" in page, \
        "the sheet no longer separates the reconstruction error from the real remainder"
    assert "Reconstructionerror" in page, "the reconstruction error line is gone"
    # the publisher must actually be wired up, and must respect --dry
    pub = io.open(os.path.join(HERE, "publish_web.py"), encoding="utf-8").read()
    assert "day_parts.upsert" in pub, "the hourly publisher stopped writing day parts"
    assert pub.index('log("--dry: nothing written")') < pub.index("day_parts.upsert"), \
        "--dry must return BEFORE any day-parts write"
    assert "data/day_parts.json" in pub, "the new file is never committed"
    print("t6 page joins both symbol books; publisher wired and --dry-safe OK")


def t7_published_file_is_sane():
    p = os.path.join(ROOT, "data", "day_parts.json")
    if not os.path.exists(p):
        print("t7 no local day_parts.json - skipped")
        return
    doc = json.loads(io.open(p, encoding="utf-8").read())
    days = doc.get("days") or {}
    assert days, "file has no days"
    for d, r in days.items():
        assert len(d) == 10 and r.get("fx"), "row %s malformed" % d
        assert r["fx"].get("HKD") == 1.0, "row %s does not pin the base currency" % d
        assert r.get("src") in ("live", "reconstructed"), "row %s has no provenance" % d
        if r["src"] == "reconstructed":
            assert isinstance(r.get("fit"), (int, float)), \
                "reconstructed row %s does not carry its own fit error" % d
        for sym, v in (r.get("pos") or {}).items():
            assert isinstance(v, list) and len(v) == 2, "row %s position %s" % (d, sym)
    print("t7 %d published days parse, every one priced in a pinned base OK" % len(days))


if __name__ == "__main__":
    t1_rates_from_ledger()
    t2_build_row_joins_ib_and_site_symbols()
    t3_upsert_replaces_the_day_and_stays_bounded()
    t4_mark_at_carries_the_last_close_forward()
    t5_fit_recovers_known_rates()
    t6_the_dashboard_joins_the_two_symbol_books()
    t7_published_file_is_sane()
    print("ALL DAY PARTS TESTS PASS")
