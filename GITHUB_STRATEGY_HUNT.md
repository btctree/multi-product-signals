# GitHub Strategy Hunt — 2026-07-13

**Task:** search GitHub/public sources for investment strategies and backtest them honestly, hunting for:
**300%/yr · maxDD ≤ 20% · Sharpe > 1 · Calmar > 1 · positive every calendar year 2016–2025.**

**Verdict up front: no such strategy exists in public code, and the target is mathematically beyond
anything ever documented.** 300%/yr at −20% DD = Calmar 15 sustained for a decade; the best verified
track record in industry history (Renaissance Medallion) ran ~39–66%/yr. The playbook's own
feasibility rule (§15) applies: *if the target needs >~100% CAGR sustained, revise the target.*

Everything below follows METHODOLOGY §0 honesty rules: no look-ahead (decide on data through *t*,
earn *t+1*; verified per-script), costs on turnover (10 bp ETF / 15 bp stock / 50 bp crypto per
side-unit), published parameters only — **zero tuning**, %-compounded equity, window
2016-01-01 → 2026-07-13. Scripts: `engine/gh_hunt/*.py` (rerunnable; price cache refetches via yfinance).

---

## Process

- 8 parallel searchers swept GitHub + quant sources across families: leveraged-ETF timing, dual
  momentum/TAA, trend/breakout, crypto, vol-premium, mean reversion, seasonality, and explicit
  high-claim repos → **60 raw candidates**.
- Triage → **14 implementable strategies** (precise daily-bar rules, Yahoo data back to ≥2015).
- Each backtested honestly; the 5 strong results were **adversarially verified** by independent
  agents instructed to refute them (rerun + look-ahead/cost/annualization/data audit).

## Results (net of costs, 2016-01 → 2026-07)

| # | Strategy (source) | CAGR | Sharpe | Calmar | maxDD | Win% | 10y+? | Verify |
|---|---|--:|--:|--:|--:|--:|:-:|---|
| 1 | **TQQQ FTLT decision tree** (Composer) | **168.8%** | 1.83 | 3.32 | −50.9% | 66% | ✅ | CONFIRMED but artifact — see below |
| 2 | Turtle Donchian 20/10 on BTC (2N stop) | 51.9% | 1.22¹ | 0.79 | −65.5% | 40% | ❌ 3 neg yrs | ADJUSTED (Sharpe √365) |
| 3 | Leverage for the Long Run, QQQ 1%-band → TQQQ | 39.7% | 0.93 | ~0.68 | −58.8% | 50% | ❌ | — |
| 4 | FinLab 3-Day Laggard (TQQQ/TECL MR) | 31.2% | 0.90 | 0.60 | −51.6% | 50% | ❌ 4 neg yrs | CONFIRMED (numbers honest, fails gates) |
| 5 | Zen BTC Momentum (weekly EMA20 + ATR stop) | 25.6% | 0.81¹ | 0.52 | −49.0% | 33% | ❌ | ADJUSTED (Sharpe √365) |
| 6 | Leverage for the Long Run, SPY→UPRO SMA200 | 23.1% | 0.77 | 0.45 | −51.1% | 41% | ❌ | — |
| 7 | Weekly MACD zero-cross QQQ → TQQQ | 21.3% | 0.66 | 0.40 | −53.4% | 50% | ❌ | — |
| 8 | Keller BAA-G4 (aggressive TAA) | 12.4% | 0.74 | 0.33 | −37.5% | 66% | ❌ 2025 −11% | — |
| 9 | **Keller HAA-Balanced** (G8/T4, TIP canary) | **11.2%** | **1.09** | 0.75 | **−15.0%** | 68% | **✅** | **CONFIRMED** |
| 10 | IBS threshold MR (SPY/QQQ <0.2/>0.8) | 10.4% | 0.84 | 0.52 | −19.9% | 64% | ❌ 2018/24 | — |
| 11 | Concretum eVRP 4-regime short-vol | 9.8% | 0.63 | 0.33 | −29.7% | 40% | ❌ | — |
| 12 | VIX/VIX3M 0.917 short-vol (SVXY) | 9.6% | 0.47 | 0.20 | −47.5% | 35% | ❌ 5 neg yrs | — |
| 13 | In & Out macro switch (Quantopian) | 9.1% | 0.71 | 0.31 | −29.2% | 57% | ❌ | — |
| 14 | Quantitativo lower-band + IBS (QQQ/PSQ) | 3.3% | 0.34 | 0.18 | −18.0% | 63% | ❌ | — |
| 15 | Turn-of-Month SPY | 0.4% | 0.09 | 0.03 | −13.8% | 54% | ❌ | — |

