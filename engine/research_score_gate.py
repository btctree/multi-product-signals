"""Backtest gate for the composite action score (role-review requirement):
run the LIVE DIP config with candidate ranking = composite score vs the
validated momentum ranking. Adopt composite ordering only if win/CAGR/DD hold.
"""
import numpy as np

from engine_rr import run, FULL_YEARS
from data_fetch import fetch_all
from scoring import composite

CFG = dict(K=3.5, K_tight=2.0, rsi_entry=25, near_high=0.88, slots=5,
           regime_exit=200, markets=("US", "HK", "JP", "EU"))


def composite_ranker(row):
    px = float(row["Close"])
    atr = float(row["atr"])
    mom = float(row["mom_90"]) if np.isfinite(row["mom_90"]) else 0.0
    rsi = float(row["rsi"]) if np.isfinite(row["rsi"]) else 50.0
    hi = float(row["hi_52w"]) if np.isfinite(row["hi_52w"]) else px
    off = (px / hi - 1) * 100
    stop_pct = min(3.5 * atr / px, 0.12) * 100
    total, _ = composite(mom, rsi, off, stop_pct)
    return total


def show(tag, s):
    print(f"{tag:26s} n={s['trades']:4d} win={s['win']*100:4.1f}% "
          f"R:R={s['rr']:4.2f} PF={s['pf']:4.2f} CAGR={s['cagr']*100:5.1f}% "
          f"dd={s['dd']*100:6.1f}% Calmar={s['calmar']:.2f}")
    print("   yr: " + " ".join(f"{y}:{s['yearly'].get(y,0)*100:+.0f}" for y in FULL_YEARS))


def main():
    data = fetch_all()
    base = run(data, **CFG)                       # momentum ranking (validated)
    comp = run(data, **CFG, ranker=composite_ranker)
    show("BASELINE momentum rank", base)
    show("COMPOSITE score rank", comp)
    ok = (comp["win"] >= base["win"] - 0.01 and comp["cagr"] >= base["cagr"] - 0.01
          and comp["dd"] >= base["dd"] - 0.02)
    print("\nGATE:", "PASS - composite may order the Actions page"
          if ok else "FAIL - keep momentum ordering")


if __name__ == "__main__":
    main()
