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


def t7_same_day_disposals_merge():
    # IB split one 8-share sell into 7 + 1. TCGA s105(1)(a) makes that ONE
    # disposal, and it must cost out exactly like the unsplit sale.
    base = [row("a", "2026-01-10", "BOT", 8, 200.0)]
    split = run(base + [row("b", "2026-07-01", "SLD", 7, 250.0, com=0.04),
                        row("c", "2026-07-01", "SLD", 1, 250.0, com=1.01)])
    whole = run(base + [row("b", "2026-07-01", "SLD", 8, 250.0, com=1.05)])
    assert len(split["disposals"]) == 1, len(split["disposals"])
    s, w = split["disposals"][0], whole["disposals"][0]
    assert s["qty"] == 8 and s["fills"] == 2, (s["qty"], s["fills"])
    for f in ("proceeds_gbp", "cost_gbp", "gain_gbp"):
        assert abs(s[f] - w[f]) < 0.02, (f, s[f], w[f])
    assert split["years"][s["tax_year"]]["disposals"] == 1
    # a different day must NOT merge
    two = run(base + [row("b", "2026-07-01", "SLD", 4, 250.0),
                      row("c", "2026-07-02", "SLD", 4, 250.0)])
    assert len(two["disposals"]) == 2
    print("t7 same-day merge OK", s["gain_gbp"], "=", w["gain_gbp"])


def t8_all_disposal_totals():
    # SA108's triggers count EVERY disposal. The captured-only totals keep
    # their old meaning; all_* must include the seed-basis disposal.
    r = run([row("a", "2026-01-10", "BOT", 50, 10.0, src="estimate"),
             row("b", "2026-07-01", "SLD", 50, 12.0),              # real sale, seeded cost
             row("c", "2026-02-10", "BOT", 10, 100.0, sym="U"),
             row("d", "2026-07-02", "SLD", 10, 90.0, sym="U")])     # captured loss
    y = r["years"]["2026/27"]
    assert y["disposals"] == 2
    assert y["proceeds_gbp"] == 675.0, y["proceeds_gbp"]          # U only
    assert y["all_proceeds_gbp"] == 1125.0, y["all_proceeds_gbp"]  # + T 450
    assert y["proceeds_estimated"] == 0                           # T's SALE is real
    assert y["in_gains"] == 2 and y["est_in_gains"] == 1, y      # T est. cost, U exact
    # U: 675 - .75 - 750.75 = -76.50 ; T: 450 - .75 - 375.75 = +73.50
    assert abs(y["net_gain_gbp"] - (-76.50)) < 0.01, y["net_gain_gbp"]
    assert abs(y["all_net_gain_gbp"] - (-3.00)) < 0.01, y["all_net_gain_gbp"]
    # a seeded SELL row marks its proceeds as estimated
    r2 = run([row("a", "2026-01-10", "BOT", 5, 10.0, src="estimate"),
              row("b", "2026-07-01", "SLD", 5, 12.0, src="estimate")])
    assert r2["disposals"][0]["proceeds_estimated"] is True
    assert r2["years"]["2026/27"]["proceeds_estimated"] == 1
    print("t8 all-disposal totals OK", y["all_proceeds_gbp"], y["all_net_gain_gbp"])


