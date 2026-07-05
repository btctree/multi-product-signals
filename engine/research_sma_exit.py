"""Does a per-product long-term SMA exit (drop a position when it closes below
its own 200 or 250 SMA) cut the 2018/2022 losses while keeping the good years?
Tests on the equity engine (C1) and the pool. Prints every year before/after.
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


def show(tag, s):
    print(f"{tag:30s} win {s['win']*100:4.1f}%  R:R {s.get('rr',0):4.2f}  "
          f"CAGR {s['cagr']*100:5.1f}%  dd {s['dd']*100:6.1f}%  | "
          f"2018 {s['yearly'].get(2018,0)*100:+.0f}%  2022 {s['yearly'].get(2022,0)*100:+.0f}%")
    print("   yr: " + " ".join(f"{y}:{s['yearly'].get(y,0)*100:+.0f}" for y in YEARS))


def main():
    data = fetch_all()
    base = dict(K=3.5, K_tight=2.0, rsi_entry=25, near_high=0.88, slots=7)

    variants = {
        "EQ baseline (trail only)": {},
        "EQ + SMA200 exit": dict(regime_exit=200),
        "EQ + SMA250 exit": dict(regime_exit=250),
        "EQ + SMA200 exit x2 closes": dict(regime_exit=200, regime_exit_consec=2),
        "EQ + SMA250 exit x2 closes": dict(regime_exit=250, regime_exit_consec=2),
    }
    eqcurves = {}
    stats = {}
    for name, kw in variants.items():
        s, eq, _ = run(data, **base, **kw)
        stats[name] = s
        eqcurves[name] = eq
        show(name, s)

    # pool impact: best SMA-exit equity vs baseline, blended with crypto
    s_cr, eq_cr, _ = run_trend({t: d for t, d in data.items() if market_of(t) == "CRYPTO"},
                               N=55, K=4.0, slots=2, entry_mode="regime", exit_sma="sma_fast")
    ro = risk_off_series(data)
    s_fx, eq_fx = run_fx_hedge({t: d for t, d in data.items() if market_of(t) == "FX"},
                               ro, slots=2)

    def pool(eq_e, se, w):
        curves = {"EQ": eq_e, "CR": eq_cr, "FX": eq_fx}
        wins = {"EQ": (se["win"], se["trades"]), "CR": (s_cr["win"], s_cr["trades"]),
                "FX": (s_fx["win"], max(s_fx["trades"], 1))}
        return combo(curves, wins, w)

    print("\n--- POOL 70/30 impact ---")
    show("Pool baseline", pool(eqcurves["EQ baseline (trail only)"],
                               stats["EQ baseline (trail only)"], {"EQ": .7, "CR": .3}))
    for key in ("EQ + SMA200 exit", "EQ + SMA250 exit", "EQ + SMA250 exit x2 closes"):
        show(f"Pool w/ {key[5:]}", pool(eqcurves[key], stats[key], {"EQ": .7, "CR": .3}))


if __name__ == "__main__":
    main()
