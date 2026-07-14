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


def place(ib, contract, action, qty, price, dry):
    if qty <= 0:
        return
    lim = round(price * (1 + LIMIT_BUFFER) if action == "BUY"
                else price * (1 - LIMIT_BUFFER), 4)
    log(f"{action} {qty} {contract.symbol} @ ~{lim} ({contract.currency})")
    if dry or not confirm(f"{action} {qty} {contract.symbol} @ {lim}"):
        return
    order = LimitOrder(action, qty, lim, tif="DAY")
    ib.placeOrder(contract, order)
    ib.sleep(1)


# ---------------- FX funding ----------------
def ensure_ccy(ib, ccy, need_base, dry):
    """Ensure enough <ccy> cash for a purchase worth ~need_base (in BASE_CCY).
    Converts BASE_CCY -> ccy via the CCY/BASE pair, sized with a live midpoint
    rate. FAIL-SAFE: on any problem it logs and returns without converting —
    the stock order may then be rejected for insufficient funds, which is the
    safe failure mode (nothing mis-sized ever gets placed)."""
    if ccy == BASE_CCY:
        return
    try:
        fx = Forex(ccy + BASE_CCY)               # e.g. USDHKD, EURHKD
        if not ib.qualifyContracts(fx):
            log(f"  ! no {ccy}{BASE_CCY} pair; FX convert skipped"); return
        [tk] = ib.reqTickers(fx)
        mid = tk.midpoint()
        if not mid or mid != mid:                # NaN guard
            mid = tk.close
        if not mid or mid != mid:
            log(f"  ! no {ccy}{BASE_CCY} rate; FX convert skipped"); return
        need_ccy = need_base / mid               # target notional in <ccy> units
        have = cash_by_ccy(ib).get(ccy, 0.0)
        if have >= need_ccy:
            return
        qty = int(round(need_ccy - have + 1))
        if qty <= 0:
            return
        log(f"  FX: BUY {qty} {ccy} with {BASE_CCY} (rate ~{mid:.4f}) to fund entry")
        if dry or not confirm(f"convert {BASE_CCY} -> {qty} {ccy}"):
            return
        ib.placeOrder(fx, MarketOrder("BUY", qty))
        ib.sleep(2)
    except Exception as e:
        log(f"  ! FX funding skipped ({e}); order may under-fund")


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

        # ---- EXITS first (free up cash + capital) ----
        for sym_local, (pos, qty) in list(held.items()):
            ysym = state.get("map", {}).get(sym_local)
            if not ysym:
                continue
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
                place(ib, pos.contract, "SELL", abs(qty), price, dry)

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
            if c.symbol in held:
                continue
            price = a.get("price") or 0
            if price <= 0:
                continue
            notional = min(per_pos, MAX_ORDER_BASE)
            ensure_ccy(ib, currency_of(ysym), notional, dry)
            shares = int(notional / price)      # TODO board-lot rounding for HK/JP
            if shares <= 0:
                continue
            place(ib, c, "BUY", shares, price, dry)
            state.setdefault("map", {})[c.symbol] = ysym
            state.setdefault("pos", {})[ysym] = {"entry": price, "hw": price,
                                                 "stop": a.get("stop") or 0}
            free -= 1
        save_state(state)
        log("done.")
    finally:
        ib.disconnect()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="compute + print, place nothing")
    args = ap.parse_args()
    if PORT == 4001 and CONFIRM_FIRST is False and not args.dry:
        log("*** LIVE + UNATTENDED mode ***")
    run(dry=args.dry)
