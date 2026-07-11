"""Re-validation of the LIVE product (P1 + SMA200, 70% equity dip / 30% crypto
trend, monthly rebalanced) on the expanded pool — user-requested comparison:

  A  BASELINE   original ~200-product universe, original config (DIP 5 slots)
  B  KILO-ORIG  full 1,002-pool, original config (DIP 5 slots)
  C  KILO-S60   full pool, score>60 gate only (mom90 >= +30%), DIP 5 slots
  D  KILO-S60-15 full pool, score>60 gate + 15 TOTAL positions (DIP 13 + CRY 2)

Honest engine throughout (next-open fills, costs, SMA200 regime exit, no
look-ahead). Mandate gates checked: maxDD <= 30%; win rate reported.
"""
import json

import engine_rr
from engine_rr import run
from research_r7_trend import run_trend
from research_r8_combo import combo
from data_fetch import fetch_all
from universe import market_of, load_universe

engine_rr.run.return_curve = True
YEARS = list(range(2016, 2027))
C1 = dict(K=3.5, K_tight=2.0, rsi_entry=25, near_high=0.88, regime_exit=200)


def old_universe_subset(data):
    """Reconstruct the pre-expansion (~200-product) universe: tickers with no
    add-reason recorded (original members) plus the 'restored' ones."""
    u = load_universe()
    reasons = u.get("add_reasons", {})
    keep = []
    for t in u["tickers"]:
        r = reasons.get(t, "")
        if r == "" or r.startswith("restored"):
            keep.append(t)
    return {t: d for t, d in data.items() if t in keep}


def two_sleeve(data_eq_src, dip_slots, min_mom, data_all, tag):
    s_eq, eq_eq, tr_eq = run(data_eq_src, **C1, slots=dip_slots, min_mom=min_mom,
                             markets=("US", "HK", "JP", "EU"))
    s_cr, eq_cr, tr_cr = run_trend({t: d for t, d in data_all.items()
                                    if market_of(t) == "CRYPTO"},
                                   N=55, K=4.0, slots=2, entry_mode="regime",
                                   exit_sma="sma_fast")
    b = combo({"EQ": eq_eq, "CR": eq_cr},
              {"EQ": (s_eq["win"], s_eq["trades"]),
               "CR": (s_cr["win"], s_cr["trades"])},
              {"EQ": .70, "CR": .30})
    print(f"\n=== {tag} ===")
    print(f"  EQUITY sleeve: n={s_eq['trades']} win={s_eq['win']*100:.1f}% "
          f"R:R={s_eq['rr']:.2f} PF={s_eq['pf']:.2f} CAGR={s_eq['cagr']*100:.1f}% "
          f"dd={s_eq['dd']*100:.1f}% Sharpe={s_eq['sharpe']:.2f}")
    print(f"  COMBINED 70/30: win={b['win']*100:.1f}% CAGR={b['cagr']*100:.1f}% "
          f"dd={b['dd']*100:.1f}% 150k->{b['final']:,.0f} "
          f"[DD gate {'PASS' if b['dd'] >= -0.30 else 'BREACH'}]")
    print("  yearly: " + " ".join(f"{y}:{b['yearly'].get(y,0)*100:+.0f}"
                                  for y in YEARS))
    return {"tag": tag, "equity": {k: v for k, v in s_eq.items()
                                   if k not in ("yearly", "per_market_win", "yearly_win", "yearly_n")},
            "combined": {"win": b["win"], "cagr": b["cagr"], "dd": b["dd"],
                         "final": b["final"],
                         "yearly": {y: b["yearly"].get(y, 0) for y in YEARS}}}


def main():
    data = fetch_all()
    old = old_universe_subset(data)
    print(f"pool: {len(data)} products | reconstructed original universe: {len(old)}")

    out = []
    out.append(two_sleeve(old, 5, 0.0, data, "A BASELINE original ~200, DIP 5 slots"))
    out.append(two_sleeve(data, 5, 0.0, data, "B KILO-ORIG full pool, DIP 5 slots"))
    out.append(two_sleeve(data, 5, 0.30, data, "C KILO-S60 full pool, score>60, DIP 5"))
    out.append(two_sleeve(data, 13, 0.30, data, "D KILO-S60-15 full pool, score>60, DIP 13 + CRY 2 = 15 pos"))

    with open("../data/revalidation.json", "w") as f:
        json.dump(out, f, indent=1, default=float)
    print("\nsaved ../data/revalidation.json")


if __name__ == "__main__":
    main()
