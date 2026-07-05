"""Ground-truth the R:R critique: what do dip-engine trades ACTUALLY exit at,
vs the displayed target/stop? Runs the live DIP config and dissects trades."""
import numpy as np
import pandas as pd

from config import STRAT
from data_fetch import fetch_all
from backtest import run_backtest
from universe import market_of

data = {t: d for t, d in fetch_all().items() if market_of(t) in ("US", "HK", "JP")}
s, tr, eq = run_backtest(data, dict(STRAT), max_pos=5, verbose=False)

tr["pct"] = tr["net_ret"] * 100
wins, losses = tr[tr.net_ret > 0], tr[tr.net_ret <= 0]

print(f"DIP engine (X4, 5 slots): {len(tr)} trades, win {len(wins)/len(tr)*100:.1f}%")
print(f"avg win {wins.pct.mean():+.2f}%  avg loss {losses.pct.mean():+.2f}%  "
      f"realized R:R {abs(wins.pct.mean()/losses.pct.mean()):.2f}:1  PF {s['profit_factor']:.2f}")
print(f"expectancy per trade {tr.pct.mean():+.2f}%  "
      f"(= HKD {tr.pct.mean()/100*20000:+,.0f} on a 20k position)\n")

print("Exit reason breakdown:")
br = tr.groupby("reason").agg(n=("pct", "size"), avg=("pct", "mean"),
                              share=("pct", lambda x: len(x)/len(tr)*100))
print(br.round(2).to_string())

print("\nWhere do LOSERS actually exit? (the '-10% stop' fear)")
bins = [-100, -9, -6, -3, 0]
labels = ["<= -9% (near full stop)", "-9 to -6%", "-6 to -3%", "-3 to 0%"]
losses = losses.copy()
losses["band"] = pd.cut(losses.pct, bins=bins, labels=labels)
lb = losses.groupby("band", observed=True).agg(n=("pct", "size"))
lb["share_of_all_trades"] = (lb.n / len(tr) * 100).round(1)
print(lb.to_string())

full_stop = (losses.pct <= -9).sum()
print(f"\nTrades that actually hit ~full 10% stop: {full_stop} "
      f"({full_stop/len(tr)*100:.1f}% of all trades)")
print(f"Median trade: {tr.pct.median():+.2f}%   "
      f"best {tr.pct.max():+.1f}%  worst {tr.pct.min():+.1f}%")
