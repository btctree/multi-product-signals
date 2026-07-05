"""Test the user's hypothesis: does FX help (profit / diversify) when equities
are in a bad environment (2018, 2022)? The dip engine can't trade FX properly,
so use a long-only TREND engine on the 4 pairs (it can hold long-USD via
USD/JPY & USD/CAD in risk-off) and check the bad-year contribution + a blend.
"""
import engine_rr
from engine_rr import run
from research_r7_trend import run_trend
from research_r8_combo import combo
from data_fetch import fetch_all
from universe import market_of

YEARS = list(range(2016, 2027))
engine_rr.run.return_curve = True


def yr(s):
    return " ".join(f"{y}:{s['yearly'].get(y, 0)*100:+.0f}" for y in YEARS)


def main():
    data = fetch_all()
    fx_data = {t: d for t, d in data.items() if market_of(t) == "FX"}

    # 1) FX long-only TREND sleeve (regime entry + 4xATR trail), 2 slots
    s_fx, eq_fx, tr_fx = run_trend(fx_data, N=55, K=4.0, slots=2,
                                   entry_mode="regime", exit_sma="sma_fast")
    print("FX TREND sleeve (long-only, 2 slots, spot, no leverage):")
    print(f"  trades={s_fx['trades']} win={s_fx['win']*100:.1f}% "
          f"CAGR={s_fx['cagr']*100:.1f}% dd={s_fx['dd']*100:.1f}% "
          f"final={s_fx['final']:,.0f}")
    print("  yearly: " + yr(s_fx))
    if not tr_fx.empty:
        pp = tr_fx.groupby("ticker").agg(n=("net_ret", "size"),
                                         win=("net_ret", lambda x: round((x > 0).mean()*100)),
                                         pnl=("pnl", lambda x: round(x.sum())))
        print("  per-pair:\n" + pp.to_string())
        tr_fx = tr_fx.copy()
        import pandas as pd
        tr_fx["yr"] = pd.to_datetime(tr_fx["exit_date"]).dt.year
        byr = tr_fx.groupby("yr")["pnl"].sum().round()
        print("  FX pnl by year:", {int(k): int(v) for k, v in byr.items()})

    # 2) C1 equity engine curve
    s_c1, eq_c1, tr_c1 = run(data, K=3.5, K_tight=2.0, rsi_entry=25,
                             near_high=0.88, slots=7)
    print(f"\nC1 equity alone: win {s_c1['win']*100:.1f}% CAGR {s_c1['cagr']*100:.1f}% "
          f"dd {s_c1['dd']*100:.1f}%")
    print("  yearly: " + yr(s_c1))

    # 3) blend C1 + FX-trend (monthly rebalance) at a few weights
    curves = {"C1": eq_c1, "FX": eq_fx}
    wins = {"C1": (s_c1["win"], s_c1["trades"]),
            "FX": (s_fx["win"], s_fx["trades"])}
    print("\nBlend C1 + FX-trend (monthly rebalance):")
    for wfx in (0.10, 0.20, 0.30):
        b = combo(curves, wins, {"C1": 1 - wfx, "FX": wfx})
        d18 = b["yearly"].get(2018, 0) * 100
        d22 = b["yearly"].get(2022, 0) * 100
        print(f"  {int((1-wfx)*100)}/{int(wfx*100)}: win {b['win']*100:.1f}% "
              f"CAGR {b['cagr']*100:.1f}% dd {b['dd']*100:.1f}% "
              f"| 2018 {d18:+.0f}% 2022 {d22:+.0f}%  (C1 alone: "
              f"2018 {s_c1['yearly'].get(2018,0)*100:+.0f}% "
              f"2022 {s_c1['yearly'].get(2022,0)*100:+.0f}%)")


if __name__ == "__main__":
    main()
