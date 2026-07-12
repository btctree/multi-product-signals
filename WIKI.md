# Multi-Product Strategy Engine — System Wiki

*Authoritative reference for how the whole system works. Last reviewed 2026-07-12
against live product **D**. Companion docs: `README.md`, `RESULTS.md`,
`PERFORMANCE.md`, `METHODOLOGY.md`, `INVESTMENT_PRODUCT_PLAYBOOK.md`, `DEPLOY.md`.*

---

## 0. Contents
1. What the system is (one paragraph)
2. Architecture & data flow
3. Market definitions (asset classes; tradeable vs monitor-only)
4. The two engines (strategies) — exact rules
5. The score & the entry gate
6. Backtest vs live signal generation (the honesty boundary)
7. Position sizing, exits, risk controls
8. Trading pattern (what you actually do)
9. Dashboard — pages, data model, and the data-push pipeline
10. Data storage — what persists, what is rebuilt
11. Validation history & current headline numbers
12. Limitations (read this before trusting anything)
13. Improvement review (suggestions, not yet actioned)

---

## 1. What it is

A **long-only, no-margin, rules-based advisory engine** that monitors ~1,000
global products daily, decides which are in a high-probability *buy-the-dip*
state, ranks them by a momentum score, and publishes the result to a
mobile-friendly web dashboard (a PWA you install on iPhone). It never places
trades — **you** execute; the app tells you *what / at what price / with what
cut-loss / why*, and tracks the positions you record. Every rule was validated
on a 10+ year honest backtest before going live.

---

## 2. Architecture & data flow

```
Yahoo Finance (free daily OHLCV, 10y)
        │  data_fetch.py  (batch download, 80/chunk, cached CSV)
        ▼
   data/prices/*.csv  ── indicators.py (SMA/RSI/ATR/momentum)
        │
        ├── engine_rr.py / research_*.py  → BACKTEST (validation, offline)
        │        └── data/revalidation.json  (headline numbers, single source of truth)
        │
        └── production.py  → LIVE per-product analysis cards
                 │  build_dashboard.py
                 ▼
        docs/data.json  +  docs/products/<sym>.json   (regenerated every run)
                 │  GitHub Actions (.github/workflows/daily.yml)
                 ▼
        GitHub Pages  →  https://btctree.github.io/multi-product-signals/
                 ▼
        iPhone PWA (dashboard) — you record buys/sells locally
```

Repo: `github.com/btctree/multi-product-signals` (public). Everything runs on
free GitHub Actions + Pages. No server, no database, no paid data.

