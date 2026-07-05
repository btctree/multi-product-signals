"""Round 7B — add a SHORT side to the X4 dip engine (user allowed long/short).

Short = mirror dip with playbook asymmetry: overbought rip (RSI(3)>85) inside a
downtrend (close<SMA200, SMA50<SMA200, mom90<0); cover target entry-2.0*ATR,
stop entry+min(2.5*ATR, 7%), RSI<30 backup cover, 25-bar time stop.
Costs: market cost + 15bp extra per side + borrow 3%/yr (8%/yr crypto) on hold.
Adverse-first same-day rule both directions. Long side identical to X4.
"""
import numpy as np
import pandas as pd

from config import STRAT, COST_BP, START_CAPITAL_HKD
from data_fetch import fetch_all
from indicators import add_features
from universe import market_of
from research_r7_trend import summarize, show, FULL_YEARS

IDX_OF = {"US": "^GSPC", "HK": "^HSI", "JP": "^N225", "CRYPTO": "BTC-USD"}


def run_ls(data, slots=7, short_frac=1.0, bear_gate=False):
    p = dict(STRAT)
    tradeable = {t: add_features(df, p) for t, df in data.items()
                 if market_of(t) in ("US", "HK", "JP", "CRYPTO")}
    calendar = sorted(set().union(*[set(df.index) for df in tradeable.values()]))
    idx = {t: {d: i for i, d in enumerate(df.index)} for t, df in tradeable.items()}

    bear = {}
    if bear_gate:
        for m, it in IDX_OF.items():
            if it in data:
                c = data[it]["Close"]
                flag = c < c.rolling(200).mean()
                bear[m] = flag.reindex(calendar).ffill().fillna(False)

    cash, positions, trades, eq = float(START_CAPITAL_HKD), {}, [], []
    pending = {}

    def cost(t):
        return COST_BP[market_of(t)] / 10_000

    for d in calendar:
        for t in list(positions):
            if d not in idx[t]:
                continue
            row = tradeable[t].iloc[idx[t][d]]
            pos = positions[t]
            side = pos["side"]
            exit_px, reason = None, None
            if pos["pending_exit"]:
                exit_px, reason = row["Open"], pos["pending_exit"]
            elif side == 1:
                if row["Open"] <= pos["stop"]:
                    exit_px, reason = row["Open"], "stop"
                elif row["Low"] <= pos["stop"]:
                    exit_px, reason = pos["stop"], "stop"
                elif row["Open"] >= pos["tp"]:
                    exit_px, reason = row["Open"], "target"
                elif row["High"] >= pos["tp"]:
                    exit_px, reason = pos["tp"], "target"
            else:
                if row["Open"] >= pos["stop"]:
                    exit_px, reason = row["Open"], "stop"
                elif row["High"] >= pos["stop"]:
                    exit_px, reason = pos["stop"], "stop"
                elif row["Open"] <= pos["tp"]:
                    exit_px, reason = row["Open"], "target"
                elif row["Low"] <= pos["tp"]:
                    exit_px, reason = pos["tp"], "target"
            if exit_px is not None:
                c = cost(t) + (0.0015 if side == -1 else 0)
                if side == 1:
                    gross = exit_px / pos["entry_px"] - 1
                else:
                    gross = (pos["entry_px"] - exit_px) / pos["entry_px"]
                borrow = 0.0
                if side == -1:
                    rate = 0.08 if market_of(t) == "CRYPTO" else 0.03
                    borrow = rate * pos["bars"] / 252
                net = gross - 2 * c - borrow
                cash += pos["notional"] * (1 + net)
                trades.append({"ticker": t, "side": side, "net_ret": net,
                               "pnl": pos["notional"] * net, "exit_date": d,
                               "market": market_of(t), "reason": reason})
                del positions[t]
                continue
            pos["bars"] += 1
            if side == 1 and row["rsi"] > p["rsi_exit"]:
                pos["pending_exit"] = "signal"
            elif side == -1 and row["rsi"] < 30:
                pos["pending_exit"] = "signal"
            elif pos["bars"] >= p["max_hold_days"]:
                pos["pending_exit"] = "time"

        for t in list(pending):
            if d not in idx[t]:
                continue
            if len(positions) < slots and t not in positions:
                row = tradeable[t].iloc[idx[t][d]]
                px = float(row["Open"])
                side, a = pending[t]["side"], pending[t]["atr"]
                frac = 1.0 if side == 1 else short_frac
                notional = frac * cash / max(1, slots - len(positions))
                if notional > 0:
                    cash -= notional
                    if side == 1:
                        stop = max(px - p["stop_atr_mult"] * a, px * 0.90)
                        tp = px + p["target_atr_mult"] * a
                    else:
                        stop = min(px + 2.5 * a, px * 1.07)
                        tp = px - 2.0 * a
                    positions[t] = {"entry_px": px, "notional": notional,
                                    "side": side, "stop": stop, "tp": tp,
                                    "bars": 0, "pending_exit": None}
            del pending[t]

        free = slots - len(positions) - len(pending)
        if free > 0:
            cands = []
            for t, df in tradeable.items():
                if t in positions or t in pending or d not in idx[t]:
                    continue
                row = df.iloc[idx[t][d]]
                if not (np.isfinite(row["sma_trend"]) and np.isfinite(row["atr"])):
                    continue
                if row["atr"] / row["Close"] <= p["min_atr_pct"]:
                    continue
                mom = row["mom_90"] if np.isfinite(row["mom_90"]) else 0
                if (row["Close"] > row["sma_trend"] and row["sma_fast"] > row["sma_trend"]
                        and mom > 0 and row["rsi"] < p["rsi_entry"]):
                    cands.append((abs(mom), t, 1, float(row["atr"])))
                elif (row["Close"] < row["sma_trend"] and row["sma_fast"] < row["sma_trend"]
                        and mom < 0 and row["rsi"] > 85):
                    if bear_gate:
                        bf = bear.get(market_of(t))
                        if bf is None or not bool(bf.get(d, False)):
                            continue
                    cands.append((abs(mom), t, -1, float(row["atr"])))
            cands.sort(reverse=True)
            for mom, t, side, a in cands[:free]:
                pending[t] = {"side": side, "atr": a}

        open_val = 0.0
        for t, pos in positions.items():
            i = idx[t].get(d)
            last = float(tradeable[t]["Close"].iloc[i]) if i is not None else pos["entry_px"]
            move = last / pos["entry_px"] - 1
            open_val += pos["notional"] * (1 + pos["side"] * move)
        eq.append((d, cash + open_val))

    eqs = pd.Series(dict(eq))
    tr = pd.DataFrame(trades)
    return summarize(eqs, tr), tr


def main():
    data = fetch_all()
    RUNS = [
        ("S1 L/S full-size", dict(short_frac=1.0)),
        ("S2 L/S half-size shorts", dict(short_frac=0.5)),
        ("S3 L/S bear-gated", dict(short_frac=1.0, bear_gate=True)),
        ("S4 L/S gated+half", dict(short_frac=0.5, bear_gate=True)),
    ]
    for name, kw in RUNS:
        s, tr = run_ls(data, slots=7, **kw)
        show(name, s)
        if len(tr):
            sh = tr[tr.side == -1]
            lg = tr[tr.side == 1]
            print(f"     longs: n={len(lg)} win={(lg.net_ret > 0).mean()*100:.1f}% "
                  f"pnl={lg.pnl.sum():+,.0f} | shorts: n={len(sh)} "
                  f"win={(sh.net_ret > 0).mean()*100:.1f}% pnl={sh.pnl.sum():+,.0f}")
        print("     " + " ".join(f"{y}:{s['yearly'].get(y,0)*100:+.0f}" for y in FULL_YEARS)
              + f" 26:{s['yearly'].get(2026,0)*100:+.0f}")


if __name__ == "__main__":
    main()
