"""Multi-Product execution bot for Interactive Brokers.

Reads the LIVE D signals from the deployed dashboard, reconciles them against your
IB positions, and places entries / exits / trailing-stop sells. Designed to run
ONCE per invocation (a daily cron on your Oracle VM), idempotently.

SAFE BY DEFAULT:
  * PORT defaults to 4002 (IB Gateway PAPER). Live is 4001 — you change it.
  * CONFIRM_FIRST=True  -> prints every intended order and waits for your Enter.
  * DRY_RUN via --dry    -> compute + print, place nothing.
  * Notional cap, max positions, and a daily-loss KILL-SWITCH are enforced.
You flip these to run unattended/live; nothing here connects to a live account
or moves money on its own until you set PORT=4001 and CONFIRM_FIRST=False.

Requires: pip install ib_async requests
Never put credentials in this file — the bot talks to your already-logged-in
IB Gateway over the local socket.
"""
import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

from broker import IB, LimitOrder, MarketOrder, Forex
from contracts import to_ib, currency_of

# ---------------- config (env-overridable) ----------------
HOST = os.environ.get("IB_HOST", "127.0.0.1")
PORT = int(os.environ.get("IB_PORT", "4002"))          # 4002 paper / 4001 live
CLIENT_ID = int(os.environ.get("IB_CLIENT_ID", "17"))
BASE_CCY = os.environ.get("IB_BASE_CCY", "HKD")
SIGNALS_URL = os.environ.get(
    "SIGNALS_URL", "https://btctree.github.io/multi-product-signals/data.json")
PRODUCTS_URL = SIGNALS_URL.rsplit("/", 1)[0] + "/products/"
TARGET_POSITIONS = int(os.environ.get("TARGET_POSITIONS", "15"))
MAX_ORDER_BASE = float(os.environ.get("MAX_ORDER_BASE", "20000"))   # per-order cap
DAILY_LOSS_KILL = float(os.environ.get("DAILY_LOSS_KILL", "0.08"))  # 8% of NetLiq
CONFIRM_FIRST = os.environ.get("CONFIRM_FIRST", "1") != "0"
LIMIT_BUFFER = float(os.environ.get("LIMIT_BUFFER", "0.005"))       # marketable limit
# Off by default. The bot's own conversions ALWAYS source from BASE_CCY (HKD) —
# this gate blocks exactly those (mandate: never exchange out of HKD). Cross-ccy
# funding (EUR->USD, JPY->USD, EUR->JPY) is IB's account-level auto-conversion
# and is unaffected. Bot FX was dead anyway: ~USD 1,800 orders sit under
# IDEALPRO's 25k minimum and every one since 23 Jul was rejected as an odd lot.
FX_CONVERT = os.environ.get("FX_CONVERT", "0") != "0"
# Fund a foreign purchase from cash held in OTHER non-base currencies. This is
# NOT the same switch as FX_CONVERT and does not weaken it: FX_CONVERT governs
# selling BASE_CCY (HKD), which stays off because the HKD balance is the
# operator's transfer funding. This path may never sell HKD - fund_from_nonbase
# excludes it as a source and _fx_order refuses it outright.
FX_FUND_NONBASE = os.environ.get("FX_FUND_NONBASE", "1") != "0"
# Time stop: exit any position held >= this many trading bars (sell at next
# open, like every other exit). 60 is the VALIDATED engine default the live
# bot had silently omitted (engine_rr.py:30 max_hold=60) — restoring it was
# board-reviewed 2026-08-01: measured twice (+0.8/+0.9pp CAGR, 2.0/2.3pp
# shallower maxDD), it re-enters the -30% DD mandate. 0 disables.
MAX_HOLD_BARS = int(os.environ.get("MAX_HOLD_BARS", "60"))
STATE = Path(__file__).with_name("state.json")
FILLS_LEDGER = Path(__file__).resolve().parent.parent / "data" / "fills_ledger.jsonl"


def log(*a):
    print("[bot]", *a, flush=True)


def bars_held(entry_date):
    """Weekdays (Mon-Fri) from entry_date to today, exclusive of entry day.
    Under the 00:35 UTC cron the seeded entry_date IS the fill calendar day
    (matches the backtest's bars=0 on fill day); the conservative bias comes
    from counting the not-yet-closed run day plus exchange holidays — worst
    case ~1.5 trading-weeks EARLY over a full 60-bar hold (HK/JP holiday
    windows), never late. Deterministic and restart-safe: recomputed from the
    stored date each run, no counter to drift on missed runs or restores."""
    from datetime import date, timedelta
    try:
        y, m, d = (int(x) for x in str(entry_date)[:10].split("-"))
        start = date(y, m, d)
    except Exception:
        return 0
    today = date.today()
    if today <= start:
        return 0
    n, cur = 0, start + timedelta(days=1)
    while cur <= today:
        if cur.weekday() < 5:
            n += 1
        cur += timedelta(days=1)
    return n


