"""Live production engine for the P1 + SMA200 product.

Per-product analysis: classify the market type, decide the best long-only action,
and give the price/target/cut-loss + confidence + reasons — for the dashboard.
Mirrors the backtested logic (dip entry in uptrends, trailing exit, SMA200 regime
exit; crypto uses its trend rules). Long only, no margin.
"""
import numpy as np
import pandas as pd

from config import PROD, CRY_TREND
from indicators import add_features
from scoring import momentum_score, band
from universe import market_of, NAMES

TRADEABLE = ("US", "HK", "JP", "EU", "CRYPTO")


def _feat(df):
    p = {"sma_trend": PROD["sma_trend"], "sma_fast": PROD["sma_fast"],
         "rsi_period": PROD["rsi_period"], "atr_period": PROD["atr_period"]}
    return add_features(df, p)


def analyze(ticker: str, df: pd.DataFrame) -> dict:
    f = _feat(df)
    row = f.iloc[-1]
    px = float(row["Close"])
    atr = float(row["atr"]) if np.isfinite(row["atr"]) else px * 0.02
    sma200 = float(row["sma_trend"]) if np.isfinite(row["sma_trend"]) else np.nan
    sma50 = float(row["sma_fast"]) if np.isfinite(row["sma_fast"]) else np.nan
    rsi = float(row["rsi"]) if np.isfinite(row["rsi"]) else 50
    mom = float(row["mom_90"]) if np.isfinite(row["mom_90"]) else 0
    hi52 = float(row["hi_52w"]) if np.isfinite(row["hi_52w"]) else px
    mkt = market_of(ticker)
    card = {"symbol": ticker, "name": NAMES.get(ticker, ticker), "market": mkt,
            "price": round(px, 4), "reasons": []}

    up = np.isfinite(sma200) and px > sma200 and sma50 > sma200
    below200 = np.isfinite(sma200) and px < sma200
    pct_vs_200 = (px / sma200 - 1) * 100 if np.isfinite(sma200) else 0
    off_high = (px / hi52 - 1) * 100

    if mkt == "CRYPTO":
        return _crypto_card(card, px, atr, sma200, sma50, mom, f)

    # ----- equity / dip-engine market-type classification -----
    if below200:
        card.update(regime="Downtrend", action="AVOID", confidence="—",
                    entry=None, target=None,
                    stop=round(sma200, 2) if np.isfinite(sma200) else None,
                    expectation="Long-term downtrend — stand aside (long-only). "
                                "Re-enter only after price reclaims its 200-day average.")
        card["reasons"] = [
            f"Price is {pct_vs_200:+.1f}% vs its 200-day average (below it = downtrend)",
            "Dip-buying in a downtrend is catching a falling knife — no edge",
            f"Watch for a reclaim of the 200-day average near {sma200:.2f} to re-arm"]
        return card

    if not up:
        card.update(regime="Neutral / transition", action="WATCH", confidence="Low",
                    entry=None, target=None, stop=None,
                    expectation="Trend not confirmed (50-day not yet above 200-day). "
                                "No action until a clean uptrend forms.")
        card["reasons"] = [
            f"Price {pct_vs_200:+.1f}% vs 200-day average, but 50-day not above 200-day yet",
            "Consolidating — wait for trend confirmation"]
        return card

    # in a confirmed uptrend
    is_dip = (rsi < PROD["rsi_entry"] and mom > PROD["min_mom"]
              and px >= PROD["near_high"] * hi52
              and atr / px > PROD["min_atr_pct"])
    entry = round(px, 2)
    stop = round(max(px - PROD["K"] * atr, px * (1 - PROD["hard_stop_pct"])), 2)
    target = round(px + PROD["target_atr_mult"] * atr, 2)
    if is_dip:
        conf = "High" if (mom > 0.15 and off_high > -12) else "Medium"
        stop_pct = (1 - stop / px) * 100
        score = momentum_score(mom)
        card.update(score=score, score_band=band(score),
                    score_facts=f"90-day momentum {mom*100:+.0f}% · dip RSI(3) {rsi:.0f} · "
                                f"{abs(off_high):.0f}% off 52w-high · cut-loss −{stop_pct:.1f}%")
        card.update(regime="Uptrend — pullback (BUY zone)", action="BUY",
                    confidence=conf, entry=entry, target=target, stop=stop,
                    expectation=f"Buy the dip at next open (~{entry}). Ride with a "
                                f"trailing stop; exit if it closes below its 200-day "
                                f"average ({sma200:.2f}). Informational target ~{target} "
                                f"(+{(target/px-1)*100:.1f}%).")
        card["reasons"] = [
            f"Confirmed uptrend: price {pct_vs_200:+.1f}% above its 200-day average, "
            f"50-day above 200-day",
            f"Short-term oversold pullback: RSI(3) = {rsi:.0f} (< {PROD['rsi_entry']}) "
            f"— the highest-win-rate long entry",
            f"Strong stock: within {abs(off_high):.0f}% of its 52-week high",
            f"90-day momentum {mom*100:+.1f}%",
            f"Cut-loss (resting stop): {stop} ({(stop/px-1)*100:.1f}%); "
            f"regime exit if it breaks {sma200:.2f}"]
    else:
        # uptrend but no dip yet -> WATCH, show the buy zone
        buy_zone = round(min(sma50, px * 0.97), 2) if np.isfinite(sma50) else round(px * 0.97, 2)
        card.update(regime="Uptrend — extended", action="WATCH", confidence="Medium",
                    entry=None, target=target,
                    stop=round(sma200, 2) if np.isfinite(sma200) else None,
                    expectation=f"Healthy uptrend but no dip to buy yet. Wait for a "
                                f"short-term pullback (RSI(3) < {PROD['rsi_entry']}), "
                                f"likely near {buy_zone}, then buy.")
        card["reasons"] = [
            f"Confirmed uptrend: price {pct_vs_200:+.1f}% above 200-day average",
            f"Not oversold yet: RSI(3) = {rsi:.0f} (need < {PROD['rsi_entry']} to buy)",
            f"Buy-the-dip zone ~{buy_zone} (near the 50-day average / a 3% pullback)",
            f"90-day momentum {mom*100:+.1f}%"]
    return card


