"""Should each product use a different-days SMA for its reference?

PART A — overfit test (the honest answer): for each product, pick the SMA period
that maximized ITS OWN trend return on the TRAIN half (2016-2020), then apply it
on the UNSEEN test half (2021-2026). If per-product-optimized SMAs do NOT beat a
single uniform SMA200 on the test half, per-product tuning is curve-fitting.

PART B — global period sweep in the real engine (uniform SMA100/150/200/250) to
confirm the chosen 200 is a robust global choice, not a lucky one.
"""
import numpy as np
import pandas as pd
from data_fetch import fetch_all
from universe import market_of
from engine_rr import run, FULL_YEARS

PERIODS = [50, 100, 150, 200, 250]
TRAIN_END = "2020-12-31"
COST = 0.0015  # per position switch


def trend_return(close, period, lo, hi):
    """Return of 'hold while close>SMA(period)' over [lo,hi], costed per switch."""
    sma = close.rolling(period).mean()
    sig = (close > sma).shift(1).fillna(False)      # no look-ahead
    ret = close.pct_change().fillna(0)
    seg = sig.loc[lo:hi]
    r = ret.loc[lo:hi]
    switches = seg.astype(int).diff().abs().fillna(0)
    net = (1 + r * seg - COST * switches)
    return net.prod() - 1


def part_a(data):
    eq = {t: d for t, d in data.items()
          if market_of(t) in ("US", "HK", "JP", "EU") and len(d) > 1500}
    train_best, uni200_test, opt_test, cheat_test = [], [], [], []
    for t, df in eq.items():
        c = df["Close"]
        lo0, hi0 = c.index[0], TRAIN_END
        lo1, hi1 = "2021-01-01", c.index[-1]
        tr = {p: trend_return(c, p, lo0, hi0) for p in PERIODS}
        te = {p: trend_return(c, p, lo1, hi1) for p in PERIODS}
        bp = max(tr, key=tr.get)          # train-optimal period for THIS product
        train_best.append(bp)
        uni200_test.append(te[200])       # uniform SMA200 on test
        opt_test.append(te[bp])           # train-optimal per product, on test
        cheat_test.append(max(te.values()))  # hindsight best on test (upper bound)
    n = len(eq)
    print(f"PART A — per-product SMA, train(->2020)/test(2021->), {n} equities")
    from collections import Counter
    print("  train-optimal period distribution:", dict(Counter(train_best)))
    print(f"  avg TEST return  — uniform SMA200      : {np.mean(uni200_test)*100:+.1f}%")
    print(f"  avg TEST return  — per-product train-opt: {np.mean(opt_test)*100:+.1f}%")
    print(f"  avg TEST return  — hindsight best (cheat): {np.mean(cheat_test)*100:+.1f}%")
    verdict = ("per-product tuning OVERFITS (does NOT beat uniform 200 OOS)"
               if np.mean(opt_test) <= np.mean(uni200_test)
               else "per-product beats uniform OOS")
    print(f"  VERDICT: {verdict}")


def part_b(data):
    print("\nPART B — uniform global SMA period in the real engine (C1 + SMA200-exit style)")
    base = dict(K=3.5, K_tight=2.0, rsi_entry=25, near_high=0.88, slots=7)
    for period in PERIODS[1:]:   # 100,150,200,250
        s = run(data, **base, sma_trend_period=period, regime_exit=period)
        reds = [y for y in FULL_YEARS if s["yearly"].get(y, 0) < 0]
        print(f"  SMA{period}: win {s['win']*100:4.1f}%  R:R {s['rr']:.2f}  "
              f"CAGR {s['cagr']*100:5.1f}%  dd {s['dd']*100:6.1f}%  "
              f"Calmar {s['calmar']:.2f}  reds {reds}")


def main():
    data = fetch_all()
    part_a(data)
    part_b(data)


if __name__ == "__main__":
    main()
