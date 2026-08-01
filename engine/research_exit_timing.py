"""EXIT-TIMING TEST — validated intraday trail vs close-check/next-open exits.

User question (2026-08-01): the live bot evaluates exits on the daily CLOSE and
executes at the NEXT OPEN; the validated engine (engine_rr) models the trail as
a resting intraday stop (gap -> open fill, touch -> fill AT the stop). Which
costs what, on the same database and the same config D?

Arms (equity sleeve only; the CRY 30% sleeve is IDENTICAL across arms so the
combined numbers differ only through the equity sleeve):
  A  validated   engine_rr.run untouched (intraday stop fills)   <- must equal
                 a faithful in-file copy, asserted, else abort
  B  close-check trail tested on Close AFTER the daily ratchet (exactly the
                 live bot's ordering, ib_bot.py:496-505), fill at next open.
                 Everything else identical to A (incl. k-anchor, time stop).
  C  live-exact  B plus the live bot's two other divergences: k-tighten anchors
                 on today's Close/ATR (not high-water/entry-day ATR) and there
                 is NO time stop.

Pre-registered: no parameter search, D config only, report all arms whole.
Data: local price cache read DIRECTLY (no refresh) — identical bars in all arms.
NOT financial advice; research only. Untracked, like the other research_*.py.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

import engine_rr
from engine_rr import summarize
from research_r7_trend import run_trend
from research_r8_combo import combo
from config import STRAT, COST_BP, START_CAPITAL_HKD, MIN_ATR_BY_MARKET
from data_fetch import cache_path
from indicators import add_features
from universe import market_of, load_universe

YEARS = list(range(2016, 2027))
C1 = dict(K=3.5, K_tight=2.0, rsi_entry=25, near_high=0.88, regime_exit=200)
EQ_MARKETS = ("US", "HK", "JP", "EU")


def load_cached():
    """Read the price cache AS-IS (fetch_all would refresh anything >20h old;
    this test must run on one frozen database)."""
    out = {}
    for t in load_universe()["tickers"]:
        p = cache_path(t)
        if not p.exists():
            continue
        df = pd.read_csv(p, index_col=0, parse_dates=True)
        if len(df) > 100:
            out[t] = df[["Open", "High", "Low", "Close", "Volume"]].dropna(
                subset=["Close"])
    return out


def run_mode(data, mode, rsi_entry=15, K=3.0, K_tight=None, tighten_at=1.5,
             slots=7, min_mom=0.0, min_atr=0.012, max_hold=60, time_stop=True,
             markets=EQ_MARKETS, near_high=0.0, regime_exit=None,
             regime_exit_consec=1):
    """Faithful copy of engine_rr.run (D-relevant paths only: partial/gate/
    ranker/slope options unused by C1 are omitted) with a `mode` switch:
      'intraday'  — byte-faithful to engine_rr (assert-anchored in main())
      'close'     — no Open/Low stop tests; after the ratchet, Close <= stop
                    -> pending exit, filled at NEXT open (live bot ordering)
      'live'      — 'close' + k-tighten on today's Close/ATR + no time stop
    """
    p = dict(STRAT)
    feats = {t: add_features(df, p) for t, df in data.items()
             if market_of(t) in markets}
    calendar = sorted(set().union(*[set(df.index) for df in feats.values()]))
    idx = {t: {d: i for i, d in enumerate(df.index)} for t, df in feats.items()}
    cash = float(START_CAPITAL_HKD)
    positions, pending, trades, eq = {}, {}, [], []

    def cost(t):
        return COST_BP[market_of(t)] / 10_000

    def close_pos(t, pos, exit_px, reason, d):
        nonlocal cash
        c = cost(t)
        final_ret = (exit_px / pos["entry_px"]) * (1 - c) / (1 + c) - 1
        final_pnl = pos["notional"] * final_ret
        total_pnl = pos["banked"] + final_pnl
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
                close_pos(t, pos, float(row["Open"]), pos["pending_exit"], d)
                continue
            if mode == "intraday":
                if row["Open"] <= pos["stop"]:
                    close_pos(t, pos, float(row["Open"]), "trail(gap)", d)
                    continue
                if row["Low"] <= pos["stop"]:
                    close_pos(t, pos, pos["stop"], "trail", d)
                    continue
            pos["bars"] += 1
            pos["hw"] = max(pos["hw"], float(row["Close"]))
            if np.isfinite(row["atr"]):
                k = K
                if K_tight is not None:
                    if mode == "live":      # live bot: today's close vs today's ATR
                        trig = float(row["Close"]) >= pos["entry_px"] \
                            + tighten_at * float(row["atr"])
                    else:                   # validated: high-water vs entry-day ATR
                        trig = pos["hw"] >= pos["entry_px"] \
                            + tighten_at * pos["atr0"]
                    if trig:
                        k = K_tight
                pos["stop"] = max(pos["stop"], pos["hw"] - k * float(row["atr"]))
            if mode in ("close", "live") and float(row["Close"]) <= pos["stop"]:
                pos["pending_exit"] = "trail(close)"
            if regime_exit:
                sma_v = row.get("sma_trend")
                if np.isfinite(sma_v) and row["Close"] < sma_v:
                    pos["below_sma"] = pos.get("below_sma", 0) + 1
                    if pos["below_sma"] >= regime_exit_consec:
                        pos["pending_exit"] = "regime(SMA)"
                else:
                    pos["below_sma"] = 0
            if time_stop and pos["bars"] >= max_hold:
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

        # 3) scan dip entries (act next open)
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
                ok = (row["Close"] > row["sma_trend"]
                      and row["sma_fast"] > row["sma_trend"]
                      and mom > min_mom and row["rsi"] < rsi_entry
                      and row["atr"] / row["Close"] > matr)
                if ok and near_high > 0:
                    hi = row.get("hi_52w")
                    ok = ok and np.isfinite(hi) and row["Close"] >= near_high * hi
                if ok:
                    cands.append((mom, t, float(row["atr"])))
            cands.sort(reverse=True)
            for _, t, a in cands[:free]:
                pending[t] = a

        # 4) mark to market
        ov = 0.0
        for t, pos in positions.items():
            i = idx[t].get(d)
            last = float(feats[t]["Close"].iloc[i]) if i is not None \
                else pos["entry_px"]
            ov += pos["banked"] + pos["notional"] * (last / pos["entry_px"])
        eq.append((d, cash + ov))

    eqs = pd.Series(dict(eq))
    tr_df = pd.DataFrame(trades)
    return summarize(eqs, tr_df), eqs, tr_df


def combo_full(curves, wins, weights):
    """research_r8_combo.combo copied verbatim, extended to also return the
    combined CURVE plus sharpe/calmar/total_ret. Anchored in main(): its
    cagr/dd/final must equal combo()'s to 1e-9 or the run aborts."""
    idx = None
    for k in weights:
        idx = curves[k].index if idx is None else idx.union(curves[k].index)
    rets = {}
    for k in weights:
        c = curves[k].reindex(idx).ffill()
        rets[k] = c.pct_change().fillna(0)
    month = idx.to_period("M")
    port = []
    w = dict(weights)
    cur_m = None
    for i, d in enumerate(idx):
        if month[i] != cur_m:
            cur_m = month[i]
            w = dict(weights)
        r = sum(w[k] * rets[k].iloc[i] for k in weights)
        port.append(r)
        tot = 1 + r
        for k in weights:
            w[k] = w[k] * (1 + rets[k].iloc[i]) / tot
    from config import START_CAPITAL_HKD as SC
    eq = SC * (1 + pd.Series(port, index=idx)).cumprod()
    yrs = (idx[-1] - idx[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1
    dd = (eq / eq.cummax() - 1).min()
    daily = eq.pct_change().dropna()
    sharpe = daily.mean() / daily.std() * np.sqrt(252) if daily.std() > 0 else 0
    ye = eq.groupby(eq.index.year).agg(["first", "last"])
    yearly = {int(y): r["last"] / r["first"] - 1 for y, r in ye.iterrows()}
    n = sum(wins[k][1] for k in weights)
    bw = sum(wins[k][0] * wins[k][1] for k in weights) / n
    return {"cagr": cagr, "dd": dd, "yearly": yearly, "win": bw,
            "final": eq.iloc[-1], "trades_yr": n / yrs, "sharpe": sharpe,
            "calmar": cagr / abs(dd) if dd < 0 else np.inf,
            "total_ret": eq.iloc[-1] / eq.iloc[0] - 1}, eq


def full(s):
    """Everything summarize() computed, JSON-safe (drop nothing the report needs)."""
    return {k: v for k, v in s.items() if k != "per_market_win"} \
        | {"per_market_win": {m: float(w) for m, w in
                              s.get("per_market_win", {}).items()}}


def main():
    data = load_cached()
    newest = max(df.index[-1] for df in data.values())
    print(f"pool: {len(data)} products | last bar in cache: {newest.date()}")

    # --- crypto sleeve: computed ONCE, identical in every arm ---
    s_cr, eq_cr, _ = run_trend({t: d for t, d in data.items()
                                if market_of(t) == "CRYPTO"},
                               N=55, K=4.0, slots=2, entry_mode="regime",
                               exit_sma="sma_fast")

    # --- fidelity anchor: arm A's copied engine must equal engine_rr.run ---
    engine_rr.run.return_curve = True
    s_ref, eq_ref, tr_ref = engine_rr.run(data, **C1, slots=13, min_mom=0.30,
                                          markets=EQ_MARKETS)
    arms, out = {}, {}
    for mode, ts, tag in (
            ("intraday", True, "A validated (intraday trail, time stop 60)"),
            ("close", True, "B close-check + time stop 60"),
            ("live", False, "C live-exact (live k-anchor, NO time stop)"),
            ("close", False, "D close-check, NO time stop"),
            ("live", True, "E live k-anchor + time stop 60")):
        s, eqs, tr = run_mode(data, mode, time_stop=ts, **C1, slots=13,
                              min_mom=0.30)
        if mode == "intraday":
            drift = abs(float(eqs.iloc[-1]) - float(eq_ref.iloc[-1]))
            assert drift < 1e-6 and len(tr) == len(tr_ref), \
                f"copy diverges from engine_rr ({drift=}, {len(tr)} vs {len(tr_ref)})"
            print("fidelity anchor OK: copied engine == engine_rr exactly "
                  f"({len(tr)} trades, final {eqs.iloc[-1]:,.0f})")
        curves = {"EQ": eqs, "CR": eq_cr}
        wt = {"EQ": (s["win"], s["trades"]), "CR": (s_cr["win"], s_cr["trades"])}
        b_ref = combo(curves, wt, {"EQ": .70, "CR": .30})
        b, _eq_comb = combo_full(curves, wt, {"EQ": .70, "CR": .30})
        for k in ("cagr", "dd", "final"):
            assert abs(b[k] - b_ref[k]) < 1e-9 * max(1, abs(b_ref[k])), \
                f"combo_full diverges from combo on {k}"
        exits_by_year = {} if tr.empty else {
            int(y): int(n) for y, n in
            pd.to_datetime(tr.exit_date).dt.year.value_counts().sort_index().items()}
        arms[tag] = (s, b, tr)
        out[tag] = {"equity": full(s),
                    "combined": {k: (b[k] if not isinstance(b[k], dict) else
                                     {y: b[k].get(y, 0) for y in YEARS})
                                 for k in ("win", "cagr", "dd", "final", "yearly",
                                           "sharpe", "calmar", "total_ret",
                                           "trades_yr")},
                    "exit_reasons": {} if tr.empty else
                    tr.reason.value_counts().to_dict(),
                    "liquidations_by_year": exits_by_year}
        print(f"\n=== {tag} ===")
        print(f"  EQUITY : n={s['trades']} win={s['win']*100:.1f}% "
              f"R:R={s['rr']:.2f} PF={s['pf']:.2f} CAGR={s['cagr']*100:.1f}% "
              f"dd={s['dd']*100:.1f}% Sharpe={s['sharpe']:.2f} "
              f"hold={s['avg_hold']:.0f} [h1 {s['h1']*100:.0f} / h2 {s['h2']*100:.0f}]")
        print(f"  COMBINED 70/30: win={b['win']*100:.1f}% CAGR={b['cagr']*100:.1f}% "
              f"dd={b['dd']*100:.1f}% 150k->{b['final']:,.0f} "
              f"[DD gate {'PASS' if b['dd'] >= -0.30 else 'BREACH'}]")
        print("  yearly: " + " ".join(f"{y}:{b['yearly'].get(y, 0)*100:+.0f}"
                                      for y in YEARS))
        print("  exits  : " + ", ".join(f"{k}:{v}" for k, v in
                                        sorted(out[tag]["exit_reasons"].items())))

    a = arms["A validated (intraday trail, time stop 60)"][1]
    for tag in list(arms)[1:]:
        b = arms[tag][1]
        print(f"\nDELTA  {tag}  vs A: "
              f"win {100*(b['win']-a['win']):+.1f}pp  "
              f"CAGR {100*(b['cagr']-a['cagr']):+.1f}pp  "
              f"maxDD {100*(b['dd']-a['dd']):+.1f}pp  "
              f"final {b['final']-a['final']:+,.0f}")

    # the two clean time-stop isolations (all else equal within each pair)
    for w, wo, lbl in ((
            "B close-check + time stop 60", "D close-check, NO time stop",
            "sticky k-anchor"), (
            "E live k-anchor + time stop 60",
            "C live-exact (live k-anchor, NO time stop)", "live k-anchor")):
        x, y = arms[w][1], arms[wo][1]
        print(f"TIME-STOP EFFECT ({lbl}): with-vs-without  "
              f"win {100*(x['win']-y['win']):+.1f}pp  "
              f"CAGR {100*(x['cagr']-y['cagr']):+.1f}pp  "
              f"maxDD {100*(x['dd']-y['dd']):+.1f}pp  "
              f"final {x['final']-y['final']:+,.0f}")

    with open("../data/exit_timing_test.json", "w") as f:
        json.dump(out, f, indent=1, default=float)
    print("\nsaved ../data/exit_timing_test.json")


if __name__ == "__main__":
    main()
