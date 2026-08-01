"""Does adding a BREAKOUT-CONTINUATION sleeve (a genuinely different, trend-entry
strategy) improve the live D product? Pre-registered, gated, no mining.

Sleeves (each its own validated-style engine, blended monthly via combo()):
  DIP : buy oversold dips in uptrends (score>60 gate) — mean reversion
  BRK : buy 55-day-high breakouts in uptrends, ride with a chandelier trail —
        trend continuation (LOW correlation to DIP by construction)
  CRY : crypto trend (BTC/ETH)

All 15 positions total. Compare to baseline D (13 dip + 2 cry). A blend is only
worth adopting if it BEATS D on risk-adjusted terms (Calmar) AND keeps DD<=30%.
Honest note: BRK win rate is expected ~45% (trend engines are right less often
but win bigger) — what matters is the BLEND, not the sleeve in isolation.
"""
import json

import engine_rr
from engine_rr import run
from research_r7_trend import run_trend
from research_r8_combo import combo
from data_fetch import fetch_all
from universe import market_of

engine_rr.run.return_curve = True
YEARS = list(range(2016, 2027))
EQ_MKTS = ("US", "HK", "JP", "EU")
C1 = dict(K=3.5, K_tight=2.0, rsi_entry=25, near_high=0.88, regime_exit=200)


def eq_curves(data, dip_slots, brk_slots):
    s_dip, eq_dip, tr_dip = run(data, **C1, slots=dip_slots, min_mom=0.30,
                                markets=EQ_MKTS)
    eqd = {t: d for t, d in data.items() if market_of(t) in EQ_MKTS}
    s_brk, eq_brk, tr_brk = run_trend(eqd, N=55, K=4.0, slots=brk_slots,
                                      entry_mode="breakout")
    return (s_dip, eq_dip), (s_brk, eq_brk)


def show(tag, s, base_calmar=None):
    dd = s["dd"]
    calmar = s["cagr"] / -dd if dd < 0 else 0
    reds = [y for y in YEARS if s["yearly"].get(y, 0) < 0]
    flag = "PASS" if dd >= -0.30 else "DD-BREACH"
    mark = ""
    if base_calmar is not None:
        mark = "  <-- beats D" if calmar > base_calmar and dd >= -0.30 else "  (no better)"
    print(f"{tag:34s} win={s['win']*100:4.1f}% CAGR={s['cagr']*100:5.1f}% "
          f"dd={dd*100:6.1f}% Calmar={calmar:.2f} 150k->{s['final']:,.0f} "
          f"[{flag}]{mark}")
    print("   yr: " + " ".join(f"{y}:{s['yearly'].get(y,0)*100:+.0f}" for y in YEARS)
          + f"  reds={reds}")
    return calmar


def main():
    data = fetch_all()
    s_cr, eq_cr, tr_cr = run_trend({t: d for t, d in data.items()
                                    if market_of(t) == "CRYPTO"},
                                   N=55, K=4.0, slots=2, entry_mode="regime",
                                   exit_sma="sma_fast")
    cry = (s_cr, eq_cr)

    # --- baseline D: dip 13 + cry 2 ---
    (s_dip13, eq_dip13), _ = eq_curves(data, 13, 1)  # brk slots irrelevant here
    base = combo({"DIP": eq_dip13, "CRY": eq_cr},
                 {"DIP": (s_dip13["win"], s_dip13["trades"]),
                  "CRY": (s_cr["win"], s_cr["trades"])},
                 {"DIP": .70, "CRY": .30})
    print("=== standalone sleeves (equity) ===")
    print(f"DIP-13: win={s_dip13['win']*100:.1f}% CAGR={s_dip13['cagr']*100:.1f}% "
          f"dd={s_dip13['dd']*100:.1f}%")
    # breakout standalone at a representative slot count
    _, (s_brk7, eq_brk7) = eq_curves(data, 1, 7)
    print(f"BRK-7 : win={s_brk7['win']*100:.1f}% CAGR={s_brk7['cagr']*100:.1f}% "
          f"dd={s_brk7['dd']*100:.1f}%  (trend engine — low win by design)")
    print(f"CRY-2 : win={s_cr['win']*100:.1f}% CAGR={s_cr['cagr']*100:.1f}% "
          f"dd={s_cr['dd']*100:.1f}%\n")

    print("=== BASELINE ===")
    base_calmar = show("D  70dip/30cry (13+2)", base)

    print("\n=== D + breakout sleeve (three sleeves, 15 positions) ===")
    variants = [
        ("V1 45dip/25brk/30cry (8+5+2)", 8, 5, {"DIP": .45, "BRK": .25, "CRY": .30}),
        ("V2 35dip/35brk/30cry (6+7+2)", 6, 7, {"DIP": .35, "BRK": .35, "CRY": .30}),
        ("V3 55dip/15brk/30cry (10+3+2)", 10, 3, {"DIP": .55, "BRK": .15, "CRY": .30}),
        ("V4 50dip/20brk/30cry (9+4+2)", 9, 4, {"DIP": .50, "BRK": .20, "CRY": .30}),
    ]
    results = {"D_baseline": base}
    for tag, ds, bs, w in variants:
        (s_dip, eq_dip), (s_brk, eq_brk) = eq_curves(data, ds, bs)
        b = combo({"DIP": eq_dip, "BRK": eq_brk, "CRY": eq_cr},
                  {"DIP": (s_dip["win"], s_dip["trades"]),
                   "BRK": (s_brk["win"], s_brk["trades"]),
                   "CRY": (s_cr["win"], s_cr["trades"])}, w)
        show(tag, b, base_calmar)
        results[tag] = {"win": b["win"], "cagr": b["cagr"], "dd": b["dd"],
                        "final": b["final"], "yearly": b["yearly"]}

    with open("../data/breakout_test.json", "w") as f:
        json.dump(results, f, indent=1, default=float)
    print("\nsaved ../data/breakout_test.json")
    print("DECISION RULE: adopt only if a blend has higher Calmar than D AND DD<=30%.")


if __name__ == "__main__":
    main()
