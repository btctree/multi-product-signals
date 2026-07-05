"""Compute every model WITH and WITHOUT the per-product SMA200 exit, dump JSON
for the Excel report. Focus: total-return comparison."""
import json
import statistics as st
import engine_rr
from engine_rr import run
from research_r7_trend import run_trend
from research_r8_combo import combo
from research_fx2 import risk_off_series, run_fx_hedge
from data_fetch import fetch_all
from universe import market_of

FY = list(range(2016, 2026))
engine_rr.run.return_curve = True


def stats_from(s, yearly):
    v = [yearly.get(y, 0) for y in FY]
    m = st.mean(v); sd = st.pstdev(v)
    return {"win": s["win"], "rr": s.get("rr", 0), "cagr": s["cagr"],
            "total": s["final"] / 150000 - 1, "final": s["final"], "dd": s["dd"],
            "calmar": s["cagr"] / -s["dd"] if s["dd"] < 0 else 0,
            "cov": sd / m if m else 0, "worst": min(v),
            "yearly": {y: yearly.get(y, 0) for y in range(2016, 2027)}}


def main():
    data = fetch_all()
    eq_configs = {
        "C1 Balanced": dict(K=3.5, K_tight=2.0, rsi_entry=25, near_high=0.88, slots=7),
        "C2 Max-R:R": dict(K=3.0, rsi_entry=15, slots=7),
        "C3 Max-R:R in DD": dict(K=3.0, rsi_entry=15, min_mom=0.05, slots=7),
    }
    # crypto + FX sleeves (shared, unchanged by SMA200 which is equity-specific)
    s_cr, eq_cr, _ = run_trend({t: d for t, d in data.items() if market_of(t) == "CRYPTO"},
                               N=55, K=4.0, slots=2, entry_mode="regime", exit_sma="sma_fast")
    ro = risk_off_series(data)
    s_fx, eq_fx = run_fx_hedge({t: d for t, d in data.items() if market_of(t) == "FX"},
                               ro, slots=2)

    rows = []
    eqcurves = {}  # (config, sma) -> curve, stats
    for name, kw in eq_configs.items():
        for sma, rex in [("No", None), ("Yes", 200)]:
            s, eq, _ = run(data, **kw, regime_exit=rex)
            eqcurves[(name, sma)] = (eq, s)
            rows.append({"model": name, "type": "Equity", "sma": sma,
                         **stats_from(s, s["yearly"])})

    # pools built on C1 equity sleeve
    def pool(eqe, se, w):
        curves = {"EQ": eqe, "CR": eq_cr, "FX": eq_fx}
        wins = {"EQ": (se["win"], se["trades"]), "CR": (s_cr["win"], s_cr["trades"]),
                "FX": (s_fx["win"], max(s_fx["trades"], 1))}
        return combo(curves, wins, w)

    pool_defs = {
        "P1 70eq/30cr": {"EQ": .70, "CR": .30},
        "P2 65/25/10FX": {"EQ": .65, "CR": .25, "FX": .10},
        "P4 70/20/10FX": {"EQ": .70, "CR": .20, "FX": .10},
    }
    for pname, w in pool_defs.items():
        for sma in ("No", "Yes"):
            eqe, se = eqcurves[("C1 Balanced", sma)]
            b = pool(eqe, se, w)
            rows.append({"model": pname, "type": "Pool", "sma": sma,
                         **stats_from(b, b["yearly"])})

    with open("../data/sma_report.json", "w") as f:
        json.dump(rows, f, indent=2, default=float)
    # console preview
    print(f"{'model':18s} {'SMA':4s} {'win':>5} {'CAGR':>6} {'total':>7} "
          f"{'final':>11} {'DD':>7} {'2018':>5} {'2022':>5}")
    for r in rows:
        print(f"{r['model']:18s} {r['sma']:4s} {r['win']*100:5.1f} {r['cagr']*100:6.1f} "
              f"{r['total']*100:6.0f}% {r['final']:11,.0f} {r['dd']*100:7.1f} "
              f"{r['yearly'].get('2018', r['yearly'].get(2018,0))*100:+5.0f} "
              f"{r['yearly'].get('2022', r['yearly'].get(2022,0))*100:+5.0f}")
    print("\nsaved ../data/sma_report.json")


if __name__ == "__main__":
    main()
