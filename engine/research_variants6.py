"""Round 6 (final) — three principled attempts to close X4's single red year
(2018, -2%). NOT a mining exercise: one diversification step (10 slots), one
payoff step (tp 2.5), one quality step (stronger momentum floor). If none is
all-green, X4 stands and the every-year-win gate is reported as infeasible.
"""
import copy

from config import STRAT
from data_fetch import fetch_all
from backtest import run_backtest
from universe import LEV_ETFS

RUNS = [
    ("Y1 10s tp2.0",        {"target_atr_mult": 2.0}, 10),
    ("Y2  7s tp2.5",        {"target_atr_mult": 2.5}, 7),
    ("Y3  7s tp2.0 mom>5%", {"target_atr_mult": 2.0, "min_mom": 0.05}, 7),
]
FULL_YEARS = range(2016, 2026)


def main():
    data = {t: df for t, df in fetch_all().items() if t not in LEV_ETFS}
    for name, patch, mp in RUNS:
        p = copy.deepcopy(STRAT)
        p.update(patch)
        s, tr, eq = run_backtest(data, p, max_pos=mp, verbose=False)
        yr = s["yearly_ret"]
        reds = [y for y in FULL_YEARS if yr.get(y, 0) < 0]
        print(f"{name:22s} win={s['win_rate']*100:5.1f}% "
              f"[h1 {s['win_rate_h1']*100:4.1f} h2 {s['win_rate_h2']*100:4.1f}] "
              f"PF={s['profit_factor']:4.2f} CAGR={s['cagr']*100:6.1f}% "
              f"dd={s['max_dd']*100:6.1f}% red={reds if reds else 'NONE'} "
              f"26ytd={yr.get(2026, 0)*100:+.1f}%")
        print("     " + " ".join(f"{y}:{yr.get(y, 0)*100:+.0f}" for y in FULL_YEARS))


if __name__ == "__main__":
    main()
