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

from ib_async import IB, LimitOrder, MarketOrder, StopOrder, Forex
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
# Broker-side trailing stops: a resting GTC SELL STP at the trail level for every
# held stock position, reconciled each run. IB then exits intraday the moment the
# stop trades — matching the validated engine's resting-stop semantics — and the
# book stays protected while the VM/bot is down. BROKER_STOPS=0 cancels all
# bot-placed stops on the next run and reverts to close-check-only exits.
BROKER_STOPS = os.environ.get("BROKER_STOPS", "1") != "0"
TRAIL_TAG = "mps-trail"        # orderRef prefix identifying our resting stops
STATE = Path(__file__).with_name("state.json")


def log(*a):
    print("[bot]", *a, flush=True)


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
def net_liq(ib):
    for v in ib.accountValues():
        if v.tag == "NetLiquidation" and v.currency == BASE_CCY:
            return float(v.value)
    for v in ib.accountValues():
        if v.tag == "NetLiquidation":
            return float(v.value)
    return 0.0


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
        if status == "REJECTED":
            log(f"  !! ORDER REJECTED: {action} {qty} {contract.symbol} — {err[:140]}")
        from datetime import datetime, timezone
        PLACED.append({"time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                       "action": action, "qty": qty, "symbol": contract.symbol,
                       "limit": "MKT-open", "ccy": contract.currency,
                       "reason": reason, "status": status, "error": err[:160]})
        return
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
    for attempt in range(6):
        order = LimitOrder(action, qty, lim, tif="DAY")
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
    if status == "REJECTED":
        log(f"  !! ORDER REJECTED: {action} {qty} {contract.symbol} — {err[:140]}")
    from datetime import datetime, timezone
    PLACED.append({"time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                   "action": action, "qty": qty, "symbol": contract.symbol,
                   "limit": lim, "ccy": contract.currency, "reason": reason,
                   "status": status, "error": err[:160]})


def _order_verdict(trade):
    """('ok'|'REJECTED', error_message) for a just-placed trade."""
    try:
        st = trade.orderStatus.status
        if st in ("Cancelled", "ApiCancelled", "Inactive"):
            msgs = [e.message for e in trade.log if e.message]
            return "REJECTED", (msgs[-1] if msgs else st).strip()
        return "ok", ""
    except Exception as e:
        return "ok", ""


# ---------------- broker-side resting trail stops ----------------
_WORKING = ("PendingSubmit", "PreSubmitted", "Submitted", "ApiPending")
_STOP_DEAD = ("Filled", "Cancelled", "ApiCancelled")


def trail_stops_open(ib):
    """symbol -> [Trade, ...] for our resting trail stops (orderRef mps-trail*).
    Includes anything not definitively dead — an 'Inactive' stop can resurrect,
    so it must stay visible to cancellation and de-duplication."""
    out = {}
    for t in ib.trades():
        ref = getattr(t.order, "orderRef", "") or ""
        if ref.startswith(TRAIL_TAG) and t.orderStatus.status not in _STOP_DEAD:
            out.setdefault(t.contract.symbol, []).append(t)
    return out


def confirm_cancelled(ib, trades, why, timeout=10):
    """Cancel the given trail stops and WAIT until each is confirmed dead.
    ib_async's cancelOrder is fire-and-forget — rejections arrive as async
    events, never exceptions — so polling the status is the only reliable
    confirmation. Raises if a stop is still alive at timeout, or if it FILLED
    meanwhile (Tokyo is open during the 00:35 UTC run): in both cases the
    caller must NOT place its replacement sell."""
    for t in trades:
        log(f"  cancel resting stop {t.contract.symbol} @ {t.order.auxPrice} ({why})")
        ib.cancelOrder(t.order)
    for _ in range(timeout):
        sts = [t.orderStatus.status for t in trades]
        if any(s == "Filled" for s in sts):
            raise RuntimeError("stop FILLED during cancel — position already exiting")
        if all(s in ("Cancelled", "ApiCancelled") for s in sts):
            return
        ib.sleep(1)
    alive = [f"{t.contract.symbol}={t.orderStatus.status}" for t in trades
             if t.orderStatus.status not in ("Cancelled", "ApiCancelled")]
    raise RuntimeError(f"stop cancel unconfirmed: {', '.join(alive)}")


