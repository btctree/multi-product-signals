"""Round 4 — return-maximization ladder (user amendment 2026-07-04).

Constraints kept: long only, NO margin, DD <= 30%, win 'very close to 70%'
(gate relaxed to >= 68%), PF > 1.15, both halves >= 64%.
Pre-registered metric: max CAGR subject to those gates.

Three mechanism levers, each honest under the constraints:
  L1 wider targets   (tp 1.5 / 2.0 ATR)      - more profit per win, win% falls
  L2 concentration   (slots 7 / 5 / 3)       - bigger stakes, lumpier DD
  L3 leveraged ETFs  (cash 2x/3x index ETFs) - larger ATR% per trade, no margin,
                                               loss capped at stake, no liquidation
"""
import copy

from config import STRAT
from data_fetch import fetch_all
from backtest import run_backtest
from universe import LEV_ETFS

BASE = dict(STRAT)  # live V9c


def drop_lev(data):
    return {t: df for t, df in data.items() if t not in LEV_ETFS}


RUNS = [
    # name, param patch, max_pos, include_lev
    ("W1  tp1.5  15 slots", {"target_atr_mult": 1.5}, 15, False),
    ("W2  tp2.0  15 slots", {"target_atr_mult": 2.0}, 15, False),
    ("W3  tp1.25  7 slots", {}, 7, False),
    ("W4  tp1.25  5 slots", {}, 5, False),
    ("W5  tp1.25  3 slots", {}, 3, False),
    ("W6  +LEV tp1.25 15", {}, 15, True),
    ("W7  +LEV tp1.25  7", {}, 7, True),
    ("W8  +LEV tp1.5   7", {"target_atr_mult": 1.5}, 7, True),
    ("W9  +LEV tp1.25  5", {}, 5, True),
    ("W10 +LEV tp1.5   5", {"target_atr_mult": 1.5}, 5, True),
]


def main():
    data_all = fetch_all()
    data_nolev = drop_lev(data_all)
    for name, patch, mp, lev in RUNS:
        p = copy.deepcopy(BASE)
        p.update(patch)
        d = data_all if lev else data_nolev
        s, tr, eq = run_backtest(d, p, max_pos=mp, verbose=False)
        lev_n = int((tr["market"] == "LEV").sum()) if not tr.empty else 0
        print(f"{name:22s} trades={s['trades']:5d} (lev {lev_n:4d}) "
              f"win={s['win_rate']*100:5.1f}% [h1 {s['win_rate_h1']*100:4.1f} "
              f"h2 {s['win_rate_h2']*100:4.1f}] PF={s['profit_factor']:4.2f} "
              f"CAGR={s['cagr']*100:6.1f}% final={eq.iloc[-1]:>12,.0f} "
              f"dd={s['max_dd']*100:6.1f}%")


if __name__ == "__main__":
    main()
