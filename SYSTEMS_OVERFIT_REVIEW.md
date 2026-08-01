# Three-System Overfit & Real-Trade Honesty Review — 2026-07-14

**Question:** how overfitted are (1) M1 vs M5, (2) BTC Power, (3) Multi-market — and can they honestly
be applied to real trading?

**Method:** per system: forensic audit (docs + actual engine code, file:line cites) → quantitative
checks run read-only against each system's own engine (split-half/walk-forward OOS, execution-delay
stress, concentration census, config-mining census) → independent adversarial scoring against a
pre-registered 7-dimension rubric. Nothing in any system folder was modified. Evidence JSONs:
scratchpad `overfit_review/review_{m1,btc,multimarket}.json`; audit scripts preserved in scratchpad.

## Scoreboard (overfit / dishonesty RISK per dimension)

| Dimension | M1 vs M5 | BTC Power | Multi-market |
|---|:-:|:-:|:-:|
| Parameter provenance | HIGH | HIGH | **MEDIUM** |
| Selection bias | HIGH | HIGH | HIGH |
| OOS discipline | HIGH | HIGH | **MEDIUM** |
| Data integrity | HIGH | HIGH | HIGH (survivorship) |
| Concentration | HIGH | HIGH | **MEDIUM** |
| Execution realism | HIGH | **MEDIUM** | **MEDIUM** |
| Live tracking | HIGH | HIGH | HIGH |
| **Overall** | **HIGH** | **HIGH** | **HIGH** (mildest) |

## Headline vs measured reality

| | Headline claim | Measured honest expectation |
|---|---|---|
| M1 vs M5 | $500→$71.3M, CAGR 160% | ~1.6–3.2× over the last 2–3y at −40% DD ≈ leveraged BTC beta or worse |
| BTC Power (Max B) | $500→$11.6M, CAGR 124%, Sharpe 1.47 | walk-forward map: CAGR ~44%, Sharpe 0.94; recent-3y: CAGR 8.3%, Sharpe 0.40, DD −56% |
| Multi-market (D) | CAGR 30.8%, DD −29.0%, win 52.5% | ~15–22%/yr, DD expected to TOUCH −30%; win 52.5% (mandate 70% unmet) |

---

## 1) M1 vs M5 — verdict: DO NOT trade the headline; effectively a mined artifact

What it is: BTC margin signal system (Excel port; X/Y engines + regime gate). M1/M5 are position-
SIZING models (M1 confidence-weighted vs M5 flat-50%), not bar intervals. Production evolved past
both: 60/50/45% sizing, 5×L/2×S, 7% trailing stop on 1-min bars. Live = Telegram signals only, no money.

Key measurements (all reproduced from the shipped trade book / engines, read-only):
- **Goal-seeking selection:** ≥4,600 configs evaluated on one 12.3y BTC path; the optimizer literally
  ranks by "CLOSEST TO $11M" (optimize.py:182-195). No untouched holdout ever existed.
- **The headline contradicts the project's own analysis**, which capped the defensible trailing-stop
  uplift at ~2.5–3× and called the 8× "a curve-fit spike... not real" (dddata11_merge_investigation.md:169-172).
- **Look-ahead in the production stop:** intraday_backtest.py:194-202 ratchets the stop from the
  current bar's high before testing the same bar's low — the "0 look-ahead" claim is false; the two
  trailing implementations disagree with each other. Entries re-anchored to 00:00 open on a 23:59 signal.
- **Edge decay:** 766× (2014-19) vs 66× (2020-26); last 3y = 3.2×, last 2y = 1.6× at −40% DD.
  Shorts-only over 12y turns $500 into $434 — half the trade book is net-negative.
- **Concentration:** top-10 trades = 73% of log growth; the other 281 trades are collectively negative.
- **Fragility:** T+1 delay on entry+exit → −98.3% of terminal wealth. 73 pre-Binance "daily-proxy"
  trades (unverifiable intraday) carry ~⅓ of log growth and the worst-365d risk stat.
- **Irreproducible:** $71.3M needs absent 1-min data; the project's own rerun yields $82.8M (16% drift);
  historical prints of the same pipeline spanned $2.5M → $23.8B during debugging.
- **Live accounting is biased:** dashboard books trades at max(close-to-close, final-peak harvest) and
  grafts "live" equity onto the backtest curve; optimism bounded between 1.04× and 10.3× depending on fill assumption.

De-risk (if ever pursued): longs-only, honest fills (next price after signal, gap-throughs at open),
exclude the proxy era, walk-forward the calibration anchors, 3–6 months reconciled paper trading, and
anchor expectations on the recent cohort — not 160%/yr.

## 2) BTC Power (Max B, LIVE ~$320) — verdict: arithmetic honest, expectation not; de-lever until re-based

What it is: daily BTC leveraged ensemble (9 Excel-era engines, regime-gated vote, EMA-5 + dead-band,
vol-targeting, floor/Pi protection stack). Live Binance cross-margin executor since 2026-07-05.

The good (verified): day-level signal timing is genuinely causal (indicators trailing, arrays
pre-shifted); costs charged on turnover with 0/50/100bp columns; @0bp labeled untradeable; docs
unusually honest about front-loading (straightness 0.07) and losing years. The auditor replicated
$11.9M / Sharpe 1.47 / DD −56% exactly from the engine — no reporting inflation.

