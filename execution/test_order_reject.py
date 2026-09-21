#!/usr/bin/env python3
"""Golden tests: an IB refusal is a rejection, and the tick retry recovers it.

Run from this directory:  python test_order_reject.py

2026-09-13/14: IB refused both BAYN buys at submission with
{'error': 'The price 48.4108 does not conform to the minimum price variation
of 0.01 for this instrument.'} and created no order - but ib_orders.place
returned normally with order_id None, so the bot recorded "sent" and its
coarser-tick retry never fired. Nothing was ever working at Xetra.
"""
import os

os.environ.setdefault("IB_BACKEND", "web")
# Every /root default at a temp path before import (review 2026-09-17, test
# isolation: none was repointed here). See testenv.py.
import testenv                                      # noqa: E402
testenv.isolate("mps-reject-")
import broker                                       # noqa: E402
import ib_bot                                       # noqa: E402
import ib_orders                                    # noqa: E402
testenv.assert_isolated()

IB_REFUSAL = {"error": "The price 48.4108 does not conform to the minimum "
                       "price variation of 0.01 for this instrument."}


def t1_refusal_raises():
    real_post, real_record = ib_orders._post, ib_orders._record
    try:
        ib_orders._record = lambda *a, **k: None
        ib_orders._post = lambda path, body: IB_REFUSAL
        try:
            ib_orders.place(68598660, "BUY", 33, order_type="LMT",
                            limit_price=48.4108, acct="U1")
            raise AssertionError("an IB refusal must not return as a submission")
        except ib_orders.OrderError as e:
            assert "minimum price variation" in str(e), e
        # a list-shaped error and a missing id are refusals too
        ib_orders._post = lambda path, body: [{"error": "rejected"}]
        try:
            ib_orders.place(1, "BUY", 1, order_type="LMT", limit_price=1.0, acct="U1")
            raise AssertionError("list-shaped error must raise")
        except ib_orders.OrderError:
            pass
        # a real acceptance still returns its id
        ib_orders._post = lambda path, body: [{"order_id": "123", "order_status": "Submitted"}]
        assert ib_orders.place(1, "BUY", 1, order_type="LMT", limit_price=1.0,
                               acct="U1")["order_id"] == "123"
    finally:
        ib_orders._post, ib_orders._record = real_post, real_record
    print("t1 an IB refusal raises; an acceptance still returns OK")


def t2_refusal_reaches_the_retry_as_error_110():
    msg = broker._translate_error("order not accepted: " + IB_REFUSAL["error"])
    # At the START, in ib_async's shape: ib_bot.place() no longer retries on a
    # '110' found anywhere (board review 2026-09-21, see t5/t6).
    assert msg.startswith("Error 110, reqId 0: "), msg
    print("t2 the refusal is translated to Error 110 OK")


