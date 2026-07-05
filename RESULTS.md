# Validation Results — Multi-Product Strategy Engine

## AMENDMENT 2026-07-04c — target 150k→8M/10y; long/short + options + bonds allowed. Live product now R2 (two sleeves)

Target math: 8M/150k in 10y = 53.3× = **48.8% CAGR**. Rounds 7–8 tested every
newly-allowed lever. Universe extended with 8 bond ETFs (TLT IEF SHY LQD HYG AGG
EMB TIP, monitor + sleeve-tested).

### Round 7 — new engines measured (all honest: next-open fills, costs, no look-ahead)

| Approach | Best result | Win | CAGR | maxDD | Verdict |
|---|---|--:|--:|--:|---|
| Equity trend (breakout 55d, 4×ATR trail, let-winners-run) | T1, 7 slots | 44.5% | 22.2% | −31.1% | fails win gate; kept as sleeve candidate |
| **Crypto trend** (BTC/ETH >SMA200, trail/SMA50 exit) | C1, 2 slots | 45.2% | **43.7%** | **−61.2%** | the return engine; DD/win far outside gates; final 8.66M over 11.2y **but** last-5-years CAGR only ~8% (2017/2020 supercycles drove it — straightness caveat) |
| Leveraged-ETF trend | L4, 3 slots | 47.0% | 32.0% | −57.1% | fails gates |
| **Shorts** (mirror dip: rip-fade in downtrends, borrow costs) | best = bear-gated half-size | 52.3% short-side | drags X4 16.7%→10.3% | −31.5% | **REJECTED — shorts lose money in every variant** (−122k…−184k short-side P&L; matches playbook asymmetry lesson) |
| **Margin on X4** (interest 6%/yr) | m=1.25 | 68.4% | 18.3% | −28.8% | max margin inside DD gate; m≥1.5 breaches (−34%…−52%, margin-call risk at 2.5×) |
| **Options** | — | — | — | — | **REJECTED**: buying calls/puts = structurally negative expectancy (theta + volatility risk premium + breakeven mechanics — [SimTrade](https://www.simtrade.fr/blog_simtrade/why-retail-option-strategies-underperform-payoffs-probabilities-cost-speculation/), [arXiv 2303.16371](https://arxiv.org/pdf/2303.16371): deep-OTM call buying loses even after spreads). Selling (put-write/buy-write, CBOE PUT/BXM benchmarks) historically ≈8–10%/yr with equity-like drawdowns — cannot approach 49% inside a 30% DD budget. Honest 10-year options backtests are impossible without paid options-chain/IV history; free data covers current chains only. |

### Round 8 — monthly-rebalanced multi-sleeve portfolios (the working structure)

Unrebalanced hybrids fail (the crypto sleeve balloons after +383% 2017 → DD −49%).
Monthly rebalancing fixes it. Gates: win ≥ 60%, DD ≤ 30%:

| Combo | Win | CAGR | maxDD | 150k→ (11.2y) | Gates |
|---|--:|--:|--:|--:|---|
| 80% dip / 20% crypto | 62.8% | 20.2% | −24.8% | 1.18M | ✅ |
| **R2: 70% dip / 30% crypto (LIVE)** | **62.8%** | **23.7%** | **−26.2%** | **1.62M** | ✅ |
| 65/25/10 bond | 61.9% | 21.1% | −23.4% | 1.28M | ✅ (defensive alt.) |
| 60% dip / 40% crypto | 62.8% | 27.0% | −32.5% | 2.18M | ✗ DD |
| 40% dip / 40% crypto / 20% levETF | 61.3% | 32.5% | −35.8% | 3.51M | ✗ DD |
| 100% crypto trend | 45.2% | 43.7% | −61.2% | 8.66M | ✗ win, DD |

Bond sleeve alone: 3.8% CAGR, −11% DD — weak as an engine, mild as a diversifier
(−3.9pp DD for −2.2pp CAGR when added at 10%); bonds stay monitored + available.

### The measured frontier vs the 8M target (10-year projections)

| Product | 10y projection | Win | maxDD | Gates |
|---|--:|--:|--:|---|
| R2 (LIVE) | ~1.3M | 62.8% | −26% | all pass |
| 60/40 | ~1.6M | 62.8% | −33% | DD slightly over |
| 40/40/20 | ~2.5M | 61.3% | −36% | DD over |
| 100% crypto trend | ~5.6M | 45.2% | −61% | win+DD far over |
| **Target** | **8.0M** | ≥60% | ≤30% | **outside every measured config** |

**Verdict:** 8M in 10 years requires 48.8%/yr. The single highest honest engine
(crypto trend) delivers 43.7% *only* by accepting −61% drawdown and 45% win —
and even it projects ~5.6M forward, with its edge concentrated in two historical
supercycles. Within your win/DD gates the maximum is ~24%/yr (R2). No
combination of long/short/options/bonds/leverage closes the gap: shorts and
option-buying subtract, option-selling and bonds cap out near 10%/yr, margin
breaches the DD gate at 1.5×. The three constraints (return ≥49%/yr, win ≥60%,
DD ≤30%) are mutually exclusive on this planet's price history — pick two.

---

## AMENDMENT 2026-07-04b — win gate relaxed to ≥60%, live config now X4

User amendment: win ≥ 60%, maximize annual return, every year green.
Round 5+6 ladder (14 configs: target 1.25–2.5 ATR × slots 3–15 × breadth gate
40/50% × momentum floor). Gates: win ≥ 60%, PF > 1.15, DD ≤ 30%.

**Adopted: X4 — 7 slots, target 2.0×ATR, sizing = cash/(7 − held):**
win **68.4%** [halves 71.5/65.3], PF **1.36**, **CAGR 16.7%**
(**HKD 150,000 → 852,035**, +462%), maxDD −23.2%, Sharpe 1.00, 1,707 trades,
avg hold 6.1 days, **9 of 10 full years green** — sole red 2018 at **−2%**
(yearly: 2016 +29, 2017 +33, 2018 −2, 2019 +31, 2020 +21, 2021 +10, 2022 +4,
2023 +24, 2024 +32, 2025 +16, 2026 YTD −4). Per-market wins: crypto 77.6%,
JP 71.3%, US 67.6%, HK 63.0% — all above the 60% gate.

**Every-year-green: NOT honestly achievable — reported as such.**
- No config among 14 was all-green 2016–2025. The market-breadth risk gate
  (no entries when <50% of universe above SMA200) turned 2018 green (+1) but
  turned 2022 from +4 to −14 — a rule that helps one base and hurts another
  fails the robustness rule (METHODOLOGY §0.6) → rejected.
- The BTC project already measured this phenomenon: losing years are not
  separable from winning years in real time (27 features, best OOS AUC 0.58).
  Hunting parameter combos until 2018 flips green would be manufactured
  overfit (playbook §1.4), producing a backtest that lies.
- X4's worst year (−2%) is the honest residual: statistically a coin-flip away
  from zero, structurally irreducible without a crystal ball.

CAGR ladder for reference (all pass win/PF/DD gates): 15 slots 8.9% → 10 slots
13.0% (h1/h2 69.9/66.7) → **7 slots 16.7%** → 5 slots 16.5% (worse 2018 −8) →
7 slots tp2.5 15.5%. 7 slots is the sweet spot; further concentration adds
risk without return.

---

## AMENDMENT 2026-07-04 — return maximization, live config now W4

User amendment: keep long-only / no-margin / DD ≤ 30%; win allowed "very close
to 70%"; raise annual return toward 150%. Round-4 pre-registered ladder
(gates: win ≥ 68%, PF > 1.15, DD ≤ 30%, halves ≥ 64%; metric: max CAGR):

| Config | Win | h1/h2 | PF | CAGR | 150k → | maxDD | Gates |
|---|--:|--:|--:|--:|--:|--:|---|
| W1 tp1.5, 15 slots | 69.2% | 69.4/69.0 | 1.23 | 7.6% | 339,901 | −24.7% | ✅ |
| W2 tp2.0, 15 slots | 67.5% | 67.7/67.3 | 1.27 | 8.9% | 390,718 | −21.4% | ✗ win |
| W3 tp1.25, 7 slots | 70.5% | 72.6/68.4 | 1.25 | 10.5% | 460,404 | −28.8% | ✅ |
| **W4 tp1.25, 5 slots (LIVE)** | **69.8%** | 72.0/67.7 | **1.24** | **11.4%** | **503,950** | **−25.5%** | ✅ |
| W5 tp1.25, 3 slots | 70.0% | 71.7/68.4 | 1.34 | 14.3% | 670,345 | −30.3% | ✗ DD |
| W6–W10 leveraged-ETF sleeve (5 configs) | 68.0–70.6% | — | 1.19–1.24 | 6.5–11.1% | — | −27.4 to −35.1% | ✗ mostly DD |

**Adopted: W4** — 5 slots (mandate max 15 still respected), sizing = cash/(5 − held),
target 1.25×ATR. Doubles the compounding rate vs the 15-slot config
(11.4% vs 6.9% CAGR) with *lower* drawdown, because fewer concurrent dip
positions means less clustered crash exposure. Costs of concentration: split-half
spread widens (72.0/67.7 vs 71.5/70.9) and single-position weight is 20% of equity.

**Leveraged-ETF sleeve: tested and REJECTED.** 2×/3× index ETFs (TQQQ, SOXL, UPRO…)
are legitimately long-only cash instruments, but their dips cluster with market
crashes: every mixed config pushed maxDD to −30.8…−35.1% without beating W4's
CAGR. They remain in the universe as monitor-only. (W5 3-slot: CAGR 14.3% but
DD −30.3% breaches the gate — documented, not shipped.)

**On the 150%/yr target:** the measured frontier under your own gates tops out at
**~11–14% CAGR**. The remaining gap to 150% is structural, not parametric — see
playbook Addendum A2. Every lever available without margin/shorts/derivatives has
now been measured (targets, concentration, utilization, leveraged ETFs); pushing
any of them further breaks the DD or win gate. The honest maximum inside the
mandate is delivered; 150% is outside it.

---

## AMENDMENT 2026-07-03 (evening) — dynamic sizing, live config now V9c

User amendments applied: daily automated monitoring run (Windows Task Scheduler,
08:00 HKT: refresh all prices/volumes/indicators + market-cap universe review +
report), and position sizing changed from fixed HKD 10k to
**next position = available cash / (15 − positions held)** — equity now compounds.

Round-3 validation (pre-registered metric: **max CAGR** s.t. win > 70%, PF > 1.15,
maxDD ≤ 30%, both halves ≥ 65%):

| Variant | Win | h1/h2 | PF | CAGR | 150k → | maxDD | Gates |
|---|--:|--:|--:|--:|--:|--:|---|
| **V9c tp=1.25ATR (LIVE)** | **71.3%** | 71.5/71.0 | **1.23** | **7.2%** | **325,253** | −22.8% | ✅ |
| V8c tp=1.0ATR | 73.3% | 73.2/73.3 | 1.18 | 5.3% | 267,737 | −25.9% | ✅ (win-buffer alt.) |
| V11 rsi<20 tp=1.0 | 72.3% | 73.0/71.7 | 1.14 | 5.8% | 281,451 | −27.8% | ✗ PF |
| V12 rsi<20 tp=1.25 | 69.6% | 70.4/68.8 | 1.15 | 6.3% | 297,425 | −28.1% | ✗ win |

Confirmed final run of the LIVE V9c config (full 138-ticker universe incl. 0175.HK
which replaced delisted 0011.HK): **win 71.2% [71.5/70.9], PF 1.21, expectancy
+HKD 53/trade, HKD 150,000 → 322,562 (+112.1%, CAGR 6.9%), maxDD −24.1%, Sharpe
0.59, 3,279 trades, avg hold 4.8 days.** Per-market wins: crypto 74.1%, JP 73.1%,
US 70.4%, HK 68.5% (HK slips below 70 individually under dynamic sizing — the
70% gate is portfolio-level and passes; noted honestly). 9 of 11 full years
positive; worst full year 2018 −HKD 12.5k; 2026 YTD −16.6k with 63.2% win
(n=185, within small-sample CI — watch).

**On the 200%/yr target — feasibility (playbook §15, mandatory honesty):**
200%/yr for 10 years = 59,049× capital. The best honest configuration of this
system delivers **7.2%/yr** under your own constraints. The constraints bind each
other: long-only + no margin + no compounding within trades + win ≥ 70% (forces
small profit targets) + DD ≤ 30% (forces diversification) mathematically cap CAGR
near 10%. For reference, the BTC Max B model — 5× leverage, shorts allowed, 43%
win rate, −56% DD — achieved 124%/yr and is still below 200%. Reaching even 30%+
CAGR long-only would require concentrated positions and a win rate near 50%,
violating two of your gates. **The 200% target and the 70%-win/30%-DD/no-margin
gates cannot coexist; the system honours the gates.** Per METHODOLOGY §8: targets
above the Kelly ceiling are unreachable at ANY setting — the honest response is
to reset the target, not to lever into ruin.

---

# Original validation (fixed HKD 10k stakes) — config V8

**Config:** "V8" (see `engine/config.py`) · **Validated:** 2026-07-03 ·
**Data:** 137 tickers, daily, 2015-04 → 2026-07 (11.2 years), yfinance adjusted closes.
All numbers **net of per-side costs** (US 10bp, HK 25bp, JP 15bp, crypto 20bp), zero
look-ahead (signal at close *t*, fill at open *t+1*; stops/targets fill intraday, and if
both are touched the same day the **stop is assumed to fill first** — conservative).

## Mandate compliance

| Gate (user spec) | Requirement | Result | Pass |
|---|---|---|---|
| Win ratio | > 70% | **73.3%** (95% CI ±1.5pp) | ✅ |
| Max drawdown | ≤ 30% | **−22.3%** | ✅ |
| Backtest length | 10 years | 11.2 years | ✅ |
| Long only, no margin | — | enforced by construction | ✅ |
| Max 15 positions × HKD 10k | — | enforced by construction | ✅ |

## Headline metrics

| Metric | Value |
|---|--:|
| Trades | 3,445 (≈ 307/yr) |
| Win rate | 73.3% — 1st half 73.2% / 2nd half 73.3% |
| Avg win / avg loss | +2.35% / −5.40% (realized R:R 0.44 — see design note) |
| Profit factor | 1.19 |
| Expectancy | +HKD 28 per trade (+0.28% per position) |
| Total P&L | **+HKD 96,283** (+62.9% on HKD 150,000) |
| CAGR | 4.5% (fixed stakes, no compounding — by mandate) |
| Sharpe | 0.50 |
| Max drawdown | −22.3% |
| Avg hold | 4.1 trading days |
| Liquidations | 0 (impossible: no margin) |

## Robustness across independent bases (the key evidence)

| Market | Trades | Win rate | P&L (HKD) |
|---|--:|--:|--:|
| US | 1,445 | 72.8% | +41,917 |
| JP | 1,316 | 74.7% | +31,389 |
| HK | 602 | 71.1% | +13,815 |
| Crypto | 82 | 74.4% | +9,162 |

Every market independently exceeds the 70% gate, and the split-half win rates are
almost identical (73.2 / 73.3). This is the pattern METHODOLOGY §0.6 demands — a rule
that works on multiple independent bases, not one mined optimum.

## Year by year

| Year | Trades | Win % | P&L (HKD) | | Year | Trades | Win % | P&L (HKD) |
|---|--:|--:|--:|---|---|--:|--:|--:|
| 2016 | 222 | 75.2 | +14,079 | | 2022 | 265 | 71.3 | −671 |
| 2017 | 378 | 77.5 | +19,553 | | 2023 | 373 | 75.9 | +29,635 |
| 2018 | 323 | 67.2 | −15,922 | | 2024 | 385 | 76.1 | +28,512 |
| 2019 | 268 | 73.5 | +3,128 | | 2025 | 363 | 72.7 | +8,672 |
| 2020 | 299 | 74.6 | +7,666 | | 2026 YTD | 195 | 67.2 | −5,359 |
| 2021 | 373 | 71.3 | +6,113 | | | | | |

8 of 11 full years positive. Worst year 2018: −15,922 (−10.6% of capital).

## Exit breakdown (V8)

| Exit type | Count | Avg net return |
|---|--:|--:|
| Target filled (limit) | 1,432 | +2.25% |
| Target gapped over | 602 | +3.45% |
| RSI backup signal | 912 | −0.03% |
| Stop filled | 372 | −8.16% |
| Stop gapped through | 121 | −10.19% |
| Time stop | 6 | −6.32% |

## Selection procedure (overfit control)

Pre-registered primary metric before any test: *maximize win rate subject to
PF > 1.15, maxDD ≤ 30%, both halves ≥ 65%.* Eleven variants were run in two rounds
(6 signal-exit, 5 target-geometry), each a single mechanism-motivated change — no
grid search. V8 won; V9 (target 1.25×ATR: win 71.1%, PF 1.24, +HKD 123,936,
DD −18.7%) is the documented growth alternative if you ever prefer P&L over
win-rate buffer.

## Honest caveats (read before trusting)

1. **Survivorship bias — the biggest one.** The universe is *today's* top-cap lists
   backtested 10 years back; 2015-you would not have held NVDA or PLTR in a top-50
   list. The regime gate (only buys existing uptrends) mitigates but does not remove
   this. The forward-looking daily universe rotation is the real product; expect the
   live win rate to be somewhat lower than 73.3%. The 3.3pp buffer over the gate and
   the 71–75% consistency across four markets is the defense.
2. **Inverted R:R by design.** Avg win +2.35% vs avg loss −5.40% is the price of a
   >70% win rate (the win%/payoff frontier, METHODOLOGY §8). The system is profitable
   because wins are 2.75× more frequent, not bigger. A losing streak of 5+ stops
   (p ≈ 0.14% per sequence) costs ~HKD 2,700 — sized to be survivable.
3. **FX not modelled.** Returns are applied in % to the HKD stake. HKD is USD-pegged;
   JPY exposure adds ~±10% FX noise on the JP slice.
4. **Fixed stakes mean modest CAGR.** +HKD 96k over 11 years on 150k is an income-style
   overlay, not a compounding machine — that is what the 10k-fixed mandate buys.
5. **2026 YTD is 67.2%** (n=195, within the ±6.6pp small-sample CI) — watch, don't panic.
6. **Commodities & indices are monitor-only**: continuous futures (GC=F etc.) carry
   roll artifacts that falsify mean-reversion backtests; trading them would need
   proper roll-adjusted data or ETF proxies (open item).

## Role review (playbook §11)

- **Data Analyst** — no look-ahead; costs on every fill; split-half and per-market
  stability verified; survivorship bias disclosed. *Approve with caveat #1.*
- **Fund Manager** — DD −22.3% inside mandate; worst year −10.6%; expectations set
  honestly (income overlay, not moonshot). *Approve.*
- **Actuary** — no leverage → ruin impossible; worst single-trade loss ≈ −10% of one
  10k stake (HKD ~1,000, 0.7% of capital); gap risk capped by position size. *Approve.*
- **Quant Trader** — turnover ~300 trades/yr is executable at retail size; the
  stop-first same-day assumption is conservative; the cost model is the reason
  quick-bank variants failed, which matches mechanism. *Approve.*
- **Programmer** — single engine feeds backtest and daily report from the same
  feature code (sync by construction); delisted tickers degrade gracefully. *Approve.*

## Open items

- Live news/policy/big-buyer evidence layer for reports (currently technical evidence
  only; needs a news API or run-time web search).
- ETF proxies (GLD/SLV/USO) to make commodities honestly tradeable.
- Point-in-time universe reconstruction to quantify caveat #1 exactly.
- GitHub/automation decision (currently local-only by design).

*Research framework. Not financial advice.*
