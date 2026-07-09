"""SP1 — High-R:R engine: dip entry + let-winners-run trailing exit.

Thesis: the dip entry (RSI(3)<x in an SMA200/SMA50 uptrend + momentum) gives a
high base win rate; replacing the small fixed target with a chandelier trailing
stop lets winners run, lifting realized R:R (avg win / avg loss) from the old
0.65:1 toward the >=2:1 the mandate now requires.

Mechanics reuse research_r7_trend.py:37-99 (ratchet trail, zero look-ahead:
the stop is tested against today's low BEFORE it is raised with today's data).
Honest: next-open fills, per-market costs each side, adverse-first same-day rule.

Gate (pre-registered primary metric): realized R:R >= 2.0 AND win >= 55% AND
maxDD <= 30% AND both split-halves >= 52% AND every traded market win >= 50%.
Maximize expectancy subject to the gate. No grid mining — variants are the
listed mechanism steps only.
"""
import json
import numpy as np
import pandas as pd

from config import STRAT, COST_BP, START_CAPITAL_HKD, MIN_ATR_BY_MARKET
from data_fetch import fetch_all
from indicators import add_features
from universe import market_of

FULL_YEARS = range(2016, 2026)
DEFAULT_MARKETS = ("US", "HK", "JP", "EU", "FX")


