#!/usr/bin/env python3
"""Broker adapter: lets ib_bot.py run over the IBKR Web API without changing a
single line of strategy logic.

WHY A SHIM AND NOT A REWRITE
ib_bot.py does far more than place orders: it ratchets the ATR trailing stops,
maintains state.json, backfills entry dates from the fills ledger, enforces the
kill switch, sweeps fills into the tax ledger and publishes the dashboard. A
standalone executor would place trades and silently stop doing all of that - the
stops would go stale, which is worse than not trading. So instead this module
impersonates ib_async's objects closely enough that ib_bot.py cannot tell the
difference, and the only production change is its import line.

BACKEND SELECTION
    IB_BACKEND=socket  (default)  re-export ib_async verbatim - byte-identical
                                  to today's behaviour, the instant rollback
    IB_BACKEND=web                the OAuth Web API path

WHAT ib_bot.py ACTUALLY NEEDS (the whole surface this must satisfy)
    IB(): connect, disconnect, sleep, positions, accountValues,
          reqContractDetails, reqTickers, qualifyContracts, placeOrder,
          openTrades, reqAllOpenOrders, reqExecutions
    Order types: MarketOrder, LimitOrder;  Contracts: Stock, Crypto, Forex
    Trade: .contract .order.action .orderStatus.status .log[].message

THE SEMANTICS THAT MATTER (each one is a way to lose money quietly)

  ib.sleep(n) is NOT a sleep. In ib_async it pumps the event loop so that
  trade.orderStatus.status is meaningful on the very next line. Here it sleeps
  AND refreshes every Trade this run has created, preserving that contract. Get
  this wrong and _order_verdict reads a stale 'PendingSubmit' as success.

  Error 110. ib_bot.py:296 does `if status != "REJECTED" or "110" not in err`
  to drive its coarser-tick retry ladder. The Web API has no error 110, so a
  price-increment rejection is TRANSLATED into an ib_async-shaped message. The
  strategy file is untouched and its retry loop fires exactly as before.

  Side vocabulary. openTrades() must report 'BUY', not the Web API's 'B', or
  pending_buys is always empty and the bot opens a 16th position.

  avgCost. ib_async reports per-share average cost; so does the Web API's
  avgCost field. Confirmed against the live book (NTAP 191.0511, URI 1058.05).
"""
import os
import time

BACKEND = os.environ.get("IB_BACKEND", "socket").strip().lower()

if BACKEND != "web":
    # ---- passthrough: exactly today's behaviour, nothing intercepted -------
    from ib_async import IB, LimitOrder, MarketOrder, Forex, Stock, Crypto  # noqa: F401
    try:
        from ib_async import ExecutionFilter  # noqa: F401
    except Exception:                                  # pragma: no cover
        ExecutionFilter = None