Code map (in `engine/`):
| File | Role |
|---|---|
| `config.py` | All parameters — the single place the product is defined |
| `universe.py` | The monitoring list + daily grow-only refresh policy + market classification |
| `data_fetch.py` | Batched 10y price download with CSV cache |
| `indicators.py` | Feature computation (SMA200/50, RSI3, ATR14, 90d momentum, 52w high) |
| `engine_rr.py` | The honest backtest engine (dip + trailing-exit portfolio sim) |
| `research_r7_trend.py` | Trend engine (used for the crypto sleeve + research) |
| `research_r8_combo.py` | Monthly-rebalanced multi-sleeve blender |
| `research_revalidate.py` | The 4-way A/B/C/D validation that produced current numbers |
| `production.py` | Live per-product classifier → analysis cards |
| `scoring.py` | The 0–100 momentum score |
| `build_dashboard.py` | Assembles `docs/data.json` + per-product files |
| `export_backtest_trades.py` | Exports the backtest trade log for the History tab |
| `notify_telegram.py` | Optional daily push (dormant until secrets set) |
| `position_cli.py` | Local CLI to record fills (mirrors the app's buttons) |

---

## 3. Market definitions

Each ticker is classified by `universe.market_of()` from its symbol suffix.
There are two tiers: **tradeable** (the validated strategy acts on them) and
**monitor-only** (analyzed and searchable, but never a trade signal — their
cards are stamped *"Analysis only"*).

| Class | Examples | Suffix / rule | Tradeable? | Engine |
|---|---|---|---|---|
| **US** | AAPL, NVDA | no suffix | ✅ | DIP |
| **HK** | 0700.HK | `.HK` | ✅ | DIP |
| **JP** | 7203.T | `.T` | ✅ | DIP |
| **EU** | ASML.AS, AZN.L | `.AS .PA .DE .MC .MI .L .SW .CO …` | ✅ | DIP |
| **CRYPTO** | BTC-USD, ETH-USD | `-USD` | ✅ | CRY |
| INDEX | ^GSPC, ^N225, DAX | `^…`, `.SS/.SZ` | ⛔ monitor | — |
| COMMODITY | GC=F, CL=F, HG=F | `=F` | ⛔ monitor | — |
| FX | EURUSD=X, USDJPY=X | `=X` | ⛔ monitor | — |
| BOND | TLT, IEF, ZROZ | curated list | ⛔ monitor | — |
| LEV | TQQQ, SOXL, SQQQ | curated list | ⛔ monitor | — |
| ETF | SPY, QQQ, SMH, GLD | curated list | ⛔ monitor | — |
| MACRO | CPER, UUP | curated list | ⛔ monitor | — |

**Why some classes are monitor-only** (all tested and rejected during
validation, documented in `RESULTS.md`):
- Commodity **futures** carry roll artifacts that falsify mean-reversion backtests.
- **Indices** aren't directly tradeable and have no clean entry edge here.
- **FX** unleveraged is a net drag (too low vol; regime-gated hedge helped only
  2022 marginally).
- **Leveraged / inverse ETFs** (incl. SQQQ) breach the 30% drawdown gate and
  decay; **bonds** are a weak engine; both stay for reference.

The **monitoring pool grows only** (§9): new top-cap entrants, momentum leaders,
and your one-tap requests are added; products are removed *only* after persistent
bottom-decile trading volume (5 consecutive daily strikes) — a market-cap drop
never removes anything.

---

## 4. The two engines (strategies)

The live product **D** blends two sleeves, monthly-rebalanced **70% equity /
30% crypto**.

### 4.1 DIP sleeve — "buy the dip in a confirmed uptrend" (equities)
The highest-win-rate long pattern on large caps: buy a *short-term* pullback
inside an *established* uptrend, then let winners run.

**Entry (all must hold), decided on the daily close, acted at next open:**
1. **Regime**: `close > SMA200` **and** `SMA50 > SMA200` (confirmed uptrend).
2. **Momentum gate (product D)**: 90-day return **≥ +30%** (= score > 60).
3. **Strength**: within 12% of the 52-week high (`close ≥ 0.88 × 52w-high`).
4. **The dip**: `RSI(3) < 25` (short-term oversold).
5. **Tradeability**: daily ATR ≥ 1.2% of price (the expected move clears costs).

**Exit (whichever comes first):**
- **Regime break**: a daily **close below its own SMA200** → sell next open. *(This
  is the single most valuable exit — it turned 2018 positive at zero cost.)*
- **Chandelier trailing stop**: `stop = high-water − K×ATR`, ratcheting up, never
  down. `K = 3.5` early, tightening to `2.0` once the trade is +1.5 ATR in profit.
- **Catastrophe stop**: `max(3.5×ATR, 12%)` below entry (rarely binds).
- **Time stop**: ~25–60 bars (rarely binds).

Slots: **13**. Markets: US/HK/JP/EU.

### 4.2 CRY sleeve — crypto trend-following
Crypto trends persistently, so it rides trends rather than buying dips.
- **Entry**: `close > SMA200` **and** `SMA50 > SMA200`.
- **Exit**: two consecutive closes below SMA50, **or** a 4.0×ATR chandelier trail.
- Slots: **2** (BTC-USD, ETH-USD).

### 4.3 Why blend them
Crypto is the return engine (huge in bull years) but −61% DD alone; the equity
sleeve is the steady base. Rebalanced monthly, diversification tames the combined
drawdown to −29% while lifting CAGR. Shorts, options, bonds, FX, and leveraged
ETFs were all tested as additional sleeves and **rejected** (see `RESULTS.md`).

---

## 5. The score & the entry gate

`scoring.momentum_score(mom) = round(clamp(mom / 0.50) × 100)` — 90-day momentum
scaled 0–100, full points at +50%. Bands: **Strong ≥65 · Moderate ≥50 · Marginal <50**.

- **This is the *ranking* factor**, sorted high→low on the Actions page.
- **Product D adds an entry gate: BUY only when score > 60** (momentum ≥ +30%).
  A qualifying dip whose score is below 60 shows as **WATCH** with the honest reason.
- A richer composite (dip depth + 52w-high proximity + risk weights) was tested
  as the ordering rule and **rejected** — it collapsed the backtest from ~27%/yr
  to ~5%/yr. Those factors are shown as *facts* on each card but do **not** reorder.
- The score is a **relative ranking among today's candidates, not a probability
  of profit.**

---

## 6. Backtest vs live signal generation — the honesty boundary

These are two different programs and the distinction matters:

**Backtest (`engine_rr.py`)** — a full portfolio simulator used *offline* to
validate rules. Honesty rules (from `METHODOLOGY.md`), enforced everywhere:
- **Zero look-ahead**: a signal at day *t* uses only data through *t*; the fill
  happens at *t+1*'s open.
- **Stops fill honestly**: at the stop price, or worse (at the open) if the market
  gaps through — never at a price the market didn't offer. If both stop and target
  are touched the same day, the **stop is assumed first** (conservative).
- **Costs on every fill**, per-market (US 10bp, HK 25bp incl. stamp, JP 15bp,
  crypto 20bp, …), charged both sides.
- **Dynamic compounding sizing**: each new position = `sleeve_cash / (slots − held)`.
- Reports split-half win rates and per-market robustness, not just the headline.

**Live (`production.py`)** — a *per-product snapshot classifier*, run hourly. For
each product it computes today's indicators and outputs one card: regime, action
(BUY / WATCH / AVOID / HOLD), reference price, target, cut-loss, confidence, score,
and point-form reasons. `scan_actions()` collects the tradeable BUYs and ranks them.

**Key point:** the live layer classifies *each product independently*; it does
**not** run a live portfolio simulation. The dashboard shows the ranked BUY
candidates — you choose up to 13 equity + 2 crypto from the top of the list. The
backtest validated the mechanical version of exactly this selection, so the live
behavior tracks the validated behavior, but live results will differ from
backtested ones (see Limitations).

---

## 7. Position sizing, exits, risk controls

- **Capital**: HKD 150,000, **long only, no margin, no leverage**.
- **15 positions max** (13 equity + 2 crypto) — the agreed system cap.
- **Sizing**: each sleeve gets its weight of capital (70% / 30%); within a sleeve,
  a new position = `sleeve_cash / (free slots)`. Equal-ish, compounding.
- **Per-trade cut-loss**: shown on every BUY card; place it as a resting stop.
- **Regime exit**: sell when a holding closes below its own 200-day average.
- **Trailing stop**: raise your stop behind the high-water mark (the app's
  Positions tab tells you when to raise it).
- **Drawdown mandate**: the *product* was chosen so the backtested max drawdown
  (−29%) stays inside your ≤30% rule. Individual months can still be red.

---

## 8. Trading pattern (what you actually do)

1. **Once a day** (after close / before your market opens) open the app.
2. **Actions tab**: the ranked BUY list (score-gated >60). For as many free slots
   as you have (up to 13 equity + 2 crypto), buy the top-scored names at the next
   open. Place the two resting orders shown: SELL-STOP at the cut-loss.
3. Tap the green **BUY** box on each card you acted on → enter fill price + quantity
   → it moves to **Positions**.
4. **Positions tab** (checked as often as you like; data refreshes hourly):
   - **⚠ SELL** banner = close below SMA200 or your stop hit → exit next open.
   - **"Raise your stop to X"** = the trail moved up → update your resting stop.
   - **Record sell** (full or partial) when you exit → it moves to **History**.
5. **Search tab**: look up any of ~1,000 products (or request-add anything else),
   filter by market/action, read the full analysis.
6. **Monthly**: nudge capital back toward 70/30 between the equity and crypto
   sleeves (a reminder appears at month start).

---

## 9. Dashboard — pages, data model, data push

### 9.1 Pages
- **Actions** — today's score-ranked BUY signals (each: BUY box = record button,
  score box, entry/target/cut-loss, reasons); then the scoring explainer +
  survivorship note; then a static **Monitoring coverage** panel (counts per class).