def _crypto_card(card, px, atr, sma200, sma50, mom, f):
    up = np.isfinite(sma200) and px > sma200 and sma50 > sma200
    trail = round(px - CRY_TREND["trail_atr_mult"] * atr, 2)
    if up:
        score = momentum_score(mom)
        card.update(score=score, score_band=band(score),
                    score_facts=f"90-day momentum {mom*100:+.0f}% · trend-ride "
                                f"(crypto sleeve) · trailing stop −{(1 - trail / px)*100:.1f}%")
        card.update(regime="Uptrend (trend-ride)", action="BUY/HOLD", confidence="Medium",
                    entry=round(px, 2), target=None, stop=trail,
                    expectation=f"Crypto trend engine is long. Ride the trend with a "
                                f"chandelier trailing stop from {trail}; exit on two "
                                f"closes below the 50-day average ({sma50:.2f}).")
        card["reasons"] = [
            "Uptrend: price above 200-day average and 50-day above 200-day",
            f"90-day momentum {mom*100:+.1f}%",
            f"Trailing stop {trail} (ratchets up, never down)"]
    else:
        card.update(regime="Downtrend / no trend", action="AVOID", confidence="—",
                    entry=None, target=None,
                    stop=round(sma200, 2) if np.isfinite(sma200) else None,
                    expectation="No uptrend — stand aside. The trend engine re-enters "
                                "only when price and the 50-day cross back above the 200-day.")
        card["reasons"] = ["Not in an uptrend (below 200-day or 50-day below 200-day)",
                           "Trend-following stands aside until the trend resumes"]
    return card


def scan_actions(data: dict) -> list:
    """Today's BUY candidates across tradeable markets, best first."""
    out = []
    for t, df in data.items():
        if market_of(t) not in TRADEABLE or len(df) < 260:
            continue
        try:
            c = analyze(t, df)
        except Exception:
            continue
        if c["action"] in ("BUY", "BUY/HOLD"):
            out.append((c.get("score", 0), c))
    out.sort(key=lambda x: -x[0])   # composite score, high -> low (gate-validated)
    return [c for _, c in out]
