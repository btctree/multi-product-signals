# Humbled Trader "Trend Join Long" (TJL) — extraction & honest backtest

**Source:** YouTube `IqvnryFzZD4` — *"I Built an AI Trading System With Claude + TradingView"*
(Shay / Humbled Trader, full-time day trader, 10+ yrs). Full auto-transcript pulled 2026-07-13.
**Task:** extract every rule she states, backtest honestly per METHODOLOGY §0.
**Status: PRE-REGISTERED SPEC — written before any backtest was run. Results appended below.**

---

## 1. What the video actually contains (extraction)

The video is mostly an AI-tooling tutorial (Claude Code + TradingView MCP + Telegram alerts).
The tradeable content is one pipeline with two stages:

### Scanner A — pre-market gap scan (universe selection, ~8:30am ET, every 30 min to 2pm)
| # | Rule (verbatim from transcript) |
|---|---|
| A1 | Stock gapped up **> 5%** from previous day |
| A2 | Price **> $3** per share |
| A3 | Pre-market volume **> 50,000** shares |
| A4 | Take the **top-10 gappers**; attach a news catalyst (Benzinga) — catalyst is informational, not a filter |

### Scanner B — TJL entry trigger (on Scanner A output; she also demos it directly on
AMD/NVDA/MU and backtests on her ~32-ticker watchlist). **All five must pass → long entry:**
| # | Rule (verbatim) |
|---|---|
| B1 | Time is **after 10:00 a.m.** market time (she does not trade the open) |
| B2 | Price **above yesterday's daily high** |
| B3 | **Yesterday's close above the 200 SMA** (daily) |
| B4 | Price **above today's pre-market high** |
| B5 | Price **above today's high of day** — i.e. the trigger is a fresh HOD breakout ("higher day breakout", "joining strength") |

### What the video does NOT specify
- **No exit rule is ever stated.** Her Pine backtest shows day-trades with P&L per trade;
  she is a day trader, so flat-by-close is the natural default. No stop, target, or trail is given.
- **No position sizing** beyond "Pine used 100% of a $1k account per trade" (she flags this
  as unrealistic herself) and "$10k account" in the Python run.
- Her watchlist (32 tickers) is behind a sign-up link — not public. META and HOOD are named
  as members; AMD/NVDA/MU/Mag-7 are used in demos.

### Her own reported results (for reproduction reference, not evidence)
- Pine, MU only, 5-min, "as far back as Pine allows": 14 trades, +$1,200 on $1k initial
  (100% equity/trade), 9/14 winners, profit factor 2.48.
- Pine, Mag-7 + MU + AMD, 15-min: "decent, highly selective, no trades on some names".
- Python, 32-ticker watchlist, **last 30 days**, $10k account: **54% win rate**, P&L positive.
- She stresses these are foundations: "I'm still testing and reiterating… I don't have all
  the answers for you today."

---

## 2. Pre-registered backtest spec (fixed before results)

Honesty rules: METHODOLOGY §0 — no look-ahead, decisions on completed bars only,
costs both sides, stops gap-filled honestly, every variant reported (no cherry-pick),
survivorship caveats stated. Costs: **15 bp per side** (gh_hunt stock convention).

**Entry implementation (all tests):** at bar *t* (only bars starting ≥ 10:00 ET), let
`HOD_prev` = max high of today's regular-session bars **before** *t*. Trigger when
`high[t] > HOD_prev` **and** `HOD_prev ≥ max(PM_high, yesterday_high)` is not required —
instead the *break price* `L = max(HOD_prev, PM_high, yesterday_high)` must be exceeded:
entry fills at `max(L, open[t])` + 15 bp (buy-stop realism; if `open[t] > L` the fill is
the open — no fill at prices that never traded). One entry per ticker per day.
`yesterday_close > SMA200(daily)` computed on data through yesterday only.

**Exit variants (pre-registered, all reported, none tuned):**
- **X1 flat-at-close** — sell at last regular bar's close (day-trader default).
- **X2 breakout-stop** — intrabar stop at `L` (the level that triggered entry); if a later
  bar's low ≤ L → out at L (or bar open if it gapped below); else flat at close.
- **X3 trigger-bar-stop + 2R target** — stop = trigger bar low, target = entry + 2×(entry − stop),
  first touch wins, conservative ordering (stop checked first when both hit in one bar); else flat at close.

