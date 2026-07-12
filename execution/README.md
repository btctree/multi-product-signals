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
| `--dry` flag | off | With it, computes + prints, places nothing. |
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

## Test it (paper, no risk)
```bash
python3 ib_bot.py --dry        # prints exactly what it WOULD do, places nothing
python3 ib_bot.py              # paper port + confirm-first: asks Enter per order
```
Watch it for a few days against paper; confirm the orders match the dashboard's
Actions/exit alerts.

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

## What it does each run
1. Pulls `data.json` (today's D signals) + each held product's card.
2. **Kill-switch** check (NetLiq vs peak).
3. **Exits first**: sells any holding that closed below its 200-day average or hit
   its trailing stop (the bot keeps an exact high-water mark in `state.json`).
4. **Entries**: buys top-score BUY signals up to free slots, sizing NetLiq/15 per
   position, converting idle cash into the needed currency (largest-balance donor).
5. Saves state; disconnects.

## Known refinements to verify on paper (flagged in code)
- **HK/JP board lots**: sizing rounds to whole shares; IB may reject non-lot HK
  orders. Check fills; add lot rounding if needed.
- **FX funding amount** is approximate when base ≠ the position currency — verify
  conversions on paper before trusting them live.
- **Crypto** needs the IB crypto permission; otherwise those BUYs are skipped.

*You bear the execution risk. Paper-verify first; the defaults keep you safe until
you deliberately switch them.*
