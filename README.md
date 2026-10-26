# Multi-Product Strategy Engine

Local, long-only, no-margin systematic engine that monitors a worldwide product
pool and reports the best current opportunities daily. The "~140 products" this
file was written around is long gone: `data/universe.json` listed **995
tickers** on 2026-09-25, after the pool expansion to ~1,000.
Built on the lessons in [INVESTMENT_PRODUCT_PLAYBOOK.md](INVESTMENT_PRODUCT_PLAYBOOK.md)
and [METHODOLOGY.md](METHODOLOGY.md).

> ⚠️ **This file describes the SUPERSEDED X4/R2 generation.** The live product
> since 2026-07-11 is **config D** (`data/revalidation.json`, tag
> `D KILO-S60-15 full pool, score>60, DIP 13 + CRY 2 = 15 pos`), traded
> **automatically** by `execution/ib_bot.py` on an Oracle VM against a live IB
> account. Do not follow the "How to run" or "Daily workflow" sections below —
> they describe the old manual laptop workflow and would have you trading
> against the bot. Current operational truth:
> [execution/README.md](execution/README.md) and [WIKI.md](WIKI.md).
> Exit rules were re-validated 2026-08-01 (`data/exit_timing_test.json`): the
> 60-bar time stop was restored to the live bot; exits are close-evaluated and
> executed market-at-next-open, with no resting broker stops by design.

## Two headline measurements — do not mix them

Several backtests get quoted around this project. Each is honest about what it
measures; they measure different things, and only one is what the dashboard
header shows.

Since 2026-10-26 the live stock model is **D3+E1** (`MODEL = "D3E1"` in
`engine/config.py`): the D dip, with an earnings filter (E1) on US dips, plus
a second entry signal (D3, "held the 0.618 retracement") that only fills
slots the dips leave. The owner chose the header figure for it:

| | What the dashboard header shows (since 2026-10-26) | Same run, stocks only | Fallback: exit-timing arm E | Full-ruleset revalidation |
|---|---|---|---|---|
| Source | `data/model_headline.json` | its `stocks_only` block | `data/exit_timing_test.json`, arm `E live k-anchor + time stop 60` | `data/revalidation.json`, tag `D KILO-S60-15 …` |
| Model | D3+E1, 15 slots, HK$50k order cap, no kill switch | D3+E1 | D (dips only), 13 + 2 slots | D (dips only), 13 + 2 slots |
| Engine | research engine, the bot's own exit rules | same | the bot's own exit rules: stop tested on the **close**, sold market-at-next-open | stop taken the moment it is hit, intraday |
| Blend | 70% stocks + 30% crypto trend sleeve, monthly | 100% stocks | 70/30 | 70/30 |
| Window | 2015-05-04 → 2026-10-23 (11.5 y) | same | 11.2 y | 11.2 y |
| Win rate | 51.6% | 53.1% | 51.6% | 52.5% |
| CAGR | 31.8% | 22.5% | 30.1% | 30.8% |
| maxDD | −28.7% | −26.9% | −28.5% | −29.0% |
| HK$150k grows to | 3.55M | — | 2.86M | 3.04M |

**The header number is not what the bot can earn.** It is a research figure
with a 30% crypto trend sleeve, and that sleeve is **on hold**: the bot trades
stocks only (its PAXOS crypto path is blocked), so `headline.basis` reads
"research engine: 70% stocks + 30% crypto (crypto on hold) · 15 slots · HK$50k
cap". The stocks-only figure from the same run is 22.5% / −26.9%, and the
live-faithful simulation of the bot (2026-10-25) is lower again, roughly
12–17% a year for D3+E1 depending on the rules in force. The evidence for the
two additions is also thinner than the header suggests: D3 was a lead only in
its own study (about half its gain is filling idle slots), and E1 failed its
bootstrap; the simulated live gain over plain E was about +2.5–3 points a year.