**Test A — exact rules, 5-min bars incl. pre-market, last ~60 trading days (yfinance limit).**
- A-gap: universe = actual Scanner-A output. Broad net: all US common stocks (NASDAQ+NYSE+AMEX,
  ~6k), daily open-gap ≥ 3.5% pre-filter, then **exact** A1–A3 from 5-min prepost data
  (PM gap = last pre-9:30 trade vs yesterday close ≥ 5%; PM volume ≥ 50k; price > $3),
  top-10 per day by PM gap. This is the strategy as designed.
- A-watch: universe = 32-ticker watchlist proxy (below). This is the strategy as she backtested it.

**Test B — daily-bar approximation, 10 years, watchlist proxy.** B1/B4/B5 are not observable
on daily bars. Approximation: enter the day at buy-stop `L = max(yesterday_high, today_open)`
when `day_high > L`, require `yesterday_close > SMA200`; exit at same-day close (X1) or stop
at L (X2). Gap-flavor adds `open ≥ 1.05 × yesterday_close`. Coarse — sample-size robustness
check only; documented as approximation.

**Test C — hourly bars incl. pre-market, ~730 days (yfinance limit), watchlist proxy.**
Same rules as Test A at hourly granularity (PM high from 4:00–9:00 prepost bars; first
trigger bar = 10:30; HOD_prev from the 9:30 bar onward). Middle fidelity, middle sample.

**Watchlist proxy (32 liquid momentum names — hers is non-public; documented substitution):**
AAPL MSFT NVDA AMZN GOOGL META TSLA NFLX AMD MU AVGO PLTR COIN MSTR HOOD SMCI ARM SNOW
CRWD PANW NET DDOG SHOP UBER ABNB RBLX SOFI MARA RIOT DKNG ROKU XYZ

**Metrics per METHODOLOGY §9 reporting standard:** trades, win% (+95% CI), avg win/loss,
payoff, expectancy/trade (bp), profit factor, per-year breakdown where sample allows;
portfolio sim = $10k notional per trade, max 10 concurrent, on $100k — CAGR, maxDD.
**Mandate gates for adoption: win ≥ 70%, maxDD ≤ 30%, and must beat the live engine's
Calmar to earn a sleeve (RESULTS.md frontier).**

**Known limitations (stated up front):**
- yfinance caps intraday history: 60d @ 5m, 730d @ 1h → Test A is a small sample by construction.
- Survivorship: symbol directory is as-of-today (60d window → negligible; 10y watchlist → real,
  favours the strategy; stated on every Test B/C figure).
- Pre-market prints on yfinance are consolidated-tape approximations; PM high on thin
  names is noisier than a broker feed.
- **yfinance reports pre-market volume as 0** (checked before any backtest ran), so A3
  (PM volume ≥ 50k) is unimplementable as stated. Substitution, fixed pre-results:
  previous-day share volume ≥ 300k AND ≥ 12 pre-market 5m bars with nonzero range
  (evidence of active PM quoting). Both knowable before entry — no look-ahead.
- News-catalyst filter (A4) not modelled — no historical news source; this can only *narrow*
  the gap list, direction of bias unknown.

---

*Results appended below after the runs complete — no edits above this line after that point.*

---

## 3. Results (net of 15 bp/side; run 2026-07-13)

**Verdict up front: TJL as stated has negative expectancy after costs at every fidelity
level, in every window, under every pre-registered exit. It fails the mandate gates
(win ≥ 70%, DD ≤ 30%) by a wide margin and does not earn a sleeve.** The only
non-negative cell is the survivorship-favoured 10-year daily gap-flavor at PF 1.02 —
statistical breakeven before survivorship is even deducted.

### Bug disclosure (honesty rule 5 — surfaced, fixed, reran)
The first A-watch/A-gap runs were **invalid**: `load_daily` parsed date-only rows via
UTC→ET conversion, shifting every daily bar one day early, so "yesterday's high" (B2)
was actually **today's** high — look-ahead (anti-edge here: it forced entries at the
day's exact top; 0% win rates flagged it). Caught by the adversarial verify pass on
Test C, independently confirmed by the Test B verifier, fixed, and every affected test
rerun. Also fixed: X2 was stopping on the trigger bar itself, contradicting the
pre-registered text ("a *later* bar's low") — text governs. The buggy-loader numbers
are preserved inside `results_c_hourly.json` (diagnostics) for reconciliation.

### Test A-gap — the pipeline as designed (exact 5m rules, 39 sessions, 2026-05-15 → 07-13)
Scanner A passed 317 ticker-days (top-10 by true PM gap, within the wide net's
top-15-per-day by daily open gap — the net binds on every session, so on the busiest
days some true Scanner-A members can be missed; disclosed per verification). **B3
killed 59%** (gappers are usually below their 200SMA); 89 more never triggered
B1/B2/B4/B5; **40 entries**. 39 candidate slots were unevaluable (< 200 daily bars —
recent IPOs, the classic gap-and-go names, are structurally excluded by B3).

