"""Live production engine for the P1 + SMA200 product.

Per-product analysis: classify the market type, decide the best long-only action,
and give the price/target/cut-loss + confidence + reasons — for the dashboard.
Mirrors the backtested logic (dip entry in uptrends, trailing exit, SMA200 regime
exit; crypto uses its trend rules). Long only, no margin.
"""
import numpy as np
import pandas as pd

from config import PROD, CRY_TREND, SCORE_ENTRY_GATE, MODEL, D3
from indicators import add_features
from model_d3e1 import d3_last, card_e1
from scoring import momentum_score, band
from universe import market_of, NAMES

TRADEABLE = ("US", "HK", "JP", "EU", "CRYPTO")


def _note(card):
    """Stamp monitor-only (non-tradeable) product cards so a BUY/WATCH there is
    never mistaken for a validated trade signal."""
    if not card.get("tradeable", True):
        card["reasons"].append("Analysis only — this product class is monitored, "
                               "not part of the validated trading strategy")
    return card


def _feat(df):
    p = {"sma_trend": PROD["sma_trend"], "sma_fast": PROD["sma_fast"],
         "rsi_period": PROD["rsi_period"], "atr_period": PROD["atr_period"]}
    return add_features(df, p)


def analyze(ticker: str, df: pd.DataFrame, e1=None) -> dict:
    """One product's card from its latest bar.

    e1: the E1 resolver, e1(ticker, bar date) -> e1_decision() dict, or None
    (no filter: every US dip is kept). Only MODEL "D3E1" uses it; only a US E
    dip asks it; only "block" changes anything (the dip becomes a WATCH).
    price, atr, sma200 - all the bot's exits read - never depend on MODEL or e1."""
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
            "price": round(px, 4), "reasons": [],
            "atr": round(atr, 6),
            "sma200": round(sma200, 4) if np.isfinite(sma200) else None,
            "tradeable": mkt in TRADEABLE}

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
        return _note(card)

    if not up:
        card.update(regime="Neutral / transition", action="WATCH", confidence="Low",
                    entry=None, target=None, stop=None,
                    expectation="Trend not confirmed (50-day not yet above 200-day). "
                                "No action until a clean uptrend forms.")
        card["reasons"] = [
            f"Price {pct_vs_200:+.1f}% vs 200-day average, but 50-day not above 200-day yet",
            "Consolidating — wait for trend confirmation"]
        return _note(card)

    # in a confirmed uptrend
    is_dip = (rsi < PROD["rsi_entry"] and mom > PROD["min_mom"]
              and px >= PROD["near_high"] * hi52
              and atr / px > PROD["min_atr_pct"])
    entry = round(px, 2)
    stop = round(max(px - PROD["K"] * atr, px * (1 - PROD["hard_stop_pct"])), 2)
    target = round(px + PROD["target_atr_mult"] * atr, 2)

    # MODEL "D3E1" (owner decision 2026-10-26): E1 may veto a US dip, and a bar
    # that is not a kept dip may be a D3 BUY. All of it - the resolver call, the
    # swing scan, every line of text - is worked out inside ONE try, into `alt`
    # (the D3 BUY or E1 WATCH fields) and `e1_kept`, before the card is touched.
    # An exception must never cost a card (build_dashboard writes none for an
    # analyze that raises): a ticker without one collects prune_dead strikes
    # until it leaves the universe, and a held name without one gets no exits.
    # On any error the card is built exactly as MODEL "E" builds it - a dip
    # stays a BUY - with a note.
    alt, e1_kept, model_note = None, None, None
    if MODEL == "D3E1" and mkt in D3["markets"]:
        try:
            if is_dip and mkt == "US" and e1 is not None:
                r = card_e1(e1(ticker, f.index[-1]))
                if r["decision"] == "block":
                    alt = _e1_watch_fields(r, rsi, mom, pct_vs_200, sma200, target)
                else:
                    e1_kept = r
            if not is_dip or alt is not None:
                d3 = _d3_fields(f, mkt, px, mom, sma200, pct_vs_200, entry, stop, target)
                if d3 is not None:
                    alt = d3                 # a vetoed dip that is also a D3 bar buys as D3
        except Exception as e:
            alt, e1_kept = None, None
            model_note = (f"D3/E1 skipped ({type(e).__name__}): card built by the "
                          f"dip rules alone")

    if is_dip and alt is None:
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
        if MODEL == "D3E1":
            # the shared data.json contract: E rows rank first (tier 0)
            card.update(signal="E", tier=0)
            if e1_kept is not None:
                card["e1"] = e1_kept
    elif alt is not None:
        card.update(alt)                     # D3 BUY, or the E1 veto's WATCH
    elif rsi < PROD["rsi_entry"]:
        # dip conditions met EXCEPT quality gates -> WATCH with the honest reason
        score = momentum_score(mom)
        why = (f"Score {score} is below the entry gate ({SCORE_ENTRY_GATE}) — "
               f"90-day momentum {mom*100:+.0f}% (< +30%)"
               if mom <= PROD["min_mom"] else
               f"Not within {int((1-PROD['near_high'])*100)}% of its 52-week high"
               if px < PROD["near_high"] * hi52 else
               "Daily range too small to clear round-trip costs")
        card.update(score=score, score_band=band(score),
                    regime="Uptrend — dip (below entry gate)", action="WATCH",
                    confidence="Low", entry=None, target=target,
                    stop=round(sma200, 2) if np.isfinite(sma200) else None,
                    expectation="A dip inside an uptrend, but it does not qualify "
                                "under the live D rules — the engine stands aside.")
        card["reasons"] = [
            f"Confirmed uptrend: price {pct_vs_200:+.1f}% above 200-day average",
            f"Oversold pullback present: RSI(3) = {rsi:.0f}",
            why]
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
    if model_note:
        card["model_note"] = model_note
    return _note(card)