def t9_same_day_beats_earlier_30_day():
    # s106A(9): a later disposal's SAME-DAY acquisition is not available to an
    # earlier disposal's 30-day rule. Board repro; the old single pass had it
    # the wrong way round.
    r = run([row("a", "2026-05-01", "BOT", 10, 100.0),
             row("b", "2026-07-01", "SLD", 10, 120.0),
             row("c", "2026-07-05", "BOT", 10, 110.0),
             row("d", "2026-07-05", "SLD", 10, 115.0)])
    by = {d["date"]: d for d in r["disposals"]}
    assert by["2026-07-01"]["rules"] == ["s104_pool"], by["2026-07-01"]["rules"]
    assert by["2026-07-05"]["rules"] == ["same_day"], by["2026-07-05"]["rules"]
    # 07-01: 900 - .75 - (750 + .75) = 148.50 ; 07-05: 862.50 - .75 - 825.75 = 36.00
    assert abs(by["2026-07-01"]["gain_gbp"] - 148.50) < 0.01, by["2026-07-01"]["gain_gbp"]
    assert abs(by["2026-07-05"]["gain_gbp"] - 36.00) < 0.01, by["2026-07-05"]["gain_gbp"]
    # across 5 April the gain must land in the right YEAR, not just the right total
    x = run([row("a", "2027-02-01", "BOT", 10, 100.0),
             row("b", "2027-04-03", "SLD", 10, 120.0),
             row("c", "2027-04-07", "BOT", 10, 110.0),
             row("d", "2027-04-07", "SLD", 10, 115.0)], today=date(2027, 12, 1))
    assert abs(x["years"]["2026/27"]["all_net_gain_gbp"] - 148.50) < 0.01, x["years"]["2026/27"]
    assert abs(x["years"]["2027/28"]["all_net_gain_gbp"] - 36.00) < 0.01, x["years"]["2027/28"]
    print("t9 same-day before earlier 30-day OK")


def t10_unvalued_proceeds():
    # a sale with no GBP rate has UNKNOWN proceeds - flagged, never counted as
    # an estimate, never silently folded into the totals
    r = run([row("a", "2026-01-10", "BOT", 5, 10.0),
             row("b", "2026-07-01", "SLD", 5, 12.0, rate=None),
             row("c", "2026-02-10", "BOT", 5, 10.0, sym="U"),
             row("d", "2026-07-02", "SLD", 5, 12.0, sym="U")])
    by = {d["symbol"]: d for d in r["disposals"]}
    assert by["T"]["proceeds_unvalued"] is True and by["T"]["proceeds_estimated"] is False
    assert by["U"]["proceeds_unvalued"] is False
    y = r["years"]["2026/27"]
    assert y["proceeds_unvalued"] == 1 and y["proceeds_estimated"] == 0, y
    assert y["all_proceeds_gbp"] == 45.0, y["all_proceeds_gbp"]      # U only: 5*12*.75
    # the unvalued sale is in no gains figure, and is NOT an estimated-cost one
    assert y["in_gains"] == 1 and y["est_in_gains"] == 0, y
    print("t10 unvalued proceeds flagged OK")


def t11_unknown_cost_not_a_gain():
    # A purchase with no GBP rate costs £0, so the whole sale read as gain -
    # and all_gains feeds the SA108 £3,000 test. Its PROCEEDS are known and
    # still count towards £50,000; its gain must not count anywhere.
    r = run([row("a", "2026-01-10", "BOT", 5, 10.0, rate=None),
             row("b", "2026-07-01", "SLD", 5, 12.0)])
    d = r["disposals"][0]
    assert d["cost_unknown"] is True, d
    assert d["gain_gbp"] is None, d["gain_gbp"]          # unknown, not proceeds-as-gain
    y = r["years"]["2026/27"]
    assert y["cost_unknown"] == 1 and y["in_gains"] == 0, y
    assert y["all_gains_gbp"] == 0.0 and y["all_losses_gbp"] == 0.0, y
    assert y["all_proceeds_gbp"] == 45.0, y["all_proceeds_gbp"]
    # selling more than the ledger holds is the other unknown-cost route
    u = run([row("a", "2026-07-01", "SLD", 5, 12.0)])
    assert u["disposals"][0]["cost_unknown"] is True
    assert u["years"]["2026/27"]["all_gains_gbp"] == 0.0
    print("t11 unknown cost kept out of gains OK")


def t12_merged_unvalued_has_no_rate():
    # 7 valued + 1 unvalued fill on one day: one unvalued disposal with NO rate,
    # whichever order the fills arrived in
    base = [row("a", "2026-01-10", "BOT", 8, 200.0)]
    for rates in ((0.75, None), (None, 0.75)):
        r = run(base + [dict(row("b", "2026-07-01", "SLD", 7, 250.0, rate=rates[0]), ts="2026-07-01 10:00"),
                        dict(row("c", "2026-07-01", "SLD", 1, 250.0, rate=rates[1]), ts="2026-07-01 10:01")])
        d = r["disposals"][0]
        assert d["fills"] == 2 and d["proceeds_unvalued"] is True, d
        assert d["gbp_rate"] is None, (rates, d["gbp_rate"])
    print("t12 merged unvalued carries no rate OK")


