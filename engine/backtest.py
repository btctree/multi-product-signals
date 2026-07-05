"""Honest 10-year portfolio backtest.

Discipline (METHODOLOGY §0):
- Signals use close(t) only; fills happen at open(t+1). Zero look-ahead.
- Stop-losses are resting orders: filled intraday at the stop (or worse, at the
  open if the market gaps through) — never at a price the market didn't offer.
- Costs charged per side on every fill (per-market bp in config).
- Fixed HKD 10,000 per position (spec), max 15 concurrent, long only, no margin.
- Instrument returns applied in % to the HKD stake (FX drift not modelled —
  stated assumption, second-order for HKD-pegged USD and ~10% JPY exposure).
"""
import numpy as np
import pandas as pd

from config import STRAT, COST_BP, MAX_POSITIONS, POSITION_HKD, \
    START_CAPITAL_HKD, SIZING
from indicators import add_features
from strategy import entry_signal, exit_signal, stop_price, candidate_score
from universe import market_of


def prepare(data: dict[str, pd.DataFrame], p: dict) -> dict[str, pd.DataFrame]:
    return {t: add_features(df, p) for t, df in data.items()}


def run_backtest(data: dict[str, pd.DataFrame], p: dict = STRAT,
                 max_pos: int = MAX_POSITIONS, verbose: bool = True):
    feats = prepare(data, p)
    # master calendar = union of all trading dates
    calendar = sorted(set().union(*[set(df.index) for df in feats.values()]))
    # per-ticker fast lookup
    idx = {t: {d: i for i, d in enumerate(df.index)} for t, df in feats.items()}

    positions: dict[str, dict] = {}   # ticker -> {entry_px, stop, tp, notional, ...}
    pending_entry: dict[str, dict] = {}
    trades: list[dict] = []
    equity_curve = []
    cash = float(START_CAPITAL_HKD)   # dynamic mode: real cash ledger (compounds)
    cash_pnl = 0.0                    # fixed mode: cumulative P&L on fixed stakes

    def cost(t):
        return COST_BP[market_of(t)] / 10_000

    # market-breadth risk gate (mechanism: dip-buying works when the tide is in;
    # a majority of the tradeable universe below its own SMA200 = bear market,
    # stand aside for NEW entries). Uses only data through each day - no look-ahead.
    breadth = None
    if p.get("breadth_min"):
        excl = set(p.get("exclude_markets", ()))
        cols = {t: (feats[t]["Close"] > feats[t]["sma_trend"])
                for t in feats if market_of(t) not in excl}
        bdf = pd.DataFrame(cols).reindex(calendar).ffill(limit=5)
        breadth = bdf.mean(axis=1)

    for d in calendar:
        # ---- 1) manage open positions ----
        for t in list(positions):
            if d not in idx[t]:
                continue
            i = idx[t][d]
            df = feats[t]
            row = df.iloc[i]
            pos = positions[t]
            exit_px, reason = None, None

            if pos.get("pending_exit"):
                exit_px, reason = row["Open"], pos["pending_exit"]
            elif row["Open"] <= pos["stop"]:
                exit_px, reason = row["Open"], "stop (gap)"
            elif row["Low"] <= pos["stop"]:
                # conservative: if stop AND target both touched today, assume stop first
                exit_px, reason = pos["stop"], "stop"
            elif p.get("tp_exit") and row["Open"] >= pos["tp"]:
                exit_px, reason = row["Open"], "target (gap)"
            elif p.get("tp_exit") and row["High"] >= pos["tp"]:
                exit_px, reason = pos["tp"], "target"

            if exit_px is not None:
                gross = exit_px / pos["entry_px"] - 1
                net = (1 + gross) * (1 - cost(t)) / (1 + cost(t)) - 1
                notional = pos["notional"]
                pnl = notional * net
                if SIZING == "dynamic":
                    cash += notional * (1 + net)
                else:
                    cash_pnl += pnl
                trades.append({
                    "ticker": t, "market": market_of(t),
                    "entry_date": pos["entry_date"], "exit_date": d,
                    "entry_px": pos["entry_px"], "exit_px": exit_px,
                    "net_ret": net, "pnl_hkd": pnl, "reason": reason,
                    "hold_days": pos["bars"],
                })
                del positions[t]
                continue

            pos["bars"] += 1
            if exit_signal(row, p):
                pos["pending_exit"] = "signal"
            elif pos["bars"] >= p["max_hold_days"]:
                pos["pending_exit"] = "time"

        # ---- 2) fill pending entries at today's open ----
        for t, pe in list(pending_entry.items()):
            if d not in idx[t]:
                continue
            if len(positions) < max_pos and t not in positions:
                i = idx[t][d]
                row = feats[t].iloc[i]
                px = float(row["Open"])
                if SIZING == "dynamic":
                    notional = cash / max(1, max_pos - len(positions))
                    if notional > cash or notional <= 0:
                        del pending_entry[t]
                        continue
                    cash -= notional
                else:
                    notional = POSITION_HKD
                positions[t] = {
                    "entry_px": px, "notional": notional,
                    "stop": stop_price(px, pe["atr"], p),
                    "tp": px + p["target_atr_mult"] * pe["atr"],
                    "entry_date": d, "bars": 0, "pending_exit": None,
                }
            del pending_entry[t]

        # ---- 3) scan today's closes for new signals (fill tomorrow) ----
        free = max_pos - len(positions) - len(pending_entry)
        if breadth is not None:
            b = breadth.get(d)
            if b is None or np.isnan(b) or b < p["breadth_min"]:
                free = 0
        if free > 0:
            cands = []
            excluded = set(p.get("exclude_markets", ()))
            for t, df in feats.items():
                if t in positions or t in pending_entry or d not in idx[t]:
                    continue
                if market_of(t) in excluded:
                    continue  # monitor-only markets (e.g. futures roll artifacts)
                row = df.iloc[idx[t][d]]
                if not np.isfinite(row["sma_trend"]) or not np.isfinite(row["atr"]):
                    continue
                if entry_signal(row, p):
                    cands.append((candidate_score(row), t, float(row["atr"])))
            cands.sort(reverse=True)
            for score, t, a in cands[:free]:
                pending_entry[t] = {"atr": a}

        # ---- 4) mark to market ----
        open_val = 0.0
        for t, pos in positions.items():
            df = feats[t]
            i = idx[t].get(d)
            last_close = float(df["Close"].iloc[i]) if i is not None else float(
                df["Close"].iloc[df.index.searchsorted(d) - 1])
            if SIZING == "dynamic":
                open_val += pos["notional"] * (last_close / pos["entry_px"])
            else:
                open_val += pos["notional"] * (last_close / pos["entry_px"] - 1)
        if SIZING == "dynamic":
            equity_curve.append((d, cash + open_val))
        else:
            equity_curve.append((d, START_CAPITAL_HKD + cash_pnl + open_val))

    eq = pd.Series(dict(equity_curve))
    tr = pd.DataFrame(trades)
    stats = compute_stats(eq, tr)
    if verbose:
        print_stats(stats, tr)
    return stats, tr, eq


