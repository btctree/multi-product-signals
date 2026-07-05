# Systematic Trading Product — Full Methodology & Logic Reference

**Purpose:** everything we built and learned for the BTC Power product, written as **reusable logic**
(not actions) so any future product investigation (other coins, stocks, commodities, portfolios) can
follow the same framework. Companion docs: `MODELS_FINDINGS.md` (numbers history),
`APEX_REVIEW.md` (role-review example), `../INVESTMENT_PRODUCT_PLAYBOOK.md` (general playbook).
Last verified: 2026-07-03 (commit 94c2e1c era). All figures from the honest engine unless marked.

---

## 0. Honesty rules (non-negotiable, apply to every product)

1. **No look-ahead** — every decision at day *t* uses only data through *t−1*; act at *t*'s price.
   All indicator arrays pre-shifted by 1 day. DSAM-style "peek" inputs must be rebuilt honestly.
2. **Costs on turnover** — fee + slippage charged on |Δexposure| every day. **The @50bp column is
   the truth**; @0bp is a diagnostic only (any strategy's story that lives at 0bp is untradeable).
3. **Honest liquidation** — intraday: if |exposure| × adverse gap (vs yesterday's close, using the
   day's high/low) ≥ 99%, the account is wiped. Report liquidation count for every run.
4. **Refresh data before every backtest** (`fetch_data.py`) — never compare runs on different vintages
   without saying so; local snapshots go stale vs the hourly CI rebuild.
5. **Reproduce the user's own figures first** before changing anything; when numbers disagree, STOP
   and surface it (e.g. the $407k/$931k cap-3 bug; the weekend-weighting bug that inflated mixed
   crypto+equity blends ~2× — closed markets must count as 0% days, `fillna(0)` before weighting).
6. **Overfitting defenses:** train/test split for any scanned parameter (the $450k rotation collapsed
   70%→30%/yr OOS); a rule is *robust* only if it improves **multiple independent bases** (the floor
   and Pi rules improved all 7 models; the adaptive band helped one and hurt another → rejected);
   prefer **pre-registered community/mechanism hypotheses** over mined features; with N tests expect
   ~N/20 false positives at AUC~0.6.
7. **Single source of truth:** one engine writes one results JSON; dashboard, Telegram, and backtest
   all read it → **in sync by construction**. Never send manual one-off messages.

---

## 1. Market type definition (regime taxonomy)

**Layer 1 — daily regime (regime_v2):** trend × volatility grid with hysteresis, 8 types:
`STRONG_UP, TREND_UP, PULLBACK_UP, STRONG_DOWN, TREND_DOWN, BOUNCE_DOWN, CHOP_HIVOL, RANGE`.
Trend from SMA-slope/ADX-style strength + price vs SMA200; vol from realized-vol rank. Hysteresis
prevents regime flapping. Each regime maps to the strategy engines allowed to vote in it
(regime→engine map found by walk-forward edge check, NOT full grid search).

**Layer 2 — cycle / structural state (the shields):**
- **200WMA floor:** price vs the 200-week MA — BTC has bottomed there every cycle (2015/2018/2022).
  Below it = cycle-bottom zone.
- **Pi Cycle top alarm:** 111DMA crossing above 2×350DMA = parabolic overheating; historically fired
  2017-12-17 (3 days before top) and 2021-04-12, zero false positives. Alarm window = 365 days.
- **Macro risk state:** dollar trend (UUP vs its SMA200) — the only cross-validated macro hint
  (AUC 0.68 train / 0.58 test); strong dollar = liquidity headwind.
- **Vol rank** (1y trailing percentile of 20d realized vol) and **funding rank** (1y percentile).

**Lesson:** losing-year "market types" (2018/2022/2025-style) are NOT separable from profit years in
real time by price-derived features (27 features tested, best OOS AUC 0.58; perfect hindsight would
have made $9.8M vs $439k for the best real-time detector — that gap is the cost of not knowing the
future). What DOES work: structural cycle rules with a mechanism (floor, Pi) and zero false positives.

## 2. Signal construction (the voting brain)

- **9 strategy engines** (MACD-reversal, MACD-vs-signal, RSI, MFI, BB, OBV, OBV-vs-ROC, EMA-cross,
  DSAM-honest), each an alternating long/short state machine, validated 100% against the original
  Excel before any optimization.
- **Regime-gated vote:** only the engines mapped to the current regime vote. Net vote = raw exposure
  in [−1, +1] = **confidence**.
- **Conviction filter:** act only when |vote| ≥ 0.40, else flat. (Votes ARE the confidence index —
  a second committee layer over model variants adds nothing: the 7-model committee agreed 96% of
  days; unanimity voting collapsed returns $11.6M→$145k by entering late.)

## 3. Turnover control (the single biggest edge at real costs)

EMA-smooth the net vote (span 5) + **dead-band 0.15** (only re-trade when the target moves ≥ 0.15
of equity). This alone decides whether 50bp slippage kills the product (Raw 8B: $14B@0bp → $13k@50bp
without it). Target ~9 trades/yr, ~40-day average hold, ~96% time-in-market.
**Corollary:** you cannot "fix" low trade count — more trades = more cost. Chop-year losses at 50bp
are pure trading cost (all models are GREEN in 2025 at 0bp) → the fix is execution quality, not rules.

## 4. Positioning, sizing, margin

- **Exposure** `e = vote × gates × min(cap, vol_target / realized_vol20)`, vt = 1.5.
  Vol-targeting sizes UP in calm markets toward the cap, DOWN in storms — this is the sizing rule.
- **Margin reality:** exchanges quantize the leverage *SETTING* (1–5×), not position size. "LONG 1.8×
  equity" = set 5× margin, open notional 1.8× equity = 36% of funds posted as margin. **Strict
  integer-EXPOSURE quantization was measured and rejected** ($644k vs $3.54M — whole-step re-leveling
  multiplies turnover cost and breaks vol-targeting).
- **Mixed long/short margin caps:** tested (5/3, 5/2, 5/1, long-only...) — **rejected**: after the
  200WMA floor removes the bad shorts, the remaining short book is the profitable bear-year kind;
  halving it turns 2018/2022 deep red. (The old "half-size shorts" wisdom applies only to UNFILTERED
  models.) 5×/3× is the one defensible conservative variant (−$2.3M for −5pp DD).

## 5. Risk controls (the stack, in order of application)

| Control | Rule | Why |
|---|---|---|
| VOL gate | vol-rank > 0.85 → ×0.5 both sides | storms kill leveraged trend |
| FUND gate | funding-rank > 0.90 → longs ×0.5; < 0.10 → shorts ×0.5 | crowded-side penalty (best hint found — already in-model) |
| Dollar gate (Steady A) | UUP > SMA200 → ×0.5 | macro liquidity headwind (cross-validated) |
| **200WMA floor** | price < 200WMA → **no shorts** | cycle floor; shorting the bottom is the classic loser |
| **Pi Cycle de-risk** | 365d after 111DMA > 2×350DMA → ×0.5 | post-parabolic bear window; n=2 events, 0 false alarms, failure mode INERT |
| dd-kill | equity < 70% of peak → ×0.5 | drawdown brake |
| **Cut-loss** | −15% **from ENTRY price, fixed** (resting stop) | catastrophe stop only |
| Liquidation | modelled honestly; at ≤1.1× exposure liq needs ~−94% (cross margin) | display both cross & isolated views |

**Cut-loss lesson:** tight stops (<~25%) destroy the edge (BTC whipsaw); trailing stops multiply
turnover → ruin at 50bp. The stop is disaster insurance, not exit logic.

## 6. Exit / strategy switch logic

Positions exit when the **ensemble flips or the regime changes** (signal-exit), never on profit
targets (let winners run — removing signal-exit drops Sharpe 1.30→0.76; adding take-profits caps the
few huge trades that pay for everything). "Strategy switch" = the regime map changing which engines
vote; the position follows the vote. Signals decided at the **daily close (UTC)**; action price =
that close; alerts within ~1h; cut-loss watched hourly intraday; the shown action is the HELD
position (entry date/price fixed) until exit/flip/resize — never re-priced by a new day.

## 7. Model registry (all configs, one engine)

All models share: conviction ensemble ≥0.4, VOL+FUND gates, dd-kill 0.30, vt where shown, honest sim.

| Model | Signal smoothing | Cap | vt | Band | Extra rules |
|---|---|---|---|---|---|
| Raw 8B | none (raw votes) | 5 (legacy 0.6 vt) | 0.6 | 0 | none — the 0bp trophy, cost-doomed |
| Core 1× | EMA-5 | 1 | 1.5 | 0.15 | — |
| Balanced 2× (cap2) | EMA-5 | 2 | 1.5 | 0.15 | — |
| Balanced 3× | EMA-5 | 3 | 1.5 | 0.15 | — |
| Growth A | EMA-5 | 5 | 1.5 | 0.15 | — |
| Aggressive B | EMA-5 | 5 | 2.0 | 0.25 | — |
| Smooth C | EMA-10 | 5 | 1.5 | 0.25 | — |
| Apex | EMA-5 + short-selective (short only STRONG/TREND_DOWN & <SMA200) | 3.25 aligned / 3.0 counter (trend-aligned) | 1.5 | 0.15 | — |
| cap2+gate | EMA-5 | 2 | 1.5 | 0.15 | dollar gate |
| **Steady A** | EMA-5 | 2 | 1.5 | 0.15 | dollar gate + floor + Pi |
| **Max B (LIVE)** | EMA-5 | 5 | 1.5 | 0.15 | floor + Pi |

### Verified headline metrics @50bp (2014→2026-06, $500 start)

| Model | Final @50bp | Final @0bp | CAGR | Sharpe | Calmar | maxDD | Win% | Trades |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| Raw 8B (no dd-kill) | $13,331 | ~$13.5B | 30% | 0.77 | 0.36 | −84% | 50% | 630 (M1M5 var.) |
| Core 1× | $41,164 | $134,724 | 43% | 1.33 | 1.45 | −29% | 46% | ~114 |
| Balanced 2× | $496,022 | $7.85M | 74% | 1.26 | 1.68 | −44% | 41% | ~116 |
| Balanced 3× | $931,379 | $50.6M | 83% | 1.25 | 1.54 | −54% | 41% | ~116 |
| Growth A | $3,536,933 | $361M | 104% | 1.35 | 1.75 | −59% | 38% | 116 |
| Aggressive B | $3,978,428 | $430M | 106% | 1.27 | 1.58 | −67% | 41% | ~110 |
| Smooth C | $1,768,003 | $14.9M | 93% | 1.30 | 1.55 | −60% | 40% | ~90 |
| Apex | $1,198,372 | $75.9M | 81–87% | 1.26–1.29 | 1.48–1.59 | −54% | 40% (L45/S34) | 102 (204 i&o) |
| **Steady A** | **$1,099,053** | $8.61M | 86% | **1.51** | — | **−42%** | ~42% | ~110 |
| **Max B** | **$11,593,525** | $772M | 124% | 1.47 | — | −56% | **43% (L46/S40)** | 109 (+1 open) |
| + stack applied to legacy: Bal3× $3.00M · AggrB **$13.45M** · SmoothC $3.10M · Apex $3.01M · Raw8B $87k | | | | | | | | |

### Max B deep metrics (the live model — the reporting standard in action)
avg win **+64.0%** · avg loss **−6.0%** · payoff (R:R realized) **10.63 : 1** · expectancy ≈ +24%/trade ·
avg hold 39.5d (max 262d) · 12 flat gaps (avg 16.7d, max 68d, clustered 2022–23 = floor working) ·
time-in-market 96% · win CI 43%±9pp · bootstrap: P(net loser)=0.08%, 90% final range **52×–31.7M×**,
median 20,086×, top-10 trades = 106% of log-return.
**Straightness** (late/early CAGR ratio; 1.0 = straight line): Max B 0.07 · Steady A 0.23 ·
Core1×+carry 0.33 (best) — no config achieves $1M+ AND ratio ≥0.3 (34-config grid; the bend is the
market's two eras, not the model).

### Yearly (@50bp, with stack) — the two production candidates

| Yr | Steady A | Max B | | Yr | Steady A | Max B |
|---|--:|--:|--|---|--:|--:|
| 2014 | −11% | +2% | | 2021 | +86% | +73% |
| 2015 | +250% | +573% | | 2022 | **+28%** | **+13%** |
| 2016 | +51% | +105% | | 2023 | +35% | +62% |
| 2017 | +1037% | +1395% | | 2024 | +16% | −0% |
| 2018 | **+6%** | **+2%** | | 2025 | −8% | −38% |
| 2019 | +51% | +434% | | 2026 | +33% | +20% |
| 2020 | +408% | +600% | | | | |

(Full grids incl. 0bp and all legacy models: `excel_reports/BTC_stack_all_models.xlsx`,
`BTC_all_models_full_yearly.xlsx`; scripts `research_stack_all.py`, `research_full_yearly.py`.)

## 8. Tested and REJECTED (do not re-run without new information)

| Idea | Result | Why it fails |
|---|---|---|
| Leverage >~4–5× / to chase 100–200%/yr | return PEAKS ~4× then collapses (10×=−22%CAGR) | vol drag L², borrow, ruin; Kelly ceiling ≈50%/yr at 60% DD; 114–180%/yr targets are above the ceiling at ANY leverage |
| Hint/feature filters (27 features, 2 rounds) | best OOS AUC 0.58; as filter −35–55% profit | profit concentrated in few huge years → one false positive in a moon year outweighs many saved bear points |
| Real-time regime separation / crisis book | 2018 −0%→+2% but hurts 2020/2022; hindsight $9.8M vs $439k | early-2018 chop is indistinguishable from early-2017/2020 chop at decision time |
| Mean-reversion sleeve in chop | negative expectancy (Sharpe −0.65) | crypto ranges break out; uncorrelated but LOSING streams dilute |
| Momentum rotation (3× ETFs) w/ scanned lookback | $450k → honest $17.5k | textbook parameter overfit (train 70% → test 30%/yr) |
| 7-model committee voting | AVG $3.1M / MAJ $4.0M / UNANIMOUS $145k vs best single $11.6M | correlated voters (same signal source); unanimity = always late |
| Funding-carry sleeve on leveraged models | hurts (−45% v1) or inert | no idle capital when levered; resize costs eat 66% of gross even on Core 1× (net +$2.9k) |
| Integer-exposure margin steps | $3.54M → $644k, DD −59→−71% | whole-step re-leveling ×costs; exchanges quantize the SETTING not the size |
| Mixed L/S caps post-floor | 5/1 & long-only turn 2018/2022 to −26/−40% | the floor already curated the shorts |
| Tight/trailing stops, TP targets | Sharpe collapse / ruin at 50bp | whipsaw + turnover; kills the convex winners |
| MVRV / F&G / on-chain / halving-calendar / cycle-math | no OOS separation or look-ahead | in-sample mirages; halving+2 broke in 2025 |
| Portfolio vol-targeting (CTA style) | 11% CAGR / Sharpe 0.76 < simple models | over-diversified base demands leverage → borrow + crisis amplification |
| Multi-timeframe 4-regime switcher (user spec v1) | +5% CAGR, −43% DD, no regime reaches win>60% & 3:1 R:R | complexity adds cost not edge; win%/payoff is a frontier, not a menu |

**Meta-lesson:** complexity underperformed simplicity in ~12 architecture attempts. Edges came from:
turnover control, vol-targeting, curated shorts (floor), cycle de-risk (Pi), diversification across
ASSETS (not model variants), and cost reduction. Not from prediction.

## 9. Roles & governance (mandatory loop for every change)

**Roles:** Mathematician · Investment-bank Financial Analyst (CFA) · Actuary · Private-bank Fund
Manager · Quant Trader · Industry Specialist · Programmer · End User · Investor (+ Data Analyst).

**The loop — every action (new rule, config change, deploy):**
1. **Refresh data** → run the full backtest (0bp + 50bp + 100bp stress).
2. Each role reviews: math correctness (look-ahead, formulas), risk (tail/ruin/DD), realism
   (costs, margin mode, capacity), code (silent failures, index shifts), clarity (would the end
   user misread it?), investor honesty (is the headline a ceiling or an expectation?).
3. Comment → amend → **re-loop until ALL roles agree**. A change that helps one base but hurts
   another fails review (robustness rule).
4. **Full-detail report BEFORE deploy** (format below), user approves the model choice.
5. Deploy in verified stages (engine ties to research numbers EXACTLY before dashboard/telegram
   are touched) → push → **verify the LIVE result** (CI log + live JSON + in-browser), not just the commit.

**Reporting standard (every model, every proposal):**
- Full-range AND **every-year** returns + intra-year maxDD, at **0bp AND 50bp** (100bp stress).
- Win ratio (all / long / short) + 95% CI · trade count (+in&out) · holding days (avg/max) ·
  flat gaps (count/avg/max) · time-in-market.
- Earnings in $ and % · CAGR · Sharpe · Calmar · maxDD · liquidation count.
- avg win / avg loss / max win / max loss / realized R:R (payoff) / expectancy per trade.
- **Straightness** (late/early CAGR ratio + log-R²) — full-period metrics hide front-loading.
- Bootstrap: P(loss), 90% final range, concentration (top-10 share of log-return).
- **Verdict line** per role + honest caveats (sample size, regime dependence, what breaks it).

## 10. Operations discipline (production)

- Pipeline: `fetch_data → engine → build_dashboard → telegram watch`, one JSON feeds all → sync by
  construction. CI on push + hourly (GitHub cron throttles; push-trigger is the reliable path).
- Signal basis printed in-product ("daily close UTC; action price = close; alerts ≤1h; stops
  watched hourly"). Stops/liquidation **anchored to entry**, displayed with both cross & isolated
  margin views. Held position shown until exit — never re-priced daily.
- Loud failure everywhere: telegram getMe SELFCHECK each run; funding-staleness warning; engines
  print their shield states (Pi alarm on/off, price vs 200WMA).
- Dashboard chart: log scale with **1-3-10 tick ladder** (visually even), linear ticks <1 decade,
  range presets (1Y/3Y/5Y/All), $/× unit toggle (multiple-of-start = ownership-proportion view).
- Secrets live in repo Actions secrets (never environment-scoped); rotate anything pasted in chat.
- Local previews are snapshots — refresh data before comparing to live.

## 11. Known open items / futures

- 2025-style slow chop stays red at 50bp for every model (green at 0bp) → execution-quality work
  (limit orders, maker fees) is the only honest lever left.
- Pi Cycle may never fire again (ETF era) — acceptable: inert failure. Review after next cycle.
- Covered-call income sleeve: same "income in chop" idea as carry via options — untestable here
  (no options data); real BTC covered-call funds exist.
- Multi-asset diversification WORKS (global 12-asset trend basket: all years green 2017–2026 at its
  own scale) — the proven route for smoother curves in other products; see `GLOBAL_RESEARCH.md`.
- Telegram repo secrets must be set by the owner (SELFCHECK logs status every run).