else:
    import datetime
    import ib_web
    import ib_orders

    # ------------------------------------------------------------ values --
    class Contract(object):
        def __init__(self, symbol="", secType="STK", currency="USD",
                     exchange="SMART", primaryExchange="", conId=0, localSymbol=""):
            self.symbol = symbol
            self.secType = secType
            self.currency = currency
            self.exchange = exchange
            self.primaryExchange = primaryExchange
            self.conId = conId
            self.localSymbol = localSymbol or symbol

        def __repr__(self):
            return "Contract(%s,%s,%s,conId=%s)" % (self.symbol, self.secType,
                                                    self.currency, self.conId)

    def Stock(symbol, exchange="SMART", currency="USD", primaryExchange=""):
        return Contract(symbol, "STK", currency, exchange, primaryExchange)

    def Crypto(symbol, exchange="PAXOS", currency="USD"):
        return Contract(symbol, "CRYPTO", currency, exchange)

    def Forex(pair, exchange="IDEALPRO"):
        pair = str(pair)
        c = Contract(pair[:3], "CASH", pair[3:6] if len(pair) >= 6 else "USD", exchange)
        c.localSymbol = pair
        return c

    class Order(object):
        def __init__(self, action="", totalQuantity=0, orderType="MKT",
                     lmtPrice=None, tif="DAY"):
            self.action = ib_orders.normalise_side(action)
            self.totalQuantity = totalQuantity
            self.orderType = orderType
            self.lmtPrice = lmtPrice
            self.tif = tif

    def MarketOrder(action, totalQuantity, tif="DAY", **kw):
        return Order(action, totalQuantity, "MKT", None, tif)

    def LimitOrder(action, totalQuantity, lmtPrice, tif="DAY", **kw):
        return Order(action, totalQuantity, "LMT", lmtPrice, tif)

    class Position(object):
        def __init__(self, contract, position, avgCost):
            self.contract = contract
            self.position = position
            self.avgCost = avgCost

    class AccountValue(object):
        def __init__(self, tag, value, currency):
            self.tag = tag
            self.value = value
            self.currency = currency

    class ContractDetails(object):
        def __init__(self, minTick=0.01, sizeIncrement=1, minSize=1):
            self.minTick = minTick
            self.sizeIncrement = sizeIncrement
            self.minSize = minSize

    class Ticker(object):
        def __init__(self, last=None, close=None, bid=None, ask=None):
            self.last = last
            self.close = close
            self.bid = bid
            self.ask = ask

        def midpoint(self):
            if self.bid and self.ask:
                return (self.bid + self.ask) / 2.0
            return None

        def marketPrice(self):
            return self.last or self.midpoint() or self.close

    class _LogEntry(object):
        def __init__(self, message=""):
            self.message = message
            self.time = datetime.datetime.now(datetime.timezone.utc)

    class _OrderStatus(object):
        def __init__(self, status="PendingSubmit"):
            self.status = status

    class Trade(object):
        def __init__(self, contract, order, order_id=None, coid=None):
            self.contract = contract
            self.order = order
            self.orderStatus = _OrderStatus()
            self.log = []
            self.order_id = order_id
            self.coid = coid

    class ExecutionFilter(object):                     # noqa: N801
        def __init__(self, *a, **kw):
            pass

    # --------------------------------------------------------------- IB ---
    _TICK_DEFAULT = 0.01

    class IB(object):
        """Duck-typed stand-in for ib_async.IB over the Web API."""

        def __init__(self):
            self._trades = []
            self._acct = None
            self._open_cache = None
            self._connected = False

        # -- session -------------------------------------------------------
        def connect(self, host=None, port=None, clientId=None, timeout=30):
            """No socket to open; instead prove the brokerage session is live so
            a failure surfaces HERE, exactly where ib_bot.py already expects a
            connection failure (and where connect_or_heal re-raises)."""
            ib_orders.ensure_session()
            self._acct = ib_web.account_id()
            self._connected = True
            return self

        def disconnect(self):
            self._connected = False

        def isConnected(self):
            return self._connected

        def sleep(self, secs=0):
            """Sleep AND refresh every Trade created this run - ib_async's
            sleep() pumps the event loop so orderStatus is fresh on the next
            line, and _order_verdict depends on that."""
            if secs:
                time.sleep(secs)
            self._refresh_trades()

        # -- account -------------------------------------------------------
        def accountValues(self, account=""):
            netliq, cash = ib_web.netliq_and_cash(self._acct)
            out = [AccountValue("NetLiquidation", str(netliq), "HKD")]
            for ccy, amt in (cash or {}).items():
                out.append(AccountValue("CashBalance", str(amt), ccy))
            return out

        def positions(self, account=""):
            out = []
            for p in ib_web.positions(self._acct):
                c = Contract(symbol=str(p["ib_symbol"]),
                             secType=p.get("sec_type") or "STK",
                             currency=p.get("ccy") or "USD",
                             conId=p.get("conid") or 0)
                out.append(Position(c, p["qty"], p.get("avg_cost") or 0.0))
            return out

        # -- contracts -----------------------------------------------------
        def qualifyContracts(self, *contracts):
            ok = []
            for c in contracts:
                try:
                    if not c.conId:
                        c.conId = ib_orders.resolve_conid(
                            c.symbol, c.currency,
                            "CASH" if c.secType == "CASH" else c.secType)
                    ok.append(c)
                except Exception:
                    pass          # ib_async also just omits what it cannot qualify
            return ok

        def reqContractDetails(self, contract):
            try:
                if not contract.conId:
                    self.qualifyContracts(contract)
                d = ib_web.client().get(
                    "iserver/contract/%s/info-and-rules" % contract.conId).data or {}
                rules = d.get("rules") or {}
                inc = rules.get("increment") or d.get("increment")
                tick = float(inc) if inc else _TICK_DEFAULT
                # incrementRules is a tiered ladder; the FIRST band is the one
                # that applies at low prices and is the conservative choice.
                ir = rules.get("incrementRules") or []
                if ir and isinstance(ir, list):
                    try:
                        tick = float(ir[0].get("increment") or tick)
                    except Exception:
                        pass
                size_inc = rules.get("sizeIncrement") or d.get("sizeIncrement") or 1
                return [ContractDetails(tick, float(size_inc), float(size_inc))]
            except Exception:
                return [ContractDetails(_TICK_DEFAULT, 1, 1)]

        def reqTickers(self, *contracts):
            out = []
            for c in contracts:
                px = None
                try:
                    if not c.conId:
                        self.qualifyContracts(c)
                    d = ib_web.client().get(
                        "iserver/marketdata/snapshot?conids=%s&fields=31,84,86"
                        % c.conId).data
                    row = (d or [{}])[0]
                    raw = row.get("31") or row.get("84") or row.get("86")
                    if raw is not None:
                        # field 31 can carry a C/H prefix (close / halted)
                        px = float(str(raw).lstrip("CHc "))
                except Exception:
                    px = None
                out.append(Ticker(last=px, close=px))
            return out

        # -- orders --------------------------------------------------------
        def placeOrder(self, contract, order):
            if not contract.conId:
                self.qualifyContracts(contract)
            t = Trade(contract, order)
            try:
                res = ib_orders.place(
                    contract.conId, order.action, order.totalQuantity,
                    order_type=order.orderType,
                    limit_price=getattr(order, "lmtPrice", None),
                    tif=getattr(order, "tif", "DAY"), acct=self._acct)
                t.order_id, t.coid = res["order_id"], res["coid"]
                t.orderStatus.status = "PendingSubmit"
            except Exception as e:
                msg = str(e)
                t.orderStatus.status = "Inactive"
                t.log.append(_LogEntry(_translate_error(msg)))
            self._trades.append(t)
            self._open_cache = None
            return t

        def _refresh_trades(self):
            for t in self._trades:
                if not t.order_id or t.orderStatus.status in ("Filled", "Cancelled",
                                                              "ApiCancelled", "Inactive"):
                    continue
                try:
                    verdict, status, msg = ib_orders.poll_status(t.order_id,
                                                                 timeout=0.1, interval=0.1)
                    if status:
                        t.orderStatus.status = _map_status(status, verdict)
                    if msg:
                        t.log.append(_LogEntry(_translate_error(msg)))
                except Exception:
                    pass

        def reqAllOpenOrders(self):
            self._open_cache = None
            return self.openTrades()

        def openTrades(self):
            """Working orders as Trade objects. Cached per run: ib_bot.py calls
            this twice and /iserver/account/orders is rate limited to about one
            request every five seconds."""
            if self._open_cache is not None:
                return self._open_cache
            out = []
            try:
                for o in ib_orders.open_orders(self._acct):
                    c = Contract(symbol=str(o.get("symbol") or ""),
                                 secType=o.get("sec_type") or "STK",
                                 conId=o.get("conid") or 0)
                    od = Order(o.get("side"), o.get("qty") or 0)
                    t = Trade(c, od, o.get("order_id"), o.get("coid"))
                    t.orderStatus.status = _map_status(o.get("status") or "", "ok")
                    out.append(t)
            except Exception:
                out = []
            self._open_cache = out
            return out

        def reqExecutions(self, execFilter=None):
            """Fills for the tax sweep. The Web API window is 7 DAYS versus
            reqExecutions' same-day, which is strictly better - fills_capture
            dedupes on execId."""
            try:
                return ib_orders.trades(7)
            except Exception:
                return []

        # ib_async compatibility no-ops used elsewhere in the codebase
        def reqMarketDataType(self, *a, **kw):
            return None

        def cancelOrder(self, order):
            return None

    def _map_status(status, verdict="ok"):
        s = str(status or "").strip().lower().replace(" ", "")
        if s in ("filled",):
            return "Filled"
        if s in ("cancelled", "canceled", "apicancelled"):
            return "Cancelled"
        if s in ("inactive", "rejected"):
            return "Inactive"
        if s in ("presubmitted", "submitted", "presubmit"):
            return "Submitted"
        return "PendingSubmit"

    _TICK_WORDS = ("price does not conform", "minimum price variation",
                   "price increment", "tick size", "minimum tick")

    def _translate_error(msg):
        """Speak ib_async's dialect so ib_bot.py's existing handling fires.

        Its coarser-tick retry ladder keys on the literal substring '110', which
        the Web API never produces. Rather than edit the strategy file, a
        price-increment rejection is reshaped into the message ib_async would
        have delivered."""
        m = str(msg or "")
        low = m.lower()
        if any(w in low for w in _TICK_WORDS) and "110" not in m:
            return ("Error 110, reqId 0: The price does not conform to the "
                    "minimum price variation for this contract. " + m[:160])
        return m