def t3_retry_ladder_lands_on_a_legal_price():
    # Replay the web shim's behaviour: a non-0.01 price is refused exactly as
    # IB refused BAYN; a 0.01 multiple is accepted.
    #
    # REWRITTEN 2026-09-17 (fix/eu-tick-bands), deliberately. This used to replay
    # the live 48.17 signal and assert 48.4108 -> 48.411 -> 48.41. EUR limits now
    # start on the RTS 11 band-6 floor (0.005 at 20-50), so 48.17 goes out as a
    # legal 48.41 FIRST time and never reaches the retry at all - that replay
    # lives on in test_eu_tick t4. The intent kept here is that a refusal still
    # REACHES the retry and recovers, so the signal is moved to 48.1741, whose
    # floor price 48.415 is legal at band 6 but not at BAYN's 0.01. The retry now
    # takes IB's stated 0.01 directly (web text) and the next rung (socket text
    # with no number); both land on 48.41 at attempt 2.
    for refusal in (
            lambda p: broker._translate_error(
                "order not accepted: The price %s does not conform to the "
                "minimum price variation of 0.01 for this instrument." % p),
            lambda p: ("Error 110, reqId 7: The price does not conform to the "
                       "minimum price variation for this contract.")):
        sent = []

        class CD:
            minTick = 0.0001                         # IB's lowest ladder band

        class Entry:
            def __init__(self, m):
                self.message = m

        class IB:
            def reqContractDetails(self, c):
                return [CD()]

            def placeOrder(self, contract, order):
                t = broker.Trade(contract, order)
                p = order.lmtPrice
                sent.append(p)
                if abs(round(p * 100) - p * 100) > 1e-6:
                    t.orderStatus.status = "Inactive"
                    t.log.append(Entry(refusal(p)))
                else:
                    t.orderStatus.status = "PreSubmitted"
                return t

            def sleep(self, n):
                pass

        class C:
            symbol, currency, conId, secType = "BAYN", "EUR", 68598660, "STK"

        old = (ib_bot.live_base_price, ib_bot.CONFIRM_FIRST)
        try:
            ib_bot.live_base_price = lambda ib, c, fallback: fallback
            ib_bot.CONFIRM_FIRST = False
            ib_bot._TICK_CACHE.clear()
            status = ib_bot.place(IB(), C(), "BUY", 33, 48.1741, False)
        finally:
            ib_bot.live_base_price, ib_bot.CONFIRM_FIRST = old
        assert sent[0] == 48.415, sent              # refused: off BAYN's 0.01 grid
        assert len(sent) == 2, sent                 # the refusal reached the retry
        assert abs(sent[-1] - 48.41) < 1e-9, sent   # the retry's legal price
        assert status != "REJECTED", (status, sent)
    print("t3 retry ladder recovers: %s" % " -> ".join(str(p) for p in sent))


def t4_exhausted_ladder_records_the_price_actually_sent():
    # Review finding 2026-09-16: after the 6th refusal the loop still re-priced,
    # logged a retry that never happened, and recorded that UNSENT limit.
    #
    # REWRITTEN 2026-09-17 (fix/eu-tick-bands), deliberately. The old scenario
    # (846.6, every price refused as "variation of 5") no longer makes six
    # submissions: the retry jumps to the stated 5 and never resends a refused
    # price, so the walk runs out of NEW prices after four (850.8, 850, 900,
    # 1000) - test_eu_tick t6 pins that. To keep proving the six-submission cap
    # and what an exhausted ladder records, IB here refuses every price with
    # ib_async's number-less text, at a price where every rung gives a distinct
    # price: 873.3, 873.4, 873.5, 873, 875, 870.
    sent = []

    class CD:
        minTick = 0.0001

    class Entry:
        def __init__(self, m):
            self.message = m

    class IB:
        def reqContractDetails(self, c):
            return [CD()]

        def placeOrder(self, contract, order):
            t = broker.Trade(contract, order)
            sent.append(order.lmtPrice)
            t.orderStatus.status = "Inactive"          # refuse EVERY price
            t.log.append(Entry("Error 110, reqId 9: The price does not conform to "
                               "the minimum price variation for this contract."))
            return t

        def sleep(self, n):
            pass

    class C:
        symbol, currency, conId, secType = "XYZ", "EUR", 1, "STK"

    old = (ib_bot.live_base_price, ib_bot.CONFIRM_FIRST)
    before = len(ib_bot.PLACED)
    try:
        ib_bot.live_base_price = lambda ib, c, fallback: fallback
        ib_bot.CONFIRM_FIRST = False
        ib_bot._TICK_CACHE.clear()
        status = ib_bot.place(IB(), C(), "BUY", 3, 869.0, False)
    finally:
        ib_bot.live_base_price, ib_bot.CONFIRM_FIRST = old
    row = ib_bot.PLACED[before]
    assert status == "REJECTED", status
    assert len(sent) == 6, sent                     # exactly six submissions
    assert len(set(sent)) == 6, sent                # none of them a resend
    assert row["limit"] == sent[-1] == 870, (row["limit"], sent)   # the price IB saw
    print("t4 exhausted ladder records %s, the last price sent OK" % row["limit"])


