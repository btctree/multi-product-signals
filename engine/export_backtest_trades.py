"""Export the live product's (P1+SMA200) backtest trade history for the
dashboard History tab. Runs in the daily CI job; writes data/backtest_trades.json.
Labeled clearly on the dashboard as VALIDATION trades, not live fills."""
import json

import engine_rr
from engine_rr import run
from research_r7_trend import run_trend
from data_fetch import fetch_all
from universe import market_of
from config import DATA_DIR

engine_rr.run.return_curve = True


def main():
    data = fetch_all()
    _, _, tr_eq = run(data, K=3.5, K_tight=2.0, rsi_entry=25, near_high=0.88,
                      slots=5, regime_exit=200,
                      markets=("US", "HK", "JP", "EU"))
    _, _, tr_cr = run_trend({t: d for t, d in data.items()
                             if market_of(t) == "CRYPTO"},
                            N=55, K=4.0, slots=2, entry_mode="regime",
                            exit_sma="sma_fast")
    rows = []
    for tr, sleeve in ((tr_eq, "DIP"), (tr_cr, "CRY")):
        if tr is None or tr.empty:
            continue
        for _, r in tr.iterrows():
            rows.append({
                "sym": r["ticker"], "sleeve": sleeve,
                "entry_date": str(r.get("entry_date"))[:10],
                "exit_date": str(r["exit_date"])[:10],
                "entry_px": round(float(r["entry_px"]), 4) if r.get("entry_px") else None,
                "ret_pct": round(float(r["net_ret"]) * 100, 2),
                "hold_days": int(r["hold"]),
                "reason": r.get("reason", ""),
            })
    rows.sort(key=lambda x: x["exit_date"], reverse=True)
    out = rows[:200]
    (DATA_DIR / "backtest_trades.json").write_text(json.dumps(out, indent=1))
    wins = sum(1 for r in out if r["ret_pct"] > 0)
    print(f"exported {len(out)} most recent backtest trades "
          f"({wins}/{len(out)} winners) -> data/backtest_trades.json")


if __name__ == "__main__":
    main()