def ledger_entry_date(sym):
    """Entry date of the CURRENT lot of sym from data/fills_ledger.jsonl:
    the earliest stock BUY fill AFTER the last SELL (a prior round trip must
    not resurrect an old date and fire an immediate time exit on a young
    re-entry). Used to backfill positions opened before the time stop existed
    (hand-over from the running deploy; all 15 current holdings verified).
    Missing/unparseable ledger -> None (caller falls back to today, which only
    ever DELAYS a time exit, never forces one)."""
    try:
        buys, last_sell = [], None
        with open(FILLS_LEDGER, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue              # one bad line must not mask good fills
                if r.get("symbol") != sym or r.get("sec_type") == "CASH":
                    continue
                d = str(r.get("date") or "")[:10]
                if len(d) != 10:
                    continue
                if r.get("side") in ("BOT", "BUY"):
                    buys.append(d)
                elif r.get("side") in ("SLD", "SELL"):
                    if last_sell is None or d > last_sell:
                        last_sell = d
        live = [d for d in buys if last_sell is None or d > last_sell]
        # partial trims (sell while still holding) push the date LATER ->
        # the time stop can only fire earlier, never later — conservative
        return min(live) if live else (min(buys) if buys else None)
    except Exception:
        return None


def safe_name(sym):
    return sym.replace("^", "_IDX_").replace("=", "_EQ_").replace(".", "_")


def get_json(url):
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.load(r)


def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_state(s):
    STATE.write_text(json.dumps(s, indent=1))


# ---------------- IB helpers ----------------
def _excluded_cash():
    """Cash sitting in the account that is NOT trading capital - money parked
    on its way somewhere else. Subtracted from NetLiq everywhere, so it cannot
    inflate position sizing, ratchet the kill-switch peak, or show up in the
    dashboard's P&L calendar as profit it never earned.

    Set via /root/excluded_cash (one number, base currency) or EXCLUDED_CASH.
    ONE place on purpose: the crontab has three ib_bot invocation sites and a
    per-site env var would inevitably drift between them.

    CLEAR IT once the money actually leaves, or NetLiq stays understated - the
    amount is logged loudly on every run precisely so that cannot go unnoticed.
    """
    raw = os.environ.get("EXCLUDED_CASH")
    if raw is None:
        try:
            raw = Path("/root/excluded_cash").read_text().strip()
        except Exception:
            return 0.0
    try:
        return float(str(raw).strip() or 0)
    except ValueError:
        log("  !! excluded-cash value %r is not a number - ignoring" % raw)
        return 0.0


def net_liq(ib):
    nl = 0.0
    vals = ib.accountValues()          # one pass: the cap below reuses it
    for v in vals:
        if v.tag == "NetLiquidation" and v.currency == BASE_CCY:
            nl = float(v.value)
            break
    else:
        for v in vals:
            if v.tag == "NetLiquidation":
                nl = float(v.value)
                break
    exc = _excluded_cash()
    if exc:
        # Earmarked money can only be excluded while it is STILL SITTING HERE as
        # cash. Capping at the live base-currency balance retires the marker by
        # itself the moment the money leaves, so a stale marker cannot understate
        # NetLiq. That is not hypothetical: on 2026-08-31 the 18,559 was
        # withdrawn while the marker stayed set, NetLiq read 191,875 instead of
        # 210,434, and the 8% kill switch tripped on a 9% drawdown that never
        # happened - freezing entries exactly as the August withdrawal did.
        held = 0.0
        for v in vals:
            if v.tag == "CashBalance" and v.currency == BASE_CCY:
                held = max(0.0, float(v.value))
                break
        if held < exc:
            log(f"  earmarked cash marker is {exc:,.0f} but only {held:,.0f} "
                f"{BASE_CCY} cash is held - the money has moved; capping")
        exc = min(exc, held)
    if exc:
        log(f"  excluding {exc:,.0f} {BASE_CCY} earmarked cash "
            f"(NetLiq {nl:,.0f} -> {nl - exc:,.0f})")
        nl -= exc
    return nl


def cash_by_ccy(ib):
    out = {}
    for v in ib.accountValues():
        if v.tag == "CashBalance" and v.currency and v.currency != "BASE":
            out[v.currency] = float(v.value)
    return out


def held_positions(ib):
    """symbol(local) -> (position obj, qty)."""
    out = {}
    for p in ib.positions():
        if p.position != 0:
            out[p.contract.symbol] = (p, p.position)
    return out


def confirm(msg):
    if not CONFIRM_FIRST:
        return True
    try:
        return input(f"  CONFIRM {msg}  [y/N] ").strip().lower() == "y"
    except EOFError:
        log("no TTY for confirm -> skipping (set CONFIRM_FIRST=0 to auto-run)")
        return False


PLACED = []            # orders actually transmitted this run (for the dashboard)
_TICK_CACHE = {}


def min_tick(ib, contract):
    """The venue's minimum price increment (IB rejects limits that violate it,
    Error 110 — e.g. US stocks tick $0.01, JPY stocks tick ¥1)."""
    key = getattr(contract, "conId", 0) or contract.symbol
    if key in _TICK_CACHE:
        return _TICK_CACHE[key]
    tick = 0.01
    try:
        cds = ib.reqContractDetails(contract)
        if cds and cds[0].minTick:
            tick = float(cds[0].minTick)
    except Exception:
        pass
    _TICK_CACHE[key] = tick
    return tick


def lot_size(ib, contract):
    """Exchange board lot (TSE = 100 shares; HK varies). Uses IB's sizeIncrement
    when available, else a JPY default of 100."""
    key = ("lot", getattr(contract, "conId", 0) or contract.symbol)
    if key in _TICK_CACHE:
        return _TICK_CACHE[key]
    lot = 1
    try:
        cds = ib.reqContractDetails(contract)
        if cds:
            ms = getattr(cds[0], "sizeIncrement", None) or getattr(cds[0], "minSize", None)
            if ms and ms == ms and float(ms) >= 1:
                lot = int(float(ms))
    except Exception:
        pass
    if lot <= 1 and contract.currency == "JPY":
        lot = 100
    _TICK_CACHE[key] = lot
    return lot


def jp_tick(price):
    """TSE price-step table (coarse/non-TOPIX500 grid — always exchange-valid;
    IB's minTick for JP stocks is often wrong, e.g. 0.1 at ¥24,700)."""
    for lim, t in ((3000, 1), (5000, 5), (30000, 10), (50000, 50),
                   (300000, 100), (500000, 500), (3000000, 1000)):
        if price <= lim:
            return t
    return 5000


def snap_to_tick(raw, tick):
    lim = round(raw / tick) * tick
    if tick >= 1:
        return int(round(lim))
    return round(lim, 2 if tick >= 0.01 else 4 if tick >= 0.0001 else 6)


def live_base_price(ib, contract, fallback):
    """IB's own view of the price (last trade, else prior close). Guards limit
    prices against stale signal cards — Yahoo's EU end-of-day bars can lag past
    midnight, seen live 24 Jul: MC sell limit priced off a day-old close sat 4%
    above the market and could never fill."""
    try:
        [tk] = ib.reqTickers(contract)
        for v in (tk.last, tk.close, tk.marketPrice()):
            if v and v == v and v > 0:
                return float(v)
    except Exception:
        pass
    return fallback


def place(ib, contract, action, qty, price, dry, reason="", mkt=False):
    if qty <= 0:
        return
    base = live_base_price(ib, contract, price)
    if price > 0 and abs(base - price) / price > 0.01:
        log(f"  signal price {price} stale vs IB quote {base} — re-based")
        price = base
    # Regime-break exits go market-at-open: the backtest's exit price IS the
    # next open, and a close-anchored sell limit misses on any down-gap
    # (MC failed to exit two days running before this).
    if mkt and contract.secType == "STK":
        log(f"{action} {qty} {contract.symbol} @ MKT-open ({contract.currency})")
        if dry or not confirm(f"{action} {qty} {contract.symbol} @ MKT"):
            return
        trade = ib.placeOrder(contract, MarketOrder(action, qty, tif="DAY"))
        ib.sleep(3)
        status, err = _order_verdict(trade)
        status = _stock_status(status)
        if status == "REJECTED":
            log(f"  !! ORDER REJECTED: {action} {qty} {contract.symbol} — {err[:140]}")
        from datetime import datetime, timezone
        PLACED.append({"time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                       "action": action, "qty": qty, "symbol": contract.symbol,
                       "limit": "MKT-open", "ccy": contract.currency,
                       "reason": reason, "status": status, "error": err[:160]})
        return status
    raw = price * (1 + LIMIT_BUFFER) if action == "BUY" else price * (1 - LIMIT_BUFFER)
    tick = min_tick(ib, contract)
    if contract.currency == "JPY":
        tick = max(tick, jp_tick(raw))
    lim = snap_to_tick(raw, tick)
    log(f"{action} {qty} {contract.symbol} @ ~{lim} ({contract.currency})")
    if dry or not confirm(f"{action} {qty} {contract.symbol} @ {lim}"):
        return
    # place; if the venue rejects the price step (Error 110), self-heal by
    # retrying with the next coarser tick from the ladder (covers venues where
    # IB's minTick metadata is wrong — seen on TSE and Euronext).
    ladder = [0.0001, 0.001, 0.01, 0.05, 0.1, 0.2, 0.5, 1, 5, 10, 50, 100, 500, 1000]
    status, err = "", ""
    # IBKR's percentage-constraint warning may be confirmed only when `lim` is
    # still the price OUR OWN policy produced: reference * (1 + LIMIT_BUFFER),
    # plus one tick for the snap. The bound is LIMIT_BUFFER rather than an
    # invented number because that is the tolerance this system actually has -
    # the backtest models slip as COST_BP per side (US 10bp, JP 15bp, HK 25bp),
    # and the 50bp buffer already exceeds every one of them. Anything wider
    # than the buffer means something re-priced the order, and that is exactly
    # when a price warning should stop it.
    #
    # Safe for a BUY because the limit is a CEILING: a stale reference costs a
    # missed fill or a cheaper one, never a worse price than `lim`. Never for a
    # SELL, where the limit is a FLOOR and staleness sells cheap.
    allow_cap = (action == "BUY" and price > 0
                 and lim <= price * (1.0 + LIMIT_BUFFER) + tick)
    if action == "BUY" and not allow_cap:
        log(f"  note: limit {lim} exceeds the {LIMIT_BUFFER:.2%} buffer over "
            f"{price} — a price-cap warning will be declined")
    for attempt in range(6):
        order = LimitOrder(action, qty, lim, tif="DAY")
        order.allow_price_cap = allow_cap
        trade = ib.placeOrder(contract, order)
        ib.sleep(3)                   # give IB a moment to accept or reject
        status, err = _order_verdict(trade)
        if status != "REJECTED" or "110" not in err:
            break
        coarser = [t for t in ladder if t > tick]
        if not coarser:
            break
        tick = coarser[0]
        lim = snap_to_tick(raw, tick)
        log(f"  retrying with coarser tick {tick} -> {lim}")
    status = _stock_status(status)
    if status == "REJECTED":
        log(f"  !! ORDER REJECTED: {action} {qty} {contract.symbol} — {err[:140]}")
    from datetime import datetime, timezone
    PLACED.append({"time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                   "action": action, "qty": qty, "symbol": contract.symbol,
                   "limit": lim, "ccy": contract.currency, "reason": reason,
                   "status": status, "error": err[:160]})
    return status


def _stock_status(status):
    """'sent' for a stock order whose outcome is not yet known.

    place() reads the verdict three seconds after transmitting, and the runs
    that matter place DAY orders BEFORE their session opens - so "not filled
    yet" is the expected answer, not news. Recording that as 'pending' would
    stamp almost every stock order pending FOREVER, because the activity log is
    append-only (publish_state does prev.activity + PLACED) and no later run
    revises an old row. 'sent' claims only what is actually known: it reached
    IB, and Positions is where you see what filled. FX keeps 'pending', because
    there _fx_order really does wait for the fill and act on the answer.
    """
    return "sent" if status == "pending" else status


def _order_verdict(trade):
    """('filled'|'pending'|'REJECTED', error_message) for a just-placed trade.

    'pending' is NOT success. IB accepts an order long before it fills - and
    accepts it just as willingly into a market that is CLOSED, which is how
    three FX conversions and a TSE buy were all recorded 'ok' at 23:35 UTC on
    2026-09-04 (08:35 JST Saturday) while the fills ledger recorded none of
    them. Anything that needs the money to have actually MOVED - FX funding -
    must require 'filled'; the dashboard shows the rest as pending rather than
    implying an execution that has not happened.
    """
    try:
        st = trade.orderStatus.status
        if st in ("Cancelled", "ApiCancelled", "Inactive"):
            msgs = [e.message for e in trade.log if e.message]
            return "REJECTED", (msgs[-1] if msgs else st).strip()
        return ("filled" if st == "Filled" else "pending"), ""
    except Exception:
        # Unreadable status reports pending, never filled: cash that cannot be
        # confirmed must not be spent as though it had arrived.
        return "pending", ""


# ---------------- FX rates & funding ----------------
# IB (IDEALPRO) only quotes certain pairs directly (USDHKD yes, JPYHKD no).
# We resolve any A->B rate by trying the direct pair, its inverse, then a USD
# cross, and we FUND any currency the same way — converting through USD when no
# direct pair exists — instead of skipping the trade.
_RATE_CACHE = {}


def _pair_mid(ib, pair):
    """(Forex, midpoint) for a 6-char pair, or (None, None) if not quotable."""
    try:
        fx = Forex(pair)
        if not ib.qualifyContracts(fx):
            return None, None
        [t] = ib.reqTickers(fx)
        m = t.midpoint()
        if not m or m != m:                      # NaN -> fall back to last close
            m = t.close
        if not m or m != m or m <= 0:
            return None, None
        return fx, m
    except Exception:
        return None, None


def fx_rate(ib, a, b):
    """Units of <b> per 1 unit of <a> (0.0 if unobtainable). Cached per run."""
    if a == b:
        return 1.0
    if (a, b) in _RATE_CACHE:
        return _RATE_CACHE[(a, b)]
    r = 0.0
    if _pair_mid(ib, a + b)[1]:                   # Forex(ab) quotes b per a
        r = _pair_mid(ib, a + b)[1]
    elif _pair_mid(ib, b + a)[1]:                 # Forex(ba) quotes a per b -> invert
        r = 1.0 / _pair_mid(ib, b + a)[1]
    elif a != "USD" and b != "USD":               # cross via USD
        ra, rb = fx_rate(ib, a, "USD"), fx_rate(ib, "USD", b)
        r = ra * rb if (ra and rb) else 0.0
    _RATE_CACHE[(a, b)] = r
    return r


# An order still live at IB. openTrades() returns the whole day's book - filled
# and cancelled rows included - so every consumer must filter on this or it will
# read history as "in flight". Defined once because _fx_already_working was
# written without it while open_syms and pending_buys had it, and that
# divergence is exactly what board review caught.
_WORKING_STATUS = ("PendingSubmit", "PreSubmitted", "Submitted", "ApiPending")

# Pairs (by conid) and target currencies with a conversion already in flight,
# plus the source cash those unfilled orders have already spoken for.
# Cleared at the top of run() so nothing leaks between runs in one process.
_FX_PENDING = set()
_FX_PENDING_CCY = set()
_FX_COMMITTED = {}


def _fx_already_working(ib, conid, sym):
    """True when a conversion on this pair is already in flight.

    Two sources, because either alone leaves a hole:
      - _FX_PENDING catches one placed moments ago in THIS run.
      - openTrades() catches one left working by an EARLIER run. placeOrder
        invalidates that cache, so it re-reads rather than serving a stale book.

    Without this the bot reconverts on every run while the first order sits
    unfilled: the cash never arrives, so the shortfall never closes. Five runs
    separate Friday's 23:35 from Monday's Tokyo open (23:35 and 09:00 daily),
    every one of them able to stack another conversion. The equivalent guard
    for stocks is pending_buys, which excludes secType CASH - correctly, since
    an FX order should not consume a position slot, but the effect was that FX
    had no duplicate check at all.
    """
    if conid and conid in _FX_PENDING:
        return True
    try:
        for t in ib.openTrades():
            if getattr(t.contract, "secType", "") != "CASH":
                continue
            if getattr(t.orderStatus, "status", "") not in _WORKING_STATUS:
                continue          # Filled/Cancelled/Inactive are history, not flight
            if conid and str(getattr(t.contract, "conId", "")) == str(conid):
                return True
            # Only an EXACT pair match. An earlier version also matched the bare
            # base currency, but openTrades rows carry the base as their symbol
            # ("USD"), so that collapsed to "any working USD pair" and one
            # USD.JPY order would have blocked USD.CHF, USD.CAD and USD.SGD too.
            # conid is present on real order rows, so this loses nothing.
            tsym = str(getattr(t.contract, "symbol", "")).upper()
            if tsym and sym and tsym == sym.upper():
                return True
    except Exception as e:
        # openTrades raises rather than reporting an empty book. Funding is
        # optional; an unreadable book must not read as "nothing is working".
        log(f"  ! cannot read working orders ({str(e)[:80]}) — assuming an FX "
            f"order is in flight, not converting")
        return True
    return False


def _fx_order(ib, base_ccy, quote_ccy, side, qty, dry, why, target=None,
              src_ccy=None, src_qty=0.0):
    """Market FX order on Forex(base+quote): side BUY/SELL of `qty` base units.

    Returns True only when the conversion FILLED. An accepted-but-unfilled
    order has moved no money, and the caller must not size a stock order
    against currency that has not arrived.
    """
    qty = int(round(qty))
    if qty <= 0:
        return True
    # Second, independent guard on selling BASE_CCY. fund_from_nonbase already
    # excludes it as a source; this refuses at the point of order construction
    # so that editing the caller cannot quietly re-enable it. Only lifted when
    # FX_CONVERT is explicitly on, which is the documented switch for letting
    # the bot trade HKD - off since 2026-07-30 because the HKD balance is the
    # operator's transfer funding.
    sells_base = ((side == "SELL" and base_ccy == BASE_CCY)
                  or (side == "BUY" and quote_ccy == BASE_CCY))
    if sells_base and not FX_CONVERT:
        log(f"  !! refusing to sell {BASE_CCY} ({side} {base_ccy}.{quote_ccy}, {why})"
            f" — FX_CONVERT is off")
        return False
    fx = Forex(base_ccy + quote_ccy)
    if not ib.qualifyContracts(fx):
        return False
    conid = getattr(fx, "conId", 0)
    pair = f"{base_ccy}.{quote_ccy}"
    if _fx_already_working(ib, conid, pair):
        # Mark the TARGET too. Without this the block is defeated: _fx_order
        # returns False before ever reaching the code that records the pending
        # currency, so fund_from_nonbase falls through and converts a second
        # source (EUR, GBP) for the same shortfall - the exact duplication this
        # guard exists to stop, just from a different balance.
        if target:
            _FX_PENDING_CCY.add(target)
        log(f"  FX {pair} already working — not converting again ({why})")
        return False
    log(f"  FX {side} {qty} {pair} ({why})")
    if dry or not confirm(f"FX {side} {qty} {base_ccy}{quote_ccy}"):
        # `not dry or True` used to sit here: a tautology, True on both branches.
        # It was harmless while the caller ignored this value, but the return now
        # means "the money arrived", so a DECLINED conversion was reporting
        # success and the stock order went ahead against currency the operator
        # had just refused to buy. In dry, treat as satisfied; a live decline is
        # a refusal.
        return bool(dry)
    trade = ib.placeOrder(fx, MarketOrder(side, qty))
    # Give a fill longer than the 3s a stock order gets: the caller now treats
    # "not filled" as "not funded" and skips the entry, so a slow status report
    # would cost a trade outright. A spot market order that is going to fill
    # does so in well under a second, making this free on the happy path.
    status, err = "pending", ""
    for _ in range(5):
        ib.sleep(2)
        status, err = _order_verdict(trade)
        if status != "pending":
            break
    if status == "REJECTED":
        log(f"  !! FX ORDER REJECTED: {side} {qty} {pair} — {err[:140]}")
    elif status != "filled":
        # Accepted but unfilled: the venue is shut, or IB is slow. Register the
        # pair so nothing converts it again, and report failure so no stock
        # order is sized against money that has not landed.
        if conid:
            _FX_PENDING.add(conid)
        if target:
            _FX_PENDING_CCY.add(target)
        if src_ccy and src_qty > 0:
            # Reserve the cash this unfilled order will consume. cash_by_ccy
            # reads the LIVE balance, which still shows money the order has
            # already spoken for, so a second target currency in the same run
            # would otherwise spend the same USD twice.
            _FX_COMMITTED[src_ccy] = _FX_COMMITTED.get(src_ccy, 0.0) + src_qty
        log(f"  FX {pair} accepted but NOT filled — treating as unfunded; the "
            f"order stays working and the entry waits for the cash")
    from datetime import datetime, timezone
    PLACED.append({"time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                   "action": f"FX {side}", "qty": qty,
                   "symbol": f"{base_ccy}.{quote_ccy}", "limit": "MKT",
                   "ccy": quote_ccy, "reason": why,
                   "status": status, "error": err[:160]})
    return status == "filled"


def convert_into(ib, ccy, need_ccy, dry):
    """Acquire ~need_ccy units of <ccy>, paying from BASE_CCY. Uses a direct pair
    if one exists, else routes through USD (BASE->USD->ccy). Returns False only
    when NO path exists (caller then leaves the order to under-fund = safe)."""
    if ccy == BASE_CCY or need_ccy <= 0:
        return True
    if _pair_mid(ib, ccy + BASE_CCY)[1]:          # BUY ccy, pay BASE  (e.g. USDHKD)
        return _fx_order(ib, ccy, BASE_CCY, "BUY", need_ccy, dry, f"{BASE_CCY}->{ccy}")
    inv = _pair_mid(ib, BASE_CCY + ccy)[1]        # pair is BASE/ccy -> SELL BASE for ccy
    if inv:
        return _fx_order(ib, BASE_CCY, ccy, "SELL", need_ccy / inv, dry, f"{BASE_CCY}->{ccy}")
    # no direct pair (e.g. JPY/HKD): go BASE -> USD -> ccy
    usd_per_ccy = fx_rate(ib, ccy, "USD")
    if not usd_per_ccy:
        return False
    need_usd = need_ccy * usd_per_ccy
    if not _fx_order(ib, "USD", BASE_CCY, "BUY", need_usd * 1.02, dry, f"{BASE_CCY}->USD"):
        return False
    if _pair_mid(ib, ccy + "USD")[1]:             # BUY ccy paying USD
        return _fx_order(ib, ccy, "USD", "BUY", need_ccy, dry, f"USD->{ccy}")
    if _pair_mid(ib, "USD" + ccy)[1]:             # pair USD/ccy (e.g. USDJPY) -> SELL USD
        return _fx_order(ib, "USD", ccy, "SELL", need_usd, dry, f"USD->{ccy}")
    return False


def fund_from_nonbase(ib, ccy, short_ccy, dry):
    """Buy ~short_ccy of <ccy> using cash held in ANY other non-base currency.

    Never sells BASE_CCY. The operator's HKD balance is earmarked as transfer
    funding, so it is excluded as a source here AND refused inside _fx_order -
    two independent guards, because one of them being edited away must not
    silently re-enable selling it.

    Sources are tried largest first, so the single balance most able to cover
    the shortfall is used rather than fragmenting across several conversions.
    Returns True only if a conversion was actually placed.
    """
    balances = cash_by_ccy(ib)
    sources = [(c, amt - _FX_COMMITTED.get(c, 0.0)) for c, amt in balances.items()
               if c not in (BASE_CCY, ccy)
               and (amt - _FX_COMMITTED.get(c, 0.0)) > 0]
    if not sources:
        log(f"  no non-{BASE_CCY} cash to fund {ccy}; skipping conversion")
        return False
    for src, have in sorted(sources, key=lambda x: -x[1]):
        rate = fx_rate(ib, ccy, src)              # src units per 1 ccy
        if not rate or rate <= 0:
            continue
        need_src = short_ccy * rate * 1.02        # buffer for slippage/fees
        if have < need_src:
            log(f"  {src} {have:,.0f} short of the {need_src:,.0f} needed to fund {ccy}")
            continue
        log(f"  funding {ccy} from {src}: converting ~{need_src:,.0f} {src}")
        if _fx_order_pair(ib, src, ccy, need_src, short_ccy, dry):
            return True
        if ccy in _FX_PENDING_CCY:
            # Working but unfilled is NOT a failed source. Falling through here
            # would convert a second balance for the same shortfall and land
            # twice the currency once both fill.
            log(f"  {ccy} conversion is working but unfilled; not starting "
                f"another from a different balance")
            return False
        # Try the NEXT balance rather than giving up. Returning here would
        # strand the trade whenever the largest source happens to fail - a
        # rejected order, a rate glitch - while other funded currencies sit
        # unused. With USD/EUR/GBP/JPY held that is a live scenario, not a
        # theoretical one; every cross among them exists on IB.
        log(f"  {src}->{ccy} did not go through; trying the next balance")
    log(f"  no non-{BASE_CCY} balance could fund {ccy}; order will not be placed")
    return False


def _fx_order_pair(ib, src, dst, qty_src, qty_dst, dry):
    """Convert src -> dst on whichever spot pair IB lists for them.

    The pair may be quoted either way round - USD.JPY has USD as base, so
    acquiring JPY means SELLing it in USD units; a DST.SRC pair would mean
    BUYing in DST units. Reading the symbol avoids assuming a direction.
    """
    if BASE_CCY in (src, dst):
        log(f"  !! refusing FX {src}->{dst}: would trade {BASE_CCY}")
        return False
    import ib_orders
    cid, sym = ib_orders.fx_pair_conid(src, dst)
    if not cid or "." not in (sym or ""):
        log(f"  no IB spot pair for {src}/{dst}; cannot fund")
        return False
    base, quote = sym.split(".", 1)
    if base.upper() == src.upper():
        return _fx_order(ib, base, quote, "SELL", qty_src, dry, f"{src}->{dst}",
                         target=dst, src_ccy=src, src_qty=qty_src)
    if base.upper() == dst.upper():
        return _fx_order(ib, base, quote, "BUY", qty_dst, dry, f"{src}->{dst}",
                         target=dst, src_ccy=src, src_qty=qty_src)
    log(f"  unexpected pair {sym} for {src}/{dst}; not guessing a side")
    return False


def reserve_working_cash(ib):
    """Reserve cash claimed by orders left working by an EARLIER run.

    _FX_COMMITTED and _FX_PENDING_CCY are process state, cleared at the top of
    every run, so on their own they only ever protected a single run. An order
    accepted but not yet filled does not reduce CashBalance - IB debits it on
    settlement - so without this the next run reads money that is already spoken
    for as free, and spends it twice. Stock buys and conversions both. Three
    cross-run holes follow, and this closes all of them:

      STOCK - run 1 places a USD limit into a shut session; run 2 starts empty,
      reads the undebited balance and funds a different candidate from the same
      dollars. open_syms and pending_buys claim the SYMBOL and a position slot,
      never the cash.

      SOURCE - run 1 leaves a conversion working having claimed 1,847 USD; run 2
      starts empty, sees the full USD balance (an unfilled order debits no
      CashBalance) and spends the same USD again for a different target.

      TARGET - _fx_already_working matches on the PAIR, so a working USD.JPY
      does not stop run 2 converting EUR into JPY. Only _FX_PENDING_CCY carries
      "something is already on its way into JPY", and it was not rebuilt.

    Both sides of the pair are handled. A SELL spends `qty` of the base and
    acquires the quote; a BUY acquires the base and spends the quote, whose
    amount needs the rate - conservative, and far better than the nothing that
    was reserved before. IB quotes EUR.USD and GBP.USD with USD as the QUOTE, so
    funding EUR or GBP out of USD takes the BUY branch: skipping it would have
    left the source unreserved for every European name in the universe.

    Best-effort by design: a pair or rate we cannot resolve reserves nothing
    rather than guessing, and says so in the log.
    """
    try:
        rows = [t for t in ib.openTrades()
                if getattr(t.orderStatus, "status", "") in _WORKING_STATUS]
    except Exception as e:
        log(f"  ! cannot read working orders to reserve cash ({str(e)[:80]})")
        return
    import ib_orders
    unresolved = 0
    for t in rows:
        if getattr(t.contract, "secType", "") != "CASH":
            # A working STOCK buy claims cash exactly as a conversion does, and
            # for the same reason: CashBalance is not debited until settlement.
            # Skipping these left the cross-run half of the very hole the
            # in-run reservation closes - run 1 places a USD limit into a shut
            # session, run 2 starts with an empty registry and funds another
            # candidate from the same dollars. open_syms and pending_buys do
            # not help: they claim the SYMBOL and a position slot, never the
            # money. A SELL is ignored deliberately - unrealised proceeds are
            # not spendable cash.
            if str(getattr(t.order, "action", "") or "").upper() != "BUY":
                continue
            ccy = str(getattr(t.contract, "currency", "") or "").upper()
            px = getattr(t.order, "lmtPrice", None)
            qy = float(getattr(t.order, "totalQuantity", 0) or 0)
            if ccy and px and qy > 0:
                _FX_COMMITTED[ccy] = _FX_COMMITTED.get(ccy, 0.0) + qy * float(px)
            else:
                unresolved += 1
            continue
        base = str(getattr(t.contract, "symbol", "") or "").upper().split(".")[0]
        qty = float(getattr(t.order, "totalQuantity", 0) or 0)
        side = str(getattr(t.order, "action", "") or "").upper()
        if not base or qty <= 0:
            continue
        try:
            quote = ib_orders.fx_quote_ccy(base, getattr(t.contract, "conId", 0))
        except Exception:
            quote = ""
        if side == "SELL":
            source, amount, target = base, qty, quote
        else:                                  # BUY base, paying the quote
            source, target = quote, base
            rate = fx_rate(ib, base, quote) if quote else 0.0
            amount = qty * rate if rate else 0.0
        if source and amount > 0:
            _FX_COMMITTED[source] = _FX_COMMITTED.get(source, 0.0) + amount
        elif not source or amount <= 0:
            unresolved += 1
        if target:
            _FX_PENDING_CCY.add(target)
    if _FX_COMMITTED:
        log("working orders reserve: "
            + ", ".join(f"{c} {a:,.0f}" for c, a in sorted(_FX_COMMITTED.items())))
    if _FX_PENDING_CCY:
        log(f"conversions already in flight into: {sorted(_FX_PENDING_CCY)}")
    if unresolved:
        log(f"  ! {unresolved} working order(s) could not be priced; the cash "
            f"they claim is NOT reserved this run")


def _spendable(ib, ccy):
    """Cash in <ccy> that is not already promised to a working FX order.

    CashBalance is NOT reduced by an accepted-but-unfilled order - IB debits it
    on settlement, not on acceptance - so the raw balance still shows money a
    pending conversion has spoken for. fund_from_nonbase nets _FX_COMMITTED off
    its candidate SOURCES, but ensure_ccy's "already funded, nothing to do"
    early-out was reading the raw figure, so a stock order could be sized
    against the very cash a conversion was mid-way through spending.
    """
    return cash_by_ccy(ib).get(ccy, 0.0) - _FX_COMMITTED.get(ccy, 0.0)


def ensure_ccy(ib, ccy, need_base, dry):
    """Make sure enough <ccy> cash exists for a purchase worth ~need_base (BASE_CCY).

    Returns True when the buy may proceed - the cash is there, or a conversion
    FILLED, or funding is IB's job because the bot's own FX is switched off.
    Returns False when the money is not in the account, and the caller MUST skip
    the order.

    That return value used to not exist: every path fell out as None and the
    caller placed the order regardless, on the docstring's claim that "an
    under-funded stock order is rejected by IB". That is not true of a margin
    account - IB fills it and settles the deficit itself, which on this account
    means reaching the HKD the mandate forbids selling. Board review caught the
    whole filled-only chain dead-ending here, one call short of the leg that
    actually spends money.
    """
    if ccy == BASE_CCY:
        return True
    if not FX_CONVERT:
        if not FX_FUND_NONBASE:
            log(f"  bot FX off — no {BASE_CCY} conversion; {ccy} buy uses existing "
                f"cash / IB auto-funding from non-{BASE_CCY} balances")
            return True          # deliberate: IB funds it, as configured
        try:
            rate = fx_rate(ib, ccy, BASE_CCY)
            if not rate:
                log(f"  ! no {ccy}/{BASE_CCY} rate; cannot size {ccy} funding")
                return False
            need_ccy = need_base / rate
            have = _spendable(ib, ccy)
            if have >= need_ccy:
                return True                  # already funded, nothing to do
            return fund_from_nonbase(ib, ccy, (need_ccy - have), dry)
        except Exception as e:
            log(f"  ! {ccy} funding skipped ({e})")
            return False
    try:
        rate = fx_rate(ib, ccy, BASE_CCY)         # BASE per 1 ccy
        if not rate:
            log(f"  ! no {ccy}/{BASE_CCY} rate; cannot fund {ccy}")
            return False
        need_ccy = need_base / rate
        have = _spendable(ib, ccy)
        if have >= need_ccy:
            return True
        short = (need_ccy - have) * 1.02          # small buffer for slippage/fees
        if not convert_into(ib, ccy, short, dry):
            log(f"  ! no FX path {BASE_CCY}->{ccy}; skipping rather than under-funding")
            return False
        return True
    except Exception as e:
        log(f"  ! FX funding skipped ({e}); skipping rather than under-funding")
        return False


# ---------------- dashboard state publishing ----------------
def publish_state(ib, state, nl):
    """Write data/bot_state.json into the repo clone and push it (best-effort),
    so the phone dashboard shows live bot positions/history automatically."""
    try:
        import subprocess
        from datetime import datetime, timezone
        repo = Path(__file__).resolve().parent.parent      # .../multi-product-signals
        out = repo / "data" / "bot_state.json"
        prev = {}
        if out.exists():
            try:
                prev = json.loads(out.read_text())
            except Exception:
                prev = {}
        smap = state.get("map", {})
        poss = []
        for p in ib.positions():
            if not p.position:
                continue
            if getattr(p.contract, "secType", "") == "CASH":
                continue                      # FX pairs are cash, not investments
            ysym = smap.get(p.contract.symbol, p.contract.symbol)
            st = state.get("pos", {}).get(ysym, {})
            poss.append({"symbol": ysym, "ib_symbol": p.contract.symbol,
                         "qty": p.position, "avg_cost": round(p.avgCost, 4),
                         "ccy": p.contract.currency,
                         "entry": st.get("entry"), "stop": st.get("stop")})
        cash_raw = cash_by_ccy(ib)
        cash = {k: round(v) for k, v in cash_raw.items() if abs(v) >= 1}
        act = (prev.get("activity") or []) + PLACED
        # Must reproduce net_liq()'s cap EXACTLY. The field's contract is "how
        # much of `cash` is already netted out of `netliq`", and net_liq() nets
        # out min(marker, base-ccy cash held) - not the raw marker. Publishing
        # the raw marker made the same JSON object carry a capped netliq beside
        # an uncapped exclusion, so the dashboard captioned "excl. 18,559 HKD
        # cash" on a day the money had been withdrawn and only 4 was excluded,
        # and the caption flipped every time the other publisher ran.
        exc_pub = min(_excluded_cash(),
                      max(0.0, float(cash_raw.get(BASE_CCY, 0) or 0)))
        snap = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                "netliq": round(nl), "base_ccy": BASE_CCY, "cash": cash,
                "excluded_cash": round(exc_pub),
                "positions": poss, "activity": act[-100:]}
        out.write_text(json.dumps(snap, indent=1))
        # daily NetLiq history for the dashboard's P&L Calendar: upsert TODAY's
        # (UTC) entry with the latest netliq on every publish — the last publish
        # of the day (23:20) therefore records the day-end value. Deposits and
        # withdrawals must be added to "flows" by hand (see execution/README.md)
        # so the calendar shows TRADING P&L, not cash movements.
        try:
            hist_p = out.parent / "netliq_history.json"
            hist = {"series": [], "flows": []}
            if hist_p.exists():
                # an UNREADABLE file must be left in place for human repair —
                # falling through to the rewrite would silently destroy the
                # backfilled series and the hand-entered flows, then push the
                # wipe (board finding 2026-08-06). Raise into the outer handler.
                hist = json.loads(hist_p.read_text())
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            ser = [e for e in hist.get("series", []) if e.get("d") != today]
            ser.append({"d": today, "nl": round(nl)})
            hist["series"] = sorted(ser, key=lambda e: e["d"])
            tmp = hist_p.with_suffix(".json.tmp")     # atomic: no torn writes
            tmp.write_text(json.dumps(hist, indent=1))
            os.replace(tmp, hist_p)
        except Exception as e:
            log(f"  !! netliq history NOT updated ({e}) — fix data/"
                f"netliq_history.json by hand (validate with python -m "
                f"json.tool); existing file left untouched")
            if not hist_p.exists():
                hist_p.touch()             # git add must not break the publish
        # tax pipeline: sweep today's executions into the fills ledger, then
        # regenerate the UK CGT report the dashboard's Tax mode reads.
        # three independent stages: a fills-sweep failure must not stop the
        # dividend sweep, and neither may stop the report rebuild
        try:
            import fills_capture
            fills_capture.capture(ib, lambda ccy: fx_rate(ib, ccy, "GBP"))
        except Exception as e:
            log(f"  note: fills sweep skipped ({e})")
        try:
            import flex_dividends
            flex_dividends.capture_if_configured()   # no-op until /root/flex.conf
        except Exception as e:
            log(f"  note: dividend sweep skipped ({e})")
        try:
            import uk_cgt
            uk_cgt.build_report()
        except Exception as e:
            log(f"  note: tax report build skipped ({e})")
        div_ledger = out.parent / "dividends_ledger.jsonl"
        if not div_ledger.exists():
            div_ledger.touch()        # git add fails on a missing pathspec
        for cmd in (["add", "data/bot_state.json", "data/fills_ledger.jsonl",
                     "data/tax_report.json", "data/dividends_ledger.jsonl",
                     "data/netliq_history.json"],
                    ["-c", "user.email=bot@vm", "-c", "user.name=ib-bot",
                     "commit", "-m", "bot: state update [skip ci]"],
                    ["push"]):
            r = subprocess.run(["git", "-C", str(repo)] + cmd,
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                log(f"  note: state publish '{cmd[0] if cmd[0] != '-c' else 'commit'}'"
                    f" skipped ({(r.stderr or r.stdout).strip()[:90]})")
                break
        else:
            log("  bot state published to dashboard")
    except Exception as e:
        log(f"  note: state publish skipped ({e})")


# ---------------- zombie-gateway self-heal ----------------
_ZOMBIE_MARKER = Path("/tmp/mps_zombie_kill")
try:
    ZOMBIE_COOLDOWN_H = float(os.environ.get("ZOMBIE_COOLDOWN_H", "3"))
except ValueError:                       # malformed env must not kill every run
    ZOMBIE_COOLDOWN_H = 3.0


def connect_or_heal(ib, client_id, timeout):
    """ib.connect with zombie-gateway detection. The failure seen 2026-08-01
    17:20Z and again 2026-08-02 11:20Z: the gateway's port keeps accepting TCP
    while its API session is dead, so ensure_gateway's port check passes and
    the outage is silent for hours until a human intervenes.
    Signature = connect TIMES OUT while the port still accepts TCP (a downed
    gateway raises ConnectionRefused instead, which the watchdog already
    handles). On the signature: kill java so ensure_gateway's 15-min cron
    relaunches with a fresh login — 2FA push in waking hours; its existing
    night hold (23:30-07:00 London) defers overnight, unchanged. Rate-limited
    to one kill per ZOMBIE_COOLDOWN_H so an IB-side outage cannot cause
    kill/2FA spam. Always re-raises: a failed connect NEVER trades/publishes."""
    try:
        ib.connect(HOST, PORT, clientId=client_id, timeout=timeout)
        return
    except Exception as e:
        if not isinstance(e, TimeoutError):        # asyncio.TimeoutError == TimeoutError (3.11+)
            raise                                  # (live tracebacks 08-01/08-02 were TimeoutError)
        import socket, subprocess, time
        try:
            with socket.create_connection((HOST, PORT), timeout=3):
                port_accepts = True
        except OSError:
            port_accepts = False
        if not port_accepts:
            raise                                  # plain down — watchdog's job
        # SIBLING GUARD (board 2026-08-02): on BST Mondays the 09:00 UTC run
        # and the 10:00-London catch-up fire simultaneously with the same
        # clientId; the loser's duplicate-clientId timeout is indistinguishable
        # from a zombie while the gateway healthily serves the winner. Never
        # kill when another ib_bot is alive; if we cannot tell, do not kill.
        sibling = True                             # fail-safe default: no kill
        try:
            out = subprocess.run(["pgrep", "-fc", "ib_bot.py"],
                                 capture_output=True, text=True, timeout=10)
            sibling = int((out.stdout or "0").strip() or 0) > 1
        except Exception:
            pass
        if sibling:
            log("!! connect timeout but a sibling ib_bot.py is running — "
                "likely clientId collision, NOT a zombie; not killing")
            raise
        # NIGHT GUARD: 23:00-07:00 London a kill buys nothing — the relaunch
        # is night-held to 07:00 anyway — while IB's nightly server resets
        # (~03:45-05:45 UTC) can stall healthy connects. Defer to daytime.
        night = False
        try:
            from zoneinfo import ZoneInfo
            from datetime import datetime
            h = datetime.now(ZoneInfo("Europe/London")).hour
            night = h >= 23 or h < 7
        except Exception:
            pass                                   # tz unavailable -> treat as day
        if night:
            log("!! ZOMBIE signature in the night window — deferring to "
                "daytime detection (relaunch would be night-held anyway)")
            raise
        try:
            recent = (time.time() - _ZOMBIE_MARKER.stat().st_mtime) \
                < ZOMBIE_COOLDOWN_H * 3600
        except OSError:
            recent = False
        if recent:
            log("!! ZOMBIE GATEWAY again within cooldown — not re-killing")
            raise
        log(f"!! ZOMBIE GATEWAY: port {PORT} accepts TCP but the API connect "
            f"timed out — killing java; ensure_gateway will relaunch with a "
            f"fresh login (2FA push follows in waking hours)")
        try:
            subprocess.run(["pkill", "-9", "java"], timeout=10)
            _ZOMBIE_MARKER.touch()                 # cooldown only on a real kill
        except Exception as ke:
            log(f"!! zombie heal failed ({ke}) — manual restart needed")
        raise


# ---------------- main reconcile ----------------
def run(dry=False):
    _FX_PENDING.clear()
    _FX_PENDING_CCY.clear()
    _FX_COMMITTED.clear()
    data = get_json(SIGNALS_URL)
    actions = [a for a in data.get("actions", []) if a.get("action") in ("BUY", "BUY/HOLD")]
    log(f"signals {data.get('generated')}: {len(actions)} BUY candidates")

    ib = IB()
    connect_or_heal(ib, CLIENT_ID, 30)
    # HOST/PORT describe the SOCKET transport only. Under IB_BACKEND=web the
    # shim ignores them entirely and talks to the live account over OAuth, so
    # printing "127.0.0.1:4002 (PAPER)" there would be actively misleading.
    if os.environ.get("IB_BACKEND", "socket").strip().lower() == "web":
        log("connected via IBKR Web API (OAuth) — LIVE account")
    else:
        log(f"connected {HOST}:{PORT} ({'PAPER' if PORT == 4002 else 'LIVE'})")
    try:
        nl = net_liq(ib)
        state = load_state()
        # --- kill-switch: gates NEW ENTRIES ONLY (checked before the entries
        # loop below). It previously returned HERE, before the exit loop —
        # freezing regime/trailing/time exits exactly when a drawdown is
        # deepest. That fired for real 2026-08-03..06: a Sunday cash
        # WITHDRAWAL left NetLiq 11% under the stale pre-withdrawal peak and
        # every run froze silently for four days (board-predicted 2026-08-01:
        # "the withdrawal case is the dangerous asymmetry"). Exits must never
        # depend on this gate; peak is still cash-flow-naive (documented
        # limitation — a withdrawal can still suspend entries until the peak
        # is manually reset, but it is now loud and never blocks de-risking).
        peak = max(state.get("_peak_netliq", nl), nl)
        state["_peak_netliq"] = peak
        killed = nl < peak * (1 - DAILY_LOSS_KILL)
        if killed:
            log(f"KILL-SWITCH: NetLiq {nl:.0f} < {(1-DAILY_LOSS_KILL)*100:.0f}% of "
                f"peak {peak:.0f} — ENTRIES BLOCKED; exits still run. If a "
                f"deposit/withdrawal moved NetLiq, reset _peak_netliq in "
                f"state.json (see execution/README.md).")
            from datetime import datetime, timezone
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if state.get("_kill_noted") != today:   # one dashboard row per day
                state["_kill_noted"] = today
                PLACED.append({"time": datetime.now(timezone.utc)
                               .strftime("%Y-%m-%d %H:%M UTC"),
                               "action": "HALT", "qty": 0, "symbol": "ENTRIES",
                               "limit": "", "ccy": BASE_CCY,
                               "reason": f"kill-switch: NetLiq {nl:.0f} vs "
                                         f"peak {peak:.0f}",
                               "status": "notice", "error": ""})
        per_pos = nl / TARGET_POSITIONS
        held = held_positions(ib)
        log(f"NetLiq {nl:.0f} {BASE_CCY} | {len(held)} positions | "
            f"target/pos ~{per_pos:.0f}")

        # ---- OPEN ORDERS: never double-place against a working order ----
        ib.reqAllOpenOrders()
        ib.sleep(2)
        open_syms = {t.contract.symbol for t in ib.openTrades()
                     if t.orderStatus.status in _WORKING_STATUS}
        if open_syms:
            log(f"open orders already working: {sorted(open_syms)} — will not duplicate")
        # Same book, read for a different purpose: cash that working orders have
        # already claimed must not look spendable to this run.
        reserve_working_cash(ib)

        # ---- EXITS first (free up cash + capital) ----
        for sym_local, (pos, qty) in list(held.items()):
            ysym = state.get("map", {}).get(sym_local)
            if not ysym:
                continue
            if sym_local in open_syms:
                continue                     # an order for it is already working
            try:
                card = get_json(PRODUCTS_URL + safe_name(ysym) + ".json")["card"]
            except Exception:
                continue
            price = card.get("price")
            sma200 = card.get("sma200")
            atr = card.get("atr") or 0
            st = state.setdefault("pos", {}).get(ysym, {})
            # exact trailing stop maintained here (server-side high-water)
            hw = max(st.get("hw", price or 0), price or 0)
            k = 2.0 if (price and st.get("entry") and price >= st["entry"] + 1.5 * atr) else 3.5
            trail = max(st.get("stop", 0), hw - k * atr) if atr else st.get("stop", 0)
            st.update(hw=hw, stop=trail)
            if not st.get("entry_date"):
                # hand-over: positions opened before the time stop existed get
                # their TRUE entry date from the fills ledger (never today's,
                # which would grant them a fresh 60 bars only as last resort)
                from datetime import date
                bf = ledger_entry_date(sym_local)
                st["entry_date"] = bf or date.today().isoformat()
                log(f"  {ysym}: entry_date backfilled -> {st['entry_date']}"
                    f" ({'ledger' if bf else 'TODAY — no ledger fill found'})")
            state["pos"][ysym] = st
            bars = bars_held(st["entry_date"])
            sell = None
            if sma200 and price and price < sma200:
                sell = "regime break (close < SMA200)"
            elif trail and price and price <= trail:
                sell = f"trailing stop {trail:.2f}"
            elif MAX_HOLD_BARS and bars >= MAX_HOLD_BARS and price:
                # `and price` guard: unlike regime/trail, the time stop needs no
                # price to DECIDE — but place() prices its sanity check off it,
                # so a null-price card here would abort the whole run (and with
                # it every later exit + all entries). Skip loudly instead.
                sell = f"time stop ({bars} bars >= {MAX_HOLD_BARS})"
            elif MAX_HOLD_BARS and bars >= MAX_HOLD_BARS:
                log(f"  !! {ysym}: time stop due ({bars} bars) but card price is "
                    f"null — deferred to next run")
            if sell and qty > 0:
                log(f"EXIT {ysym}: {sell}")
                # route through a clean SMART contract — the raw position
                # contract requests direct routing (Error 10311 rejections)
                xc = to_ib(ysym)
                sold = None
                if xc is not None:
                    qx = ib.qualifyContracts(xc)
                    if qx:
                        sold = qx[0]
                place(ib, sold if sold is not None else pos.contract,
                      "SELL", abs(qty), price, dry, reason=sell, mkt=True)

        # ---- ENTRIES (top score first, up to free slots) ----
        # working BUY orders consume slots too: with two trading runs a day, a
        # 23:35 order still unfilled at the 09:00 run would otherwise let the
        # bot open a 16th position against NetLiq/15 sizing (attempted live on
        # 2026-07-31; only IB's rejection stopped it). The validated engine
        # counts pending the same way: free = slots - positions - pending.
        pending_buys = {t.contract.symbol for t in ib.openTrades()
                        if t.orderStatus.status in _WORKING_STATUS
                        and t.order.action == "BUY"
                        and getattr(t.contract, "secType", "") != "CASH"
                        and t.contract.symbol not in held}
        if pending_buys:
            log(f"working BUY orders hold {len(pending_buys)} slot(s): "
                f"{sorted(pending_buys)}")
        free = (TARGET_POSITIONS
                - len([q for _, (_, q) in held.items() if q > 0])
                - len(pending_buys))
        if killed:
            free = 0                     # kill-switch: no new entries, exits ran
        for a in sorted(actions, key=lambda x: -(x.get("score") or 0)):
            if free <= 0:
                break
            ysym = a["symbol"]
            c = to_ib(ysym)
            if c is None:
                continue
            q = ib.qualifyContracts(c)
            if not q:
                log(f"  skip {ysym}: IB could not qualify"); continue
            c = q[0]
            if c.symbol in held or c.symbol in open_syms:
                continue                     # held, or an order is already working
            price = a.get("price") or 0
            if price <= 0:
                continue
            notional = min(per_pos, MAX_ORDER_BASE)          # in BASE_CCY
            ccy = currency_of(ysym)
            # rate = BASE_CCY per 1 <ccy> (via direct pair or USD cross)
            rate = fx_rate(ib, ccy, BASE_CCY) if ccy != BASE_CCY else 1.0
            if not rate or rate != rate or rate <= 0:
                log(f"  skip {ysym}: no {ccy}/{BASE_CCY} rate to size order")
                continue
            shares = int(notional / rate / price)
            lot = lot_size(ib, c)
            if lot > 1:
                shares = (shares // lot) * lot      # exchange board-lot multiple
                if shares <= 0:
                    log(f"  skip {ysym}: 1 board lot ({lot} sh ~"
                        f"{int(lot*price*rate):,} {BASE_CCY}) exceeds the position size")
                    continue
            if shares <= 0:
                continue
            # Fund only once the order is known to be placeable. Converting
            # ABOVE this point bought currency for candidates that then hit
            # `continue`: on 2026-09-04 three JP names each converted ~1,847 USD
            # and only 7733 ever became an order. Sizing on the ROUNDED share
            # count also stops us converting for the fraction that board-lot
            # rounding just discarded.
            if not ensure_ccy(ib, ccy, shares * price * rate, dry):
                # The cash is not in the account. Skip WITHOUT consuming a slot
                # or writing state - the signal is re-evaluated next run, by
                # which time a working conversion may have filled.
                log(f"  skip {ysym}: {ccy} funding did not complete")
                continue
            st = place(ib, c, "BUY", shares, price, dry,
                       reason=f"entry signal, score {a.get('score')}")
            if st != "REJECTED":
                # Reserve what this order will spend. CashBalance is not debited
                # until settlement, so without this the next same-currency
                # candidate reads the SAME cash as free and is funded from it
                # too - both fill at the open and the currency goes negative,
                # which IB settles as a margin loan against the HKD balance.
                # This is the stock-side twin of _FX_COMMITTED, and the reason
                # _spendable exists at all.
                # Reserve at the LIMIT, not the signal price: place() sends
                # price * (1 + LIMIT_BUFFER), so reserving the bare price
                # under-counts every order by the buffer. Commission is still
                # not modelled anywhere, so this remains a slight under-estimate
                # of the true cost - erring small, but knowingly.
                _FX_COMMITTED[ccy] = (_FX_COMMITTED.get(ccy, 0.0)
                                      + shares * price * (1.0 + LIMIT_BUFFER))
            state.setdefault("map", {})[c.symbol] = ysym
            from datetime import date
            state.setdefault("pos", {})[ysym] = {"entry": price, "hw": price,
                                                 "stop": a.get("stop") or 0,
                                                 "entry_date": date.today().isoformat()}
            free -= 1
        save_state(state)
        publish_state(ib, state, nl)
        log("done.")
    finally:
        ib.disconnect()


def publish_only():
    """Connect, read the account, publish state for the dashboard — trade nothing."""
    ib = IB()
    connect_or_heal(ib, CLIENT_ID + 3, 25)
    try:
        publish_state(ib, load_state(), net_liq(ib))
    finally:
        ib.disconnect()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="compute + print, place nothing")
    ap.add_argument("--publish-only", action="store_true",
                    help="just refresh the dashboard state (hourly cron)")
    args = ap.parse_args()
    if args.publish_only:
        publish_only()
    else:
        if PORT == 4001 and CONFIRM_FIRST is False and not args.dry:
            log("*** LIVE + UNATTENDED mode ***")
        run(dry=args.dry)
