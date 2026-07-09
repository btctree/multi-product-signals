"""Round 7A — LET-WINNERS-RUN engines (trend/trail-stop), long-only, no margin.

Generalized loop: breakout or regime entry, chandelier K*ATR ratchet trail
(zero look-ahead: existing stop tested against today's low BEFORE ratcheting
with today's data), 2-consecutive-close regime exit, dynamic compounding
sizing, per-market costs. Variants pre-registered in RUNS.
Hybrid = separate sleeves compounding their own capital fraction (stated assumption).
"""
import numpy as np
import pandas as pd

from config import STRAT, COST_BP, START_CAPITAL_HKD
from data_fetch import fetch_all
from indicators import add_features
from universe import market_of, LEV_ETFS
from backtest import run_backtest

FULL_YEARS = range(2016, 2026)


def run_trend(data, N=55, K=4.0, slots=7, entry_mode="breakout",
              exit_sma="sma_trend", trail=True, start=START_CAPITAL_HKD):
    p = dict(STRAT)
    feats = {}
    for t, df in data.items():
        f = add_features(df, p)
        f["roll_high"] = f["Close"].rolling(N).max()
        feats[t] = f
    calendar = sorted(set().union(*[set(df.index) for df in feats.values()]))
    idx = {t: {d: i for i, d in enumerate(df.index)} for t, df in feats.items()}
    cash, positions, trades, eq = float(start), {}, [], []
    pending: dict = {}

    def cost(t):
        return COST_BP[market_of(t)] / 10_000

    for d in calendar:
        for t in list(positions):
            if d not in idx[t]:
                continue
            row = feats[t].iloc[idx[t][d]]
            pos = positions[t]
            exit_px, reason = None, None
            if pos["pending_exit"]:
                exit_px, reason = row["Open"], "regime"
            elif row["Open"] <= pos["stop"]:
                exit_px, reason = row["Open"], "trail (gap)"
            elif row["Low"] <= pos["stop"]:
                exit_px, reason = pos["stop"], "trail"
            if exit_px is not None:
                net = (exit_px / pos["entry_px"]) * (1 - cost(t)) / (1 + cost(t)) - 1
                cash += pos["notional"] * (1 + net)
                trades.append({"ticker": t, "market": market_of(t), "net_ret": net,
                               "pnl": pos["notional"] * net, "exit_date": d,
                               "hold": pos["bars"], "reason": reason,
                               "entry_px": pos["entry_px"],
                               "entry_date": pos.get("entry_date")})
                del positions[t]
                continue
            pos["bars"] += 1
            # ratchet AFTER surviving today (no look-ahead on today's data)
            if trail:
                pos["hw"] = max(pos["hw"], float(row["Close"]))
                if np.isfinite(row["atr"]):
                    pos["stop"] = max(pos["stop"], pos["hw"] - K * float(row["atr"]))
            ex_ref = row[exit_sma]
            if np.isfinite(ex_ref) and row["Close"] < ex_ref:
                pos["below"] += 1
                if pos["below"] >= 2:
                    pos["pending_exit"] = True
            else:
                pos["below"] = 0

        # fills for yesterday's signals
        for t in list(pending):
            if d not in idx[t]:
                continue
            if len(positions) < slots and t not in positions:
                row = feats[t].iloc[idx[t][d]]
                px = float(row["Open"])
                notional = cash / max(1, slots - len(positions))
                if notional > 0 and np.isfinite(pending[t]):
                    cash -= notional
                    positions[t] = {"entry_px": px, "notional": notional,
                                    "stop": px - K * pending[t], "hw": px,
                                    "bars": 0, "below": 0, "pending_exit": False,
                                    "entry_date": d}
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
                if entry_mode == "breakout":
                    sig = row["Close"] >= row["roll_high"] and row["Close"] > row["sma_trend"] \
                        and row["sma_fast"] > row["sma_trend"]
                else:  # regime
                    sig = row["Close"] > row["sma_trend"]
                if sig:
                    mom = row["mom_90"] if np.isfinite(row["mom_90"]) else 0
                    cands.append((mom, t, float(row["atr"])))
            cands.sort(reverse=True)
            for mom, t, a in cands[:free]:
                pending[t] = a

        open_val = sum(pos["notional"] *
                       (float(feats[t]["Close"].iloc[idx[t][d]]) / pos["entry_px"]
                        if d in idx[t] else 1.0)
                       for t, pos in positions.items())
        eq.append((d, cash + open_val))

    eqs = pd.Series(dict(eq))
    tr = pd.DataFrame(trades)
    return summarize(eqs, tr), eqs, tr


