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
import broker                                       # noqa: E402
import ib_bot                                       # noqa: E402
import ib_orders                                    # noqa: E402

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
    assert "110" in msg, msg
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


if __name__ == "__main__":
    t1_refusal_raises()
    t2_refusal_reaches_the_retry_as_error_110()
    t3_retry_ladder_lands_on_a_legal_price()
    t4_exhausted_ladder_records_the_price_actually_sent()
    print("ALL ORDER-REJECT TESTS PASS")