def run(data, rsi_entry=15, K=3.0, K_tight=None, tighten_at=1.5, slots=7,
        min_mom=0.0, min_atr=0.012, partial=False, partial_frac=1 / 3,
        partial_at=1.5, max_hold=60, markets=DEFAULT_MARKETS,
        near_high=0.0, min_price_over_fast=None, market_gate=None,
        regime_exit=None, regime_exit_consec=1,
        sma200_rising=False, sma50_rising=False, max_ext_atr=None,
        sma_trend_period=None):
    """market_gate: None=off; 'spx'=block NEW entries when S&P closes below its
    200-day SMA (broad risk-off) — dip-buying in a market downtrend is catching
    falling knives (the 2018/2022 failure mode). Held positions ride their trail."""
    """One backtest. K = chandelier ATR multiple. If K_tight is set, the trail
    tightens from K to K_tight once the trade is +tighten_at*ATR in profit
    (asymmetric: room early, lock gains once the move proves itself)."""
    p = dict(STRAT)
    if sma_trend_period:
        p["sma_trend"] = sma_trend_period      # global long-term SMA period override
    feats = {t: add_features(df, p) for t, df in data.items()
             if market_of(t) in markets}
    gate = None
    if market_gate == "spx" and "^GSPC" in data:
        spx = data["^GSPC"]["Close"]
        gate = (spx < spx.rolling(200).mean()).shift(1).fillna(False)  # risk-off=True
    calendar = sorted(set().union(*[set(df.index) for df in feats.values()]))
    idx = {t: {d: i for i, d in enumerate(df.index)} for t, df in feats.items()}
    cash = float(START_CAPITAL_HKD)
    positions, pending, trades, eq = {}, {}, [], []

    def cost(t):
        return COST_BP[market_of(t)] / 10_000

    def close(t, pos, exit_px, reason, d):
        nonlocal cash
        c = cost(t)
        final_ret = (exit_px / pos["entry_px"]) * (1 - c) / (1 + c) - 1
        final_pnl = pos["notional"] * final_ret          # remaining size
        total_pnl = pos["banked"] + final_pnl            # incl. scaled-out part
        cash += pos["notional"] * (1 + final_ret)
        net = total_pnl / pos["entry_notional"]
        trades.append({"ticker": t, "market": market_of(t), "net_ret": net,
                       "pnl": total_pnl, "exit_date": d, "hold": pos["bars"],
                       "reason": reason, "entry_px": pos["entry_px"],
                       "entry_date": pos.get("entry_date")})
        del positions[t]

    for d in calendar:
        # 1) manage open positions
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
            # partial scale-out (banks part of a winner, raises win rate)
            if partial and not pos["scaled"]:
                lvl = pos["entry_px"] + partial_at * pos["atr0"]
                if row["High"] >= lvl:
                    c = cost(t)
                    part = pos["notional"] * partial_frac
                    ret = (lvl / pos["entry_px"]) * (1 - c) / (1 + c) - 1
                    pos["banked"] += part * ret
                    cash += part * (1 + ret)
                    pos["notional"] -= part
                    pos["scaled"] = True
                    pos["stop"] = max(pos["stop"], pos["entry_px"])  # runner to breakeven
            pos["bars"] += 1
            pos["hw"] = max(pos["hw"], float(row["Close"]))
            if np.isfinite(row["atr"]):
                k = K
                if K_tight is not None and pos["hw"] >= pos["entry_px"] + tighten_at * pos["atr0"]:
                    k = K_tight
                pos["stop"] = max(pos["stop"], pos["hw"] - k * float(row["atr"]))
            # per-product regime exit: close below its own long-term SMA (200/250)
            if regime_exit:
                sma_col = "sma_250" if regime_exit == 250 else "sma_trend"
                sma_v = row.get(sma_col)
                if np.isfinite(sma_v) and row["Close"] < sma_v:
                    pos["below_sma"] = pos.get("below_sma", 0) + 1
                    if pos["below_sma"] >= regime_exit_consec:
                        pos["pending_exit"] = "regime(SMA)"
                else:
                    pos["below_sma"] = 0
            if pos["bars"] >= max_hold:
                pos["pending_exit"] = "time"

        # 2) fill yesterday's signals at today's open
        for t in list(pending):
            if d not in idx[t]:
                continue
            if len(positions) < slots and t not in positions:
                row = feats[t].iloc[idx[t][d]]
                px = float(row["Open"])
                a = pending[t]
                notional = cash / max(1, slots - len(positions))
                if notional > 0:
                    cash -= notional
                    positions[t] = {"entry_px": px, "entry_notional": notional,
                                    "notional": notional, "atr0": a,
                                    "stop": px - K * a, "hw": px, "bars": 0,
                                    "banked": 0.0, "scaled": False,
                                    "pending_exit": None, "entry_date": d}
            del pending[t]

        # 3) scan dip entries (act next open); risk-off gate blocks NEW entries
        free = slots - len(positions) - len(pending)
        if gate is not None and bool(gate.get(d, False)):
            free = 0
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
                ok = (row["Close"] > row["sma_trend"] and row["sma_fast"] > row["sma_trend"]
                      and mom > min_mom and row["rsi"] < rsi_entry
                      and row["atr"] / row["Close"] > matr)
                # SMA200-based market-type refinement for ENTRY
                if ok and sma200_rising:      # long-term trend must be rising
                    ok = np.isfinite(row.get("sma200_slope")) and row["sma200_slope"] > 0
                if ok and sma50_rising:
                    ok = np.isfinite(row.get("sma50_slope")) and row["sma50_slope"] > 0
                if ok and max_ext_atr is not None:   # not too extended above SMA200
                    ok = np.isfinite(row.get("ext_atr")) and row["ext_atr"] < max_ext_atr
                if ok and near_high > 0:  # strong stock: close within x of 52w high
                    hi = row.get("hi_52w")
                    ok = ok and np.isfinite(hi) and row["Close"] >= near_high * hi
                if ok and min_price_over_fast is not None:  # shallow pullback only
                    ok = ok and row["Close"] >= min_price_over_fast * row["sma_fast"]
                if ok:
                    cands.append((mom, t, float(row["atr"])))
            cands.sort(reverse=True)
            for mom, t, a in cands[:free]:
                pending[t] = a

        # 4) mark to market
        ov = 0.0
        for t, pos in positions.items():
            i = idx[t].get(d)
            last = float(feats[t]["Close"].iloc[i]) if i is not None else pos["entry_px"]
            ov += pos["banked"] + pos["notional"] * (last / pos["entry_px"])
        eq.append((d, cash + ov))

    eqs = pd.Series(dict(eq))
    tr_df = pd.DataFrame(trades)
    s = summarize(eqs, tr_df)
    if run.return_curve:
        return s, eqs, tr_df
    if run.return_trades:
        return s, tr_df
    return s


run.return_trades = False
run.return_curve = False


