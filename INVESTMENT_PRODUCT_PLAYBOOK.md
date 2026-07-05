# Systematic Investment Product — Build Playbook

A reusable, instrument-agnostic framework for researching, building, validating and
deploying a rules-based trading/investment product. Distilled from the **BTC Power Signal**
project; every principle below was *learned the hard way* on real data, and most carry a
worked example. Use it to scaffold the next product (any asset: crypto, equities, FX, futures).

> **Prime directive:** a number you can *print in a backtest* is worthless until you've proven
> you can *keep it after real frictions*. Optimise for what survives out-of-sample and live —
> not for the prettiest equity curve.

---

## 0. How to use this
Work the eight design decisions (§3–§10) in order, run the Build·Verify·Loop process (§12)
with the 4-role review (§11) until all roles agree **and** the result generalises, then deploy
(§13). Keep the honesty checklist (§14) open the entire time.

---

## 1. Cross-cutting principles (the hard lessons)

1. **Reproduce before you trust.** Re-implement the existing system and match its own numbers
   to the cent *before* changing anything. *(We ported 9 Excel strategies and matched every
   signal 100% and the $-P&L to the cent — only then were findings credible.)*

2. **Zero look-ahead, everywhere — including the plumbing.** A signal at time *t* may use only
   data through *t*. Two real bugs we caught:
   - a helper column averaged the **next 5 days** (future) → inflated one strategy ~10×;
   - a trailing stop that **raised the stop using today's high before checking today's low** →
     premature exits and a fake equity curve. *Rule: check the existing stop first, then ratchet.*

3. **Validate out-of-sample; the in-sample optimum is a trap.** Split into train / validation /
   **untouched test**. *(Tuning every cell hit Sharpe 1.70 in-sample but 1.15 on the test set —
   worse than the simple config's 1.21. Selecting the "most robust" on train+val still gave
   −0.05 on test.)* **Prefer the setting robust across independent periods over the one that maxes
   in-sample.**

4. **"Try all combinations" is impossible and counter-productive.** Per-cell sizing×leverage alone
   is ~10¹³–10¹⁶ combinations. You cannot enumerate them, and sampling-then-picking-the-best
   *manufactures* overfit. Use principled choices + walk-forward, not brute force.

5. **Leverage is (mostly) a slippage mirage.** Sharpe is ~constant across leverage — leverage
   scales return *and* drawdown together until path-dependency/ruin. The headline only exists
   under perfect fills. *(Same config: perfect-fill $1.67B → 150bp-slippage $35k, −86% DD.)*
   **Model fees + funding + slippage stress (0/50/100/150 bp) before believing any leveraged number.**

6. **Account honestly.** Use **%-compounded equity**, never summed $ price-differences (which
   over-weight whichever era had bigger nominal prices). Include fees (~5 bp/side) and funding;
   report drawdown, Sharpe, Sortino, Calmar, win rate, profit factor, **liquidation count**.

7. **The edge is convex — let winners run, cut losers hard.** *(Top-10 trades = 98% of all gains;
   ~48–58% win rate; median trade ~flat.)* Capping winners (early TP / tight trailing) destroys
   the fat tail that drives returns; a hard catastrophe stop caps the left tail cheaply.

8. **Full 1× deployment ≈ leverage without liquidation.** Deploying ~100% of equity per trade at
   1× compounds far faster than fractional sizing and carries **no liquidation risk** — usually a
   better risk/return than leverage on the same signal.

9. **Asymmetry beats symmetry.** Longs and shorts on a structurally-drifting asset are *not*
   mirror images. *(Shorting BTC ≈ breakeven overall and *loses* in bull pullbacks; only worth it
   in chop/bear-trend, at half size and a tighter stop.)* Give each direction its own rules.

10. **Match the target to reality.** Compute the CAGR a goal implies before promising it.
    *($500→$80M/10y = 231%/yr; the honest edge tops out far below that without ruinous leverage.)*

---

## 2. (reserved — see §3–§10 for the design decisions)

## 3. Market definition (regime segmentation)
Classify every period into a regime **before** deciding any trade, and always include a
**"do nothing"** state. Segment finely enough that each regime maps to a distinct edge.

Reference (BTC), classified daily from SMA50/SMA200, 10-day slope, ATR%, Bollinger-band width
and RSI — each "high/low" measured **vs its own trailing-365-day median** so it self-calibrates:

| Regime | Definition | Has edge? |
|---|---|---|
| BULL_TREND | close > SMA50 > SMA200, SMA50 rising, above SMA20 | strong (trend) |
| BULL_PULLBACK | uptrend but a short dip (close < SMA20) | strongest (dip-buy) |
| RANGE_LOWVOL | RSI 40–60, narrow bands | **none → stand aside** |
| CHOP_HIGHVOL | high ATR%, no net trend | thin (one engine only) |
| BEAR_TREND | close < SMA50 < SMA200, falling | weak (capital preservation) |
| BEAR_BOUNCE | downtrend but short pop (close > SMA20) | moderate (bounce) |

**Principle:** match the engine to the regime; **never force a trade in a regime where you have
no measured edge** (RANGE_LOWVOL = stand aside).

**Regime hysteresis (anti-whipsaw):** require a regime to persist (e.g. 2 closes) or use
separate enter/exit thresholds before switching engines, so noise near a boundary doesn't churn
you in and out. Whipsaw between regimes silently bleeds fees.

## 4. Strategy selection per regime
- Score each candidate engine by its **in-regime risk-adjusted return** (Sharpe of being
  in-position during that regime), then assign each regime its best engine.
- **One strategy active at a time**; a new position only when flat. Exit on: take-profit /
  cut-loss / **regime change to one the engine isn't suited to** / the engine's own reversal.
- Reference map: BULL_TREND→trend engine; BULL_PULLBACK→dip engine; CHOP→mean-reversion engine;
  BEAR_TREND→defensive/aside; BEAR_BOUNCE→bounce engine; RANGE/NEUTRAL→aside.