¹ Sharpe corrected by verifier to √365 annualization (crypto trades every calendar day).

**Nothing passes all five gates. Nothing is within 100 CAGR points of 300%/yr at any drawdown.**

## The two strategies worth understanding

### #1 TQQQ FTLT (168.8%/yr, all 10 years positive) — an execution artifact, not an edge
The famous Composer "TQQQ For The Long Term" symphony: SPY>SMA200 gate, RSI-10 overbought/oversold
leaves rotating TQQQ/TECL/SQQQ/UVXY/TLT. The adversarial verifier reproduced every number and found
the code clean — and then broke the result three ways:
- **Same-close execution assumption**: compute RSI on the closing print AND fill at that same close
  (Composer trades ~3:50pm). Delay execution to the next day and CAGR collapses **168.8% → 42.3%**,
  Sharpe 1.83 → 0.88, maxDD −78%, **2018 and 2022 turn negative** — the all-positive-years claim dies.
- **Concentration**: 2020 alone is +2,868% (March 2020 +330%); TECL/SQQQ/UVXY contribute 64.7% of
  total log-return on 12.8% of days. This is COVID-tape regime luck, well documented for this symphony.
- **Cost realism**: 10 bp on UVXY/SQQQ switches during crash liquidity is optimistic.
Per METHODOLOGY §0.2 this is a "story that lives at 0bp" generalized to timing: the edge lives
inside a ~10-minute execution window on 3× and volatility ETFs. Not investable as printed.

### #9 Keller HAA-Balanced — the only honest all-gates-but-return survivor
Monthly rebalance, 8-asset offensive universe, top-4 by 13612U momentum, TIP canary → defensive.
Verified: no look-ahead; **T+1 execution barely changes it** (CAGR 11.22% → 11.21%) — a real,
robust edge, not a timing artifact. Passes maxDD ≤ 20% (−15.0%), Sharpe > 1 (1.09), positive all
10 years (caveat: 2024 was +0.38% — knife-edge). Fails Calmar (0.75) and, of course, the return
target by 27×. This is what "positive every year at ≤20% DD" actually pays: **~11%/yr**.

## What the frontier says (per-gate best, all mutually exclusive)

- Best CAGR at ANY drawdown (honest, robust to T+1): Turtle BTC ~52%/yr — with −65% DD and 3 losing years.
- Best CAGR with maxDD ≤ 20%: HAA-Balanced 11.2%/yr.
- Best CAGR with all 10 years positive AND robust execution: HAA-Balanced 11.2%/yr.
- The gates the target combines have never been jointly satisfied by anything found in 60 candidates
  across 8 strategy families — the 3 best returners all carry 50–66% drawdowns and losing years, and
  the smooth strategies all return 9–12%. This is the same frontier RESULTS.md keeps measuring:
  smoothness and speed trade off; they don't stack.

## Feasibility math (playbook §15)

| Target | Implied 10y multiple | Implied Calmar | Closest documented reality |
|---|--:|--:|---|
| 300%/yr, −20% DD | ×1,048,576 | 15.0 | none, ever, anywhere |
| 100%/yr, −20% DD | ×1,024 | 5.0 | none sustained 10y |
| Best found (verified, robust) | HAA ×2.9 | 0.75 | passes 3 of 5 gates |

