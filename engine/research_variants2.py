"""Round 2 — pre-registered target/stop-geometry variants.

Mechanism: with a wide catastrophe stop and a modest intraday profit target,
most uptrend dips recover past the target before the stop; win rate rises
mechanically (the win%/payoff frontier). Expectancy and DD must stay honest.
Commodities/indices excluded from TRADING (futures roll artifacts; monitor-only).
Primary metric unchanged: win rate >= 70% with PF > 1.15, DD <= 30%, both halves >= 65%.
"""
import copy

from config import STRAT
from data_fetch import fetch_all
from backtest import run_backtest

BASE = {"rsi_entry": 15, "stop_atr_mult": 3.5, "tp_exit": True,
        "exclude_markets": ["COMMODITY", "INDEX"],
        "rsi_exit": 70, "max_hold_days": 25}

VARIANTS = {
    "V6 tp=1.0ATR": {"target_atr_mult": 1.0},
    "V7 tp=0.75ATR stop=3.0": {"target_atr_mult": 0.75, "stop_atr_mult": 3.0},
    "V8 tp=1.0ATR + mom>0 + atr%>1.2": {"target_atr_mult": 1.0, "min_mom": 0.0,
                                        "min_atr_pct": 0.012},
    "V9 tp=1.25ATR + mom>0": {"target_atr_mult": 1.25, "min_mom": 0.0},
    "V10 tp=1.0ATR + mom>0": {"target_atr_mult": 1.0, "min_mom": 0.0},
}


def main():
    data = fetch_all()
    for name, patch in VARIANTS.items():
        p = copy.deepcopy(STRAT)
        p.update(BASE)
        p.update(patch)
        s, tr, eq = run_backtest(data, p, verbose=False)
        print(f"{name:38s} trades={s['trades']:5d} win={s['win_rate']*100:5.1f}% "
              f"[h1 {s['win_rate_h1']*100:4.1f} h2 {s['win_rate_h2']*100:4.1f}] "
              f"PF={s['profit_factor']:4.2f} pnl={s['total_pnl_hkd']:+9,.0f} "
              f"dd={s['max_dd']*100:5.1f}% hold={s['avg_hold']:.1f}")
        if not tr.empty:
            reasons = tr.groupby("reason")["net_ret"].agg(["size", "mean"])
            print("   exits: " + " | ".join(
                f"{r}: n={int(v['size'])}, avg {v['mean']*100:+.2f}%"
                for r, v in reasons.iterrows()))


if __name__ == "__main__":
    main()
