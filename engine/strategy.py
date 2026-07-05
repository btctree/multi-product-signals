"""Long-only 'Uptrend Dip' strategy (pre-registered, playbook-compliant).

Design rationale (INVESTMENT_PRODUCT_PLAYBOOK §3-§7):
- Regime gate: only trade instruments in an established uptrend
  (close > SMA200 and SMA50 > SMA200). Everything else = stand aside.
- Entry: short-term oversold pullback inside that uptrend (RSI(3) < 20).
  Buying dips in uptrends is the highest-win-rate long edge on large caps.
- Exit: mean-reversion complete (RSI(3) > 60), or time stop, or stop-loss.
  The user mandate is win-rate ≥ 70%, which favours banking the snap-back
  rather than riding trends (documented trade-off vs playbook §1.7).
- Stop: max(2.5×ATR, 10%) below entry — catastrophe insurance, rarely binds.
- Confidence: regime quality + depth of dip + momentum rank → sizes nothing
  (fixed HKD 10k per spec) but ranks candidates when slots are scarce.
"""
import pandas as pd
from config import STRAT


def entry_signal(row: pd.Series, p: dict = STRAT) -> bool:
    import numpy as np
    ok = (
        row["Close"] > row["sma_trend"]
        and row["sma_fast"] > row["sma_trend"]
        and row["rsi"] < p["rsi_entry"]
        and row["Close"] >= p["min_price"]
    )
    if not ok:
        return False
    # quality filters (mechanism: dip-buys pay only when the snap-back clears costs
    # and the medium-term tide is rising)
    if p.get("min_mom") is not None:
        if not np.isfinite(row.get("mom_90", np.nan)) or row["mom_90"] <= p["min_mom"]:
            return False
    if p.get("min_atr_pct"):
        if not row["atr"] / row["Close"] > p["min_atr_pct"]:
            return False
    return True


def exit_signal(row: pd.Series, p: dict = STRAT) -> bool:
    if p.get("exit_mode") == "strength":
        # bank the snap-back on the first strong close (close above prior high)
        return bool(row["Close"] > row["prev_high"]) or row["rsi"] > 75
    return row["rsi"] > p["rsi_exit"]


def stop_price(entry_px: float, entry_atr: float, p: dict = STRAT) -> float:
    return max(entry_px - p["stop_atr_mult"] * entry_atr,
               entry_px * (1 - p["hard_stop_pct"]))


def target_price(entry_px: float, entry_atr: float, p: dict = STRAT) -> float:
    return entry_px + p["target_atr_mult"] * entry_atr


def candidate_score(row: pd.Series) -> float:
    """Rank entry candidates when signals > free slots.
    Higher = better: strong medium-term momentum, deeper dip."""
    mom = row.get("mom_90") or 0.0
    dip = (20 - row["rsi"]) / 20  # 0..1, deeper oversold = more spring
    return float(mom) + 0.1 * float(dip)


def evidence(row: pd.Series, ticker: str) -> list[str]:
    """Human-readable reasons for the pick (spec item 6, technical portion;
    news/policy evidence is layered on at report time)."""
    ev = []
    pct_above = (row["Close"] / row["sma_trend"] - 1) * 100
    ev.append(f"Established uptrend: price {pct_above:+.1f}% above its 200-day average, "
              f"50-day average above 200-day (regime gate passed)")
    ev.append(f"Short-term oversold pullback: RSI(3) = {row['rsi']:.0f} "
              f"(< {STRAT['rsi_entry']}) — dip-buy setup inside an uptrend, "
              f"historically the highest-win-rate long entry")
    if row.get("mom_90") and row["mom_90"] > 0:
        ev.append(f"Positive 90-day momentum: {row['mom_90'] * 100:+.1f}%")
    if row.get("hi_52w"):
        off_hi = (row["Close"] / row["hi_52w"] - 1) * 100
        ev.append(f"Price {off_hi:.1f}% from its 52-week high")
    if row.get("vol_surge") and row["vol_surge"] > 1.5:
        ev.append(f"Volume surge: {row['vol_surge']:.1f}× the 20-day average — "
                  f"possible large-buyer accumulation on the dip")
    return ev