How `engine/build_dashboard.py` picks the header: with `MODEL = "D3E1"` it
reads `data/model_headline.json` and writes it into `docs/data.json` as
`headline` (`win`, `cagr`, `maxdd`, `grows`, `years`, `basis`). If that file
is missing or malformed, or `MODEL = "E"`, it falls back to the old reading:
arm `E` of the exit-timing study with `"basis": "the bot's own exit rules"`,
or failing that the revalidation figures with `"basis": "validated engine"`.

Why arm E and the revalidation differ: the validated engine can take a stop
intraday because a backtest can see the whole bar. The live bot cannot — it
decides on a close and acts at the next open — so the exit-timing study re-ran
the same 11.2 years under the bot's own rules. The operator asked on
2026-09-21 for the header to show what actually runs.

Traps that follow from this:

- `model_headline.json` and `exit_timing_test.json` are **study results** that
  no build refreshes. Re-run the D3+E1 study when D3, E1, the slot count or
  the order cap change, and `engine/research_exit_timing.py` whenever config D
  or the revalidation changes, or the header will describe an old run.
- Rolling back to plain E is one line: `MODEL = "E"`. Every card is then built
  exactly as before, and the header returns to arm E.
- The `docs/data.json` committed in this repo is a stale build (generated
  2026-07-11) and still carries the old 52.5% / 30.8% / −29.0% headline. The
  hourly CI build overwrites it and deploys it as a Pages artifact, so the
  committed copy is never what the site serves. Do not read headline figures
  out of the checked-in file.

## Mandate (user spec — as originally written for X4)

| Rule | Value |
|---|---|
| Direction | Long only, no margin, spot/cash |
| Structure | **Two sleeves, monthly rebalance**: DIP 70% (equity dip, 5 slots) + CRY 30% (crypto trend, 2 slots) — *aspirational: the live bot runs ONE 15-slot pool sized NetLiq/15 with no sleeve split and has never traded crypto* |
| Size per position | sleeve cash / (sleeve slots − held) — compounds |
| Win-rate requirement | ≥ 60% (62.8% blended validated) — *not met by the shipped product: 52.5% full-ruleset, 51.6% under the bot's own exit rules; the gate was renegotiated down and finally dropped, see SYSTEMS_OVERFIT_REVIEW.md* |
| Max drawdown | ≤ 30% (−26.2% validated) — *config D models −29.0%; the bot's own rules model −28.5% with the 60-bar time stop (arm E) and −30.8% without it (arm C), which is what the live variant ran before the stop was restored* |
| Validation | 11.2-year honest backtest, zero look-ahead, net of costs |
| Automation | **Oracle Cloud VM, 24/7** (the `MultiProductDaily` Windows task is dead and unused) |

Shorts, options (buy & sell), bonds-as-engine, leveraged-ETF sleeve and margin
≥1.5× were each backtested/researched and **rejected with numbers** —
see [RESULTS.md](RESULTS.md) amendment 2026-07-04c for the full frontier.

## Universe (updated daily)

Top-50 US caps · Top-30 HK caps · Top-50 JP caps · S&P 500 · Hang Seng · Nikkei 225 ·
Gold · Silver · WTI Oil · BTC · ETH. `universe.py update_universe()` re-ranks each
equity market by live market cap and rotates newcomers in / fallen names out
(held positions are never force-rotated).

## Strategy — "Uptrend Dip" (LIVE config V8, validated 2026-07-03)

1. **Regime gate** (playbook §3): close > SMA200, SMA50 > SMA200, and positive
   90-day momentum. Everything else = stand aside.
2. **Entry**: RSI(3) < 15 — a deep short-term pullback *inside* the uptrend —
   with ATR ≥ 1.2% of price so the expected snap-back clears round-trip costs.
3. **Exit**: resting GTC SELL LIMIT at entry + 2.0×ATR (fills intraday);
   backup RSI(3) > 70 signal exit; 25-bar time stop.
4. **Cut-loss**: resting SELL STOP at max(3.5×ATR, −10%) below entry —
   catastrophe insurance, honestly gap-filled in the backtest.
