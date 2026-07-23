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

from ib_async import IB, LimitOrder, MarketOrder, Forex
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


def place(ib, contract, action, qty, price, dry, reason=""):
    if qty <= 0:
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
        try:
            import fills_capture
            import uk_cgt
            fills_capture.capture(ib, lambda ccy: fx_rate(ib, ccy, "GBP"))
            uk_cgt.build_report()
        except Exception as e:
            log(f"  note: tax pipeline skipped ({e})")
        for cmd in (["add", "data/bot_state.json", "data/fills_ledger.jsonl",
                     "data/tax_report.json"],
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
                f"peak {peak:.0f} — no new orders."); save_state(state); return
        per_pos = nl / TARGET_POSITIONS
        held = held_positions(ib)
        log(f"NetLiq {nl:.0f} {BASE_CCY} | {len(held)} positions | "
            f"target/pos ~{per_pos:.0f}")

        # ---- OPEN ORDERS: never double-place against a working order ----
        ib.reqAllOpenOrders()
        ib.sleep(2)
        open_syms = {t.contract.symbol for t in ib.openTrades()
                     if t.orderStatus.status in ("PendingSubmit", "PreSubmitted",
                                                 "Submitted", "ApiPending")}
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
                # route through a clean SMART contract — the raw position
                # contract requests direct routing (Error 10311 rejections)
                xc = to_ib(ysym)
                sold = None
                if xc is not None:
                    qx = ib.qualifyContracts(xc)
                    if qx:
                        sold = qx[0]
                place(ib, sold if sold is not None else pos.contract,
                      "SELL", abs(qty), price, dry, reason=sell)

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