def compute_stats(eq: pd.Series, tr: pd.DataFrame) -> dict:
    if tr.empty:
        return {"trades": 0}
    wins = tr[tr["net_ret"] > 0]
    losses = tr[tr["net_ret"] <= 0]
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    total_ret = eq.iloc[-1] / eq.iloc[0] - 1
    dd = (eq / eq.cummax() - 1).min()
    # robustness: split-half win rates
    mid = tr["exit_date"].sort_values().iloc[len(tr) // 2]
    h1 = tr[tr["exit_date"] <= mid]
    h2 = tr[tr["exit_date"] > mid]
    daily = eq.pct_change().dropna()
    sharpe = daily.mean() / daily.std() * np.sqrt(252) if daily.std() > 0 else 0
    # calendar-year equity returns (the every-year-win gate)
    yr_eq = eq.groupby(eq.index.year).agg(["first", "last"])
    yearly = {int(y): r["last"] / r["first"] - 1 for y, r in yr_eq.iterrows()}
    return {
        "yearly_ret": yearly,
        "trades": len(tr),
        "win_rate": len(wins) / len(tr),
        "win_rate_h1": len(h1[h1.net_ret > 0]) / max(len(h1), 1),
        "win_rate_h2": len(h2[h2.net_ret > 0]) / max(len(h2), 1),
        "avg_win": wins["net_ret"].mean() if len(wins) else 0,
        "avg_loss": losses["net_ret"].mean() if len(losses) else 0,
        "profit_factor": (wins["pnl_hkd"].sum() / -losses["pnl_hkd"].sum())
        if losses["pnl_hkd"].sum() < 0 else np.inf,
        "total_pnl_hkd": tr["pnl_hkd"].sum(),
        "total_return": total_ret,
        "cagr": (1 + total_ret) ** (1 / years) - 1 if years > 0 else 0,
        "max_dd": dd,
        "sharpe": sharpe,
        "avg_hold": tr["hold_days"].mean(),
        "expectancy_hkd": tr["pnl_hkd"].mean(),
        "years": years,
    }


def print_stats(s: dict, tr: pd.DataFrame):
    if s.get("trades", 0) == 0:
        print("No trades.")
        return
    print(f"""
================ PORTFOLIO BACKTEST ({s['years']:.1f}y) ================
Trades          : {s['trades']}   (avg hold {s['avg_hold']:.1f} bars)
Win rate        : {s['win_rate'] * 100:.1f}%   [1st half {s['win_rate_h1'] * 100:.1f}% | 2nd half {s['win_rate_h2'] * 100:.1f}%]
Avg win / loss  : {s['avg_win'] * 100:+.2f}% / {s['avg_loss'] * 100:+.2f}%
Profit factor   : {s['profit_factor']:.2f}
Expectancy      : HKD {s['expectancy_hkd']:+,.0f} per trade
Total P&L       : HKD {s['total_pnl_hkd']:+,.0f}  ({s['total_return'] * 100:+.1f}% on HKD {START_CAPITAL_HKD:,})
CAGR            : {s['cagr'] * 100:.1f}%   Sharpe {s['sharpe']:.2f}
Max drawdown    : {s['max_dd'] * 100:.1f}%
""")
    if not tr.empty:
        by_mkt = tr.groupby("market").agg(
            trades=("net_ret", "size"),
            win=("net_ret", lambda x: (x > 0).mean()),
            pnl=("pnl_hkd", "sum"))
        by_mkt["win"] = (by_mkt["win"] * 100).round(1)
        by_mkt["pnl"] = by_mkt["pnl"].round(0)
        print(by_mkt.to_string())
        yr = tr.copy()
        yr["year"] = pd.to_datetime(yr["exit_date"]).dt.year
        by_yr = yr.groupby("year").agg(
            trades=("net_ret", "size"),
            win=("net_ret", lambda x: round((x > 0).mean() * 100, 1)),
            pnl=("pnl_hkd", lambda x: round(x.sum())))
        print()
        print(by_yr.to_string())


if __name__ == "__main__":
    from data_fetch import fetch_all
    data = fetch_all()
    run_backtest(data)