If the mandate becomes "maximize CAGR subject to DD ≤ 20% + Sharpe > 1 + all years positive," the
honest answer from this sweep is a HAA-class TAA at ~11%/yr — and the existing Multi-Product engine
ladder (RESULTS.md) should be benchmarked against exactly that.

## Reproduce

```
python engine/gh_hunt/<script>.py   # each prints its full results block
```
Scripts fetch/cache Yahoo data themselves (auto-adjusted daily bars from 2014-06). Verifier fixes
already applied where noted (√365 crypto Sharpe). Workflow journal (agent-level evidence):
session `wf_fd5ba19a-41e`.

---

# Part 2 — Intraday hunt + best-of-GitHub sweep (2026-07-13)

**Questions:** (a) were Part 1's strategies already "the best on GitHub"? (b) is there a better
INTRADAY strategy? Second sweep: 8 searchers (ORB, intraday momentum, session/overnight anomalies,
crypto intraday, intraday mean reversion, vol/session, best-of-GitHub by stars, high-claim intraday
repos) → 59 raw candidates → 14 backtested. **Zero met the verification threshold** (CAGR ≥ 20% /
Sharpe ≥ 1.2 / all-years-positive) — the weakest sweep of the two.

**Data classes (honest constraint):** crypto intraday = Binance official klines 5m/1h,
2017-08 → 2026-06 (only 8 full years exist — Binance launched Aug 2017); US equity intraday = Yahoo
60m bars, **last ~2.9 years only** (free-data ceiling; flagged, can never satisfy a 10-year gate);
session strategies = daily OHLC, full 2016–2026. Costs per side on turnover: crypto 15 bp headline,
equity intraday 5 bp + commission, daily-session 10 bp; full ladders reported by every script.

## Results (net at headline costs)

| # | Strategy | Class | CAGR | Sharpe | maxDD | Win% | Gross CAGR | Verdict |
|---|---|---|--:|--:|--:|--:|--:|---|
| 1 | Larry Williams range breakout BTC/ETH (k=0.6) | crypto 5m | 14.6% | 0.67 | −54.6% | 43% | 63.1% | edge NEGATIVE every k since 2022 |
| 2 | Dual Thrust UTC-day breakout BTC+ETH | crypto 5m | 13.6% | 0.75 | −22.3% | 49% | 34.2% | decayed: +2.5%/yr in 2024–26 regime |
| 3 | BTC overnight seasonality 22:00→00:00 UTC | crypto 1h | −64.1% | −4.89 | −100% | 30% | +7.5% | gross edge real (+2.6 bp/trade), costs ×12 larger |
| 4 | BTC last-half-hour TSMOM (Shen et al. 2022) | crypto 5m | −45.8% | −8.16 | −100% | 14% | −5.4% | fails even GROSS; paper effect decayed post-2020 |
| 5 | Freqtrade Strategy005 (official repo) | crypto 5m | −38.4% | −3.17 | −98.7% | 44% | −5.5% | negative gross edge + churn |
| 6 | Beat the Market intraday momentum (SSRN 4824172) | equity 60m, 2.9y | −2.3% | −0.32 | −9.5% | 40% | +5.8% | gross edge exists; 80 RT/yr of costs eat it; 60m is a degraded proxy for the paper's 1-min data |
| 7 | First-hour→last-half-hour momentum (JFE 2018) | equity 60m, 2.9y | −14.0% | −6.13 | −35.5% | 26% | −0.8% | effect INVERTED post-publication |
| 8 | Turnaround Tuesday (SPY MOC + QQQ MOO merged) | session 10y | 6.0% | 0.67 | −16.8% | **70.4%** | 10.4% | fails 2018/2019 |
| 9 | Connors Double Seven (SPY/QQQ) | session 10y | 4.6% | 0.47 | −16.6% | **76.2%** | 7.1% | fails 2018/2022 |
| 10 | IBS family — best variant QQQ IBS<0.2→>0.8 | session 10y | 13.7% | 0.93 | −16.6% | 66% | 20.3% | fails 2018/2024; headline SPY base variant is −1.5% |
| 11 | Overnight Mon/Tue/Thu nights (QQQ) | session 10y | −17.3% | −1.93 | −86.5% | 41% | +10.9%, Sh 1.12 | textbook case: anomaly replicates gross, ~30%/yr cost drag |
| 12 | Concretum Vol Edge VRP rotation | session 8.4y | 6.4% | 0.48 | −30.1% | 41% | 7.1% | 2019 negative |
| 13 | In & Out mechanical (QQQ vs IEF/TLT) | session 8.9y | 10.7% | 0.61 | −44.2% | 57% | 11.3% | −10 pp/yr vs QQQ buy-and-hold; 2022 −41.8% |
| 14 | Antonacci GEM (SPY/VEU/AGG/BIL) | session 10y | 9.4% | 0.63 | −33.7% | 65% | 9.8% | fails 2018/2022 |

