"""Round 7C — margined X4 frontier (user allowed margin).

Method: lever the validated X4 daily equity returns by m, minus margin interest
6%/yr on the borrowed fraction: r_lev = m*r - (m-1)*0.06/252.
Honest caveats printed: assumes intraday stops hold at levered size and ignores
forced liquidation below broker maintenance margin (flagged when worst equity
would breach a 30%-of-gross maintenance level).
"""
import numpy as np

from config import STRAT
from data_fetch import fetch_all
from backtest import run_backtest
from universe import market_of


def main():
    data = fetch_all()
    p = dict(STRAT)
    s, tr, eq = run_backtest(data, p, max_pos=7, verbose=False)
    r = eq.pct_change().dropna()
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    print(f"base X4: CAGR={s['cagr']*100:.1f}% dd={s['max_dd']*100:.1f}% win={s['win_rate']*100:.1f}%")
    for m in (1.25, 1.5, 1.75, 2.0, 2.5):
        rl = m * r - (m - 1) * 0.06 / 252
        eql = (1 + rl).cumprod()
        cagr = eql.iloc[-1] ** (1 / yrs) - 1
        dd = (eql / eql.cummax() - 1).min()
        # maintenance check: equity fraction of gross = 1 - (m-1)/m / (eq/eq_peak_leverage)
        # worst-case proxy: at drawdown trough, equity = 1+dd of peak; borrowed = (m-1) of peak stake
        worst_frac = (1 + dd) / (m * (1 + dd) + (m - 1) * (-dd))
        flag = "MARGIN-CALL RISK" if worst_frac < 0.30 else "ok"
        yearly = eql.groupby(eql.index.year).agg(["first", "last"])
        reds = [int(y) for y, row in yearly.iterrows()
                if row["last"] / row["first"] - 1 < 0 and 2016 <= y <= 2025]
        print(f"m={m:4.2f}: CAGR={cagr*100:6.1f}% maxDD={dd*100:6.1f}% "
              f"maint_frac={worst_frac:.2f} [{flag}] red={reds}")
    print("win rate unchanged by leverage (same trades). "
          "Assumes stops fill at levered size; slippage stress not applied.")


if __name__ == "__main__":
    main()