# ---- board review 2026-09-21: only a genuine tick refusal walks the ladder --
# ib_bot.place() retried whenever the bare substring '110' was anywhere in the
# refusal. A refusal that merely ECHOES the order - a cOID stamped 2026-11-05, a
# conid, an ib_async orderId, a quantity of 110, a price of 110.xx - was re-sent
# under a new cOID at a coarser price, up to six times.

class _WebIB(broker.IB):
    """The real web shim - placeOrder -> ib_orders.place -> _translate_error ->
    trade.log - with only the wall-clock wait and the contract-details request
    stubbed. ib_orders.place / poll_status are replaced by the caller."""

    def __init__(self, min_tick):
        broker.IB.__init__(self)
        self.min_tick = min_tick

    def reqContractDetails(self, contract):
        return [broker.ContractDetails(self.min_tick)]

    def sleep(self, secs=0):
        self._refresh_trades()                      # the real refresh, no wait


def _web_place(refuse, price, min_tick=0.01, conid=110521):
    """ib_bot.place() over _WebIB; refuse(limit) -> refusal text, or None to
    accept. Returns (status, prices sent, the PLACED row)."""
    sent = []

    def fake_place(conid_, action, qty, order_type="MKT", limit_price=None,
                   tif="DAY", acct=None, coid=None, outside_rth=False,
                   allow_price_cap=False):
        sent.append(limit_price)
        why = refuse(limit_price)
        if why:
            raise ib_orders.OrderError(why)
        return {"order_id": "900%d" % len(sent), "coid": "mps-x-B-%d" % len(sent)}

    c = broker.Stock("XYZ", "SMART", "USD")
    c.conId = conid
    old = (ib_orders.place, ib_orders.poll_status,
           ib_bot.live_base_price, ib_bot.CONFIRM_FIRST)
    before = len(ib_bot.PLACED)
    try:
        ib_orders.place = fake_place
        ib_orders.poll_status = lambda *a, **k: ("ok", "PreSubmitted", "")
        ib_bot.live_base_price = lambda ib, c_, fallback: fallback
        ib_bot.CONFIRM_FIRST = False
        ib_bot._TICK_CACHE.clear()
        status = ib_bot.place(_WebIB(min_tick), c, "BUY", 110, price, False)
    finally:
        (ib_orders.place, ib_orders.poll_status,
         ib_bot.live_base_price, ib_bot.CONFIRM_FIRST) = old
    return status, sent, ib_bot.PLACED[before]


def _socket_place(message_for, price, min_tick):
    """ib_bot.place() over an ib_async-shaped fake: a refused order is
    'Cancelled' with ib_async's own log text, as wrapper.error() leaves it."""
    sent = []

    class CD:
        minTick = min_tick

    class Entry:
        def __init__(self, m):
            self.message = m

    class IB:
        def reqContractDetails(self, c):
            return [CD()]

        def placeOrder(self, contract, order):
            t = broker.Trade(contract, order)
            sent.append(order.lmtPrice)
            m = message_for(order.lmtPrice)
            if m:
                t.orderStatus.status = "Cancelled"
                t.log.append(Entry(m))
            else:
                t.orderStatus.status = "PreSubmitted"
            return t

        def sleep(self, n):
            pass

    class C:
        symbol, currency, conId, secType = "XYZ", "USD", 1105, "STK"

    old = (ib_bot.live_base_price, ib_bot.CONFIRM_FIRST)
    try:
        ib_bot.live_base_price = lambda ib, c, fallback: fallback
        ib_bot.CONFIRM_FIRST = False
        ib_bot._TICK_CACHE.clear()
        status = ib_bot.place(IB(), C(), "BUY", 110, price, False)
    finally:
        ib_bot.live_base_price, ib_bot.CONFIRM_FIRST = old
    return status, sent


def _off_cent(p):
    return abs(round(p * 100) - p * 100) > 1e-6


