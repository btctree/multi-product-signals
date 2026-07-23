"""Golden tests for the UK share-matching engine (run: python test_uk_cgt.py)."""
from datetime import date
import uk_cgt


def row(execId, d, side, qty, price, rate=0.75, ccy="USD", src="api", com=1.0, sym="T"):
    return {"execId": execId, "date": d, "ts": d + " 10:00", "symbol": sym,
            "con_id": 1, "sec_type": "STK", "side": side, "qty": qty, "price": price,
            "ccy": ccy, "commission": com, "commission_ccy": ccy,
            "gbp_rate": rate, "gbp_rate_commission": rate, "source": src, "name": sym}


def run(rows, today=date(2026, 12, 1)):
    return uk_cgt.compute(rows, today=today)


def t1_same_day():
    r = run([row("a", "2026-07-01", "BOT", 100, 10.0),
             row("b", "2026-07-01", "SLD", 100, 12.0)])
    d = r["disposals"][0]
    assert d["rules"] == ["same_day"], d["rules"]
    # proceeds 100*12*.75=900; cost 100*10*.75+0.75=750.75; sell fee .75 -> gain 148.50
    assert abs(d["gain_gbp"] - 148.50) < 0.01, d["gain_gbp"]
    print("t1 same-day OK", d["gain_gbp"])


def t2_thirty_day():
    # sell from an old pool, re-buy 10 days later -> the RE-BUY matches the sale
    r = run([row("a", "2026-01-10", "BOT", 100, 10.0),
             row("b", "2026-07-01", "SLD", 100, 8.0),
             row("c", "2026-07-11", "BOT", 100, 7.0)])
    d = r["disposals"][0]
    assert d["rules"] == ["30_day"], d["rules"]
    # proceeds 600; cost = re-buy 100*7*.75 + fee .75 = 525.75; fee .75 -> gain 73.50
    assert abs(d["gain_gbp"] - 73.50) < 0.01, d["gain_gbp"]
    # the OLD lot must remain as the open pool
    assert r["open_positions"][0]["qty"] == 100
    print("t2 30-day re-match OK", d["gain_gbp"])


def t3_s104_average():
    r = run([row("a", "2026-01-10", "BOT", 100, 10.0),   # cost 750 + .75
             row("b", "2026-02-10", "BOT", 100, 20.0),   # cost 1500 + .75
             row("c", "2026-07-01", "SLD", 100, 18.0)])
    d = r["disposals"][0]
    assert d["rules"] == ["s104_pool"], d["rules"]
    # pool 200 sh cost 2251.50 -> avg 11.2575; sell 100: proceeds 1350 - fee .75 - 1125.75 = 223.50
    assert abs(d["gain_gbp"] - 223.50) < 0.01, d["gain_gbp"]
    assert abs(r["open_positions"][0]["cost_gbp"] - 1125.75) < 0.01
    print("t3 s104 average OK", d["gain_gbp"])


def t4_estimate_excluded():
    r = run([row("a", "2026-01-10", "BOT", 50, 10.0, src="estimate"),
             row("b", "2026-07-01", "SLD", 50, 12.0)])
    d = r["disposals"][0]
    assert d["basis_quality"] == "ESTIMATED"
    y = r["years"][d["tax_year"]]
    assert y["excluded_estimated"] == 1 and y["proceeds_gbp"] == 0.0
    print("t4 estimate exclusion OK")


def t5_reentry_and_provisional():
    r = run([row("a", "2026-07-01", "BOT", 10, 100.0),
             row("b", "2026-07-20", "SLD", 10, 110.0),
             row("c", "2026-09-01", "BOT", 10, 90.0)],
            today=date(2026, 7, 25))                       # 5 days after sale
    d = r["disposals"][0]
    assert d["provisional_until"] is not None
    assert d["rules"] == ["s104_pool"]
    print("t5 re-entry + provisional OK")


def t6_partial_multi_rule():
    r = run([row("a", "2026-01-10", "BOT", 60, 10.0),
             row("b", "2026-07-01", "SLD", 100, 12.0),
             row("c", "2026-07-01", "BOT", 30, 11.0),      # same-day 30
             row("d", "2026-07-15", "BOT", 10, 11.5)])     # 30-day 10, pool 60
    d = r["disposals"][0]
    assert set(d["rules"]) == {"same_day", "30_day", "s104_pool"}, d["rules"]
    assert sum(s["qty"] for s in d["slices"]) == 100
    print("t6 multi-rule split OK", d["rules"])


if __name__ == "__main__":
    t1_same_day(); t2_thirty_day(); t3_s104_average()
    t4_estimate_excluded(); t5_reentry_and_provisional(); t6_partial_multi_rule()
    print("ALL GOLDEN TESTS PASS")