- **Positions** — your device-tracked holdings with live P&L (% and money), exit
  alerts, partial/full Record-sell, Remove, and Export/Import backup.
- **History** — your closed trades (with a performance summary) + the engine's
  most recent 200 **backtest** trades (labeled validation, not live fills).
- **Search** — type name/symbol/code; filter chips (Market × Action); with an
  empty box it lists all products with their status; unknown symbols offer a
  one-tap **Add to monitoring**.

### 9.2 Data model (`docs/data.json`)
```
generated, product, headline{win,cagr,maxdd,grows},   ← headline from revalidation.json
universe_count, universe_updated, universe_changes[],  ← single source of truth
actions[ card ], positions[], history[], backtest_trades[], index[ {sym,name,market,action,score,price} ]
```
Plus one `docs/products/<safe_sym>.json` per product: `{prices[], sma200[], card{…}}`
for the chart and detail view. `safe_sym` replaces `^ = .` so filenames are URL-safe.

**Single source of truth:** the headline stats (win/CAGR/maxDD) are read from
`data/revalidation.json` (the last validation run), never hardcoded — so the
number you see on the app is the number the backtest actually produced.

### 9.3 The push pipeline (GitHub Actions, free)
Triggers: **hourly** at `:05`, a dedicated **daily** run at `00:20 UTC`, on every
**push**, and manual.