def cancel_trail_stop(ib, sym, dry, why=""):
    """Cancel ALL our resting stops on sym before ANY other SELL is placed on
    it — a bot exit and a resting stop working together would both fill
    (double-sell). Raises when cancellation cannot be CONFIRMED (or the stop
    filled first) — callers must then skip their sell; the position is either
    already exiting via the stop or still protected by it."""
    ts = trail_stops_open(ib).get(sym) or []
    if not ts:
        return
    if dry:
        log(f"  [dry] would cancel {len(ts)} resting stop(s) on {sym} ({why})")
        return
    confirm_cancelled(ib, ts, why)


def snap_down_tick(raw, tick):
    """Floor to tick. The resting stop must never sit ABOVE the raw trail —
    that would trigger marginally earlier than the validated engine."""
    lim = int(raw / tick + 1e-9) * tick
    if tick >= 1:
        return int(round(lim))
    return round(lim, 2 if tick >= 0.01 else 4 if tick >= 0.0001 else 6)


def _put_stop(ib, contract, existing_order, qty, aux, tick, sym):
    """Place (or modify in place) a GTC SELL STP, self-healing venue tick
    rejections (Error 110) with the same coarser-tick ladder place() uses —
    IB's minTick metadata is wrong on exactly the venues we trade (TSE,
    Euronext), and a stop that re-rejects nightly is silent unprotection."""
    ladder = [0.0001, 0.001, 0.01, 0.05, 0.1, 0.2, 0.5, 1, 5, 10, 50, 100, 500, 1000]
    status, err = "", ""
    for _ in range(6):
        if existing_order is None:
            o = StopOrder("SELL", qty, aux, tif="GTC",
                          orderRef=f"{TRAIL_TAG}:{sym}")
        else:
            o = existing_order
            o.auxPrice = aux
            o.totalQuantity = qty
        trade = ib.placeOrder(contract, o)
        ib.sleep(2)
        status, err = _order_verdict(trade)
        if status != "REJECTED" or "110" not in err:
            break
        coarser = [t for t in ladder if t > tick]
        if not coarser:
            break
        tick = coarser[0]
        aux = snap_down_tick(aux, tick)
        log(f"  retrying stop with coarser tick {tick} -> {aux}")
    return status, err, aux