| Exit | Trades | Win% (95% CI) | Avg win | Avg loss | Expectancy | PF |
|---|--:|--:|--:|--:|--:|--:|
| X1 flat-at-close | 40 | 45% ± 15 | +6.8% | −8.2% | **−146 bp** | 0.68 |
| X2 stop-at-level | 40 | 7.5% ± 8 | +6.3% | −0.8% | **−29 bp** | 0.62 |
| X3 2R + trigger stop | 40 | 32.5% ± 15 | +5.1% | −3.0% | **−33 bp** | 0.84 |

Tail: JEM 2026-07-01 entered $10.87 (12:40), closed $3.21 = **−70.6% in one afternoon**
— without a stop, one gapper collapse erases ~9 average X1 winners. X1 is negative even
at 0 bp (gross −116 bp/trade): the entry itself is mistimed on gappers, not just cost-eaten.

### Test A-watch — the strategy as she backtested it (exact 5m rules, ~60d, 32-ticker proxy)
1,920 ticker-days scanned → 378 entries (22 tickers).

| Exit | Trades | Win% (95% CI) | Expectancy | PF | $100k sim (88d) |
|---|--:|--:|--:|--:|--:|
| X1 | 378 | 41.3% ± 5 | −12.4 bp | 0.85 | −4.2%, maxDD −10.9% |
| X2 | 378 | 5.6% ± 2 | −25.0 bp | 0.35 | −9.0% |
| X3 | 378 | 31.8% ± 5 | −26.5 bp | 0.47 | −9.5% |

Gross (0 bp) X1 expectancy is **+17.6 bp/trade** — a real but tiny intraday drift that
round-trip costs triple-eat (METHODOLOGY §0.2: a story that lives at 0bp is untradeable).

**Reproduction of her MU Pine claim (14 trades, 9 wins, PF 2.48): FAILED.** Same ticker,
same timeframe, overlapping window: 22 trades, 41% win, PF 0.91, −8 bp/trade. Her Pine
strategy's exact entry/exit code was AI-generated on stream and never shown; whatever it
traded, it is not the five stated rules under honest fills.

### Test B — daily approximation, 10.3 years, watchlist proxy (survivorship-FAVOURED)
22,279 trades base / 681 gap-flavor. Split-half win rates stable (40.4% / 42.6%).
Adversarially verified: independent re-implementation reproduced **all 22,960 trades 1:1**.

| Flavor · exit | Trades | Win% | Expectancy | PF |
|---|--:|--:|--:|--:|
| base X1 | 22,279 | 41.6% | −23.6 bp | 0.75 |
| base X2-floor | 22,279 | 0% (floor bound) | −30 bp | 0.00 |
| gap (≥5% open) X1 | 681 | 47.0% | **+5.4 bp** | 1.02 |
| gap X2-floor | 681 | 0% (floor bound) | −30 bp | 0.00 |

Fixed $10k/trade sizing on $100k reaches **ruin** on the base flavor. The gap flavor's
+5.4 bp is breakeven-at-noise on a list picked in 2026 for having survived and run —
the honest expectation is at or below zero.

### Test C — hourly + pre-market, 730 days, watchlist proxy
4,550 entries, 31 tickers (ARM missing hourly cache).

| Exit | Trades | Win% | Expectancy | PF | Yearly win% 23/24/25/26 |
|---|--:|--:|--:|--:|---|
| X1 | 4,550 | 37.4% | −25.3 bp | 0.63 | 38 / 39 / 36 / 36 |
| X2 (later-bar stop) | 4,550 | 14.3% | −27.9 bp | 0.44 | 17 / 15 / 13 / 16 |
| X3 | 4,550 | 36.2% | −29.5 bp | 0.53 | 38 / 39 / 33 / 36 |

Negative every calendar year; gross X1 edge ≈ +4.7 bp ≈ zero. Fixed-size sim: ruin.

### Cross-test picture

- The five entry rules select a *fresh intraday high after 10am* — the data says that
  moment has ~zero forward drift to the close (gross ≈ 0–18 bp), and −25 to −146 bp after
  realistic costs. "Joining strength" late is exactly what the intraday-reversal
  literature says doesn't pay on liquid names.
- On actual gappers the pipeline barely ever fires (40 entries in 39 days across the
  whole US market) because gappers set their high before 10am; when it does fire, it
  carries pump-collapse tail risk that X1 (her implied day-trade exit) does not cap.