def _d3_fields(f, mkt, px, mom, sma200, pct_vs_200, entry, stop, target):
    """The D3 BUY card's fields when the last bar is a D3 signal, else None.
    Entry, target and stop are the dip's own - stop = the higher of close -
    3.5 ATR and the 12% floor (analyze, above) - so the bot sizes it and seeds
    its first stop exactly as for a dip (live_sim's stop0 with the floor; the
    research ran D3 without the floor). D3 needs a finite ATR and momentum
    over 30%, so `mom` and the card's ATR are the real values here, never
    analyze's stand-ins. tier 1: every E row ranks first (shared contract)."""
    ok, lv = d3_last(f, mkt)
    if not ok:
        return None
    score = momentum_score(mom)
    stop_pct = (1 - stop / px) * 100
    sh, sl = lv["swing_high"], lv["swing_low"]
    return {
        "score": score, "score_band": band(score),
        "score_facts": f"90-day momentum {mom*100:+.0f}% · held the 0.618 level "
                       f"{lv['level_0618']:.2f} · cut-loss −{stop_pct:.1f}%",
        "regime": "Uptrend — held 0.618 retracement (BUY zone)", "action": "BUY",
        "confidence": "Medium", "entry": entry, "target": target, "stop": stop,
        "expectation": f"Buy at next open (~{entry}). The pullback from the {sh:.2f} "
                       f"swing high held its 0.618 retracement ({lv['level_0618']:.2f}) "
                       f"and closed back up. Ride with a trailing stop; exit if it "
                       f"closes below its 200-day average ({sma200:.2f}). "
                       f"Informational target ~{target} (+{(target/px-1)*100:.1f}%).",
        "reasons": [
            f"Confirmed uptrend: price {pct_vs_200:+.1f}% above its 200-day average, "
            f"50-day above 200-day",
            f"Swing: high {sh:.2f} within the last {D3['look_hi']} bars, from a low of "
            f"{sl:.2f} (+{(sh / sl - 1) * 100:.0f}%)",
            f"Pullback held the 0.618 level {lv['level_0618']:.2f}: 5-day low "
            f"{lv['low5']:.2f}, above the 0.886 floor {lv['floor_0886']:.2f}",
            "Closed back above the level, and up on the day",
            f"90-day momentum {mom*100:+.1f}%",
            f"Cut-loss (resting stop): {stop} ({(stop/px-1)*100:.1f}%); "
            f"regime exit if it breaks {sma200:.2f}",
            "Second signal (D3): ranked after every dip (E) candidate"],
        "signal": "D3", "tier": 1, "d3": lv}


def _e1_watch_fields(r, rsi, mom, pct_vs_200, sma200, target):
    """The WATCH card for a US dip E1 vetoes: trailing-12-month EPS below the
    twelve months before, as filed on the signal day. Laid out like the
    below-the-gate dip WATCH (stop shows the SMA200)."""
    score = momentum_score(mom)
    g = r.get("growth")
    eps = (f"Trailing-12-month EPS {g*100:+.0f}% vs a year earlier"
           if isinstance(g, (int, float)) and np.isfinite(g) else
           "Trailing-12-month EPS lower than a year earlier")
    return {
        "score": score, "score_band": band(score),
        "regime": "Uptrend — dip, EPS falling (filter)", "action": "WATCH",
        "confidence": "Low", "entry": None, "target": target,
        "stop": round(sma200, 2) if np.isfinite(sma200) else None,
        "expectation": "A qualifying dip, but the company's earnings per share are "
                       "shrinking, so the earnings filter (E1) stands aside.",
        "reasons": [
            f"Confirmed uptrend: price {pct_vs_200:+.1f}% above 200-day average",
            f"Oversold pullback present: RSI(3) = {rsi:.0f}",
            f"{eps} (SEC filings, newest quarter to {r.get('qend') or 'n/a'}) — "
            f"E1 skips US dips while earnings shrink"],
        "e1": r}


def _crypto_card(card, px, atr, sma200, sma50, mom, f):
    card.setdefault("tradeable", True)
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


def scan_actions(data: dict, e1=None) -> list:
    """Today's BUY candidates across tradeable markets, best first: by tier
    (E rows 0, then D3 rows 1 - D3 only fills slots the dips leave, as
    tested), then score high -> low. The sort is stable, so equal scores keep
    universe order, as before. Without a tier (MODEL "E", crypto) a row
    counts as tier 0, which is the old score-only order exactly. The bot and
    the digest read this order (build_dashboard keeps the first 20)."""
    out = []
    for t, df in data.items():
        if market_of(t) not in TRADEABLE or len(df) < 260:
            continue
        try:
            c = analyze(t, df, e1=e1)
        except Exception:
            continue
        if c["action"] in ("BUY", "BUY/HOLD"):
            out.append(c)
    out.sort(key=lambda c: (c.get("tier", 0), -c.get("score", 0)))   # momentum score
    return out