The three problems that break the headline as a live expectation:
- **Structural look-ahead in the regime→engine map** (the big one): stable_combo.py:37-56 fits the
  map on the FULL 2013-2026 sample (min(H1,H2) in-regime Sharpe = a robustness filter inside the
  window, NOT walk-forward — METHODOLOGY.md:40's claim is contradicted by the code). Identical
  engine/window/costs with an honest expanding walk-forward map: **$1.71M → $24k (71×), Sharpe
  1.46 → 0.94**. The 2020-frozen map has ZERO eligible engines in TREND_DOWN/BOUNCE_DOWN — the
  profitable bear-side assignments exist only with hindsight. live_engine refits this map on full
  history every run, silently.
- **Same-close fills:** one extra close of delay collapses $11.9M → $407k (−96.6%). The live bot
  fills 35–95 min after the UTC close via MARKET orders — between the two cases, never measured.
- **Edge decay:** recent-3y = CAGR 8.3%, Sharpe 0.40 at −56% DD; 2024 −0%, 2025 −38% (the de facto
  OOS year, failed at the cost level the headline claims to survive). Top-10 trades = 106% of
  log-return. ~8,000 configs evaluated; the model swapped 3× in 9 days; Max B shipped 4 days after
  the project's own PRODUCTS_REPORT concluded "NOT recommended — front-loaded, flat since 2021".
- Also: 100% of pre-2017-08 rows have synthetic OHLC (high=low=close) → intraday liquidation checks
  inert during the highest-CAGR years; FUND gate inert pre-2020 (no funding data).

De-risk: freeze + version the eligibility map and republish the walk-forward number (~44%/0.94) as
the headline; correct METHODOLOGY.md:40; report T+1 as base case; re-anchor on 2020+; at walk-forward
Sharpe ~0.9 the 5× cap is unjustified — Steady-A cap-2 or 1× spot is the defensible live config;
run 6–12 months of expected-vs-realized reconciliation before scaling past $320.

## 3) Multi-market (D, LIVE via IB bot) — verdict: real but smaller edge; the LIVE BOT is the bigger problem

The most defensible backtest of the three:
- Headline reproduces exactly (win 52.5 / DD −29.0 / CAGR 30.6 vs 30.8 = 2 days drift).
- Engine verified look-ahead-clean at code level (stop-tested-before-ratchet, next-open fills,
  stop-first) — the exact bug classes the other two systems have are correctly avoided here.
- **Passes the T+2 delay test almost unmoved (CAGR −0.4pp)** — the FTLT-killer test; the edge is not
  an execution artifact.

The honest haircuts (all measured with the project's own engine):
- **Train→test decay:** 2015-20: 37.8%/yr, Sharpe 1.36 → 2021-26: **23.7%/yr, Sharpe 0.79**. Recent-3y 33.7%.
- **Survivorship:** universe built 2026-07-09 from current index members; pre-expansion pool at D's
  own 13+2 slots: **21.3%/yr** (−9.3pp); DD −21.7% still passes the gate; true point-in-time floor
  is lower (delisted names absent entirely).
- **Concentration:** top-10 tickers = 48.1% of equity P&L; 7 of the 10 entered via the 2026
  expansion. Dropping them: 26.1%/yr with maxDD exactly −30.0% — **the DD gate has zero buffer.**
- **Selection:** ≥170–220 configs on the same visible window; live config changed 6× in 9 days;
  win gate renegotiated 70→68→60→silently dropped (D ships at 52.5%).
- Minor: monthly rebalance flows charged zero cost in combo(); Sharpe 0.88 in config.py is the
  equity-sleeve number mislabeled (combined ≈1.04 — benign direction).

**Deployment infidelity — bigger than the overfit findings, and live NOW:**
1. **ib_bot.py does not implement validated D**: flat NetLiq/15 sizing (crypto ~13% weight, not
   30%), no monthly rebalance, no 13+2 sleeve split → live P&L will track neither the return nor
   the risk profile of anything backtested.
2. **Kill-switch deadlock:** NetLiq <92% of an all-time peak makes `run()` return at ib_bot.py:167
   BEFORE the exit-management loop at :174 — in any >8% drawdown the bot freezes entirely,
   including exits, and cannot un-freeze while unmanaged positions keep falling. The backtest's own
   path includes a −29% drawdown; this switch would have triggered and left positions unmanaged.
3. **FX conversion unit bug** (ib_bot.py:141-147): buys `need_base` UNITS of currency, not
   need_base-HKD-worth (~18× under-funding for JPY).
4. Exits are once-daily snapshot sells with no resting broker stops (backtest fills trails
   intraday) — systematic negative slippage on gap-downs.

De-risk: fix the three bot defects and make the executor implement D exactly; publish test-half
(23.7%) and off-pool (21.3%) numbers next to the headline and state 15–22%/yr as the live base
case; freeze config D for 6–12 months; build a live-vs-sim reconciliation report; point-in-time
universe for the US sleeve.

---

## Cross-system lessons

1. All three share the same core sin: **selection from a large tested menu on a fully visible
   window, with no untouched holdout for the shipped config** — despite the playbook (§1.3) and
   METHODOLOGY (§0.6) forbidding exactly this. The rules were applied to reject ideas, never to
   validate the winner.
2. Overfit shows up as **edge decay toward the present** in every system (M1M5 766×→66×; BTC Sharpe
   1.82→1.06→0.40; Multi 37.8→23.7%). Anchor all live expectations on the most recent window.
3. The T+1/T+2 delay stress separates real edges from artifacts cheaply: Multi-market passed
   (−0.4pp), BTC Power failed (−96.6%), M1M5 failed (−98.3%). Adopt it as a standard report column.
4. Live-money reality: only Multi-market and BTC Power trade real money, both for days only, and
   **neither has a live-vs-backtest reconciliation harness** — the only honest OOS still available
   to all three is walk-forward on future data. Build the harness before scaling any of them.

*All figures net of each project's own cost model; scripts to reproduce every number are in the
session scratchpad (`overfit_review/`). Hypothetical research; not financial advice.*
