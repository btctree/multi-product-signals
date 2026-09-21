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

  Error 110. ib_bot.place() drives its coarser-tick retry ladder only when a
  refusal STARTS with ib_async's own prefix "Error 110, reqId N: ". The Web
  API has no error 110, so a price-increment rejection is TRANSLATED into that
  shape, and nothing else is: every other refusal keeps its own text, which
  never starts that way. It used to be the bare substring '110' anywhere, and a
  cOID, conid, quantity or price echoed in an unrelated refusal re-sent the
  order (board review 2026-09-21).

  Side vocabulary. openTrades() must report 'BUY', not the Web API's 'B', or
  pending_buys is always empty and the bot opens a 16th position.

  avgCost. ib_async reports per-share average cost; so does the Web API's
  avgCost field. Confirmed against the live book (NTAP 191.0511, URI 1058.05).
"""
import os
import re
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
        def __init__(self, minTick=0.01, sizeIncrement=1, minSize=1, fraqInt=0,
                     priceBands=None, isFallback=False):
            self.minTick = minTick
            self.sizeIncrement = sizeIncrement
            self.minSize = minSize
            # decimal places IB accepts for a FRACTIONAL quantity, 0 = whole
            # units only. ETH reports 5, so sizeIncrement=1 does NOT mean the
            # instrument is whole-unit-only.
            self.fraqInt = fraqInt
            # IB's WHOLE price-banded tick ladder, [(lowerEdge, increment)]
            # sorted by edge. minTick stays the first band, as every existing
            # reader expects; only ib_bot's band lookup reads this. ib_async's
            # ContractDetails has no such field, so readers use getattr.
            self.priceBands = list(priceBands or [])
            # True when these are the shim's DEFAULTS because IB said nothing
            # (the request raised, or the payload carried no increment at all).
            # A caller that caches rules must not freeze a failure for the run.
            self.isFallback = bool(isFallback)

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

    # IBKR returns currency: null on both secdef/search and account/trades, so
    # it is derived from the venue. An unmapped exchange yields None and
    # fills_capture flags the row rate_missing rather than guessing a rate.
    _EXCH_CCY = {
        "NYSE": "USD", "NASDAQ": "USD", "ARCA": "USD", "AMEX": "USD",
        "BATS": "USD", "IEX": "USD", "PSE": "USD", "SEHK": "HKD",
        "TSEJ": "JPY", "IBIS": "EUR", "IBIS2": "EUR", "XETRA": "EUR",
        "AEB": "EUR", "SBF": "EUR", "EBS": "EUR", "BVME": "EUR",
        "LSE": "GBP", "LSEETF": "GBP",
        # The euro venues ib_orders now resolves (Madrid, Brussels, Helsinki,
        # Vienna, Lisbon). Without them a fill there reached the tax ledger with
        # no currency, guessed as USD and flagged, instead of booked in EUR.
        "BM": "EUR", "ENEXT.BE": "EUR", "HEX": "EUR", "VSE": "EUR", "BVL": "EUR",
    }

    class _Exec(object):
        execId = ""; time = None; side = "SLD"; shares = 0.0; price = 0.0
        # Who placed it: IB's order_ref is the cOID sent at submission
        # (ib_orders.make_coid -> "mps-..." on every bot order), order_id is
        # IB's own id. None = IB did not send one.
        order_ref = None; order_id = None

    class _Comm(object):
        commission = 0.0; currency = ""

    class _Fill(object):
        execution = None; contract = None; commissionReport = None; time = None

    def _trade_time(raw, epoch_ms=None):
        """IBKR sends "20260903-13:30:01"; fall back to the epoch-ms field."""
        import datetime as _dt
        try:
            return _dt.datetime.strptime(str(raw), "%Y%m%d-%H:%M:%S")
        except Exception:
            pass
        try:
            return _dt.datetime.utcfromtimestamp(float(epoch_ms) / 1000.0)
        except Exception:
            return None


    class ExecutionFilter(object):                     # noqa: N801
        def __init__(self, *a, **kw):
            pass

    def _listing_venue(c):
        """The listing exchange resolve_conid must match, or "" for none.

        US is the exception, deliberately. contracts.to_ib stamps
        primaryExchange="NASDAQ" on EVERY US stock - NYSE listings such as SNOW
        included - as a SMART-routing hint, not as the listing. Passing it on
        would refuse every NYSE name, so a USD lookup keeps matching the whole
        US venue set, exactly as it did before venues were passed at all."""
        if str(getattr(c, "currency", "") or "").upper() == "USD":
            return ""
        return str(getattr(c, "primaryExchange", "") or "").strip()

    def _price_bands(increment_rules):
        """[(lowerEdge, increment)] sorted by edge, from IB's incrementRules.

        IB: "if the current mark price is at or above the lower edge, the given
        increment is used". A malformed row is dropped rather than guessed at:
        a missing band only means ib_bot falls back to its RTS 11 floor."""
        out = []
        for r in increment_rules if isinstance(increment_rules, list) else []:
            try:
                edge = float(r.get("lowerEdge"))
                inc = float(r.get("increment"))
            except Exception:
                continue
            if edge == edge and inc == inc and edge >= 0 and 0 < inc < float("inf"):
                out.append((edge, inc))
        return sorted(out)

    # IBKR's FIRST /iserver/marketdata/snapshot for a conid is a pre-flight: it
    # opens the stream and answers with no price fields at all. reqTickers
    # asked once, so under the web backend every stock quote came back None,
    # live_base_price always fell back to the signal price, and ib_bot.place()'s
    # "signal price stale vs IB quote - re-based" guard could never fire (board
    # review 2026-09-21). The snapshot is asked again a few times, a short pause
    # apart, before the quote falls back to None as before. On an account with
    # no market data for the venue every ask stays empty: that costs about
    # three seconds per order, and the order still goes out on the card's price.
    _SNAPSHOT_TRIES = 4
    _SNAPSHOT_WAIT_S = 1.0
    _snapshot_sleep = time.sleep          # the tests swap in a no-op

    def _snapshot_number(raw):
        """A positive price from a snapshot field, or None. Field 31 can carry
        a C (prior close) or H (halted) prefix."""
        if raw is None or raw == "":
            return None
        try:
            px = float(str(raw).strip().lstrip("CHc "))
        except ValueError:
            return None
        return px if px == px and 0 < px < float("inf") else None

    def _snapshot_price(conid):
        """Last (31), else bid (84), else ask (86) for one conid, or None.

        Through ib_orders._get, NOT ib_web.client() directly - the same rule as
        the FX branch of reqTickers: an /iserver path needs a live brokerage
        session and _get calls ensure_session() first. A failed GET raises, and
        reqTickers turns that into None."""
        for i in range(_SNAPSHOT_TRIES):
            if i:
                _snapshot_sleep(_SNAPSHOT_WAIT_S)
            d = ib_orders._get(
                "iserver/marketdata/snapshot?conids=%s&fields=31,84,86" % conid)
            row = d[0] if isinstance(d, list) and d and isinstance(d[0], dict) else {}
            for f in ("31", "84", "86"):
                px = _snapshot_number(row.get(f))
                if px is not None:
                    return px
        return None

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

        def positions(self, account="", fresh=False):
            """fresh=True flushes IBKR's positions cache first and RAISES if it
            cannot (ib_web.positions). Web-only: ib_async's positions() has no
            such argument, because its positions are pushed live by the socket."""
            out = []
            for p in ib_web.positions(self._acct, fresh=fresh):
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
                    if str(c.secType).upper() == "CASH":
                        # Quotes come from /iserver/exchangerate and need no
                        # conid, but PLACING an FX order does - without this a
                        # conversion would be sent with conid 0. secdef/search
                        # has no per-pair conid; /iserver/currency/pairs does.
                        if not c.conId:
                            cid, sym = ib_orders.fx_pair_conid(c.symbol, c.currency)
                            if cid:
                                c.conId = cid
                                c.localSymbol = sym or c.localSymbol
                        ok.append(c)
                        continue
                    if not c.conId:
                        # The venue MUST go through. Without it SAN.MC
                        # (Santander, Madrid) was looked up as "SAN in EUR" and
                        # matched Sanofi on SBF; place() would then re-base the
                        # limit onto Sanofi's ~76 quote, turning 129 shares
                        # sized for 12.14 into a ~EUR 9,900 order against
                        # ~EUR 1,566 funded (review 2026-09-17, reproduced with
                        # a stubbed search).
                        c.conId = ib_orders.resolve_conid(
                            c.symbol, c.currency, c.secType,
                            primary_exchange=_listing_venue(c))
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
                bands = []
                if ir and isinstance(ir, list):
                    try:
                        tick = float(ir[0].get("increment") or tick)
                    except Exception:
                        pass
                    # ...but it is NOT a legal increment at higher prices:
                    # BAYN went out at 48.4108 on the 0.0001 first band and
                    # Xetra refused it (2026-09-13/14). Keep every band too, so
                    # ib_bot can price on the band the order actually sits in.
                    bands = _price_bands(ir)
                answered = bool(inc) or bool(bands)
                size_inc = rules.get("sizeIncrement") or d.get("sizeIncrement") or 1
                # fraqInt: decimals allowed on a fractional order. ETH returns
                # 5 alongside sizeIncrement 1 - reading only sizeIncrement made
                # a sub-1-unit position size round to zero and skip silently.
                fraq = 0
                try:
                    if rules.get("fraqTypes"):
                        fraq = int(rules.get("fraqInt") or 0)
                except Exception:
                    fraq = 0
                return [ContractDetails(tick, float(size_inc), float(size_inc), fraq,
                                        priceBands=bands, isFallback=not answered)]
            except Exception:
                return [ContractDetails(_TICK_DEFAULT, 1, 1, 0, isFallback=True)]

        def reqTickers(self, *contracts):
            out = []
            for c in contracts:
                px = None
                if str(c.secType).upper() == "CASH":
                    # Forex(ab) means "units of b per 1 a", which is exactly
                    # what /iserver/exchangerate?source=a&target=b returns.
                    # Verified live: USD->GBP 0.73874613, HKD->GBP 0.0942238,
                    # JPY->GBP 0.00474472. Quoting a resolved conid instead
                    # gave 0.7388 for every pair, because the conid is the
                    # CURRENCY, not the pair.
                    try:
                        # via ib_orders._get, NOT ib_web.client() directly:
                        # /iserver/exchangerate needs a live brokerage session,
                        # and _get calls ensure_session() for iserver paths.
                        # Going direct worked at 23:35 (session already up from
                        # placing orders) and failed at 09:00 with no session -
                        # every entry then died "no USD/HKD rate to size order".
                        d = ib_orders._get(
                            "iserver/exchangerate?target=%s&source=%s"
                            % (c.currency, c.symbol)) or {}
                        r = d.get("rate") if isinstance(d, dict) else None
                        px = float(r) if r else None
                        if px is not None and px <= 0:
                            px = None
                    except Exception:
                        px = None
                    out.append(Ticker(last=px, close=px))
                    continue
                try:
                    if not c.conId:
                        self.qualifyContracts(c)
                    # No conid, no quote: asking for conid 0 four times over
                    # only spends the pauses.
                    if c.conId:
                        px = _snapshot_price(c.conId)
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
                    tif=getattr(order, "tif", "DAY"), acct=self._acct,
                    allow_price_cap=bool(getattr(order, "allow_price_cap", False)))
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
                    # currency defaults to USD on Contract, which would be a
                    # silent lie for a TSE or SEHK order; pass "" when the row
                    # does not say, so callers can tell "unknown" from "USD".
                    c = Contract(symbol=str(o.get("symbol") or ""),
                                 secType=o.get("sec_type") or "STK",
                                 currency=str(o.get("currency") or ""),
                                 conId=o.get("conid") or 0)
                    od = Order(o.get("side"), o.get("qty") or 0)
                    # totalQuantity stays what is LEFT to fill, as ib_bot's
                    # cash reserve reads it; totalSize is the whole order, as
                    # ib_async's own totalQuantity is. Only ib_commands reads
                    # it (review 2026-09-17, phone SELL netting).
                    od.totalSize = o.get("total_qty")
                    try:
                        od.lmtPrice = (float(o["price"])
                                       if o.get("price") not in (None, "") else None)
                    except (TypeError, ValueError):
                        od.lmtPrice = None
                    t = Trade(c, od, o.get("order_id"), o.get("coid"))
                    t.orderStatus.status = _map_status(o.get("status") or "", "ok")
                    out.append(t)
            except Exception as e:
                # Do NOT fall back to []. That tells the caller "nothing is
                # working", and ib_bot then re-places orders that ARE live:
                # two sells of the same position, and a short if both fill.
                # openTrades() is read BEFORE any order is placed, so raising
                # here aborts the run cleanly with nothing sent. A skipped run
                # is recoverable; a duplicate fill is not.
                raise RuntimeError(
                    "cannot read working orders (%s) - refusing to trade blind: "
                    "an empty list here would duplicate live orders" % str(e)[:160])
            self._open_cache = out
            return out

        def reqExecutions(self, execFilter=None, strict=False):
            """Fills for the tax sweep, shaped like ib_async's Fill.

            The Web API window is 7 DAYS versus reqExecutions' same-day, which
            is strictly better - fills_capture dedupes on execId.

            strict=True RAISES when the trades read fails instead of returning
            []. The tax sweep wants [] (a failed sweep must not block the
            dividend sweep), but ib_bot's start-of-run pocket sweep must be able
            to tell "no fills" from "could not read fills": a fill it cannot see
            leaves the bot's HKD pocket too high, the direction that could spend
            the operator's transfer money.

            order_ref and order_id are kept on the Execution. order_ref is the
            cOID every bot order carries ("mps-..."); earmark.bot_pocket uses it
            to tell the bot's own HKD from the operator's.

            It MUST return objects, not the raw dicts: fills_capture reads
            f.execution / f.contract / f.commissionReport, so handing back
            dicts failed with "'dict' object has no attribute 'execution'".
            That exception is swallowed by fills_capture's own try/except (so a
            fills failure cannot block the dividend sweep), which is why the
            CGT ledger silently recorded nothing on a day three positions were
            closed - tax_report.json still showed open_positions 15 against an
            actual 12.
            """
            out = []
            try:
                rows = ib_orders.trades(7) or []
            except Exception:
                if strict:
                    raise
                return []
            if strict and not isinstance(rows, list):
                raise RuntimeError("unexpected trades payload %r" % str(rows)[:120])
            for t in rows:
                if not isinstance(t, dict):
                    continue
                try:
                    exch = str(t.get("exchange") or "").upper()
                    sec = str(t.get("sec_type") or "STK")
                    ccy = t.get("currency") or _EXCH_CCY.get(exch) or ""
                    if sec.upper() == "CASH" and not ccy:
                        # An FX fill's price and commission are denominated in
                        # the pair's QUOTE currency. IDEALPRO rows carry no
                        # `currency`, and IDEALPRO is rightly absent from
                        # _EXCH_CCY - no one currency describes it - so this
                        # was landing empty and being guessed as USD further
                        # down. Recover the pair from the conid instead.
                        ccy = ib_orders.fx_quote_ccy(t.get("symbol"),
                                                     t.get("conid"))
                    c = Contract(symbol=str(t.get("symbol") or ""),
                                 secType=sec,
                                 currency=ccy, exchange=exch,
                                 conId=int(t.get("conid") or 0))
                    e = _Exec()
                    e.execId = str(t.get("execution_id") or "")
                    e.time = _trade_time(t.get("trade_time"), t.get("trade_time_r"))
                    e.side = ("BOT" if str(t.get("side") or "").upper().startswith("B")
                              else "SLD")
                    e.shares = float(t.get("size") or 0)
                    e.price = float(t.get("price") or 0)   # IBKR sends these as strings
                    ref = t.get("order_ref")
                    e.order_ref = str(ref) if ref not in (None, "") else None
                    e.order_id = t.get("order_id")
                    cr = _Comm()
                    cr.commission = float(t.get("commission") or 0)
                    cr.currency = ccy
                    f = _Fill()
                    f.execution, f.contract, f.commissionReport = e, c, cr
                    f.time = e.time
                    out.append(f)
                except Exception:
                    continue        # one malformed row must not lose the rest
            return out

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

    # ib_async's own prefix for a price-increment refusal, and the ONLY thing
    # ib_bot.place() retries on: "Error 110, reqId N: " at the very start. The
    # comma matters - "Error 1100, reqId -1: Connectivity ... lost" is not one.
    _TICK_MARK = re.compile(r"Error 110, reqId -?\d+: ")

    def _translate_error(msg):
        """Speak ib_async's dialect so ib_bot.py's existing handling fires.

        Its coarser-tick retry ladder keys on a message that STARTS with
        "Error 110, reqId N: ", which the Web API never produces. Rather than
        edit the strategy file, a price-increment rejection is reshaped into the
        message ib_async would have delivered.

        EVERY genuine tick refusal is tagged, including one whose own price
        contains 110 ("The price 110.005 does not conform ..."): the old test
        here skipped any message with '110' anywhere in it. Every other message
        passes through with its own text - "order not accepted: ...", "POST ...
        failed: ..." and the like - so an echoed cOID ('mps-1-B-20261105...'),
        conid, quantity or price can never read as a tick refusal and re-send
        the order under a new cOID (board review 2026-09-21).

        Every trade.log message passes through here, and trade.log is what
        ib_bot and ib_commands copy into the PUBLISHED activity rows - so the
        account id is redacted here too, for any exception that did not come
        from ib_orders (whose OrderError already redacts)."""
        m = ib_web.redact(msg or "")
        low = m.lower()
        if any(w in low for w in _TICK_WORDS) and not _TICK_MARK.match(m):
            return ("Error 110, reqId 0: The price does not conform to the "
                    "minimum price variation for this contract. " + m[:160])
        return m