## 5. Entry & exit logic
- **Entries are multi-condition gated**, not a single price trigger; decided on the chosen
  decision timeframe (we used the **daily close** — that's where the edge lives).
- **Exits ride intraday:** the position is managed on a finer timeframe (1-minute) so the stop
  can fill intraday, not only at the daily close. **Let winners run** (no fixed near-term TP for
  trend trades); exit on reversal, regime change, or the trailing stop.

## 6. Cut-loss / stop logic
- **Trailing hard stop** that ratchets behind the high-water (long) / low-water (short),
  **never loosens**, with **0 look-ahead** (test the *existing* stop, then raise it).
- **Asymmetric:** wider for longs (≈10%), tighter for shorts (≈7%, because counter-trend
  squeezes are violent).
- For spot/1× it's a **catastrophe stop** (rarely binds — the signal usually exits first); for
  leverage it's also the liquidation guard (keep the stop *inside* the liquidation price).
- **Place it as a resting exchange order** at the published cut-loss price so it fills intraday —
  this is your main defence against slippage. Surface the exact price in every entry alert.

## 7. Confidence index (setup-quality score)
- **Confidence = the regime–strategy fit** (the in-regime Sharpe), bucketed **High / Med / Low**;
  below a floor → **stand aside**.
- It **sizes the bet; it never creates or overrides a signal.**
- **Validate it:** higher buckets must realise higher edge. *(Ours did: High +3.16%/trade vs
  Med +1.10% — monotonic.)* If not monotonic, re-bucket or discard.

## 8. Sizing & margin — split by position & market
Size by confidence, and treat long vs short asymmetrically. Reference scheme:

| Regime | Long size (of equity) | Long lev | Short size | Short lev | Long stop | Short stop |
|---|---|---|---|---|---|---|
| BULL_TREND | 100% (High) | 1× (spot) | — | — | 10% | — |
| BULL_PULLBACK | 100% (High) | 1× | — | — | 10% | — |
| CHOP_HIGHVOL | 100% (High) | 1× | 50% (half) | 1× | 10% | 7% |
| BEAR_TREND | 70% (Med) | 1× | 35% (half) | 1× | 10% | 7% |
| BEAR_BOUNCE | 70% (Med) | 1× | — | — | 10% | — |
| RANGE_LOWVOL / NEUTRAL | 0 (stand aside) | — | 0 | — | — | — |

Rules of thumb:
- **Spot 1× is the default product** (no funding, no liquidation, survives slippage). It is the
  number you actually trade.
- **Shorts: half the long size, a tighter stop, only in the regimes where they have edge.**
- **If using margin, cap deployed margin (e.g. ≤50% of equity)** so even a liquidation costs at
  most that slice, not the whole account. But see §9.

## 8.5 Risk controls (define these FIRST)
- **Mandate first, config second.** Decide the **maximum tolerable drawdown** up front
  (e.g. "≤35%") and choose the leverage/sizing tier that fits it — never pick the config for its
  return and discover the DD later.
- **Per-trade risk budget.** Cap the loss a single trade can inflict: `risk ≈ size × leverage ×
  stop_distance ≤ R%` of equity (e.g. R ≈ 1–3%). This ties sizing, leverage and stop together so
  no one knob can blow up the account.
- **Drawdown kill-switch.** When equity is in a deep drawdown (e.g. >25–30% off its peak),
  automatically **halve exposure** until it recovers — caps the tail at a modest return cost.
- **Gap-risk sizing.** Stops can **gap through** in a crash. Size leverage so a plausible
  overnight/illiquid gap (for crypto, 20–40%) **cannot** cause a >100% loss. This is the real
  reason high leverage is unsafe even with a stop.

## 9. Leverage policy
- **Asymmetric and hard-capped** (e.g. 5× long / 2× short, cap ≤5×). It amplifies gains **and**
  losses and is path-dependent.
- **Leverage adds no Sharpe** — only return and drawdown, until ruin. Treat any leveraged
  headline as *optimistic / perfect-fill* and always show it beside its **slippage-stressed**
  value and its **maxDD**. Default the product to **1× spot**; offer leverage only as a clearly
  labelled, risk-warned option.

## 10. Costs, realism & validation (do this BEFORE trusting any number)
- Fees ~5 bp/side; model **funding** if using perps; **stress slippage at 0/50/100/150 bp** on
  stop fills. (1× usually survives 150 bp; leverage often collapses.)
- **Walk-forward / out-of-sample:** train / validation / **untouched test**. Report each.
- Metrics: total return, CAGR, **Sharpe, Sortino, Calmar, maxDD**, win rate, profit factor,
  **liquidation count**, exposure, worst rolling 12-month.
- **Pre-register the primary metric** (e.g. test-set Calmar) before searching, so you don't
  metric-shop after seeing results.
- **Sample size matters.** A config with too few trades (e.g. <~50) or that leans on a handful of
  winners is *not* validated — check trade count and the result's dependence on its top trades.
- **Turnover & capacity.** Fees scale with turnover and trade at *your* size; confirm the edge
  survives realistic costs at the capital you'll actually deploy, not just at $500.
- State assumptions honestly (e.g. "leveraged ratios assume the stop holds intraday — optimistic").

---

## 11. The 4-role review protocol
After producing anything, review it through four professional lenses; they **debate**, then must
**all agree** before shipping. (Run them yourself if you hold the full context; only delegate to a
separate agent for genuinely independent/parallel work — a cold agent re-derives context and is
costlier.)

| Role | What they check | Typical veto |
|---|---|---|
| **Data Analyst** | Does the math reproduce? No look-ahead? Is the edge real out-of-sample, or curve-fit? Are figures honest (% not $, net of costs)? | "In-sample-max decays out-of-sample — reject." |
| **IB Fund Manager** | Is it investable? Drawdown tolerable? Capital preserved? Is the headline an expectations liability? | "−65% DD is uninvestable; lead with the real number." |
| **Actuary** | Ruin/liquidation probability, tail risk, gap risk, guaranteed bleeds (funding). Positive expectancy after **all** costs? | "Leverage + gap risk = ruin; reject." |
| **Quant Trader** | Where does the edge actually come from? Microstructure, turnover, fees, fill realism, robustness. | "Tight stops whipsaw the edge; ride trend instead." |

**Convergence rule:** stop when all four agree on the design **and** the result holds on the
untouched test set. Disagreement that's only about *risk appetite* is resolved by offering tiers
(conservative / balanced / growth), not by overriding a veto.

## 12. Build · Verify · Loop (process)
1. Restate the goal in one line; define "done" and **how it's verified**. Lock fork-in-the-road
   decisions up front.
2. **Reproduce** the existing/benchmark result.
3. Build honestly (no look-ahead, fees/funding/slippage, %-compounding, liquidation modelled).
4. **A/B + walk-forward**; pick the robust config, not the in-sample winner.
5. **4-role review** (§11) → modify → repeat until convergence.
6. Build the regime-aware dashboard + alert; **deploy, then verify the live result**, not just the commit.

## 13. Deployment pattern (live)
- **Decision cadence = where the edge is** (daily). **Management cadence = finer** (intraday) only
  for the stop. Don't imply intraday *alpha* you didn't test.
- Reference stack: **GitHub Actions** (hourly = reliable; sub-hourly = flaky → use an always-on
  host) → recompute signal → **Telegram** alerts (ENTER with cut-loss price, EXIT with P&L, daily
  status) → **GitHub Pages** mobile/PWA dashboard. Persist position state across runs.
- **Alert format:** title → state line → sections → compact levels line → one-line honest
  disclaimer. Entry alert **must** include the exact cut-loss price to pre-set.
- **Dashboard:** regime-aware; default to the real (spot/1×) curve; leverage behind a labelled,
  drawdown-shown toggle; interactive (pan/zoom/scale); recent trades; "no-trade → next action".
  Degrade gracefully on missing data/expired token — never blank or lie.

## 14. Red-flag / honesty checklist
- [ ] Hyper-precise "magic" constants (e.g. 30.13, 4.2707) → **overfit**; test out-of-sample.
- [ ] Any column referencing future bars → **look-ahead**.
- [ ] Gross (no-fee) numbers presented as results → re-run **net**.
- [ ] Leveraged headline without a slippage-stress column → **mirage**.
- [ ] Backtest window cherry-picked to a favourable era → normalise the period.
- [ ] "0 liquidations" at high leverage → artefact of perfect fills; re-check with gap/slippage.
- [ ] Win rate sounds great but median trade loses → fine **only if** the right tail pays; confirm.
- [ ] Target implies an implausible CAGR → reset the target, don't lever into ruin.

## 15. Feasibility math (set targets honestly)
`required_CAGR = (target/start)^(1/years) − 1`. Sanity anchors: doubling yearly (100% CAGR) turns
$500 into ~$512k in 10y; 40% CAGR → ~$14k; 75% → ~$90k. If the target needs >~100% CAGR
sustained, it almost certainly requires leverage that guarantees eventual ruin — revise it.

---

## Appendix — BTC Power Signal (worked example, as of 2026-06)
- **Data:** Binance BTCUSDT daily (2017-08+) + 1-minute intraday fills; CoinGecko daily pre-2017.
- **Final product:** regime-switch, one engine at a time, conf-scaled longs (100/70%), half-size
  shorts only in CHOP/BEAR_TREND, 10%/7% trailing stops, **spot 1×**.
- **Honest results (real fills):** $500 → ~$45k (2018+) / ~$670k (2014+), Sharpe ~1.25, Calmar
  ~1.7–1.85, maxDD ~−40 to −57%, **0 liquidations**, ~55–59% win, robust across walk-forward halves.
- **Leverage reality:** 5×/2× perfect-fill from 2014 = **$1.67B** but **−99% DD** and collapses to
  **$35k at 150 bp slippage** — the canonical "looks huge, isn't real" case.
- **Live:** github.com/btctree/btc-power · hourly Actions · Telegram · Pages dashboard.

*Hypothetical research framework. Not financial advice. Validate everything out-of-sample on the
specific instrument before risking capital.*

---

## Addendum — Multi-Product Engine operations (2026-07-04)

### A1. Daily refresh vs laptop sleep
The scheduler is Windows Task Scheduler (`MultiProductDaily`, 08:00 daily), configured with:
- **WakeToRun** — wakes the laptop from sleep for the run. Works **when plugged into AC**
  (this machine's power plan allows wake timers on AC, disables them on battery).
- **StartWhenAvailable** — if the 08:00 run was missed (on battery / lid closed / powered
  off), the run fires **as soon as the laptop next wakes**. So a missed morning is caught
  up automatically the moment you open the laptop.
- Limits: a fully shut-down machine cannot self-start; signals are computed on daily
  closes, so a late run the same day produces identical signals — timeliness, not
  correctness, is what sleep costs.

### A2. Return-target feasibility under long-only / no-margin (the honest frontier)
Requirement history: 200%/yr (2026-07-03) → 150%/yr (2026-07-04), keeping long-only,
no margin, DD ≤ 30%, win ≈ 70%. Feasibility math (§15) applies unchanged:
- The win%/payoff frontier is a *frontier, not a menu*: win ≥~70% forces small profit
  targets (≈1–1.25 ATR ≈ 2–6%/trade), and DD ≤ 30% forces diversification. Both cap
  compounding speed regardless of signal quality.
- Measured levers, in order of honesty: (1) wider targets while win stays near 70;
  (2) concentration (fewer, larger slots) until DD hits the 30% wall; (3) **leveraged
  index ETFs held as cash instruments** — the only no-margin instrument class whose
  per-trade % moves are structurally 2–3× bigger; loss capped at stake, zero
  liquidation risk (vol-decay is second-order at ~5-day holds).
- Every config on the measured ladder is recorded in RESULTS.md with win/PF/CAGR/DD;
  configs failing any gate are rejected, not shipped. The gap that remains between the
  best honest CAGR and a 150% target is the market's answer, not a tuning shortfall —
  closing it would require margin/shorts/derivatives (excluded by mandate) or
  prediction (measured not to work, METHODOLOGY §8).
