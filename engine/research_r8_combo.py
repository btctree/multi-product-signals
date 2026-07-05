"""Round 8 — MONTHLY-REBALANCED multi-sleeve portfolios (+ bonds, per user).

Sleeves (each an already-validated honest engine):
  DIP   = X4 equity dip engine (5 slots, US/HK/JP)          16.7% CAGR, -23% DD, win 68%
  CRY   = crypto trend engine (BTC/ETH, 2 slots)            43.7% CAGR, -61% DD, win 45%
  LEV   = leveraged-ETF trend engine (3 slots, trail)       32.0% CAGR, -57% DD, win 47%
  BND   = bond trend engine (TLT/IEF/LQD/HYG/EMB, 2 slots)  new
Rebalance: portfolio daily return = sum(w_i * r_i); weights reset to target at
each month start (realistic manual monthly rebalance). Blended win rate =
trade-count-weighted across sleeves. Gates: win>=60%, DD<=30%. Metric: max CAGR.
"""
import numpy as np
import pandas as pd

from config import STRAT, START_CAPITAL_HKD
from data_fetch import fetch_all
from backtest import run_backtest
from universe import market_of, LEV_ETFS, BONDS
from research_r7_trend import run_trend, FULL_YEARS


def combo(curves, wins, weights):
    idx = None
    for k in weights:
        idx = curves[k].index if idx is None else idx.union(curves[k].index)
    rets = {}
    for k in weights:
        c = curves[k].reindex(idx).ffill()
        rets[k] = c.pct_change().fillna(0)
    month = idx.to_period("M")
    # monthly-rebalanced: within each month weights drift with returns
    port = []
    w = dict(weights)
    cur_m = None
    for i, d in enumerate(idx):
        if month[i] != cur_m:
            cur_m = month[i]
            w = dict(weights)  # rebalance to target at month start
        r = sum(w[k] * rets[k].iloc[i] for k in weights)
        port.append(r)
        tot = 1 + r
        for k in weights:
            w[k] = w[k] * (1 + rets[k].iloc[i]) / tot
    eq = START_CAPITAL_HKD * (1 + pd.Series(port, index=idx)).cumprod()
    yrs = (idx[-1] - idx[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1
    dd = (eq / eq.cummax() - 1).min()
    ye = eq.groupby(eq.index.year).agg(["first", "last"])
    yearly = {int(y): r["last"] / r["first"] - 1 for y, r in ye.iterrows()}
    n = sum(wins[k][1] for k in weights)
    bw = sum(wins[k][0] * wins[k][1] for k in weights) / n
    return {"cagr": cagr, "dd": dd, "yearly": yearly, "win": bw,
            "final": eq.iloc[-1], "trades_yr": n / yrs}


def main():
    data = fetch_all()
    p = dict(STRAT)

    dip_s, dip_tr, dip_eq = run_backtest(
        {t: d for t, d in data.items() if market_of(t) in ("US", "HK", "JP")},
        p, max_pos=5, verbose=False)
    cry_s, cry_eq, cry_tr = run_trend(
        {t: d for t, d in data.items() if market_of(t) == "CRYPTO"},
        N=55, K=4.0, slots=2, entry_mode="regime", exit_sma="sma_fast")
    lev_s, lev_eq, lev_tr = run_trend(
        {t: d for t, d in data.items() if t in LEV_ETFS},
        N=55, K=4.5, slots=3, entry_mode="regime", trail=True)
    bnd_s, bnd_eq, bnd_tr = run_trend(
        {t: d for t, d in data.items() if t in ("TLT", "IEF", "LQD", "HYG", "EMB")},
        N=55, K=4.0, slots=2, entry_mode="regime", trail=False)
    print(f"sleeve BND: CAGR={bnd_s['cagr']*100:.1f}% dd={bnd_s['dd']*100:.1f}% "
          f"win={bnd_s['win']*100:.1f}%")

    curves = {"DIP": dip_eq, "CRY": cry_eq, "LEV": lev_eq, "BND": bnd_eq}
    wins = {"DIP": (dip_s["win_rate"], dip_s["trades"]),
            "CRY": (cry_s["win"], cry_s["trades"]),
            "LEV": (lev_s["win"], lev_s["trades"]),
            "BND": (bnd_s["win"], bnd_s["trades"])}

    COMBOS = [
        ("R1 80dip/20cry", {"DIP": .8, "CRY": .2}),
        ("R2 70dip/30cry", {"DIP": .7, "CRY": .3}),
        ("R3 60dip/40cry", {"DIP": .6, "CRY": .4}),
        ("R4 50dip/50cry", {"DIP": .5, "CRY": .5}),
        ("R5 60dip/25cry/15lev", {"DIP": .6, "CRY": .25, "LEV": .15}),
        ("R6 50dip/30cry/20lev", {"DIP": .5, "CRY": .3, "LEV": .2}),
        ("R7 55dip/25cry/10lev/10bnd", {"DIP": .55, "CRY": .25, "LEV": .1, "BND": .1}),
        ("R8 65dip/25cry/10bnd", {"DIP": .65, "CRY": .25, "BND": .1}),
        ("R9 40dip/40cry/20lev", {"DIP": .4, "CRY": .4, "LEV": .2}),
    ]
    for name, w in COMBOS:
        s = combo(curves, wins, w)
        reds = [y for y in FULL_YEARS if s["yearly"].get(y, 0) < 0]
        ok = "PASS" if (s["win"] >= 0.60 and s["dd"] >= -0.30) else "fail"
        print(f"{name:30s} win={s['win']*100:5.1f}% CAGR={s['cagr']*100:6.1f}% "
              f"dd={s['dd']*100:6.1f}% final={s['final']:>12,.0f} "
              f"[{ok}] red={reds}")
        print("     " + " ".join(f"{y}:{s['yearly'].get(y,0)*100:+.0f}"
                                 for y in FULL_YEARS)
              + f" 26:{s['yearly'].get(2026,0)*100:+.0f}")


if __name__ == "__main__":
    main()
