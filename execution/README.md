# IB Execution Bot — setup & run (Oracle Cloud, no laptop)

The bot reads the live **D** signals from your dashboard, reconciles them against
your IB positions, and places entries / exits / trailing-stop sells. It runs
**once per day** as a cron on an always-on Oracle Cloud VM — your laptop stays off.

> **Boundary:** these are steps *you* perform. The bot connects to *your*
> logged-in IB Gateway over a local socket; no credentials ever live in the code,
> and I never run the live-money loop for you.

## Safety defaults (change only when you're ready)
| Setting | Default | Meaning |
|---|---|---|
| `IB_PORT` | `4002` | IB Gateway **paper**. Live = `4001`. |
| `CONFIRM_FIRST` | `1` | Prints each order, waits for your Enter. Set `0` for unattended. |
| `--dry` flag | off | With it, computes + prints, places nothing — and **writes nothing**: no `state.json`, no `data/bot_state.json`, no dashboard commit, no `/root/conid_cache.json` update (it is still read). Safe on the live VM to preview a run. Two caveats: it is *ignored* (with a warning) if you also pass `--publish-only`, and the zombie-gateway self-heal runs before it, so a dry run against a **wedged** gateway can still `pkill java` and force a 2FA re-login. |
| `MAX_ORDER_BASE` | 50000 | Per-order notional cap (base ccy). A **code** default since 2026-10-26 (was 20000): leave the env var unset on the VM. Every live run publishes the value in effect (below), so a leftover override shows. |
| `DAILY_LOSS_KILL` | 0.08 | The 8% kill switch's threshold - but the switch is **OFF** since 2026-10-26 (code flag `KILL_SWITCH` in `ib_bot.py` and `daily_signal.py`). Never "turn it off" with `DAILY_LOSS_KILL=0`: that blocks entries on every day that is not a new high. |
| `TARGET_POSITIONS` | 15 | Stock slots. Each entry is sized **free cash / free slots** since 2026-10-26. The 30% crypto sleeve is on hold and crypto entries are blocked. |

**The rules a live run actually traded with** (code defaults with any env
override applied) are published into `data/bot_state.json` as `"config"` at the
end of every live trading run: `max_order_base`, `target_positions`, `sizing`,
`kill_switch`, `fx_convert`, `own_hkd_funding`, `hk_enabled`, `park_mode`,
`market_filter`, `crypto_entries`, the code `commit`, and under `"env"` the raw value of every
trading-rule variable set in that run's environment (a fixed whitelist -
nothing else from the environment is ever published). `publish_web.py` carries
it over unchanged every hour; it never writes its own. Check it after a deploy:
`env` should hold only what you meant to set (e.g. `HK_ENABLED`, `IB_BACKEND`).

## One-time setup

### A. Interactive Brokers (your account)
1. Download **IB Gateway** (not TWS).
2. Config → API → Settings: enable "ActiveX and Socket Clients"; **uncheck
   Read-Only API**; note the port (4002 paper / 4001 live); trusted IP `127.0.0.1`.
3. Make sure your **paper account** is active. For crypto, enable the Paxos/
   ZeroHash permission. Subscribe to market data for US/HK/JP/EU only when going live.

### B. Oracle Cloud VM (always-free)
1. Create an **Always Free** VM (Ampere/ARM Ubuntu is plenty). Save the SSH key
   **it gives you** — that key is yours alone.
2. Install IB Gateway on the VM + **IBC** (auto-restarts Gateway on IB's daily
   reset). Log Gateway in once; approve the 2FA on IBKR Mobile.
3. Install the bot:
   ```bash
   sudo apt update && sudo apt install -y python3-pip
   git clone https://github.com/btctree/multi-product-signals.git
   cd multi-product-signals/execution
   pip3 install -r requirements.txt
   ```
   That is `execution/requirements.txt` (`ib_async`, `requests`) — the bot's own
   two dependencies. The repo root's `requirements.txt` is the signal engine's
   list and is only needed for the test export below. Neither is what CI
   installs: GitHub Actions installs the pinned, hashed `requirements-ci.txt`.

## Test it (paper, no risk)
```bash
python3 ib_bot.py --dry        # prints exactly what it WOULD do; places nothing, writes nothing
python3 ib_bot.py              # paper port + confirm-first: asks Enter per order
```
Watch it for a few days against paper; confirm the orders match the dashboard's
Actions/exit alerts.