## Findings

1. **Intraday is WORSE than daily under honest costs, not better.** Every famous intraday anomaly
   tested either (a) replicates gross but dies at realistic per-side costs because intraday = high
   turnover (#3, #6, #11 — the overnight anomaly has gross Sharpe 1.12 and loses −17%/yr net), or
   (b) has already decayed/inverted since publication (#1, #2, #4, #7). This is METHODOLOGY §3 and
   §11 confirmed externally: turnover control IS the edge at real costs; more trades = more cost.
2. **Publication kills edges.** The three academic intraday effects (BTC seasonality 2022, BTC
   TSMOM 2022, JFE first-hour momentum 2018) all measurably decayed or inverted after their
   publication windows. The GitHub repos' claimed numbers were gross, pre-cost, or pre-decay.
3. **The famous Zarattini 5-min ORB on TQQQ (claimed +1,600% 2016–2023) is NOT honestly testable
   with free data** — it needs 10 years of 1-minute/5-minute US equity bars (paid: Polygon,
   Databento, IQFeed). Its published Achilles heel is the same as #6: commissions/slippage per
   trade at ~250 trades/yr. Untested ≠ validated: treat its claims as unverified.
4. **Best-of-GitHub sweep answer:** the highest-credible-claim public strategies at ANY frequency
   are the ones already tested in Part 1 (leveraged-ETF trend/rotation, TAA, BTC trend). The
   intraday universe added nothing that beats them. Part 1's conclusion stands: **best verified
   robust result remains Keller HAA-Balanced (11.2%/yr, Sharpe 1.09, −15% DD, all 10 years
   positive); best raw CAGR remains Turtle BTC ~52%/yr at −65% DD.**
5. **Only two strategies cleared the 70% win-rate mandate:** Turnaround Tuesday (70.4%) and
   Double Seven (76.2%) — both session mean-reversion, both single-digit CAGR, both with two
   losing years. Win rate and compounding speed remain a frontier, not a menu (Playbook A2).

## Reproduce (Part 2)

```
python engine/gh_hunt/intraday/<script>.py
```
Daily-session scripts read `engine/gh_hunt/prices/`. Intraday scripts read
`engine/gh_hunt/intraday/intraday_cache/` — 1h crypto + 60m ETF CSVs are committed; the large 5m
Binance files (~140 MB) are NOT copied — rebuild them first with
`python engine/gh_hunt/intraday/build_intraday_cache.py` (downloads official Binance monthly zips).
Workflow journal: session `wf_5bf7a2a8-3da`.

---

# Part 3 — Transfer map: what (if anything) improves our three products (2026-07-13)

Assessment only — nothing below is adopted until it passes the full METHODOLOGY §9 loop
(refresh → 0/50bp backtest → role review → robustness across bases).

## 1) Multi-Product engine (15×10k HKD, win≥70%, DD≤30%)

**Candidate A — equity mean-reversion sleeve (the win-rate lever).** The only strategies in both
sweeps clearing the 70% win gate were session MR on index ETFs: Double Seven (76.2% win, 4.6%/yr,
−16.6% DD), Turnaround Tuesday (70.4%, 6.0%/yr, −16.8%), IBS-B QQQ (66.4%, 13.7%/yr, −16.6%,
Sharpe 0.93). R2 LIVE sits at 62.8% win. A 10–20% MR sleeve is the one measured instrument that
pulls blended win% toward the 70 gate while adding a stream uncorrelated with dip/trend sleeves.
NOTE: METHODOLOGY §8 rejected MR *in crypto chop* (negative expectancy); equity index MR measured
POSITIVE expectancy over the full 10y here — different asset class, not a re-run of the rejected
idea. Each candidate has 2 losing years standalone; only the blend matters. **Testable now with
the existing engine (monthly-rebalance blend like R2), scripts already in the repo.**

**Candidate B — TIP canary (13612U) as a defensive gate.** The mechanism behind HAA's all-years-
positive result: TIP's 1/3/6/12-month blended momentum < 0 → risk-off. Same family as the already
cross-validated dollar gate (UUP>SMA200). Pre-registered, published, one parameter, mechanism-
based (real-rate/liquidity shock detector) — exactly the gate type §1 says works. Test as an
additional gate on the equity sleeves; robustness rule applies (must help multiple bases).

**Confirmed by external evidence, no change needed:** daily cadence (every intraday edge died at
real costs — do not add intraday trading to this product); leveraged-ETF sleeve verdict (LFTLR
QQQ 1%-band 39.7%/yr @ −59% DD ≈ our L4, still fails gates); shorts/options rejections stand.

## 2) BTC Power

**Candidate C — TIP canary alongside the dollar gate** (same as B, applied Steady-A-style: canary
negative → ×0.5). Only surviving new macro hint from 74 candidates; must improve multiple model
bases or be rejected per the robustness rule (the adaptive-band precedent).

**Candidate D — Sharpe annualization audit (reporting only).** Both hunt verifiers independently
caught √252-on-crypto bugs (BTC trades 365 d/yr; correct factor √365; Max B's 1.47 is understated
if computed at √252). Conservative bias, but the reporting standard should state the convention.

**Validated, no change:** daily-close cadence — BTC intraday seasonality/TSMOM are dead net of
costs (gross edges exist but are 12× smaller than realistic fees); the 2022-decay of every crypto
breakout k confirms the turnover-control philosophy. Zen weekly-EMA regime underperforms the
existing regime_v2 stack.

## 3) M1 vs M5 (BTC model-variant comparison)

**Candidate E — add a T+1 execution-delay stress column to the standard model report.** The
sharpest lesson of the whole hunt: TQQQ FTLT printed 168.8%/yr with every year positive and CLEAN
code — and collapsed to 42%/yr with losing years when execution moved one day. HAA moved 0.01pp
under the same test. Any variant comparison (M1 vs M5 included) where the ranking FLIPS under
T+1 delay is measuring an execution artifact, not an edge. Cheap to add (one shift(1)), catches a
failure mode the current 0/50/100bp cost ladder cannot see.

**Candidate F (lower priority) — Donchian 20/10 + 2N stop as a 10th voting engine.** The only
crypto entry logic that beat our C1 crypto-trend sleeve's profile honestly (Sharpe 1.22 @ 50bp,
52%/yr vs 43.7%) — but same DD (−65%) and 2017-concentration. Only worth testing as a
regime-gated vote member (pre-registered mechanism: range breakout ≠ MA trend), never as a
standalone; §8 meta-lesson (complexity) is the null hypothesis.

**Not transferable (do not re-run):** short-vol strategies (VIX-sized VXX position concentrated
risk exactly when vol was high — mechanism opposite to our vol-targeting, which is correct);
overnight/seasonal anomalies (cost-dominated); freqtrade MR (negative gross); HFEA (no signal,
DD-bound); ORB (untestable with free data — treat claims as unverified).

---

# Part 4 — Candidate A measured: MR sleeve blended into LIVE D (2026-07-13)

Backtest only; no engine file modified. Script: `engine/gh_hunt/blend_mr_research.py`
(imports the production research stack read-only; blend convention = `research_r8_combo.combo`
verbatim — monthly reset, intra-month drift, trade-count-weighted win, no rebalance cost charged,
same for baseline and blends so the comparison is like-for-like).

**Reproduction first (honesty rule §0.5):** rebuilt LIVE D → win 52.5% (exact), maxDD −29.0%
(exact), CAGR 30.6% vs 30.8% recorded, final 2.99M vs 3.04M (two days' data drift since the
2026-07-11 validation). Sleeves standalone on the engine window: DIP 21.7%/−25.3%/win 54.2%
(n=1467) · CRY 44.6%/−61.2%/win 45.2% (n=336) · MR D7 4.3%/win 75.0% (n=132) · MR TT 5.6%/win
69.2% (n=247) · MR IBS-B QQQ 12.6%/win 65.2% (n=319) — all consistent with the Part-2 hunt numbers.

## Results (18 blends, DIP/CRY/MR; best rows shown, full table in script output)

| Blend | Win% | CAGR | maxDD | Sharpe | 150k→ | Red years |
|---|--:|--:|--:|--:|--:|---|
| **D baseline 70/30/0** | **52.5** | **30.6%** | **−29.0%** | **1.04** | **2.99M** | 2022 |
| IBS 60/30/10 | 54.4 | 29.9% | −25.8% | 1.09 | 2.81M | 2022 |
| IBS 55/25/20 | 54.4 | 27.7% | **−21.3%** | 1.11 | 2.32M | 2018, 2022 |
| IBS 50/30/20 | 54.4 | 29.1% | −24.5% | **1.13** | 2.62M | 2018, 2022 |
| TT 60/30/10 | 54.5 | 26.6% | −28.4% | 1.04 | 2.61M | 2022 |
| D7 60/30/10 | 54.1 | 26.4% | −28.4% | 1.03 | 2.55M | 2022 |

## Verdict

1. **The win-rate hypothesis FAILS.** Blended win moves 52.5% → 54.5% max. Structural, not
   fixable by weighting: win is trade-count-weighted and DIP contributes 1,467 trades vs MR's
   132–319 — a 10–20% MR sleeve cannot move the pooled number toward the 70% mandate. The win
   gate can only move by changing the DIP engine's own trade mix (or the win-rate definition).
2. **CAGR never improves.** Every one of 18 blends is below baseline (best 29.9% vs 30.6%);
   MR CAGRs of 4–13% dilute a 30.6% base. The MR sleeve is not a return lever either.
3. **What it actually is: a modest defensive diversifier.** IBS-B blends improve Sharpe
   (1.04→1.13) and cut maxDD by 3–8pp; IBS 60/30/10 gives up 0.7pp CAGR for −3.2pp DD and
   +0.05 Sharpe. Calmar: 1.16 vs 1.06 baseline. But higher MR weights flip 2018 slightly red
   (−0 to −2% vs +3% baseline), and IBS-B carries selection risk (best of a variant family,
   single instrument QQQ).
4. **Recommendation: do not adopt for the stated purpose.** Candidate A was proposed as the
   win-rate lever and it measurably isn't one. If a lower-DD tier is ever wanted, IBS 60/30/10
   is the honest starting point for a full §9 loop (50bp stress, T+1 delay, role review) — as a
   *defensive option*, not an upgrade to D.

*Hypothetical research. Not financial advice.*
