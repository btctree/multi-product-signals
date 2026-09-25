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
| `MAX_ORDER_BASE` | 20000 | Per-order notional cap (base ccy). |
| `DAILY_LOSS_KILL` | 0.08 | Halts new orders if NetLiq falls 8% below its peak. |
| `TARGET_POSITIONS` | 15 | 13 equity + 2 crypto. |

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
2. **Kill-switch** check (NetLiq vs peak).
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
4. **Entries**: buys top-score BUY signals up to free slots, sizing NetLiq/15 per
   position. The bot places no FX orders (`FX_CONVERT=0` default) — its only FX
   path converted out of HKD, which is blocked by mandate (and its ~USD 1,800
   sizes were under IDEALPRO's 25k minimum, rejected as odd lots every time).
   Cross-currency funding (EUR->USD, JPY->USD) still happens via IB's
   account-level auto-conversion; truly under-funded orders are rejected by IB,
   the intended fail-safe. Set `FX_CONVERT=1` to re-enable bot FX.
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
- **Crypto** needs the IB crypto permission; otherwise those BUYs are skipped.

## Rollback / kill switches (all are env vars on the VM)

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
| Bot FX conversion (2026-07-30) | `FX_CONVERT=1` | bot may convert out of HKD again |
| All trading | stop the gateway / remove the cron lines | exits stop too — see the kill-switch caveat |
| Kill-switch tripped by a deposit/withdrawal | reset `_peak_netliq` in `/root/multi-product-signals/execution/state.json` to the post-flow NetLiq high | entries resume next run (exits are never blocked once the 2026-08-06 fix is deployed) |

**Kill-switch behaviour (fixed 2026-08-06):** an 8% NetLiq drop from peak now
blocks **new entries only** — exits (regime, trailing, time) always run. The
peak is still cash-flow-naive: a withdrawal can trip the gate spuriously
(happened 2026-08-03..06, four silent frozen days under the old behaviour) and
then needs the manual `_peak_netliq` reset above; the trip is now logged loudly
and surfaced as a HALT row in the dashboard History. After a GENUINE crash the
same reset is what resumes entries — deliberately a human decision: the pause
after real losses is intended behaviour, so reset only when you have decided
to re-risk.

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

Then commit and push; the VM picks it up at the next :25 publish. Also remember
the kill-switch `_peak_netliq` reset above for the same event.

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
from the pool the bot sizes against, so it cannot be spent on a position, and
it cannot ratchet the kill-switch peak either — which is why an earmarked
pass-through needs no `_peak_netliq` reset, while a real deposit does. It is
capped at the HKD actually held, so a stale marker can never exclude more money
than exists. **Clear it once the money leaves**, or NetLiq stays understated;
every run logs the amount loudly so that cannot go unnoticed. HKD that is held but not earmarked is flagged on the Positions
card ("is NOT earmarked, so the bot is sizing against it") rather than hidden —
if you see that warning and the money is a pass-through, earmark it.

*You bear the execution risk. Paper-verify first; the defaults keep you safe until
you deliberately switch them.*
