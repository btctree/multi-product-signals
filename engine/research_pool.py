"""The unified rotation pool: each market on its best-fit engine, mixed and
monthly-rebalanced. Equity (US/HK/JP/EU) -> dip engine (C1 balanced); crypto ->
trend engine; FX -> regime-gated risk-off hedge (only active when S&P<SMA200).
Reports full metrics + per-year and whether the gated FX smooths bad years
without dragging good ones.
"""
import engine_rr
from engine_rr import run
from research_r7_trend import run_trend
from research_r8_combo import combo
from research_fx2 import risk_off_series, run_fx_hedge
from data_fetch import fetch_all
from universe import market_of

YEARS = list(range(2016, 2027))


def line(tag, s):
    print(f"{tag:26s} win {s['win']*100:4.1f}%  R:R {s.get('rr',0):4.2f}  "
          f"CAGR {s['cagr']*100:5.1f}%  dd {s['dd']*100:6.1f}%  "
          f"Sharpe {s.get('sharpe',0):.2f}  Calmar {s.get('calmar',0):.2f}  "
          f"150k->{s['final']:,.0f}")
    print("   yr: " + " ".join(f"{y}:{s['yearly'].get(y,0)*100:+.0f}" for y in YEARS))


def main():
    data = fetch_all()
    engine_rr.run.return_curve = True

    # sleeves (each = best engine for that market)
    s_eq, eq_eq, _ = run(data, K=3.5, K_tight=2.0, rsi_entry=25, near_high=0.88, slots=7)
    s_cr, eq_cr, _ = run_trend({t: d for t, d in data.items() if market_of(t) == "CRYPTO"},
                               N=55, K=4.0, slots=2, entry_mode="regime", exit_sma="sma_fast")
    ro = risk_off_series(data)
    s_fx, eq_fx = run_fx_hedge({t: d for t, d in data.items() if market_of(t) == "FX"},
                               ro, slots=2)

    curves = {"EQ": eq_eq, "CR": eq_cr, "FX": eq_fx}
    wins = {"EQ": (s_eq["win"], s_eq["trades"]),
            "CR": (s_cr["win"], s_cr["trades"]),
            "FX": (s_fx["win"], max(s_fx["trades"], 1))}

    print("Sleeves standalone:")
    line("  EQ dip (C1)", s_eq)
    line("  CR trend", s_cr)
    line("  FX gated hedge", s_fx)

    POOLS = [
        ("P1 70EQ/30CR", {"EQ": .70, "CR": .30}),
        ("P2 65EQ/25CR/10FX", {"EQ": .65, "CR": .25, "FX": .10}),
        ("P3 60EQ/30CR/10FX", {"EQ": .60, "CR": .30, "FX": .10}),
        ("P4 70EQ/20CR/10FX", {"EQ": .70, "CR": .20, "FX": .10}),
        ("P5 55EQ/35CR/10FX", {"EQ": .55, "CR": .35, "FX": .10}),
    ]
    print("\nRotation pools (monthly rebalance):")
    for tag, w in POOLS:
        b = combo(curves, wins, w)
        line(tag, b)


if __name__ == "__main__":
    main()