def summarize(eqs, tr):
    yrs = (eqs.index[-1] - eqs.index[0]).days / 365.25
    cagr = (eqs.iloc[-1] / eqs.iloc[0]) ** (1 / yrs) - 1
    dd = (eqs / eqs.cummax() - 1).min()
    ye = eqs.groupby(eqs.index.year).agg(["first", "last"])
    yearly = {int(y): r["last"] / r["first"] - 1 for y, r in ye.iterrows()}
    win = (tr["net_ret"] > 0).mean() if len(tr) else 0
    mid = tr["exit_date"].sort_values().iloc[len(tr) // 2] if len(tr) else None
    h1 = (tr[tr.exit_date <= mid].net_ret > 0).mean() if len(tr) else 0
    h2 = (tr[tr.exit_date > mid].net_ret > 0).mean() if len(tr) else 0
    w, l = tr[tr.net_ret > 0], tr[tr.net_ret <= 0]
    pf = w.pnl.sum() / -l.pnl.sum() if len(l) and l.pnl.sum() < 0 else np.inf
    return {"trades": len(tr), "win": win, "h1": h1, "h2": h2, "pf": pf,
            "cagr": cagr, "dd": dd, "yearly": yearly, "final": eqs.iloc[-1]}


def show(name, s):
    reds = [y for y in FULL_YEARS if s["yearly"].get(y, 0) < 0]
    print(f"{name:26s} n={s['trades']:4d} win={s['win']*100:5.1f}% "
          f"[{s['h1']*100:4.1f}/{s['h2']*100:4.1f}] PF={s['pf']:4.2f} "
          f"CAGR={s['cagr']*100:6.1f}% dd={s['dd']*100:6.1f}% "
          f"final={s['final']:>11,.0f} red={reds}")


def main():
    data = fetch_all()
    eq_only = {t: d for t, d in data.items()
               if market_of(t) in ("US", "HK", "JP", "CRYPTO")}
    results = {}
    RUNS = [
        ("T1 brk55 K4 7s", dict(N=55, K=4.0, slots=7), eq_only),
        ("T2 brk100 K4 7s", dict(N=100, K=4.0, slots=7), eq_only),
        ("T3 brk55 K3 7s", dict(N=55, K=3.0, slots=7), eq_only),
        ("T5 brk55 K4 7s US+crypto", dict(N=55, K=4.0, slots=7),
         {t: d for t, d in eq_only.items() if market_of(t) in ("US", "CRYPTO")}),
        ("C1 crypto regime 2s", dict(N=55, K=4.0, slots=2, entry_mode="regime",
                                     exit_sma="sma_fast"),
         {t: d for t, d in data.items() if market_of(t) == "CRYPTO"}),
        ("L1 levETF sma200 3s", dict(N=55, K=4.5, slots=3, entry_mode="regime",
                                     trail=False),
         {t: d for t, d in data.items() if t in LEV_ETFS}),
        ("L4 levETF sma200+trail 3s", dict(N=55, K=4.5, slots=3, entry_mode="regime",
                                           trail=True),
         {t: d for t, d in data.items() if t in LEV_ETFS}),
        ("L5 levETF idx-only 3s", dict(N=55, K=4.5, slots=3, entry_mode="regime",
                                       trail=False),
         {t: d for t, d in data.items() if t in ("TQQQ", "QLD", "SSO", "UPRO", "SPXL")}),
    ]
    import sys
    if "--hybrid-only" in sys.argv:
        RUNS = [r for r in RUNS if r[0].startswith("C1")]
    for name, kw, d in RUNS:
        s, eqs, tr = run_trend(d, **kw)
        results[name] = (s, eqs, tr)
        show(name, s)
        print("     " + " ".join(f"{y}:{s['yearly'].get(y,0)*100:+.0f}" for y in FULL_YEARS)
              + f" 26:{s['yearly'].get(2026,0)*100:+.0f}")

    # hybrids: crypto trend sleeve + X4 dip sleeve, separate capital fractions
    p = dict(STRAT)
    x4_stats, x4_tr, x4_eq = run_backtest(
        {t: d for t, d in data.items() if market_of(t) in ("US", "HK", "JP")},
        p, max_pos=5, verbose=False)
    c_eq = results["C1 crypto regime 2s"][1]
    for name, wc in [("H2 hybrid 2/7 crypto", 2 / 7), ("H3 hybrid 3/7 crypto", 3 / 7)]:
        idx_u = x4_eq.index.union(c_eq.index)
        a = c_eq.reindex(idx_u).ffill().bfill()
        b = x4_eq.reindex(idx_u).ffill().bfill()
        comb = START_CAPITAL_HKD * (wc * a / a.iloc[0] + (1 - wc) * b / b.iloc[0])
        tr_pool = pd.concat([results["C1 crypto regime 2s"][2], x4_tr.rename(
            columns={"pnl_hkd": "pnl"})[["net_ret", "pnl", "exit_date"]]])
        s = summarize(comb, tr_pool.reset_index(drop=True))
        show(name, s)
        print("     " + " ".join(f"{y}:{s['yearly'].get(y,0)*100:+.0f}" for y in FULL_YEARS)
              + f" 26:{s['yearly'].get(2026,0)*100:+.0f}")


if __name__ == "__main__":
    main()
