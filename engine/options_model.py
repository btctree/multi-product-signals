"""SP2 — Options overlay MODEL (leverage via long calls; "short" via long puts).

============================ HONESTY BANNER ============================
This is a MODEL, not a historical-chain backtest. There is NO free 10-year
historical options/IV data, so option prices here are computed with
Black-Scholes using a CONSERVATIVE implied-vol proxy:
    IV = realized_vol(20d, annualized) * (1 + VRP_MARKUP) + spread
VRP_MARKUP (default 0.25) deliberately makes options DEARER than realized vol
(real IV > realized vol — that gap is the seller's edge) so we do NOT flatter
long-option buying. Only DEEP-IN-THE-MONEY options (delta >= ~0.75) are used:
they behave like defined-risk leveraged stock (high delta tracks the underlying's
win rate; low theta), which is the least model-sensitive region. Real fills will
differ. Account stays long-only cash: we only ever BUY options (loss capped at
premium) — no writing, no margin, no liquidation.
=======================================================================

Leverage = long call. Bearish/"short" = long put. Both defined-risk.
"""
import math
import numpy as np
import pandas as pd

from config import COST_BP, START_CAPITAL_HKD, MIN_ATR_BY_MARKET
from data_fetch import fetch_all
from indicators import add_features
from universe import market_of
from engine_rr import summarize, FULL_YEARS, DEFAULT_MARKETS

VRP_MARKUP = 0.25          # implied-vol premium over realized (conservative)
OPT_SPREAD_BP = 80         # option bid/ask + commission per side (bp of premium-notional)
DTE = 90                   # days to expiry at entry (quarterly)
ITM = 0.10                 # strike set 10% in-the-money (call: K=S*(1-ITM))
R = 0.03                   # risk-free (annual)