def sync_trail_stops(ib, state, dry):
    """Reconcile resting broker-side stops with holdings and the freshly
    ratcheted trail levels. Idempotent each run: place missing stops, re-price
    ratcheted ones IN PLACE (same orderId — no unprotected gap), resize after
    manual trims, de-duplicate, cancel orphans whose position is truly gone.
    New entries get their stop on the first run AFTER the buy fills — a cash
    account rejects a SELL stop on shares not yet held."""
    ib.reqAllOpenOrders()                     # fresh snapshot: the 10-min phone
    ib.sleep(2)                               # poller may have acted meanwhile
    open_stops = trail_stops_open(ib)
    held = held_positions(ib)
    if not BROKER_STOPS:
        for sym, ts in open_stops.items():
            log(f"  BROKER_STOPS off — cancelling resting stop(s) {sym}")
            if not dry:
                try:
                    confirm_cancelled(ib, ts, "BROKER_STOPS=0")
                except Exception as e:
                    log(f"  ! cancel {sym}: {e}")
        return
    # a working SELL beside a stop risks a double-fill -> stop must go; a
    # working BUY is harmless -> defer changes, keep the protection resting
    sell_busy, buy_busy = set(), set()
    for t in ib.openTrades():
        if (getattr(t.order, "orderRef", "") or "").startswith(TRAIL_TAG):
            continue
        if t.orderStatus.status in _WORKING:
            (sell_busy if t.order.action == "SELL" else buy_busy).add(
                t.contract.symbol)
    rmap = state.get("map", {})
    n_placed = n_repriced = n_ok = 0
    for sym_local, (pos, raw_qty) in held.items():
        try:
            if getattr(pos.contract, "secType", "") != "STK":
                continue                      # stocks only (FX artifacts, crypto)
            # pop OUR stops first — anything still in open_stops after this
            # loop is cancelled as an orphan, and a held position's stop must
            # never fall through to that fate (state loss = keep protection)
            ts = open_stops.pop(sym_local, [])
            if raw_qty <= 0:
                continue                      # short/zero: never stop-sell into it
            if sym_local in sell_busy:
                if ts:
                    log(f"  stop {sym_local}: cancelling — a SELL is working "
                        f"(double-sell guard)")
                    if not dry:
                        confirm_cancelled(ib, ts, "sell working")
                continue
            if sym_local in buy_busy:
                if ts:
                    log(f"  stop {sym_local}: kept — a BUY is working, "
                        f"reconcile deferred")
                continue
            existing = None
            if ts:
                # de-duplicate: keep the tightest (highest) stop, cancel extras
                ts.sort(key=lambda t: float(t.order.auxPrice or 0), reverse=True)
                existing, extras = ts[0], ts[1:]
                if extras:
                    log(f"  stop {sym_local}: cancelling {len(extras)} "
                        f"duplicate stop(s)")
                    if not dry:
                        confirm_cancelled(ib, extras, "duplicate")
            qty = int(abs(raw_qty))
            ysym = rmap.get(sym_local)
            stop = float((state.get("pos", {}).get(ysym) or {}).get("stop") or 0) \
                if ysym else 0.0
            if qty <= 0 or stop <= 0:
                # unmapped/unseeded (e.g. state.json lost): do NOT touch an
                # existing resting stop — it is the protection of last resort
                if existing is not None:
                    log(f"  stop {sym_local}: kept at {existing.order.auxPrice}"
                        f" (no state entry)")
                continue
            # clean SMART contract (Error 10311) + venue-valid price (Error 110)
            ref_c = existing.contract if existing is not None else None
            if ref_c is None:
                xc = to_ib(ysym)
                if xc is not None:
                    qx = ib.qualifyContracts(xc)
                    ref_c = qx[0] if qx else None
                if ref_c is None:
                    # last resort: patch the raw position contract the same way
                    # ib_commands does — unpatched it direct-routes (10311)
                    pc = pos.contract
                    pc.exchange = pc.exchange or "SMART"
                    qx = ib.qualifyContracts(pc)
                    if not qx:
                        log(f"  ! stop {sym_local}: no usable contract — skipped")
                        continue
                    ref_c = qx[0]
            tick = min_tick(ib, ref_c)
            if ref_c.currency == "JPY":
                tick = max(tick, jp_tick(stop))
            aux = snap_down_tick(stop, tick)  # floor: never above the raw trail
            # sanity: a stop at/above the market fires instantly — that is state
            # corruption (e.g. a split adjusted the price, not our state), not a
            # trail; leave the symbol to the close-check exits and say so loudly
            ref_px = live_base_price(ib, ref_c, 0)
            if ref_px and aux >= ref_px:
                log(f"  !! stop {sym_local}: {aux} >= market {ref_px} — NOT "
                    f"placed (state suspect: split?)")
                continue
            if existing is not None:
                o = existing.order
                filled = float(existing.orderStatus.filled or 0)
                if filled > 0:
                    # totalQuantity includes the filled part — a naive modify
                    # under-covers; cancel (confirmed) and place fresh below
                    log(f"  stop {sym_local}: partial fill {filled} — replacing")
                    if dry:
                        continue
                    confirm_cancelled(ib, [existing], "partial-filled stop")
                    existing = None
                else:
                    cur = float(o.auxPrice)
                    if aux < cur - tick / 2:
                        # never widen a resting stop: state went backwards
                        # (restore from an old backup?) — keep the tighter level
                        log(f"  stop {sym_local}: state {aux} below resting "
                            f"{cur} — keeping {cur} (never widen)")
                        aux = cur
                    if abs(cur - aux) < tick / 2 and int(o.totalQuantity) == qty:
                        n_ok += 1
                        continue              # already correct — leave resting
            verb = "ratchet" if existing is not None else "protect"
            log(f"  stop {sym_local}: SELL STP {qty} @ {aux} GTC ({verb})")
            if dry:
                continue
            status, err, aux = _put_stop(
                ib, existing.contract if existing is not None else ref_c,
                existing.order if existing is not None else None,
                qty, aux, tick, sym_local)
            if status == "REJECTED":
                log(f"  !! STOP REJECTED {sym_local} @ {aux} — {err[:140]}")
            if existing is None or status == "REJECTED":
                # dashboard visibility: first placements + failures (nightly
                # ratchets stay log-only — they would flood the activity feed)
                from datetime import datetime, timezone
                PLACED.append({"time": datetime.now(timezone.utc)
                               .strftime("%Y-%m-%d %H:%M UTC"),
                               "action": "STOP", "qty": qty, "symbol": sym_local,
                               "limit": aux, "ccy": ref_c.currency,
                               "reason": "resting trail stop",
                               "status": status, "error": err[:160]})
            if existing is None:
                n_placed += 1
            else:
                n_repriced += 1
        except Exception as e:
            log(f"  ! stop sync {sym_local}: {e}")
    for sym, ts in open_stops.items():        # candidates for orphan cleanup
        if sym in held:
            # belt-and-braces: NEVER cancel a held position's stop here, no
            # matter how the loop above skipped it
            log(f"  stop {sym}: left resting (held but unmanaged this run)")
            continue
        log(f"  cancel orphan stop {sym} (position closed)")
        if not dry:
            try:
                confirm_cancelled(ib, ts, "orphan")
            except Exception as e:
                log(f"  ! cancel orphan {sym}: {e}")
    log(f"  stops: {n_ok} unchanged / {n_placed} placed / {n_repriced} repriced")


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


