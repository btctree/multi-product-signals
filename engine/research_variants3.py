"""Round 3 — dynamic compounding sizing (user amendment 2026-07-03).

Sizing: new position = cash / (15 - n_held); compounds. Pre-registered metric:
max CAGR subject to win >= 70%, PF > 1.15, maxDD <= 30%, both halves >= 65%.
V11/V12 test the utilization mechanism (shallower dip -> more signals -> more
capital deployed); everything else unchanged from validated V8/V9.
"""
import copy

from config import STRAT
from data_fetch import fetch_all
from backtest import run_backtest

BASE = {"rsi_entry": 15, "stop_atr_mult": 3.5, "tp_exit": True,
        "exclude_markets": ["COMMODITY", "INDEX"], "min_mom": 0.0,
        "min_atr_pct": 0.012, "rsi_exit": 70, "max_hold_days": 25}

VARIANTS = {
    "V8c  rsi15 tp1.0 (live cfg, compounding)": {"target_atr_mult": 1.0},
    "V9c  rsi15 tp1.25": {"target_atr_mult": 1.25},
    "V11  rsi20 tp1.0 (utilization)": {"rsi_entry": 20, "target_atr_mult": 1.0},
    "V12  rsi20 tp1.25": {"rsi_entry": 20, "target_atr_mult": 1.25},
}


def main():
    data = fetch_all()
    for name, patch in VARIANTS.items():
        p = copy.deepcopy(STRAT)
        p.update(BASE)
        p.update(patch)
        s, tr, eq = run_backtest(data, p, verbose=False)
        print(f"{name:42s} trades={s['trades']:5d} win={s['win_rate']*100:5.1f}% "
              f"[h1 {s['win_rate_h1']*100:4.1f} h2 {s['win_rate_h2']*100:4.1f}] "
              f"PF={s['profit_factor']:4.2f} CAGR={s['cagr']*100:5.1f}% "
              f"final={eq.iloc[-1]:>11,.0f} dd={s['max_dd']*100:5.1f}%")


if __name__ == "__main__":
    main()