5. **Ranking**: when signals exceed free slots, prefer strongest 90-day momentum.
6. **Monitor-only**: gold/silver/oil futures and the three indices are watched
   and reported but never take slots (continuous-futures roll artifacts make
   their mean-reversion backtests unreliable).

**11.2-year validated result (LIVE config X4 — 7 slots, target 2.0×ATR):**
win rate **68.4%**, profit factor 1.36, **CAGR 16.7%**, maxDD **−23.2%**,
**9 of 10 full years green** (sole red: 2018 at −2%). Every-year-green was
tested across 14 configs and is not honestly achievable — the closest
mechanisms fix one red year by breaking another (details in
[RESULTS.md](RESULTS.md)). Feasibility analysis of high return targets:
playbook Addendum A2.

Design note: the ≥70% win-rate mandate favours banking the mean-reversion
snap-back over riding trends (a documented trade-off vs playbook §1.7 —
avg win is modest; the regime gate + wide stop keeps the left tail short).

## Dependencies

`requirements.txt` is the short human-readable floors list, installed on the
**desktop** and by the VM's python3.11 test export. **GitHub Actions does not
use it**: both workflows install `requirements-ci.txt`, a fully pinned lock
with every sha256, via `pip install --require-hashes -r requirements-ci.txt`.
After changing a line in `requirements.txt`, regenerate that lock — its header
says how — or CI keeps running the old pins. `execution/requirements.txt`
(`ib_async`, `requests`) is separate and is what the VM's bot environment
installs.

## How to run

```
cd engine
python run_daily.py               # fetch fresh closes -> daily report
python run_daily.py --update      # + refresh universe by market cap
python run_daily.py --backtest    # + rerun the 10-year validation
python position_cli.py buy 0700.HK 512.0    # record a real fill
python position_cli.py sell 0700.HK 545.0   # record an exit
```

Daily workflow: run the report after market close → BUY listed candidates at
next open → immediately place the two resting GTC orders (SELL LIMIT at target,
SELL STOP at cut-loss) printed by `position_cli.py buy` → record exits when
either fills.

**Dead path, kept only for reading the old code.** `position_cli.py` writes
`data/positions.json`, and that file no longer exists in the repo: the live
Positions and History tabs are fed by `data/bot_state.json`, which the VM's
`execution/publish_web.py` publishes hourly. Recording a fill here would put a
second, hand-kept set of books beside the bot's and change nothing the
dashboard shows.

Reports land in `reports/daily_YYYY-MM-DD.md`: current holdings with
daily-updated target & cut-loss, best new BUY candidates with evidence,
and a per-market regime overview.

## Honesty rules baked in

- Signals use only data through each day's close; fills at the **next open**.
- Stops fill at the stop price, or at the open when the market gaps through.
- Per-market costs charged per side (US 10bp, HK 25bp incl. stamp, JP 15bp,
  crypto 20bp, commodities 15bp).
- Split-half win rates reported (robustness, not just the full-period number).
- FX drift HKD↔USD/JPY not modelled (stated assumption; HKD is USD-pegged).

## Files

```
engine/config.py      mandate + strategy parameters
engine/universe.py    monitoring list + daily market-cap refresh
engine/data_fetch.py  10y daily OHLCV cache (yfinance)
engine/indicators.py  SMA/RSI/ATR/momentum features
engine/strategy.py    entry/exit/stop/target/evidence logic
engine/backtest.py    honest portfolio backtest + metrics
engine/report.py      daily markdown report, position state
engine/run_daily.py   orchestrator
data/                 price cache, universe.json (positions.json: written only
                      by the dead position_cli path, absent from the repo)
reports/              daily reports
```

The live pipeline has moved on from this list. `engine/build_dashboard.py`
builds `docs/data.json` and the per-product cards from `engine/production.py`
(with the D3+E1 logic in `engine/model_d3e1.py`, and the SEC facts E1 reads
fetched by `engine/sec_fetch.py` in its own CI step); the bot, the publisher, the
digest, the phone-command poller and the alert outbox all live under
`execution/`. See [WIKI.md](WIKI.md) for the file-by-file walkthrough.

*Research framework. Not financial advice.*
