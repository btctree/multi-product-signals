#!/usr/bin/env python3
"""Order placement over the IBKR Client Portal Web API (OAuth 1.0a).

This is the WRITE half of the Web API migration. It is deliberately a separate
module from ib_web.py (reads) so the reporting path can never place an order.

Everything here exists because ib_bot.py's transport - the TWS socket API via IB
Gateway - died on 2026-08-24 when IBKR forced passkey 2FA and a headless login
became impossible. The strategy logic is unchanged; only the pipe is new.

THE FOUR THINGS THAT WILL BITE, and how each is handled:

1. SIDE VOCABULARY. Requests take "BUY"/"SELL"; reads come back as "B"/"S".
   ib_bot.py:751 tests `t.order.action == "BUY"` to count pending buys, so an
   unnormalised "B" makes pending_buys permanently empty and the bot opens a
   16th position against NetLiq/15 sizing. normalise_side() is applied to every
   read, not just some.

2. STATUS IS POLLED, NOT PUSHED. The socket API pushed orderStatus; here we must
   ask. Asking too early returns PendingSubmit, which reads as success - so a
   REJECTED order would be recorded as a fill and the bot would believe it had
   exited a position it still holds. poll_status() waits for a TERMINAL state and
   treats "still pending after the timeout" as unknown, never as success.

3. REPLY QUESTIONS. Placing an order can return questions instead of an order id.
   Blanket-confirming them is the fastest way to turn a precautionary warning
   into an unintended fill, so only an explicit allow-list is confirmed; anything
   unrecognised is DECLINED and the order is treated as rejected.

4. CROSS-SESSION IDENTITY. /iserver/account/order/status only covers the current
   brokerage session, so a once-daily bot cannot verify yesterday's orders with
   it. Every order carries a unique cOID and is reconcilable via
   /iserver/account/trades?days=7 on order_ref.
"""
import datetime
import json
import os
import re
import time

import ib_web

CONID_CACHE = os.environ.get("MPS_CONID_CACHE", "/root/conid_cache.json")
ORDERS_LEDGER = os.environ.get("MPS_ORDERS_LEDGER", "/root/orders_ledger.jsonl")

# Message ids we are willing to auto-confirm. These are the precautionary
# questions the TWS API path already bypassed silently, so confirming them
# preserves existing behaviour rather than adding new permissiveness.
# Anything NOT in here is declined - see reply handling below.
SUPPRESSIBLE = {
    "o163",   # price cap / percentage-of-market warning
    "o354",   # market data not subscribed for this instrument
    "o382",   # order size exceeds a display threshold
    "o403",   # pre-open / outside regular trading hours routing
    "o451",   # order value exceeds a soft notional threshold
}

# The OAuth Web API returns a UUID as the question id, not an "oNNN" code, so
# matching ids alone can NEVER hit - on 2026-09-03 a SNOW sell was declined
# over question 0093d5b7-... carrying the routine "without market data"
# warning. Match the TEXT instead.
#
# Deliberately minimal. Only warnings that cannot change what gets executed
# belong here. Anything about price caps, size, or RTH routing DOES change
# execution and must keep being declined until reviewed - a blanket confirm is
# how a bot fills an order it was warned about.
SUPPRESSIBLE_TEXT = (
    "submitting an order without market data",
    # IBKR asks these two together for every market order, and our exits are
    # market-at-open by design (the backtest's exit price IS the next open).
    "market order confirmation",
    # "IB may set a cap (buy) or floor (sell) ... THIS MAY CAUSE AN ORDER THAT
    # WOULD OTHERWISE BE MARKETABLE NOT TO BE TRADED." Reviewed deliberately:
    # the cap is MANDATORY on any IB market order, so declining does not avoid
    # it - it only means the exit never executes at all. Confirming is the only
    # path to a fill. The residual risk is real but unavoidable: on a violently
    # disorderly open the floor can hold the sell back, which is exactly when a
    # stop most wants to fire.
    "confirm mandatory cap price",
)


