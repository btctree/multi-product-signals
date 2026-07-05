# Multi-Product Strategy Engine

Local, long-only, no-margin systematic engine that monitors ~140 major investment
products worldwide and reports the best current opportunities daily.
Built on the lessons in [INVESTMENT_PRODUCT_PLAYBOOK.md](INVESTMENT_PRODUCT_PLAYBOOK.md)
and [METHODOLOGY.md](METHODOLOGY.md).

## Mandate (user spec)

| Rule | Value |
|---|---|
| Direction | Long only, no margin, spot/cash |
| Structure | **Two sleeves, monthly rebalance**: DIP 70% (equity dip, 5 slots) + CRY 30% (crypto trend, 2 slots) |
| Size per position | sleeve cash / (sleeve slots − held) — compounds |
| Win-rate requirement | ≥ 60% (62.8% blended validated) |
| Max drawdown | ≤ 30% (−26.2% validated) |
| Validation | 11.2-year honest backtest, zero look-ahead, net of costs |
| Automation | Windows Task Scheduler `MultiProductDaily`, 08:00 daily; wakes from sleep on AC power, catches up missed runs on wake (see playbook Addendum A1) |

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
data/                 price cache, universe.json, positions.json
reports/              daily reports
```

*Research framework. Not financial advice.*
