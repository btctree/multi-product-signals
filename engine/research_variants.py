"""Pre-registered variant test (playbook §1.4: principled choices, no grid mining).

Six variants, each a single mechanism-motivated change vs baseline. Judged on:
win rate (target >=70%), split-half stability, profit factor, maxDD (<=30%).
Primary metric pre-registered: WIN RATE subject to profit factor > 1.15 and
maxDD <= 30% and both halves >= 65%.
"""
import copy

from config import STRAT
from data_fetch import fetch_all
from backtest import run_backtest

VARIANTS = {
    "V0 baseline (rsi<20 -> rsi>60, 2.5ATR)": {},
    "V1 deeper dip (rsi<15)": {"rsi_entry": 15},
    "V2 quick bank (rsi>50)": {"rsi_exit": 50},
    "V3 wide stop (3.5ATR)": {"stop_atr_mult": 3.5},
    "V4 strength exit (close>prev high)": {"exit_mode": "strength"},
    "V5 deep dip + quick bank + wide stop": {"rsi_entry": 15, "rsi_exit": 50,
                                             "stop_atr_mult": 3.5},
}


def main():
    data = fetch_all()
    rows = []
    for name, patch in VARIANTS.items():
        p = copy.deepcopy(STRAT)
        p.update(patch)
        stats, tr, eq = run_backtest(data, p, verbose=False)
        rows.append((name, stats))
        s = stats
        print(f"{name:45s} trades={s['trades']:5d} win={s['win_rate']*100:5.1f}% "
              f"[h1 {s['win_rate_h1']*100:4.1f} h2 {s['win_rate_h2']*100:4.1f}] "
              f"PF={s['profit_factor']:4.2f} pnl={s['total_pnl_hkd']:+9,.0f} "
              f"dd={s['max_dd']*100:5.1f}% hold={s['avg_hold']:.1f}")


if __name__ == "__main__":
    main()