def _question_is_suppressible(qid, msgs):
    """EVERY message in the bundle must be recognised.

    Matching on "any" would confirm a bundle because ONE message was known,
    carrying an unrecognised warning along with it - and IBKR bundles several
    messages under a single question id, so that is the normal shape, not an
    edge case.
    """
    if str(qid) in SUPPRESSIBLE:
        return True
    items = [str(m) for m in (msgs or [])]
    if not items:
        return False
    for m in items:
        plain = re.sub(r"<[^>]+>", " ", m).replace("&nbsp;", " ").lower()
        if not any(pat in plain for pat in SUPPRESSIBLE_TEXT):
            return False
    return True


def _is_session_error(msg):
    """IBKR errors that mean the brokerage session died, not that the order
    was bad. Kept narrow on purpose: a broad match would retry real
    rejections."""
    low = str(msg).lower()
    return "no bridge" in low or "please query /accounts first" in low


def _reset_session():
    global _iserver_ready
    _iserver_ready = False

TERMINAL_OK = {"filled", "submitted", "presubmitted"}
TERMINAL_BAD = {"cancelled", "apicancelled", "inactive", "rejected"}


class OrderError(RuntimeError):
    pass


def normalise_side(s):
    """'B'/'S'/'BUY'/'SELL' -> 'BUY'/'SELL'. See failure mode 1 above."""
    if not s:
        return ""
    s = str(s).strip().upper()
    if s in ("B", "BUY", "BOT"):
        return "BUY"
    if s in ("S", "SELL", "SLD"):
        return "SELL"
    return s


_iserver_ready = False


def _init_brokerage_session():
    """Establish AND BRIDGE the brokerage session.

    IBKR rejected live orders with

        400 bad request: no bridge. try calling
        'initialize_brokerage_session()' first

    while /iserver/accounts and every read kept working. The cause was posting
    ssodh/init with QUERY PARAMS and no body:

        post("iserver/auth/ssodh/init?publish=true&compete=true")

    IBKR accepts that, reports a session, and still does not bridge it for the
    order routes. ibind's own helper posts the documented JSON body instead, so
    prefer it and fall back to making that exact call on builds without it.
    """
    c = ib_web.client()
    fn = getattr(c, "initialize_brokerage_session", None)
    if callable(fn):
        return fn()
    return c.post("iserver/auth/ssodh/init",
                  params={"publish": True, "compete": True})


def ensure_session(retries=4, delay=3.0):
    """Prime the brokerage session before ANY /iserver/* call.

    Two things are required and both are easy to miss:

    1. POST /iserver/auth/ssodh/init establishes the brokerage session. It
       returns a transient 500 "can't connect to backend service" fairly often
       (observed repeatedly on 2026-08-31), so it is retried rather than trusted
       first time.
    2. GET /iserver/accounts must then be called BEFORE any other /iserver
       endpoint, or they all fail with 500 "Please query /accounts first".
       This is the documented order-of-operations and there is no way around it.

    Note /portfolio/* needs NEITHER of these - it works off the access token
    alone, which is why reporting kept working while ordering did not.
    """
    global _iserver_ready
    if _iserver_ready:
        return True
    last = None
    for _ in range(retries):
        init_ok = False
        try:
            _init_brokerage_session()
            init_ok = True
        except Exception as e:
            last = e                      # transient 500s are expected here
        try:
            accts = ib_web.client().get("iserver/accounts").data
            # BOTH must hold. /iserver/accounts answers happily on a session
            # that was never bridged, so trusting it alone let unbridged
            # sessions reach the order path - orders then died with
            # 400 "no bridge", while every read looked perfectly healthy.
            if accts and init_ok:
                _iserver_ready = True
                return True
        except Exception as e:
            last = e
        time.sleep(delay)
    raise OrderError("could not establish a brokerage session: %s" % str(last)[:300])


