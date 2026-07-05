"""Investigate 2018 & 2022 and test a mechanism-based defense.

Pattern (both down years): broad risk-off / Fed-tightening bear markets where
EVERYTHING fell together (equities AND crypto), so within-market diversification
didn't help. Defense hypothesis (principled, not mined): stop buying dips while
the BROAD market is in a downtrend (S&P < its 200-day). Dip-buying in a market
downtrend = catching falling knives = the 2018/2022 loss mechanism.

Hard constraint (user): the fix must improve 2018/2022 WITHOUT hurting good years.
We print EVERY year before/after so any collateral damage is visible.
"""
import engine_rr
from engine_rr import run
from research_r7_trend import run_trend
from research_r8_combo import combo
from research_fx2 import risk_off_series, run_fx_hedge
from data_fetch import fetch_all
from universe import market_of

YEARS = list(range(2016, 2027))
engine_rr.run.return_curve = True


def yline(tag, y):
    return f"{tag:22s} " + " ".join(f"{y}:{ny*100:+.0f}" for y, ny in
                                    [(y, y2) for y, y2 in
                                     [(yr, y.get(yr, 0)) for yr in YEARS]])


def show(tag, s):
    print(f"{tag:26s} win {s['win']*100:4.1f}%  CAGR {s['cagr']*100:5.1f}%  "
          f"dd {s['dd']*100:6.1f}%  |  2018 {s['yearly'].get(2018,0)*100:+.0f}%  "
          f"2022 {s['yearly'].get(2022,0)*100:+.0f}%")
    print("   all yr: " + " ".join(f"{y}:{s['yearly'].get(y,0)*100:+.0f}" for y in YEARS))


def main():
    data = fetch_all()
    ro = risk_off_series(data)
    print(f"S&P risk-off (below 200d): {ro.mean()*100:.0f}% of days\n")

    # --- equity engine: baseline vs risk-off-gated ---
    s0, eq0, _ = run(data, K=3.5, K_tight=2.0, rsi_entry=25, near_high=0.88, slots=7)
    s1, eq1, _ = run(data, K=3.5, K_tight=2.0, rsi_entry=25, near_high=0.88, slots=7,
                     market_gate="spx")
    show("EQUITY baseline", s0)
    show("EQUITY + risk-off gate", s1)

    # --- crypto sleeve (own trend gate already dodges crashes) ---
    s_cr, eq_cr, _ = run_trend({t: d for t, d in data.items() if market_of(t) == "CRYPTO"},
                               N=55, K=4.0, slots=2, entry_mode="regime", exit_sma="sma_fast")
    print(f"\nCRYPTO trend sleeve alone: 2018 {s_cr['yearly'].get(2018,0)*100:+.0f}%  "
          f"2022 {s_cr['yearly'].get(2022,0)*100:+.0f}%")

    # --- gated FX hedge ---
    s_fx, eq_fx = run_fx_hedge({t: d for t, d in data.items() if market_of(t) == "FX"},
                               ro, slots=2)

    # --- pools: baseline vs full-defense (gated equity + crypto + FX hedge) ---
    def pool(eq_e, w):
        curves = {"EQ": eq_e, "CR": eq_cr, "FX": eq_fx}
        wins = {"EQ": (s0["win"], s0["trades"]), "CR": (s_cr["win"], s_cr["trades"]),
                "FX": (s_fx["win"], max(s_fx["trades"], 1))}
        return combo(curves, wins, w)

    print("\n--- POOLS (per-year) ---")
    show("P1 baseline 70/30", pool(eq0, {"EQ": .70, "CR": .30}))
    show("P1 + gate 70/30", pool(eq1, {"EQ": .70, "CR": .30}))
    show("DEF 65eq(gate)/25cr/10fx", pool(eq1, {"EQ": .65, "CR": .25, "FX": .10}))
    show("DEF 60eq(gate)/30cr/10fx", pool(eq1, {"EQ": .60, "CR": .30, "FX": .10}))


if __name__ == "__main__":
    main()