def t5_an_echoed_110_is_not_a_tick_refusal():
    web = (
        # a duplicate-cOID refusal echoing a cOID stamped 2026-11-05
        "POST iserver/account/U***/orders failed: 400 Bad Request: {'error': "
        "'Local order ID mps-1-B-20261105233501 is already registered.'}",
        # a timeout echoing the order body: conid, quantity and price all hold 110
        "POST iserver/account/U***/orders failed: Read timed out. {'conid': 110521, "
        "'side': 'BUY', 'quantity': 110.0, 'price': 110.35, "
        "'cOID': 'mps-1-B-20261105233501'}",
        "order not accepted: insufficient funds for 110 shares",
    )
    for why in web:
        # the translator leaves it alone: its own text, no tick marker
        assert broker._translate_error(why) == why, broker._translate_error(why)
        status, sent, row = _web_place(lambda p: why, 50.0)
        assert sent == [50.25], (why, sent)             # exactly ONE submission
        assert status == "REJECTED" and row["limit"] == 50.25, (status, row)
        assert row["error"] == why[:160], row
    socket = (
        # ib_async orderId 1105 echoed as the reqId of an unrelated refusal
        "Error 201, reqId 1105: Order rejected - reason: YOUR ORDER IS NOT ACCEPTED.",
        # "Error 1100" begins with "Error 110"; the comma keeps it out
        "Error 1100, reqId -1: Connectivity between IB and Trader Workstation has been lost.",
        # ib_async's WARNING form: the order is still live at IB (ib_async
        # leaves it ValidationError), so it must never be re-sent - even if a
        # status slip made it read as refused, as this fake does
        "Warning 110, reqId 1105: The price does not conform to the minimum price "
        "variation for this contract.",
    )
    for why in socket:
        status, sent = _socket_place(lambda p: why, 50.0, 0.01)
        assert sent == [50.25], (why, sent)             # exactly ONE submission
        assert status == "REJECTED", (why, status)
    print("t5 %d refusals echoing 110 each make exactly one submission OK"
          % (len(web) + len(socket)))


def t6_a_tick_refusal_whose_price_holds_110_still_retries():
    # Web: IB's text names the refused price, 110.005. The old translator
    # skipped any message with '110' in it, so this one was never tagged.
    text = ("order not accepted: The price %s does not conform to the minimum "
            "price variation of 0.01 for this instrument.")
    tagged = broker._translate_error(text % 110.005)
    assert tagged.startswith("Error 110, reqId 0: "), tagged
    assert ib_bot.ib_stated_tick(tagged) == 0.01, tagged   # IB's number survives
    # An already-tagged socket message is not tagged twice.
    sock = ("Error 110, reqId 1105: The price does not conform to the minimum "
            "price variation for this contract.")
    assert broker._translate_error(sock) == sock
    # IB's contract details say 0.005 (wrong, as seen on TSE and Euronext), so
    # the first limit is 110.005; Nasdaq refuses it, the retry takes IB's
    # stated 0.01 and lands on 110.0.
    status, sent, row = _web_place(lambda p: text % p if _off_cent(p) else None,
                                   109.4577, min_tick=0.005)
    assert sent == [110.005, 110.0], sent
    assert status == "sent" and row["limit"] == 110.0, (status, row)
    # Socket: ib_async's text names no increment and its reqId is 1105; the
    # rung after 0.001 is 0.01, which lands on 110.0.
    status, sent = _socket_place(lambda p: sock if _off_cent(p) else None,
                                 109.4577, 0.001)
    assert sent == [110.005, 110.0], sent
    assert status == "sent", status
    print("t6 a tick refusal at 110.005 still retries: web and socket -> 110.0 OK")


if __name__ == "__main__":
    t1_refusal_raises()
    t2_refusal_reaches_the_retry_as_error_110()
    t3_retry_ladder_lands_on_a_legal_price()
    t4_exhausted_ladder_records_the_price_actually_sent()
    t5_an_echoed_110_is_not_a_tick_refusal()
    t6_a_tick_refusal_whose_price_holds_110_still_retries()
    print("ALL ORDER-REJECT TESTS PASS")