def _post(path, payload=None):
    if path.startswith("iserver"):
        ensure_session()
    for _attempt in (0, 1):
        try:
            c = ib_web.client()
            # ibind's RestClient.post takes `params`, NOT `json`: it forwards it
            # as the JSON body -- request(method="POST", ..., json=params).
            r = c.post(path, params=payload) if payload is not None else c.post(path)
            return r.data
        except Exception as e:
            # The session can die MID-RUN: on 2026-09-03 the first sell went
            # through and the next two came back "no bridge". _iserver_ready is
            # a latch, so ensure_session() returned instantly without
            # re-establishing anything. Clear it and re-init once.
            if _attempt == 0 and path.startswith("iserver") and _is_session_error(e):
                _reset_session()
                try:
                    ensure_session()
                    continue
                except Exception:
                    pass
            raise OrderError("POST %s failed: %s" % (path, str(e)[:300]))


def _get(path):
    if path.startswith("iserver"):
        ensure_session()
    try:
        return ib_web.client().get(path).data
    except Exception as e:
        raise OrderError("GET %s failed: %s" % (path, str(e)[:300]))


# ---------------------------------------------------------------- conid ----
def _load_cache():
    try:
        return json.load(open(CONID_CACHE, encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(d):
    tmp = CONID_CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, CONID_CACHE)


# /iserver/secdef/search returns NO `currency` field - every row comes back with
# currency=None - so matching on it could never succeed and every entry lookup
# died with "ambiguous conid ... none in USD". The book being full at 15/15 hid
# it: the entry loop breaks before resolving anything.
#
# What the search DOES return is `description`: the primary listing exchange
# (NYSE, NASDAQ, VALU, GETTEX...). Map the currency we want onto the exchanges
# that trade in it and match on that. The `exchange` field inside `sections` is
# NOT usable - it belongs to the OPT section, not the STK one.
_CCY_EXCHANGES = {
    "USD": {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS", "IEX", "PSE"},
    "HKD": {"SEHK"},
    "JPY": {"TSEJ"},
    "EUR": {"IBIS", "IBIS2", "XETRA", "AEB", "SBF", "EBS", "BVME"},
    "GBP": {"LSE", "LSEETF"},
}
# Every search response carries this catch-all "Corporate Fixed Income" row.
_BOGUS_CONIDS = {"2147483647"}


def resolve_conid(ib_symbol, currency=None, sec_type="STK", cache=True):
    """Symbol -> IBKR contract id, cached.

    Resolution is the identity join for the whole system, so it refuses to
    guess: a wrong conid means trading a different instrument entirely. A
    search for SNOW returns Snowflake on NYSE, SNOWBIRD NV on VALU and SNOW INC
    on GETTEX - picking the first would buy a Dutch company.
    """
    key = "%s|%s|%s" % (ib_symbol, currency or "", sec_type)
    c = _load_cache() if cache else {}
    if key in c:
        return c[key]
    rows = _get("iserver/secdef/search?symbol=%s" % ib_symbol) or []
    cands = []
    for r in rows:
        if not isinstance(r, dict):
            # secdef/search does not always return objects - an "ETH-USD"
            # lookup came back with bare strings and crashed the resolver with
            # "'str' object has no attribute 'get'". Skip, do not crash: the
            # caller must get a clean refusal, not a stack trace.
            continue
        cid = str(r.get("conid") or "").strip()
        if not cid.isdigit() or cid in _BOGUS_CONIDS:
            continue
        secs = r.get("sections") or []
        # No "or not secs" fallback: a row with no sections is not evidence of
        # a tradable line, and accepting it is how a bond row becomes a buy.
        if not any(str(x.get("secType", "")).upper() == sec_type.upper() for x in secs):
            continue
        cands.append(r)
    if not cands:
        raise OrderError("no %s contract found for %r" % (sec_type, ib_symbol))

    def _exch(r):
        return str(r.get("description") or "").upper()

    pick = None
    want = _CCY_EXCHANGES.get(str(currency).upper()) if currency else None
    if currency and not want:
        raise OrderError("no exchange mapping for currency %r - refusing to guess "
                         "which %r listing to trade" % (currency, ib_symbol))
    if want:
        hits = [r for r in cands if _exch(r) in want]
        if len(hits) == 1:
            pick = hits[0]
        elif len(hits) > 1:
            raise OrderError("ambiguous conid for %r: %d %s listings (%s) - refusing to guess"
                             % (ib_symbol, len(hits), currency,
                                ",".join(sorted(_exch(r) for r in hits))))
        else:
            raise OrderError("no %s listing for %r in %s - saw %s - refusing to guess"
                             % (sec_type, ib_symbol, currency,
                                ",".join(sorted(_exch(r) for r in cands)) or "nothing"))
    if pick is None:
        if len(cands) > 1:
            raise OrderError("ambiguous conid for %r (%d candidates, no currency given) "
                             "- refusing to guess" % (ib_symbol, len(cands)))
        pick = cands[0]
    conid = int(pick["conid"])
    if cache:
        c[key] = conid
        _save_cache(c)
    return conid


# ---------------------------------------------------------------- place ----
def make_coid(sym, action, attempt=0):
    """Unique client order id: <=64 chars, [A-Za-z0-9._-], unique per 24h."""
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9]", "", str(sym))[:12]
    coid = "mps-%s-%s-%s" % (safe, normalise_side(action)[:1], stamp)
    if attempt:
        coid += "-r%d" % attempt
    return coid[:64]


def _record(row):
    try:
        with open(ORDERS_LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass          # never let bookkeeping block or fail an order


def place(conid, action, qty, order_type="MKT", limit_price=None, tif="DAY",
          acct=None, coid=None, outside_rth=False):
    """Place ONE order. Returns {order_id, coid, replies, raw}.

    tif defaults to DAY deliberately: ibind's own OrderRequest defaults to GTC,
    and a GTC order silently outliving the session is a real strategy deviation
    for a bot whose every order is meant to expire with the day.
    """
    acct = acct or ib_web.account_id()
    action = normalise_side(action)
    if action not in ("BUY", "SELL"):
        raise OrderError("bad side %r" % action)
    if not qty or qty <= 0:
        raise OrderError("bad quantity %r" % qty)
    coid = coid or make_coid(conid, action)

    order = {"conid": int(conid), "orderType": order_type, "side": action,
             "quantity": float(qty), "tif": tif, "cOID": coid,
             "outsideRTH": bool(outside_rth)}
    if order_type.upper() in ("LMT", "LIMIT"):
        if not limit_price:
            raise OrderError("limit order without a price")
        order["price"] = float(limit_price)

    _record({"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
             "event": "submit", "coid": coid, "conid": int(conid),
             "side": action, "qty": float(qty), "type": order_type,
             "limit": limit_price, "tif": tif})

    resp = _post("iserver/account/%s/orders" % acct, {"orders": [order]})
    replies = []
    # The response is either [{order_id,...}] or a list of question dicts.
    for _ in range(10):
        if not isinstance(resp, list) or not resp:
            break
        first = resp[0]
        if "order_id" in first or "orderId" in first:
            break
        qid = first.get("id")
        msgs = first.get("message") or []
        if not qid:
            break
        known = _question_is_suppressible(qid, msgs)
        replies.append({"id": qid, "confirmed": known, "message": msgs})
        if not known:
            # Never blanket-confirm. An unrecognised question is a refusal.
            _record({"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                     "event": "declined", "coid": coid, "question_id": qid,
                     "message": msgs})
            raise OrderError("unrecognised order question %s declined: %s"
                             % (qid, "; ".join(str(m) for m in msgs)[:300]))
        resp = _post("iserver/reply/%s" % qid, {"confirmed": True})

    order_id = None
    if isinstance(resp, list) and resp:
        order_id = resp[0].get("order_id") or resp[0].get("orderId")
    _record({"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
             "event": "submitted", "coid": coid, "order_id": order_id,
             "replies": replies, "raw": str(resp)[:400]})
    return {"order_id": order_id, "coid": coid, "replies": replies, "raw": resp}


# --------------------------------------------------------------- status ----
def poll_status(order_id, timeout=25.0, interval=2.0):
    """Wait for a TERMINAL order state.

    Returns ('ok'|'REJECTED'|'UNKNOWN', status_string, message). 'UNKNOWN' is
    returned when the order is still pending at timeout - deliberately NOT
    treated as success, because recording an unconfirmed exit as done is how a
    bot ends up believing it is flat while still holding the position.
    """
    deadline = time.time() + timeout
    last, msg = "", ""
    while time.time() < deadline:
        try:
            d = _get("iserver/account/order/status/%s" % order_id)
        except OrderError:
            time.sleep(interval)
            continue
        if isinstance(d, dict):
            last = str(d.get("order_status") or d.get("status") or "").strip()
            msg = "; ".join(str(x) for x in (d.get("order_status_message") or []))[:300] \
                if isinstance(d.get("order_status_message"), list) else str(d.get("order_status_message") or "")
            low = last.lower().replace(" ", "")
            if low in TERMINAL_BAD:
                return "REJECTED", last, msg
            if low == "filled":
                return "ok", last, msg
            if low in TERMINAL_OK:
                return "ok", last, msg
        time.sleep(interval)
    return "UNKNOWN", last or "pending", msg


def trades(days=7, retries=3, delay=2.0):
    """Fills over a multi-day window. The socket API's reqExecutions was
    same-day only, so this is strictly better for reconciling yesterday's
    orders via order_ref == cOID.

    Like /iserver/account/orders, this endpoint answers the FIRST call of a
    session with an empty list and only fills in on a later one. Measured
    2026-09-03: call 1 -> 0 rows, call 2 -> 3 rows, those being that day's
    SNOW/BEN/PANW fills (e.g. "Sold 5 @ 331.57 on NASDAQ"). Believing the first
    answer is why fills_capture wrote nothing and the CGT ledger recorded no
    disposals on a day three positions were closed.

    An empty window is legitimate - no fills in `days` - so this cannot poll
    until non-empty. It retries a bounded number of times and returns what it
    has; a genuinely quiet week costs two extra calls.
    """
    rows = []
    for attempt in range(retries):
        rows = _get("iserver/account/trades?days=%d" % days) or []
        if rows:
            return rows
        if attempt + 1 < retries:
            time.sleep(delay)
    return rows


def open_orders(acct=None, retries=5, delay=2.0):
    """Working orders, with sides normalised. See failure mode 1.

    /iserver/account/orders answers the FIRST call with
    {"orders": [], "snapshot": false} - the book is not collated yet - and only
    a later call carries the real list. Measured live on 2026-09-03:

        call 1: snapshot=False  orders=0
        call 2: snapshot=True   orders=3   <- SNOW/BEN/PANW, genuinely working

    Believing the first answer reports an EMPTY order book while orders are
    live, and the caller (ib_bot's open_syms) then re-places every one of them:
    a second sell of the same position, and a short if both fill. So poll for
    snapshot=true, and if it never arrives, RAISE - never return [] as though
    the book were empty.
    """
    acct = acct or ib_web.account_id()
    d = None
    for _ in range(retries):
        d = _get("iserver/account/orders")
        if not isinstance(d, dict):
            break                      # older/bare-list shape: take it as-is
        if d.get("snapshot"):
            break
        time.sleep(delay)
    else:
        raise OrderError("orders snapshot never ready after %d attempts - refusing "
                         "to report an empty order book" % retries)
    rows = (d or {}).get("orders") if isinstance(d, dict) else d
    out = []
    for o in (rows or []):
        out.append({
            "order_id": o.get("orderId") or o.get("order_id"),
            "coid": o.get("cOID") or o.get("order_ref"),
            "conid": o.get("conid"),
            "symbol": o.get("ticker") or o.get("symbol"),
            "side": normalise_side(o.get("side")),
            "qty": o.get("remainingQuantity") or o.get("totalSize"),
            "status": o.get("status") or o.get("order_status"),
            "sec_type": o.get("secType") or o.get("assetClass"),
        })
    return out


if __name__ == "__main__":
    # read-only self-test; places nothing
    print("account:", ib_web.account_id())
    print("open orders:", json.dumps(open_orders(), indent=1)[:800])
    t = trades(7)
    print("trades(7d):", len(t))
    for r in t[:5]:
        print("  ", r.get("symbol"), r.get("side"), r.get("size"), r.get("price"),
              r.get("order_ref"), r.get("trade_time"))