def t13_missing_commission_rate_falls_back():
    # Guard, not a fix: a lot whose COMMISSION rate is missing must fall back to
    # its trade rate (_fee_gbp) and keep an exact, captured cost - never £0.
    a = row("a", "2026-01-10", "BOT", 10, 100.0)
    a["gbp_rate_commission"] = None
    b = row("b", "2026-02-10", "BOT", 10, 100.0)
    r = run([a, b, row("c", "2026-07-01", "SLD", 20, 110.0)])
    d = r["disposals"][0]
    # cost = 20*100*.75 + two fees at .75 = 1501.50
    assert abs(d["cost_gbp"] - 1501.50) < 0.01, d["cost_gbp"]
    assert d["cost_unknown"] is False and d["basis_quality"] == "captured", d
    print("t13 missing commission rate falls back OK", d["cost_gbp"])


def t14_pool_resets_after_full_exit():
    # A rate-less lot taints the pool only while its shares are IN it. Sold out
    # and re-bought, the new pool is clean and the later gain counts.
    r = run([row("a", "2026-01-10", "BOT", 5, 10.0, rate=None),
             row("b", "2026-03-01", "SLD", 5, 12.0),
             row("c", "2026-06-01", "BOT", 10, 100.0),
             row("d", "2026-09-01", "SLD", 10, 150.0)])
    by = {d["date"]: d for d in r["disposals"]}
    assert by["2026-03-01"]["cost_unknown"] is True
    late = by["2026-09-01"]
    assert late["cost_unknown"] is False and late["basis_quality"] == "captured", late
    # 1125 - .75 - (750 + .75) = 373.50
    assert abs(late["gain_gbp"] - 373.50) < 0.01, late["gain_gbp"]
    y = r["years"]["2026/27"]
    assert y["in_gains"] == 1 and abs(y["all_gains_gbp"] - 373.50) < 0.01, y
    # likewise a SEEDED lot is estimated only while it is in the pool
    sd = run([row("a", "2026-01-10", "BOT", 5, 10.0, src="estimate"),
              row("b", "2026-03-01", "SLD", 5, 12.0),
              row("c", "2026-06-01", "BOT", 10, 100.0),
              row("d", "2026-09-01", "SLD", 10, 150.0)])
    assert {d["date"]: d for d in sd["disposals"]}["2026-09-01"]["basis_quality"] == "captured"
    print("t14 pool resets after a full exit OK")


def t15_unvalued_and_unknown_cost():
    # No GBP rate on the day at all: a same-day round trip is BOTH unvalued and
    # unknown-cost. It is counted as unvalued only - the unknown-cost note says
    # "the proceeds still count", and here they cannot.
    r = run([row("a", "2026-07-01", "BOT", 5, 10.0, rate=None),
             row("b", "2026-07-01", "SLD", 5, 12.0, rate=None),
             row("c", "2026-01-10", "BOT", 5, 10.0, sym="U"),
             row("d", "2026-07-02", "SLD", 5, 12.0, sym="U")])
    both = {d["symbol"]: d for d in r["disposals"]}["T"]
    assert both["proceeds_unvalued"] and both["cost_unknown"], both
    y = r["years"]["2026/27"]
    assert y["proceeds_unvalued"] == 1 and y["cost_unknown"] == 0, y
    assert y["in_gains"] == 1 and y["all_proceeds_gbp"] == 45.0, y
    print("t15 unvalued + unknown cost counted once OK")


if __name__ == "__main__":
    t1_same_day(); t2_thirty_day(); t3_s104_average()
    t4_estimate_excluded(); t5_reentry_and_provisional(); t6_partial_multi_rule()
    t7_same_day_disposals_merge(); t8_all_disposal_totals()
    t9_same_day_beats_earlier_30_day(); t10_unvalued_proceeds()
    t11_unknown_cost_not_a_gain(); t12_merged_unvalued_has_no_rate()
    t13_missing_commission_rate_falls_back()
    t14_pool_resets_after_full_exit(); t15_unvalued_and_unknown_cost()
    print("ALL GOLDEN TESTS PASS")