## Run the test suites
`run_all_tests.py` runs every `test_*.py` under a guard that blocks and fails any
access to `/root`. On the desktop, from `execution/`:
```bash
PYTHONIOENCODING=utf-8 IB_BACKEND=web python run_all_tests.py      # ends: ALL N SUITES PASS
```
**Not from a checkout under `/root`.** On the VM the repo is
`/root/multi-product-signals`, where the guard blocks the suite files themselves,
and `ib_bot.STATE` / `FILLS_LEDGER` inside it are the live files - so the guard
is not loosened. `run_all_tests.py` and `testenv.isolate()` (which the suites
call before importing a bot module) stop there with exit code 2.
Run them from a clean export instead (`git archive`, so no untracked live state
comes along):
```bash
rm -rf /tmp/mps-test && mkdir -p /tmp/mps-test && git -C /root/multi-product-signals archive HEAD | tar -x -C /tmp/mps-test && cd /tmp/mps-test && PYTHONIOENCODING=utf-8 IB_BACKEND=web python3.11 execution/run_all_tests.py
```
`engine/test_data_fetch.py` also needs pandas and numpy under python3.11, which
the VM's bot environment does not install: add them in the export first
(`python3.11 -m pip install -r requirements.txt`), or treat the desktop run as
the gate for the engine suite.

## Go live (your decision, your hands)
```bash
export IB_PORT=4001            # live Gateway
export CONFIRM_FIRST=0         # unattended (or leave 1 to keep confirming)
export IB_BASE_CCY=HKD
```
Schedule it daily after the signals refresh (~00:30 UTC), e.g. crontab:
```
35 0 * * *  cd ~/multi-product-signals/execution && IB_PORT=4001 CONFIRM_FIRST=0 python3 ib_bot.py >> bot.log 2>&1
```
⚠️ **Check the crontab's time zone before you read that line as a UTC time.**
The live VM's root crontab sets `CRON_TZ=Europe/London`, so `35 0` is 23:35 UTC
in summer and 00:35 UTC in winter. See the dated hazard under "Rollback / kill
switches" below.

