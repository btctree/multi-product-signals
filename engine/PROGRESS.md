# Build Progress — High-R:R Multi-Market Engine (plan 1)

Checkpoint tracker for token-budgeted sub-projects. Any session: read this,
run the next `TODO`, mark `DONE`, stop when the token window is near its limit.
Run order SP0 → SP4 (SP5/SP6 later). Full plan: `../.claude/plans/1-*.md`.

| SP | What | State | Result |
|----|------|-------|--------|
| SP0 | Universe: add EU shares, copper, USD index, FX | **DONE** | 194 tickers; FX majors added (EUR/USD, USD/JPY, GBP/USD, USD/CAD), FX-aware 0.3% ATR floor |
| SP1 | High-R:R engine (dip entry + let-winners-run trail) | **FRONTIER (needs user pick)** | win≥55 AND R:R≥2 impossible (PF 2.44 needed, best 1.87). Scorecard w/ FX in PERFORMANCE.md. Candidates C1/C2/C3/C4 |
| SP2 | Options overlay model (deep-ITM calls/puts) | **DONE (model)** | full deep-ITM calls = only path to 8M but DD −92%; any leverage breaks 30% DD gate |
| SP2 | Options overlay model (deep-ITM calls/puts) | TODO | modeled, not chain-backtested |
| SP3 | 7-role review + convergence | TODO | |
| SP4 | Per-symbol advisor + report/PnL upgrade | TODO | |
| SP5 | iPhone webpage | LATER | |
| SP6 | Automation/resume glue | LATER | |

## Hard gates (this build)
- Win ≥ 55% (floor), target ≥ 60%. R:R ≥ 2:1 (hard), ideal 3:1. maxDD ≤ 30%.
- No liquidation (buy-only; options never written).
- If gates can't both hold: keep R:R ≥ 2:1, let win fall toward 55% (never below).
  If still unreachable → report frontier honestly, no overfit.

## FX verdict (2026-07-05)
- FX via dip engine: rarely trades, net loss (−8k) → not a tradeable profit sleeve.
- FX via trend engine: −1.2% CAGR standalone → net drag.
- **Regime-gated FX hedge (long-USD only when S&P<SMA200)**: ZERO drag in good years
  (holds nothing), +8.9% in 2022. Works as designed BUT fixed allocation dilutes
  return via opportunity cost; unleveraged benefit is small. Marginal DD-smoother,
  not a profit driver. Keep FX monitor-only + optional 10% gated hedge if DD matters.

## Notes
- Options: NO free historical chain data → SP2 is a labelled Black-Scholes MODEL,
  deep-ITM only (delta ≥ 0.75), conservative IV = realized-vol × (1+VRP markup).
- Do NOT touch the existing BTC repo/folder.