def _fx_order(ib, base_ccy, quote_ccy, side, qty, dry, why):
    """Market FX order on Forex(base+quote): side BUY/SELL of `qty` base units."""
    qty = int(round(qty))
    if qty <= 0:
        return True
    fx = Forex(base_ccy + quote_ccy)
    if not ib.qualifyContracts(fx):
        return False
    log(f"  FX {side} {qty} {base_ccy}.{quote_ccy} ({why})")
    if dry or not confirm(f"FX {side} {qty} {base_ccy}{quote_ccy}"):
        return not dry or True                    # in dry, treat as satisfied
    trade = ib.placeOrder(fx, MarketOrder(side, qty))
    ib.sleep(3)
    status, err = _order_verdict(trade)
    if status == "REJECTED":
        log(f"  !! FX ORDER REJECTED: {side} {qty} {base_ccy}.{quote_ccy} — {err[:140]}")
    from datetime import datetime, timezone
    PLACED.append({"time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                   "action": f"FX {side}", "qty": qty,
                   "symbol": f"{base_ccy}.{quote_ccy}", "limit": "MKT",
                   "ccy": quote_ccy, "reason": why,
                   "status": status, "error": err[:160]})
    return status != "REJECTED"


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


def ensure_ccy(ib, ccy, need_base, dry):
    """Make sure enough <ccy> cash exists for a purchase worth ~need_base (BASE_CCY),
    converting from BASE_CCY (through USD if needed). FAIL-SAFE: any problem just
    logs and returns — a resulting under-funded stock order is rejected by IB, so
    nothing mis-sized is ever placed."""
    if ccy == BASE_CCY:
        return
    if not FX_CONVERT:
        log(f"  bot FX off — no {BASE_CCY} conversion; {ccy} buy uses existing "
            f"cash / IB auto-funding from non-{BASE_CCY} balances")
        return
    try:
        rate = fx_rate(ib, ccy, BASE_CCY)         # BASE per 1 ccy
        if not rate:
            log(f"  ! no {ccy}/{BASE_CCY} rate; cannot fund {ccy}"); return
        need_ccy = need_base / rate
        have = cash_by_ccy(ib).get(ccy, 0.0)
        if have >= need_ccy:
            return
        short = (need_ccy - have) * 1.02          # small buffer for slippage/fees
        if not convert_into(ib, ccy, short, dry):
            log(f"  ! no FX path {BASE_CCY}->{ccy}; order may under-fund")
    except Exception as e:
        log(f"  ! FX funding skipped ({e}); order may under-fund")


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
        cash = {k: round(v) for k, v in cash_by_ccy(ib).items() if abs(v) >= 1}
        act = (prev.get("activity") or []) + PLACED
        snap = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                "netliq": round(nl), "base_ccy": BASE_CCY, "cash": cash,
                "positions": poss, "activity": act[-100:]}
        out.write_text(json.dumps(snap, indent=1))
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
                     "data/tax_report.json", "data/dividends_ledger.jsonl"],
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