Each run: install deps → **download 10y prices for the full pool** (batched) →
*(daily only)* refresh the monitoring list + export backtest trades →
**build the dashboard JSON** → *(daily only)* Telegram alert + commit universe/name
cache → **deploy `docs/` to Pages**.

- **Hourly** = signal/price refresh + redeploy (dashboard data is deployed as an
  artifact, never committed → the repo doesn't bloat).
- **Daily 00:20** = additionally re-ranks the universe (adds/removes) and commits
  the small state files. The market-cap scan runs **weekly (Mondays)** for speed;
  analyst momentum/52w-high adds and illiquidity removals run daily.
- Deliberately **not sub-hourly**: GitHub skips sub-hourly crons under load and
  Yahoo throttles heavy scraping (our own BTC-project finding). Signals are on
  daily closes anyway, so hourly is already more than the strategy needs.

---

## 10. Data storage — what persists vs what is rebuilt

| Item | Where | Persistence |
|---|---|---|
| 10-year price history | `data/prices/*.csv` | **Rebuilt from Yahoo every run** (not committed). Idempotent — always a complete, adjusted dataset; old & new data can't drift. |
| Monitoring list | `data/universe.json` | Committed (grow-only; add reasons + low-vol strike counters). |
| Company names | `data/company_names.json` | Committed cache (so CI doesn't refetch). |
| Backtest trade log | `data/backtest_trades.json` | Committed; refreshed daily. |
| Validation numbers | `data/revalidation.json` | Committed; the dashboard headline. |
| Dashboard output | `docs/data.json`, `docs/products/` | Regenerated & deployed each run (gitignored). |
| **Your positions/history** | your phone's **localStorage** | **Only on that device.** Export/Import gives a portable text backup. Nothing is uploaded. |
| Secrets (GitHub PAT, Telegram) | `Multi-Market System.txt` | **Git-ignored** — never pushed. Rotate the exposed PAT. |

---

## 11. Validation & current numbers (product D)

Re-validated 2026-07-11 on the full ~1,000-product pool, honest engine, net of
costs, 11.2 years, HKD 150,000 start, 70% equity dip / 30% crypto trend,
monthly rebalance. Chosen from a 4-way comparison (`data/revalidation.json`):

| Config | Win | CAGR | maxDD | 150k → | Sharpe | DD gate |
|---|--:|--:|--:|--:|--:|---|
| A original ~200 pool, 5+2 | 51.1% | 30.5% | −26.4% | 2.95M | 0.80 | ✅ |
| B full pool, 5+2 | 52.0% | 35.8% | −32.5% | 4.63M | 0.87 | ❌ |
| C full pool, score>60, 5+2 | 52.5% | 34.6% | −31.3% | 4.18M | 0.84 | ❌ |
| **D full pool, score>60, 13+2 = 15 (LIVE)** | **52.5%** | **30.8%** | **−29.0%** | **3.04M** | **0.88** | ✅ |

D is the only full-pool config that passes the ≤30% drawdown mandate; spreading
capital across 15 positions is what tames the tail. ~131 trades/yr.

---

## 12. Limitations (read before trusting anything)

1. **Survivorship bias — the biggest one.** Backtests use *today's* index members
   over 10 years; you couldn't have known in 2016 which names would still be in the
   index. Full-pool figures (B/C/D) are somewhat *flattered* versus the original
   pool. Treat D as "≈ baseline with better diversification," not "strictly better,"
   and expect live results below backtested ones.
2. **Live ≠ backtest.** The live layer classifies each product independently and
   *you* pick the basket; the backtest picked mechanically. Real fills, timing,
   FX, and your discretion introduce slippage vs the curve.
3. **Exit alerts are approximate.** The app re-derives the trailing stop from the
   product's current ATR/price — it doesn't know your position's exact high-water
   mark (positions live only on your device). Use it as guidance; the SMA200-break
   SELL is exact, the trail-raise number is an estimate.
4. **Monitor-only cards still say BUY/WATCH.** They're stamped "Analysis only," but
   the label is a caption, not a suppression — don't trade FX/bonds/inverse ETFs off
   these cards; they were never validated (and for VIX/inverse the dip logic is
   semantically wrong).
5. **No live cross-device sync.** Positions/history are per-device; back them up.
6. **Search-anything isn't live-priced.** A static page can't call Yahoo (CORS);
   unknown symbols are added to the pool via the one-tap request (~5 min), not
   analyzed instantly.
7. **Free-data quality.** Yahoo has occasional gaps, delistings, and rate limits;
   a handful of tickers may show "no data" on any given run.
8. **FX drift not modeled** in the HKD P&L (HKD is USD-pegged; JPY adds ~±10% noise
   on the JP slice). Costs are flat per-market bp, not exact tax/commission.
9. **Headline is a snapshot.** `revalidation.json` reflects the 2026-07-11 run; as
   the pool grows daily it is **not** continuously re-validated (see §13).
10. **Two down years remain** (2018-style, 2022) — the −13% in 2022 is the honest
    cost of the edge; attempts to hedge/avoid them were tested and made things worse.

---

## 13. Improvement review (suggestions — NOT actioned)

Ranked by value. None applied without your go-ahead.

**High value**
- **Scheduled auto-re-validation.** The headline is a manual snapshot; as the pool
  grows, add a monthly CI job that re-runs `research_revalidate.py` and rewrites
  `revalidation.json`, so the displayed stats never drift from the live pool. Also
  auto-writes `PERFORMANCE.md`.
- **Point-in-time universe** to quantify (or remove) survivorship bias — the single
  biggest honesty gap. Needs historical index-membership data (a modest paid source
  or a hand-maintained delisting list); until then keep the §12.1 disclaimer prominent.
- **Server-side exact position tracking (optional).** If you ever want exact trail
  stops and cross-device sync, a tiny state file (committed on each recorded trade
  via the existing add-product token) would make exit alerts exact instead of
  approximate. Trade-off: positions leave your device.

**Medium value**
- **Suppress trade verbs on monitor-only cards** — show "Trend: up/down" instead of
  "BUY/WATCH" for FX/bonds/indices so the caption can't be misread.
- **Confidence/score calibration report** — periodically verify higher score buckets
  really realize higher forward returns (they should be monotonic; validate it).
- **Turn on Telegram** (already built) once you've reviewed everything — two repo
  secrets, then daily push of new signals + exit warnings.
- **Rebalance helper** — a Positions-tab widget showing current sleeve weights vs
  70/30 and the exact cash to move, instead of just a month-start note.

**Low value / housekeeping**
- Remove the ~30 dead tickers Yahoo can't serve (they just log warnings).
- Add a tiny unit test for `momentum_score`, `market_of`, and the backtest's
  no-look-ahead invariant, run in CI, so a future refactor can't silently break them.
- Consolidate the many `research_*.py` files into a `research/` folder (they're the
  audit trail; harmless but cluttered).

**Explicitly out of scope** (tested & rejected — don't re-open without new
information): shorts, option buying/selling, margin ≥1.25×, bonds/FX/leveraged-ETF
trading sleeves, per-product SMA optimization, real-time regime prediction. All
documented in `RESULTS.md` and `METHODOLOGY.md`.

---

*Long only. Not financial advice. Backtested framework with the honest caveats
above — validate on the specific instrument before risking capital.*