def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(S, K, T, sigma, call=True):
    """Black-Scholes premium. T in years, sigma annualized."""
    if T <= 0 or sigma <= 0:
        return max(0.0, (S - K) if call else (K - S))
    d1 = (math.log(S / K) + (R + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if call:
        return S * _norm_cdf(d1) - K * math.exp(-R * T) * _norm_cdf(d2)
    return K * math.exp(-R * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs_delta(S, K, T, sigma, call=True):
    if T <= 0 or sigma <= 0:
        return (1.0 if S > K else 0.0) if call else (-1.0 if S < K else 0.0)
    d1 = (math.log(S / K) + (R + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return _norm_cdf(d1) if call else _norm_cdf(d1) - 1


def realized_vol(close, i, n=20):
    if i < n + 1:
        return 0.3
    r = np.log(close.iloc[i - n:i].values[1:] / close.iloc[i - n:i].values[:-1])
    return float(np.std(r) * math.sqrt(252)) or 0.3


def run_calls(data, rsi_entry=15, K=3.0, slots=7, min_mom=0.0, min_atr=0.012,
              max_hold=60, markets=DEFAULT_MARKETS, use_options=True,
              opt_frac=1.0, itm=ITM):
    """Same dip+trail signal as engine_rr, but each long is expressed as a
    deep-ITM CALL (use_options=True) or as spot (False, for apples-to-apples).
    opt_frac = fraction of each stake spent on the call (rest held as cash) —
    dials effective leverage down from the full deep-ITM ~6x toward DD<=30%.
    itm = how deep in-the-money the strike is (bigger = higher delta, less lev)."""
    from config import STRAT
    p = dict(STRAT)
    feats = {t: add_features(df, p) for t, df in data.items()
             if market_of(t) in markets}
    calendar = sorted(set().union(*[set(df.index) for df in feats.values()]))
    idx = {t: {d: i for i, d in enumerate(df.index)} for t, df in feats.items()}
    cash = float(START_CAPITAL_HKD)
    positions, pending, trades, eq = {}, {}, [], []

    def cost(t):
        return COST_BP[market_of(t)] / 10_000

    def opt_value(pos, S, d):
        T = max(1, (pos["expiry"] - d).days) / 365
        return bs_price(S, pos["strike"], T, pos["iv"], call=True)

    def close(t, pos, S_exit, reason, d):
        nonlocal cash
        if use_options:
            prem = opt_value(pos, S_exit, d)
            proceeds = pos["contracts"] * prem * (1 - OPT_SPREAD_BP / 10_000)
        else:
            c = cost(t)
            proceeds = pos["notional"] * (S_exit / pos["entry_px"]) * (1 - c) / (1 + c)
        cash += proceeds
        net = proceeds / pos["stake"] - 1
        trades.append({"ticker": t, "market": market_of(t), "net_ret": net,
                       "pnl": proceeds - pos["stake"], "exit_date": d,
                       "hold": pos["bars"], "reason": reason})
        del positions[t]

    for d in calendar:
        for t in list(positions):
            if d not in idx[t]:
                continue
            row = feats[t].iloc[idx[t][d]]
            pos = positions[t]
            if pos["pending_exit"]:
                close(t, pos, float(row["Open"]), pos["pending_exit"], d)
                continue
            if row["Open"] <= pos["stop"]:
                close(t, pos, float(row["Open"]), "trail(gap)", d)
                continue
            if row["Low"] <= pos["stop"]:
                close(t, pos, pos["stop"], "trail", d)
                continue
            pos["bars"] += 1
            pos["hw"] = max(pos["hw"], float(row["Close"]))
            if np.isfinite(row["atr"]):
                pos["stop"] = max(pos["stop"], pos["hw"] - K * float(row["atr"]))
            near_expiry = use_options and (pos["expiry"] - d).days <= 5
            if pos["bars"] >= max_hold or near_expiry:
                pos["pending_exit"] = "time/expiry"

        for t in list(pending):
            if d not in idx[t]:
                continue
            if len(positions) < slots and t not in positions:
                i = idx[t][d]
                row = feats[t].iloc[i]
                S = float(row["Open"])
                a = pending[t]
                stake = cash / max(1, slots - len(positions))
                if stake <= 0:
                    del pending[t]
                    continue
                cash -= stake
                if use_options:
                    iv = realized_vol(feats[t]["Close"], i) * (1 + VRP_MARKUP)
                    strike = S * (1 - itm)
                    expiry = d + pd.Timedelta(days=DTE)
                    prem = bs_price(S, strike, DTE / 365, iv, call=True) \
                        * (1 + OPT_SPREAD_BP / 10_000)
                    spend = stake * opt_frac                 # rest stays as cash
                    cash += stake - spend
                    contracts = spend / prem if prem > 0 else 0
                    positions[t] = {"entry_px": S, "stake": stake, "atr0": a,
                                    "stop": S - K * a, "hw": S, "bars": 0,
                                    "strike": strike, "iv": iv, "expiry": expiry,
                                    "contracts": contracts, "pending_exit": None}
                else:
                    positions[t] = {"entry_px": S, "stake": stake, "notional": stake,
                                    "atr0": a, "stop": S - K * a, "hw": S, "bars": 0,
                                    "pending_exit": None}
            del pending[t]

        free = slots - len(positions) - len(pending)
        if free > 0:
            cands = []
            for t, df in feats.items():
                if t in positions or t in pending or d not in idx[t]:
                    continue
                row = df.iloc[idx[t][d]]
                if not (np.isfinite(row["sma_trend"]) and np.isfinite(row["atr"])):
                    continue
                mom = row["mom_90"] if np.isfinite(row["mom_90"]) else -1
                matr = MIN_ATR_BY_MARKET.get(market_of(t), min_atr)
                if (row["Close"] > row["sma_trend"] and row["sma_fast"] > row["sma_trend"]
                        and mom > min_mom and row["rsi"] < rsi_entry
                        and row["atr"] / row["Close"] > matr):
                    cands.append((mom, t, float(row["atr"])))
            cands.sort(reverse=True)
            for mom, t, a in cands[:free]:
                pending[t] = a

        ov = 0.0
        for t, pos in positions.items():
            i = idx[t].get(d)
            S = float(feats[t]["Close"].iloc[i]) if i is not None else pos["entry_px"]
            ov += pos["contracts"] * opt_value(pos, S, d) if use_options \
                else pos["notional"] * (S / pos["entry_px"])
        eq.append((d, cash + ov))

    return summarize(pd.Series(dict(eq)), pd.DataFrame(trades))


def main():
    data = fetch_all()
    print("=" * 60)
    print("SP2 OPTIONS MODEL  (deep-ITM calls; MODELED, not chain-backtested)")
    print(f"IV = realized_vol x (1+{VRP_MARKUP}) ; {DTE}d expiry ; {ITM*100:.0f}% ITM ; "
          f"spread {OPT_SPREAD_BP}bp/side")
    print("=" * 60)
    # sanity: unit check on one deep-ITM call
    prem = bs_price(100, 90, 90 / 365, 0.30, call=True)
    dlt = bs_delta(100, 90, 90 / 365, 0.30, call=True)
    print(f"unit check: S100 K90 90d iv30% -> call={prem:.2f} delta={dlt:.2f} "
          f"leverage~={100 * dlt / prem:.1f}x")
    print("\nCall-leverage frontier vs the DD<=30% budget (A5 base: K3 rsi15 mom>5%):")
    print(f"{'config':28s} {'win%':>5} {'R:R':>5} {'CAGR%':>6} {'final':>13} {'maxDD%':>7}")
    configs = [
        ("SPOT 1x", dict(use_options=False)),
        ("calls 20% (deep 10%ITM)", dict(opt_frac=0.20, itm=0.10)),
        ("calls 30% (deep 10%ITM)", dict(opt_frac=0.30, itm=0.10)),
        ("calls 25% (deeper 25%ITM)", dict(opt_frac=0.25, itm=0.25)),
        ("calls 40% (deeper 25%ITM)", dict(opt_frac=0.40, itm=0.25)),
        ("calls 100% (deep 10%ITM)", dict(opt_frac=1.0, itm=0.10)),
    ]
    for tag, kw in configs:
        s = run_calls(data, K=3.0, rsi_entry=15, min_mom=0.05, slots=7, **kw)
        reds = [y for y in FULL_YEARS if s["yearly"].get(y, 0) < 0]
        flag = "" if s["dd"] >= -0.30 else "  <-DD BREACH"
        print(f"{tag:28s} {s['win']*100:5.1f} {s['rr']:5.2f} {s['cagr']*100:6.1f} "
              f"{s['final']:13,.0f} {s['dd']*100:7.1f}{flag}")


if __name__ == "__main__":
    main()