# ---------------- main reconcile ----------------
def run(dry=False):
    data = get_json(SIGNALS_URL)
    actions = [a for a in data.get("actions", []) if a.get("action") in ("BUY", "BUY/HOLD")]
    log(f"signals {data.get('generated')}: {len(actions)} BUY candidates")

    ib = IB()
    ib.connect(HOST, PORT, clientId=CLIENT_ID, timeout=30)
    log(f"connected {HOST}:{PORT} ({'PAPER' if PORT == 4002 else 'LIVE'})")
    try:
        nl = net_liq(ib)
        state = load_state()
        # --- kill-switch ---
        peak = max(state.get("_peak_netliq", nl), nl)
        state["_peak_netliq"] = peak
        if nl < peak * (1 - DAILY_LOSS_KILL):
            log(f"KILL-SWITCH: NetLiq {nl:.0f} < {(1-DAILY_LOSS_KILL)*100:.0f}% of "
                f"peak {peak:.0f} — no new orders.")
            # even when killed, keep the resting stops reconciled: they are the
            # book's only protection while the bot refuses to trade
            try:
                ib.reqAllOpenOrders(); ib.sleep(2)
                sync_trail_stops(ib, state, dry)
            except Exception as e:
                log(f"  ! stop sync failed ({e})")
            save_state(state); return
        per_pos = nl / TARGET_POSITIONS
        held = held_positions(ib)
        log(f"NetLiq {nl:.0f} {BASE_CCY} | {len(held)} positions | "
            f"target/pos ~{per_pos:.0f}")

        # ---- OPEN ORDERS: never double-place against a working order ----
        ib.reqAllOpenOrders()
        ib.sleep(2)
        # our own resting trail stops do NOT count as "working" here — every
        # position would otherwise be skipped by exits and never re-evaluated
        open_syms = {t.contract.symbol for t in ib.openTrades()
                     if t.orderStatus.status in _WORKING
                     and not (getattr(t.order, "orderRef", "") or "")
                     .startswith(TRAIL_TAG)}
        if open_syms:
            log(f"open orders already working: {sorted(open_syms)} — will not duplicate")

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
            state["pos"][ysym] = st
            sell = None
            if sma200 and price and price < sma200:
                sell = "regime break (close < SMA200)"
            elif trail and price and price <= trail:
                sell = f"trailing stop {trail:.2f}"
            if sell and qty > 0:
                log(f"EXIT {ysym}: {sell}")
                # the resting stop must die BEFORE the exit sell exists — both
                # working at once would double-fill at the open (incl. on a
                # regime break, where the stop level never triggered)
                try:
                    cancel_trail_stop(ib, sym_local, dry, why=sell)
                except Exception:
                    continue                 # stop still resting = still protected
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
        free = TARGET_POSITIONS - len([q for _, (_, q) in held.items() if q > 0])
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
            ensure_ccy(ib, ccy, notional, dry)               # convert funds if short
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
            place(ib, c, "BUY", shares, price, dry,
                  reason=f"entry signal, score {a.get('score')}")
            state.setdefault("map", {})[c.symbol] = ysym
            state.setdefault("pos", {})[ysym] = {"entry": price, "hw": price,
                                                 "stop": a.get("stop") or 0}
            free -= 1

        # ---- RESTING STOPS: reconcile broker-side protection last, so stops
        # reflect tonight's ratchets and any exits placed above ----
        try:
            sync_trail_stops(ib, state, dry)
        except Exception as e:
            log(f"  ! stop sync failed ({e})")
        save_state(state)
        publish_state(ib, state, nl)
        log("done.")
    finally:
        ib.disconnect()


def publish_only():
    """Connect, read the account, publish state for the dashboard — trade nothing."""
    ib = IB()
    ib.connect(HOST, PORT, clientId=CLIENT_ID + 3, timeout=25)
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