- Her own demo numbers (54% win on 30 days, PF 2.48 on one ticker) are consistent with
  a lucky 30-day window on a survivor list during an up-tape, measured by an unshown,
  AI-generated Pine implementation — not with the stated rules under honest fills.

### Adversarial verification record (independent agents, instructed to refute)

| Test | Verdict | Evidence |
|---|---|---|
| A-watch | **CONFIRMED** (after fix) | independent replica reproduced all 1,134 trade rows exactly; 7 trades hand-recomputed from raw bars; the stale pre-fix run was REFUTED (that refutation is what proves the audit works) |
| B-daily | **CONFIRMED** | independent re-implementation reproduced all 22,960 trades 1:1; 10 trades hand-checked; portfolio sim to the dollar |
| C-hourly | **CONFIRMED** (X2 label adjusted) | independent no-shared-code replica matched all 13,650 rows exactly; 6 trades hand-checked; zero look-ahead assertions per trade |
| A-gap | **CONFIRMED** | independent no-shared-code replica matched all 120 rows 1:1, funnel counts exact (317/188/89/40), sims to the dollar; 9 trades hand-checked incl. the JEM −70.6% (real: PM pump, 12:40 trigger, volatility halt, collapse to close) and the CLRO +33.6% winner |

Genuine data flaws found during verification (all robustness-checked, none flip a conclusion):
- **2026-06-04 yfinance 5m junk spike prints** (e.g. AVGO printed +12.6% above its true
  daily high → a few phantom fills in A-watch). De-spiked rerun: X1 388 trades, 41% win,
  −13.6 bp — unchanged. No A-gap trades fell on that day.
- **Adjusted-daily vs raw-5m mismatch** in A-gap's Scanner A: dividend-adjusted daily
  closes overstate pm_gap on 3 of 40 entries (all still pass the ≥5% gate); excluding
  them: X1 45.9% win / −150 bp, X2 −28.9 bp, X3 −33.6 bp — unchanged.
- X1 exits at the last 5m bar's close, not the official auction print (±0.1–0.3%).
Treat yfinance 5m consolidated-tape extremes as suspect in any future intraday test.

### Mandate check (win ≥ 70% · maxDD ≤ 30% · beat live engine Calmar)

| Gate | Best TJL cell | Live engine (X4) | Pass? |
|---|---|---|---|
| Win rate ≥ 70% | 47.0% (survivorship-favoured) | 68.4% | ❌ |
| maxDD ≤ 30% | ruin at fixed sizing | −23.2% | ❌ |
| Positive expectancy after costs | −12 bp best honest cell | +16.7%/yr CAGR | ❌ |

**Not adopted. No parameter of the mandate is within reach.** Nothing here justifies
re-testing variants: the gross edge is ~zero, so no exit/sizing scheme can rescue it
(§0.2 corollary — the fix would have to be execution cost magic, not rules).

## 4. What IS worth keeping from the video

The AI-tooling pipeline (scanner → Telegram alerts → scheduled runs) mirrors what this
repo already runs in production. The one transferable idea: her Scanner A (PM gap ≥5%,
>$3, PM volume) is a reasonable *universe refresh* mechanism for a future intraday
product — but any entry logic on that universe must survive the B3 paradox found here
(gap stocks are either below SMA200 or too young for it; requiring 200d of history +
above-trend + a post-10am fresh high leaves ~1 tradeable signal per day market-wide).

## 5. Reproduce

```
engine/gh_hunt/tjl/fetch_universe_daily.py   # ~6,200 symbols, 5mo daily bars
engine/gh_hunt/tjl/fetch_watchlist.py        # 32 tickers: 10y 1d, 730d 1h, 60d 5m
engine/gh_hunt/tjl/find_gappers.py           # wide-net gap candidates + 5m/1d fetch
engine/gh_hunt/tjl/backtest_5m_gappers.py    # Test A-gap
engine/gh_hunt/tjl/backtest_5m_watch.py      # Test A-watch
engine/gh_hunt/tjl/backtest_daily.py         # Test B
engine/gh_hunt/tjl/backtest_hourly.py        # Test C
```
Shared engine: `tjl_common.py`. Per-trade CSVs + results JSONs alongside the scripts.
Intraday data cannot be re-fetched beyond yfinance's rolling windows (60d @ 5m,
730d @ 1h) — the cached CSVs in `tjl/cache/` are the evidence base.
Workflow journal (agent-level evidence): run `wf_49413f04-433`.

*Hypothetical research. Not financial advice.*