def summarize(eqs, tr):
    yrs = (eqs.index[-1] - eqs.index[0]).days / 365.25
    cagr = (eqs.iloc[-1] / eqs.iloc[0]) ** (1 / yrs) - 1
    dd = (eqs / eqs.cummax() - 1).min()
    daily = eqs.pct_change().dropna()
    sharpe = daily.mean() / daily.std() * np.sqrt(252) if daily.std() > 0 else 0
    calmar = cagr / abs(dd) if dd < 0 else np.inf
    ye = eqs.groupby(eqs.index.year).agg(["first", "last"])
    yearly = {int(y): r["last"] / r["first"] - 1 for y, r in ye.iterrows()}
    base = {"cagr": cagr, "dd": dd, "sharpe": sharpe, "calmar": calmar,
            "yearly": yearly, "final": eqs.iloc[-1],
            "total_ret": eqs.iloc[-1] / eqs.iloc[0] - 1}
    if tr.empty:
        return {**base, "trades": 0, "win": 0, "rr": 0, "yearly_win": {}}
    w, l = tr[tr.net_ret > 0], tr[tr.net_ret <= 0]
    aw, al = w.net_ret.mean(), l.net_ret.mean()
    mid = tr.exit_date.sort_values().iloc[len(tr) // 2]
    h1 = (tr[tr.exit_date <= mid].net_ret > 0).mean()
    h2 = (tr[tr.exit_date > mid].net_ret > 0).mean()
    pm = {m: (g.net_ret > 0).mean() for m, g in tr.groupby("market")}
    tr = tr.copy()
    tr["yr"] = pd.to_datetime(tr["exit_date"]).dt.year
    yearly_win = {int(y): (g.net_ret > 0).mean() for y, g in tr.groupby("yr")}
    yearly_n = {int(y): len(g) for y, g in tr.groupby("yr")}
    return {**base, "trades": len(tr), "win": (tr.net_ret > 0).mean(),
            "avg_win": aw, "avg_loss": al,
            "rr": abs(aw / al) if al else np.inf,
            "pf": w.pnl.sum() / -l.pnl.sum() if l.pnl.sum() < 0 else np.inf,
            "h1": h1, "h2": h2, "per_market_win": pm,
            "expectancy": tr.net_ret.mean(), "avg_hold": tr.hold.mean(),
            "yearly_win": yearly_win, "yearly_n": yearly_n}


def gate(s):
    return (s["trades"] >= 100 and s["win"] >= 0.55 and s["rr"] >= 2.0
            and s["dd"] >= -0.30 and min(s["h1"], s["h2"]) >= 0.52
            and (not s["per_market_win"] or min(s["per_market_win"].values()) >= 0.50))


VARIANTS = {
    "A1 K3 rsi15": dict(K=3.0, rsi_entry=15),
    "A2 K4 rsi15": dict(K=4.0, rsi_entry=15),
    "A3 K2.5 rsi15": dict(K=2.5, rsi_entry=15),
    "A4 K3 rsi10 deepdip": dict(K=3.0, rsi_entry=10),
    "A5 K3 rsi15 mom>5%": dict(K=3.0, rsi_entry=15, min_mom=0.05),
    "A6 two-stage 3.5->2.0": dict(K=3.5, K_tight=2.0, tighten_at=1.5, rsi_entry=15),
    "A7 K3 partial@1.5": dict(K=3.0, rsi_entry=15, partial=True, partial_at=1.5),
    "A8 two-stage + mom>5%": dict(K=3.5, K_tight=2.0, rsi_entry=15, min_mom=0.05),
}


def main():
    data = fetch_all()
    rows = {}
    for name, kw in VARIANTS.items():
        s = run(data, slots=7, **kw)
        rows[name] = s
        g = "PASS" if gate(s) else "----"
        reds = [y for y in FULL_YEARS if s["yearly"].get(y, 0) < 0]
        print(f"{name:24s} n={s['trades']:4d} win={s['win']*100:5.1f}% "
              f"[{s['h1']*100:4.1f}/{s['h2']*100:4.1f}] "
              f"R:R={s['rr']:4.2f} (w{s['avg_win']*100:+.1f}/l{s['avg_loss']*100:+.1f}) "
              f"PF={s['pf']:4.2f} CAGR={s['cagr']*100:5.1f}% dd={s['dd']*100:5.1f}% "
              f"[{g}] hold={s['avg_hold']:.0f} reds={len(reds)}")

    passing = {k: v for k, v in rows.items() if gate(v)}
    best = max(passing or rows, key=lambda k: rows[k]["expectancy"] if passing
               else rows[k]["rr"])
    out = {"best": best, "gate_passed": bool(passing),
           "metrics": {k: {kk: (vv if not isinstance(vv, dict) else vv)
                           for kk, vv in v.items() if kk != "yearly"}
                       for k, v in rows.items()},
           "yearly_best": rows[best]["yearly"]}
    with open("results_rr.json", "w") as f:
        json.dump(out, f, indent=2, default=float)
    print(f"\nBest: {best}  (gate {'PASSED' if passing else 'NOT met — frontier reported'})")
    print("per-market win:", {m: round(w * 100, 1)
                              for m, w in rows[best]["per_market_win"].items()})


if __name__ == "__main__":
    main()
