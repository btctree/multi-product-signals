"""Round 5 — every-year-green ladder (user amendment 2026-07-04b).

Gates: win >= 60%, PF > 1.15, maxDD <= 30%, and EVERY full calendar year
(2016-2025) equity return >= 0. Metric: max CAGR. 2026 YTD reported separately
(half-year, not gated). New mechanism under test: market-breadth risk gate
(no new entries when < X% of tradeable universe is above its own SMA200) —
pre-registered, classic risk-off filter, targets 2018/2022-type red years.
"""
import copy

from config import STRAT
from data_fetch import fetch_all
from backtest import run_backtest
from universe import LEV_ETFS

RUNS = [
    ("X1  5s tp1.5",           {"target_atr_mult": 1.5}, 5),
    ("X2  5s tp2.0",           {"target_atr_mult": 2.0}, 5),
    ("X3  7s tp1.5",           {"target_atr_mult": 1.5}, 7),
    ("X4  7s tp2.0",           {"target_atr_mult": 2.0}, 7),
    ("X5  15s tp2.0",          {"target_atr_mult": 2.0}, 15),
    ("X6  5s tp1.25 br50",     {"breadth_min": 0.50}, 5),
    ("X7  5s tp1.5  br50",     {"target_atr_mult": 1.5, "breadth_min": 0.50}, 5),
    ("X8  7s tp2.0  br50",     {"target_atr_mult": 2.0, "breadth_min": 0.50}, 7),
    ("X9  15s tp2.0 br50",     {"target_atr_mult": 2.0, "breadth_min": 0.50}, 15),
    ("X10 5s tp2.0  br50",     {"target_atr_mult": 2.0, "breadth_min": 0.50}, 5),
    ("X11 5s tp2.0  br40",     {"target_atr_mult": 2.0, "breadth_min": 0.40}, 5),
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
        ytd26 = yr.get(2026, 0)
        print(f"{name:20s} win={s['win_rate']*100:5.1f}% PF={s['profit_factor']:4.2f} "
              f"CAGR={s['cagr']*100:6.1f}% dd={s['max_dd']*100:6.1f}% "
              f"red_yrs={reds if reds else 'NONE'} 26ytd={ytd26*100:+.1f}%")
        print("     " + " ".join(f"{y}:{yr.get(y, 0)*100:+.0f}" for y in FULL_YEARS))


if __name__ == "__main__":
    main()
