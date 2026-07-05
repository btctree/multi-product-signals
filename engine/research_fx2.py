"""Regime-GATED FX hedge: hold long-USD FX ONLY when the broad market is
risk-off (S&P < its 200-day), flat otherwise. Goal (user's ask): profit in bad
years without dragging good years. The gate is a general risk-off condition
(not fitted to one event), so it should help 2018/2020/2022 and be INERT in
good years by construction (no positions => no drag).
"""
import numpy as np
import pandas as pd

from config import STRAT, COST_BP, START_CAPITAL_HKD
from data_fetch import fetch_all
from indicators import add_features
from universe import market_of
from engine_rr import summarize, run
import engine_rr
from research_r8_combo import combo

YEARS = list(range(2016, 2027))


def risk_off_series(data):
    """True when S&P closes below its 200-day SMA (shifted 1d, no look-ahead)."""
    spx = data["^GSPC"]["Close"]
    sma = spx.rolling(200).mean()
    return (spx < sma).shift(1).fillna(False)


def run_fx_hedge(fx_data, risk_off, slots=2, K=4.0, start=START_CAPITAL_HKD):
    feats = {t: add_features(df, STRAT) for t, df in fx_data.items()}
    calendar = sorted(set().union(*[set(df.index) for df in feats.values()]))
    idx = {t: {d: i for i, d in enumerate(df.index)} for t, df in feats.items()}
    cash = float(start)
    positions, pending, trades, eq = {}, {}, [], []

    def cost(t):
        return COST_BP[market_of(t)] / 10_000

    for d in calendar:
        ro = bool(risk_off.get(d, False))
        for t in list(positions):
            if d not in idx[t]:
                continue
            row = feats[t].iloc[idx[t][d]]
            pos = positions[t]
            exit_px = reason = None
            if pos["pending_exit"]:
                exit_px, reason = float(row["Open"]), pos["pending_exit"]
            elif row["Open"] <= pos["stop"]:
                exit_px, reason = float(row["Open"]), "trail(gap)"
            elif row["Low"] <= pos["stop"]:
                exit_px, reason = pos["stop"], "trail"
            if exit_px is not None:
                net = (exit_px / pos["entry_px"]) * (1 - cost(t)) / (1 + cost(t)) - 1
                cash += pos["notional"] * (1 + net)
                trades.append({"ticker": t, "market": "FX", "net_ret": net,
                               "pnl": pos["notional"] * net, "exit_date": d,
                               "hold": pos["bars"], "reason": reason})
                del positions[t]
                continue
            pos["bars"] += 1
            pos["hw"] = max(pos["hw"], float(row["Close"]))
            if np.isfinite(row["atr"]):
                pos["stop"] = max(pos["stop"], pos["hw"] - K * float(row["atr"]))
            if not ro:                       # risk-on returned -> close the hedge
                pos["pending_exit"] = "risk-on"

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
                    positions[t] = {"entry_px": px, "notional": notional,
                                    "stop": px - K * a, "hw": px, "bars": 0,
                                    "pending_exit": None}
            del pending[t]

        # entries only in risk-off: long the trending (rising) FX pairs
        if ro:
            free = slots - len(positions) - len(pending)
            if free > 0:
                cands = []
                for t, df in feats.items():
                    if t in positions or t in pending or d not in idx[t]:
                        continue
                    row = df.iloc[idx[t][d]]
                    if not (np.isfinite(row["sma_fast"]) and np.isfinite(row["atr"])):
                        continue
                    mom = row["mom_90"] if np.isfinite(row["mom_90"]) else -1
                    if row["Close"] > row["sma_fast"] and mom > 0:
                        cands.append((mom, t, float(row["atr"])))
                cands.sort(reverse=True)
                for mom, t, a in cands[:free]:
                    pending[t] = a

        ov = sum(p["notional"] * (float(feats[t]["Close"].iloc[idx[t][d]]) / p["entry_px"]
                                  if d in idx[t] else 1.0)
                 for t, p in positions.items())
        eq.append((d, cash + ov))

    eqs = pd.Series(dict(eq))
    return summarize(eqs, pd.DataFrame(trades)), eqs


def main():
    data = fetch_all()
    ro = risk_off_series(data)
    fx_data = {t: d for t, d in data.items() if market_of(t) == "FX"}
    print(f"Risk-off days (S&P<SMA200): {ro.sum()} of {len(ro)} "
          f"({ro.mean()*100:.0f}% of the time)")

    s, eq_fx = run_fx_hedge(fx_data, ro, slots=2)
    print(f"\nGATED FX hedge (active only in risk-off): trades={s['trades']} "
          f"win={s['win']*100:.1f}% CAGR={s['cagr']*100:.1f}% dd={s['dd']*100:.1f}% "
          f"final={s['final']:,.0f}")
    print("  yearly: " + " ".join(f"{y}:{s['yearly'].get(y,0)*100:+.1f}" for y in YEARS))

    # blend into C1 (equity). In good years FX holds nothing -> must NOT hurt.
    engine_rr.run.return_curve = True
    s_c1, eq_c1, _ = run(data, K=3.5, K_tight=2.0, rsi_entry=25, near_high=0.88, slots=7)
    curves = {"C1": eq_c1, "FX": eq_fx}
    wins = {"C1": (s_c1["win"], s_c1["trades"]), "FX": (s["win"], max(s["trades"], 1))}
    print(f"\nC1 alone:      win {s_c1['win']*100:.1f}%  CAGR {s_c1['cagr']*100:.1f}%  "
          f"dd {s_c1['dd']*100:.1f}%")
    print("  yearly: " + " ".join(f"{y}:{s_c1['yearly'].get(y,0)*100:+.0f}" for y in YEARS))
    for wfx in (0.10, 0.15, 0.20):
        b = combo(curves, wins, {"C1": 1 - wfx, "FX": wfx})
        print(f"\nC1 + {int(wfx*100)}% gated-FX:  win {b['win']*100:.1f}%  "
              f"CAGR {b['cagr']*100:.1f}%  dd {b['dd']*100:.1f}%")
        print("  yearly: " + " ".join(f"{y}:{b['yearly'].get(y,0)*100:+.0f}" for y in YEARS))


if __name__ == "__main__":
    main()