## What it does each run
1. Pulls `data.json` (today's D signals) + each held product's card.
2. **Kill switch: OFF** since 2026-10-26 (owner decision; `KILL_SWITCH = False`
   in `ib_bot.py` and in the digest). Entries are no longer gated on the NetLiq
   peak, no HALT row is written. The gate's code and `kill_switch.py` are kept,
   so switching it back on is one line in each file (see "Kill-switch
   behaviour" below for how it works when on).
2b. **Hand-bought holdings the owner asked the bot to manage** (`execution/adopt.json`,
   since 2026-10-23): the exit loop below manages only holdings in `state['map']`,
   which the bot writes when IT buys, so a share bought by hand in IB gets no
   cut-loss, trailing, regime or time exit - and still takes a slot. To hand one
   over, add a line to `adopt.json`: `entry` = the signal close and `stop` = the
   signal's cut-loss (what the bot's own entry records), `entry_date` = the FILL
   day (the backtest's bars=0-on-fill-day rule; the bot's own 23:35 UTC buys
   stamp the signal day, one weekday earlier), optionally `hw` = the highest
   close since. Commit
   and push; the next run's git pull brings it. `adopt_holdings` seeds it into
   `state.json` once, logs `ADOPT <sym>` and adds an ADOPT notice to History. It
   refuses (logged, nothing changed) a holding not held long, an IB symbol that
   already belongs to another instrument, unusable numbers (stop must be below
   entry, hw not below it, entry_date a past or present YYYY-MM-DD), and a lot
   the fills ledger dates more than 10 days from `entry_date` - so an old line
   can never adopt a later re-purchase. Once adopted it is skipped, so lines can
   stay - but only a record of that lot counts as adopted: `map`/`pos` are never
   removed after a sale, so an older lot's leftover record is replaced (logged).
   `--dry` seeds in memory only. MSFT (2 shares, 2026-10-23) was the first.
3. **Exits first**: sells any holding that closed below its 200-day average, hit
   its trailing stop (the bot keeps an exact high-water mark in `state.json`),
   or has been held **>= 60 trading bars** (`MAX_HOLD_BARS`, the validated
   engine's time stop, restored 2026-08-01; counts weekdays since entry — a few
   days early per quarter vs true exchange bars, never late; 0 disables). All
   exits are close-evaluated, executed market-at-next-open. Positions opened
   before the time stop existed get their true entry date from the fills ledger.
   A market whose daily bar is still in session is left for a later run
   (`market_decidable`, 2026-09-17): on a local weekday from its open until 90
   minutes after its close, that market's holdings are not evaluated at all (no
   stop ratchet, no exit) and its BUY signals are skipped, each with a log line.
   The weekday 09:00 UTC run therefore decides US and JP but defers EU and HK.
   No holiday calendar: a holiday counts as a trading day. The clock rule lives
   in `market_clock.py`, which the `/update` digest shares: a symbol whose market
   is still in session is listed there as "decided after the close", not as a
   SELL or BUY line. The data must be fresh too: a market is decided only when
   the build's `generated_at` (card first, else `data.json`) is at or after that
   market's last close + 90 min (`last_settled_close`). An older build defers the
   market exactly as the clock does (logged). Only when the newest build is more
   than 26 h old is one "signals are stale" alert queued per UTC day - a morning
   JP deferral to the 23:35 run is routine. A build with no `generated_at` is
   judged on the clock alone (logged). The `/update` and 23:40 digests mirror
   this build check too, as a label only: such a symbol is listed under
   "decided after the close" ("newest build started HH:MMZ, before its close
   settled"), one reason line per market, not as a SELL or BUY line.
4. **Entries** (rules of 2026-10-26, owner decisions):
   - **Order**: BUY and BUY/HOLD rows, by `(tier, -score)` - `tier` 0 for the E
     rows, 1 for the D3 rows of the D3+E1 model, absent = 0 - so D3 fills a
     slot only after every E candidate. Without a tier field this is the old
     score order. The digest uses the same filter and order. The History row
     names the kind when data.json carries it: "entry signal D3, score 72".
   - **Crypto is not traded**: a `-USD` symbol or market `CRYPTO` is skipped
     before any IB call - no slot, no state. PAXOS is not offered by IBKR HK,
     and the crypto sleeve is on hold. Exits and the phone SELL are untouched.
   - **Slots**: free = 15 − stock holdings − working stock BUYs (SGOV, the
     parking ETF, never counts).
   - **Sizing = free cash / free slots**, worked out ONCE per run after the
     exits, the working-order reservations and the earmark freeze. Free cash =
     every balance net of what working orders and this run's entries claim,
     valued at the sizing rate - but not GBP (the month-end pass-through), not
     the HKD balance (only the bot's own HKD, when its pocket is known), and a
     balance with no rate is left out (a DEBT with no rate places no entries).
     Any read error: no entries this run, exits and the save/publish as usual.
     First run on the 2026-10-26 book: HK$78,351 / 4 free = **HK$19,588 a
     slot** (was NetLiq/15 = HK$14,800). Under this rule the last free slot can
     take up to the cap - one HK$50k order is ~22% of NetLiq.
   - **Per-order cap HK$50,000** (`MAX_ORDER_BASE`), and each order is cut to
     what ONE conversion can pay (own cash + the single largest other balance).
   - **Value guard**: the entry is re-priced at IB's quote first; more than 10%
     from the signal price and it is skipped (one of the two is wrong).
     Otherwise it is sized, funded and reserved at the higher of the two, and
     the limit is set from that same quote, never above cap × 1.01.
   - **Funding**: cash in the order's currency first, then ONE conversion from
     the largest other balance (`FX_FUND_NONBASE`, on). Since 2026-10-26 the
     bot's **own** stamped HKD pocket is one of those sources for a
     USD/JPY/EUR order - bounded by min(pocket, HKD held − earmark), less what
     working HK buys need ×1.03, less HK$100, and only while the pocket is
     known, the earmark frozen and every working order reserved. It never
     sells the operator's HKD, and **never use `FX_CONVERT=1` for this**: that
     pays every shortfall from HKD with no pocket or earmark check. An **HK
     buy** spends HKD only from the known pocket; with the pocket unknown it
     spends no HKD at all and is funded wholly by conversion into HKD.
4b. **Cash parking (SHADOW)**: after the entries, one log line - `park (SHADOW -
   nothing placed): ... -> would BUY n SGOV` (or SELL, or nothing) - on what
   parking idle USD in the SGOV T-bill ETF would do: keep 3 slots' worth of
   spendable USD, park the rest above US$1,000, unpark below half the buffer.
   Nothing is placed. It goes live only by a code change (`PARK_MODE`), after
   about a week of these lines and a check that the account may hold a
   US-domiciled ETF. SGOV never takes a slot or a `state['map']` entry.
4c. **Market filter (SHADOW - log only, since 2026-10-26)**: the per-country
   rule the owner asked to test - no new buy in a market while its home index
   (US `^GSPC`, HK `^HSI`, JP `^N225`, EU `^STOXX50E`) closes below its 100-day
   simple average on its last finished bar; exits never gated. The engine
   publishes each index against its average as `data.json` `market_trend`
   (`engine/market_trend.py`, finished bars only, by `market_clock`'s settle
   rule). The bot (`MARKET_FILTER = "shadow"`) **sends every order exactly as
   it would without the filter** - the exit loop, slots, sizing, funding and
   placement never read it. It only:
   - logs the four markets once per run: `market filter (SHADOW - log only,
     ...): US above +0.26% (bar 2026-10-23) · HK below -6.60% ...`;
   - for each BUY it actually sends (after every existing check) in a market
     that is below: logs `shadow market filter: would have skipped <SYM> (<market>
     index <close> is <pct> below its 100-day average, bar <date>)`, adds
     `"shadow": "market filter would have skipped"` to that History row, and
     keeps `{time, symbol, market, pct}` in `state.json` `_market_filter_skips`
     (last 50);
   - publishes `"market_filter"` in `data/bot_state.json` - `mode`, the
     `markets` it read, `would_have_skipped` - which `--publish-only` and
     `publish_web.py` carry over like `"config"`. `--dry` logs, records nothing.

   A missing or malformed `market_trend` is "unknown" and tags nothing. The
   market is the row's own `market` field (as in the backtest), so CHF, DKK,
   SEK and NOK names are EU; crypto has no index. **Where to see it**: the
   Positions tab strip "Market filter · log only", the "filter would have
   skipped" label in History, the digest's Notes line, `/root/bot.log`, and
   the "Sim: you with market filter (shadow)" line on the Growth vs S&P 500
   card (since 2026-10-27, display only): the account with the skipped bot
   BUYs taken out, from this record plus `data/filter_backfill.json` for the
   bot BUYs before it (`engine/build_filter_backfill.py`, by hand; run it
   again once the first shadow run has published, and commit the file).
   Nothing in `execution/` reads either.
   **Switch off**: `MARKET_FILTER = "off"` in `ib_bot.py` (and in
   `daily_signal.py` for the digest line). **A live mode is not built** -
   actually skipping orders needs the owner's approval after the shadow record
   has been read; any other value, `"live"` included, runs as shadow.
5. Saves state; disconnects. A live run that dies on an exception still saves
   `state.json` before the error propagates (logged `!! run aborted`), so an
   order already sent keeps its map entry and stops; it publishes nothing.
   `--dry` writes nothing on that path either. The orders that run did send are
   carried in `state["_unpublished_activity"]` and published by the next live
   run, so they still reach the dashboard's History instead of vanishing.

## One lock: the bot and the phone poller cannot both sell

Two processes on the VM can send an order: the trading run (`ib_bot.py`) and
the phone-command poller (`ib_commands.py`, every 10 minutes). Cron starts them
in the same minute, and nothing serialized them — so a phone SELL and the bot's
own exit for the same holding could both be sent, each having read the order
book before the other's order existed. Both fill at the open and the account is
left short a position no exit path ever closes (board review 2026-09-21).

`execution/runlock.py` is now the single lock: one exclusive OS file lock on
`/root/mps_run.lock` (override with `MPS_LOCK_FILE`; `fcntl` on the VM,
`msvcrt` on a Windows checkout so the tests run).

| Process | Waits | If it cannot get the lock |
|---|---|---|
| `ib_bot.py` live run (`run_locked()`) | up to **600 s** (`RUN_LOCK_WAIT_S`) | logs, alerts, exits **3** (`RUN_LOCK_EXIT`), having read and sent nothing |
| `ib_commands.py` poller | tries once (`wait_s=0`) | skips the turn; the commands are **not** marked done, so the next poll picks them up |

The run takes the lock before the signals are read and before IB is connected,
and holds it until `run()` returns — exits, entries, `save_state` and
`publish_state` are all inside it. The poller deliberately does not queue behind
a run: it waits for the next poll, which then reads a book that already holds
the run's exits. `--dry` takes no lock at all, so a preview never holds up a
real run. The lock is released by the OS when a process dies, so a crashed run
cannot leave the account locked.

## Crash safety and what alerts you

- **Atomic writes.** `state.json` and `commands_done.json` are written to a
  temp file, flushed, fsynced and `os.replace`d. A power cut or a kill mid-write
  can no longer leave a truncated file that the next run refuses to parse.
- **A run that DIES now tells you.** Before this, a run that raised — the IB
  session failing at 23:35, say — left a traceback in `/root/bot.log` and
  nothing else, while the publisher and the digest used other endpoints and
  stayed green: the dashboard looked fresh and the digest still listed a SELL
  that was never sent. One alert per UTC hour.
- **A held position whose product card 404s** alerts once per symbol per day —
  without its card the bot has no price, ATR or SMA200 to judge the exit on.
- **The phone-command poller alerts** when it cannot read GitHub or its
  done-list, once per outage, after ~30 minutes (`POLL_ALERT_AFTER_S = 1800`).
  A brief blip heals on the next poll and stays quiet.
- **A refused push is retried.** `execution/gitpush.py` `push_with_retry`
  (pull `--rebase --autostash`, up to 3 pushes, aborts a conflicted rebase,
  clears a stranded one, never raises) is used by `ib_bot.publish_state` and
  `publish_web`. A push that still fails leaves the commit local and the next
  `:25` reset drops it — as it always did.
- **A dead VM is noticed from outside.** Every Telegram message comes from the
  VM, so a dead VM is silent. The hourly CI run checks the `updated` stamp in
  `data/bot_state.json` and warns (and messages Telegram) when it is more than
  3 h old — at least two missed publishes. While the VM is down, no stops and
  no exits run.

## Known refinements to verify on paper (flagged in code)
- **HK/JP board lots**: sizing rounds to whole shares; IB may reject non-lot HK
  orders. Check fills; add lot rounding if needed.
- **FX funding amount** is approximate when base ≠ the position currency — verify
  conversions on paper before trusting them live.
- **Crypto** entries are blocked in code since 2026-10-26 (PAXOS is not offered
  by IBKR HK; the crypto sleeve is on hold). The dashboard may still show BTC
  and ETH cards - the bot does not trade them.

## Rollback / kill switches (env vars on the VM, or code flags where marked)

⚠️ A rollback env var must be set in **every** site that runs `ib_bot.py`
without `--publish-only`, or the old behaviour persists in the sites you
missed. **Dump the real crontab first: `sudo crontab -l`** — that is the only
authority, and this file is a note about it.

What the 2026-09-22 Cloud Shell inspection found (not verifiable from the
repo): the root crontab runs under **`CRON_TZ=Europe/London`**, not UTC. The
daily trading line is `35 0 * * *` = **23:35 UTC** while the UK is on BST, and
the 09:00 and 11:00 UTC runs come from `/root/monday_catchup.sh` (`0 10` and
`0 12` London) rather than from separate crontab lines.

**Two statements in this repo disagree about how many sites there are.** An
earlier version of this file said three — the daily line, the catch-up script,
and a separate 09:00 weekday run — and `_excluded_cash()` in `ib_bot.py` still
says "the crontab has three ib_bot invocation sites". The 2026-09-22 reading
accounts for the 09:00 run as the catch-up script. Neither claim can be settled
from the repo, so **count them yourself from `crontab -l`** and set the env var
in every one you find.

✅ **Was a dated hazard, fixed 2026-09-25.** The crontab is written in London
time, so when UK clocks go back on **25 October 2026** the old `35 0` line would
have become **00:35 UTC = 09:35 Tokyo** — inside the Japanese session — and
`.T` holdings would have been deferred on weekdays by the `market_decidable`
rule. The trading line alone is now pinned to UTC:

    CRON_TZ=UTC
    35 23 * * * cd /root/multi-product-signals && ... ib_bot.py
    CRON_TZ=Europe/London

so it fires at 23:35 UTC all year. Everything else keeps London time, including
the `0 10` / `0 12` catch-up lines (09:00 and 11:00 UTC today, 10:00 and 12:00
UTC from 25 Oct) and the `40 23` digest (22:40 UTC today, 23:40 UTC from 25 Oct,
which puts it 5 minutes after the run rather than an hour before it).

⚠️ The catch-up script does **not** pull new code first. Only the hourly
`publish_web` line at `25 * * * *` does (`git fetch; git reset --hard
origin/main`), so a deploy reaches the VM at the next `:25`.

| Change | Revert with | Effect |
|---|---|---|
| Time stop (2026-08-01) | `MAX_HOLD_BARS=0` | positions never exit on age |
| Bot FX out of HKD (off since 2026-07-30) | **do not** set `FX_CONVERT=1` | it would pay every USD/JPY/EUR shortfall from HKD with no pocket or earmark check - a margin loan against the operator's transfer money. The bot's own HKD is funded through the narrow pocket path instead |
| The 2026-10-26 package (free-cash sizing, HK$50k cap, value guard, crypto block, own-HKD funding, kill switch off, SGOV shadow, published config) | Revert the whole 2026-10-26 release merge in one step - `git revert -m 1 <the release merge on main>`, pushed; the VM picks it up at the next `:25`. Reverting single bot commits is not clean: b419e921 and 312446ca touch the same `place()` call, and reverting only the bot also drops the tier sort and the crypto block while the engine keeps publishing D3 rows. To roll back the model alone, commit `MODEL = "E"` in `engine/config.py` instead | the old NetLiq/15 sizing, 20k cap and 8% switch. No `state.json` change was made, so old and new code read the same state |
| Kill switch off (2026-10-26) | code: `KILL_SWITCH = True` in `ib_bot.py` AND `daily_signal.py` | the 8% gate as described below - never via `DAILY_LOSS_KILL` |
| Own-HKD funding (2026-10-26) | code: `OWN_BASE_FUND = False` in `ib_bot.py` | HKD is never a funding source again |
| SGOV shadow log (2026-10-26) | code: `PARK_MODE = "off"` | the log line stops; nothing else changes |
| Market filter shadow log (2026-10-26) | code: `MARKET_FILTER = "off"` in `ib_bot.py` (and `daily_signal.py`) | no filter log lines, History tags or new record rows; the page hides the strip once a live run publishes `mode: "off"`. Orders were never affected, so nothing else changes |
| All trading | stop the gateway / remove the cron lines | exits stop too — see the kill-switch caveat |
| Kill-switch tripped by a deposit/withdrawal (only when switched on) | since 2026-10-23: enter it in `data/netliq_history.json` `flows`; the gate nets recorded flows out on its own (`kill_switch.py`). No `state.json` edit | entries resume at the next run (exits are never blocked) |

**Kill-switch behaviour (OFF since 2026-10-26; this is how it works when
`KILL_SWITCH = True`):** an 8% fall from the peak blocks **new entries
only** — exits (regime, trailing, time) always run (fixed 2026-08-06). The
trip is logged loudly and surfaced as a HALT row in the dashboard History and in
the digest. While it is off the run logs `kill switch: OFF`, the digest says
"Kill switch: off", `_peak_netliq` is no longer refreshed and
`_kill_peak_since` is inert.

**Since 2026-10-23 the peak is net of deposits and withdrawals**
(`execution/kill_switch.py`, shared by the bot and the digest). It is tracked in
trading wealth — NetLiq minus the net money paid in so far, from `flows` — so
a RECORDED deposit or withdrawal is neither a new high nor a fall. The old
cash-flow-naive peak (240,603, taken while the July deposit sat in the account,
before the 32,000 withdrawal) blocked entries on 28 Sep–1 Oct and from 15 Oct
while the real fall from the real high (230,661 on 13 Oct) never passed 5.4%.
Every run recomputes the peak from the published history; the stale
`_peak_netliq` is never read by the rule. A withdrawal you do NOT enter in `flows` still
reads as a fall, so keep `flows` current. After a GENUINE 8% fall, entries stay
blocked until the account recovers — deliberately: the pause after real losses
is intended. To override it after deciding to re-risk, set
`"_kill_peak_since": "YYYY-MM-DD"` in `state.json`: the peak is then taken only
from that day on. Editing `_peak_netliq` does nothing any more - it is only a
report of the peak used. Nothing is remembered between runs: the peak is
recomputed from the published history every time, so a flows entry made a day
late, or corrected, fixes itself at the next run. Date each flows entry the day
the money actually moved.

## Money in and out — two different routines

The dashboard's Calendar tab shows TRADING P&L, so any money that moves for a
reason other than trading has to be taken out of the comparison. There are two
ways to do that and **they are not interchangeable**. Pick by one question:
*is this money staying in the account?*

| | Money that STAYS (a real deposit or withdrawal) | Money PASSING THROUGH (arrives, waits, leaves) |
|---|---|---|
| Example | you top the account up to trade with; you take profit out for good | the monthly GBP deposit converted to HKD and withdrawn early the next month |
| What you do | add a `flows` entry by hand (below) | **earmark it** (below) |
| Put it in `flows`? | yes | **NO — never, neither leg** |
| Does the bot size against it? | yes | no |

### A. Money that stays — the `flows` list

`data/netliq_history.json` holds the daily NetLiq series (maintained
automatically by the hourly publish) plus a manual `flows` list. After a real
deposit or withdrawal, add an entry BY HAND:

    {"d": "YYYY-MM-DD", "amt": 23746, "note": "deposit GBP 2,250"}

(amount in HKD, positive = deposit, negative = withdrawal). Validate before
committing — a malformed file stops history updates (loudly) until repaired:

    python -m json.tool data/netliq_history.json

Then commit and push; the VM picks it up at the next :25 publish. The
kill-switch reads the same `flows`, so a recorded deposit or withdrawal needs no
separate reset (since 2026-10-23).

### B. Money passing through — the earmark, and NOT `flows`

⚠️ **Correction (2026-09-22).** Earlier operator notes said to enter a
pass-through in `flows` as well. **That instruction is wrong and must not be
followed.** Both publishers already write NetLiq *net* of the earmark
(`execution/earmark.py`, used identically by `ib_bot`, `publish_web` and the
digest), so a deposit earmarked on arrival never moves the published figure. A
`flows` entry on top of that subtracts the same money a second time: the
calendar reads a fake loss on the day it arrives and a fake gain on the day it
leaves. The 2026-09-19 experiment that added the day's earmark change back into
P&L had the mirror-image effect and was removed on 2026-09-22 — earmarked cash
now takes no part in the calendar arithmetic at all. It is published beside
NetLiq as `excluded_cash`; it is simply not P&L.

The routine, unchanged and correct:

1. The GBP deposit lands; convert it to HKD.
2. **Earmark it the same day** — Positions tab → the earmark box → the HKD
   amount → **Set**. The phone-command poller validates the author and writes
   `/root/excluded_cash`; it applies within ~10 minutes.
3. Withdraw it early the next month.
4. **Send 0** to clear the earmark once the money has left.
5. Enter **neither leg** in `flows`.

What the earmark actually does: the amount is excluded from net worth **and**
from what the bot may spend, so it cannot be spent on a position, and it
cannot ratchet the kill-switch peak either (the switch is off since 2026-10-26;
a real deposit or withdrawal needs only its `flows` entry: the kill-switch nets
recorded flows out on its own when it is on). It is
capped at the HKD actually held, so a stale marker can never exclude more money
than exists. **Clear it once the money leaves**, or NetLiq stays understated;
every run logs the amount loudly so that cannot go unnoticed. HKD that is held but not earmarked is flagged on the Positions
card ("is NOT earmarked, so the bot is sizing against it") rather than hidden —
if you see that warning and the money is a pass-through, earmark it. Since
2026-10-26 entries are sized from free cash, which leaves the HKD balance out
(only the bot's own stamped HKD counts) and the GBP balance too, and an HK buy
spends no HKD that is not provably the bot's - so an unmarked pot is no longer
spent or sized on; it still counts in NetLiq until earmarked.

*You bear the execution risk. Paper-verify first; the defaults keep you safe until
you deliberately switch them.*
