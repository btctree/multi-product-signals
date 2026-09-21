#!/usr/bin/env python3
"""Golden tests: a web-backend stock quote survives IBKR's pre-flight snapshot.

Run from this directory:  python test_web_quote.py

Board review 2026-09-21: IBKR's FIRST /iserver/marketdata/snapshot for a conid
only opens the stream and answers with no price fields. broker.IB.reqTickers
asked once, so under IB_BACKEND=web every stock quote came back None,
ib_bot.live_base_price always fell back to the signal price, and place()'s
"signal price stale vs IB quote - re-based" guard could never fire (the 24 Jul
MC case: a limit priced off a card whose last bar lagged the venue). The
snapshot is now asked a few times, a short pause apart, through ib_orders._get
(the session-safe GET the FX branch already uses), and still falls back to
None. Nothing here talks to IBKR: every GET is a stub, and the pause is a no-op.
"""
import os

os.environ.setdefault("IB_BACKEND", "web")
# Every /root default at a temp path before import. See testenv.py.
import testenv                                      # noqa: E402
testenv.isolate("mps-webquote-")
import broker                                       # noqa: E402
import ib_bot                                       # noqa: E402
import ib_orders                                    # noqa: E402
import ib_web                                       # noqa: E402
testenv.assert_isolated()

SNAP = "iserver/marketdata/snapshot?conids=%s&fields=31,84,86"
PREFLIGHT = [{"conid": 68598660, "conidEx": "68598660", "_updated": 1}]


class Stubs:
    """ib_orders._get answers from `replies` in order (the last one repeats);
    the pause is recorded, never slept; ib_web.client() must not be reached -
    the quote goes through ib_orders._get, which primes the session."""

    def __init__(self, replies, resolve=None):
        self.replies, self.resolve = list(replies), resolve
        self.gets, self.pauses = [], []

    def _get(self, path):
        self.gets.append(path)
        r = self.replies[min(len(self.gets), len(self.replies)) - 1]
        if isinstance(r, Exception):
            raise r
        return r

    def _client(self):
        raise AssertionError("reqTickers went to ib_web.client() directly")

    def _resolve(self, *a, **k):
        if self.resolve is None:
            raise ib_orders.OrderError("no listing")
        return self.resolve

    def __enter__(self):
        self.old = (ib_orders._get, ib_web.client, ib_orders.resolve_conid,
                    broker._snapshot_sleep)
        ib_orders._get = self._get
        ib_web.client = self._client
        ib_orders.resolve_conid = self._resolve
        broker._snapshot_sleep = self.pauses.append
        return self

    def __exit__(self, *a):
        (ib_orders._get, ib_web.client, ib_orders.resolve_conid,
         broker._snapshot_sleep) = self.old


def stock(conid=68598660):
    c = broker.Stock("BAYN", "SMART", "EUR", primaryExchange="IBIS")
    c.conId = conid
    return c


def t1_preflight_then_price():
    with Stubs([PREFLIGHT, [{"conid": 68598660, "31": "C48.41"}]]) as s:
        [tk] = broker.IB().reqTickers(stock())
    assert tk.last == tk.close == 48.41, (tk.last, tk.close)
    assert s.gets == [SNAP % 68598660] * 2, s.gets          # asked again, once
    assert s.pauses == [broker._SNAPSHOT_WAIT_S], s.pauses  # one pause between
    print("t1 empty pre-flight, then priced: quote 48.41 after 2 asks OK")


def t2_bid_or_ask_when_no_last():
    with Stubs([PREFLIGHT, [{"31": "", "84": "48.40", "86": "48.42"}]]):
        [tk] = broker.IB().reqTickers(stock())
    assert tk.last == 48.40, tk.last
    print("t2 no last trade: the bid (84) is the quote OK")


def t3_never_priced_falls_back_to_none():
    with Stubs([PREFLIGHT]) as s:
        [tk] = broker.IB().reqTickers(stock())
        # ...and ib_bot prices the order off the card, as before
        base = ib_bot.live_base_price(broker.IB(), stock(), 47.0)
    assert tk.last is None and tk.marketPrice() is None, tk.last
    assert len(s.gets) == 2 * broker._SNAPSHOT_TRIES, s.gets     # bounded
    assert len(s.pauses) == 2 * (broker._SNAPSHOT_TRIES - 1), s.pauses
    assert base == 47.0, base
    print("t3 never priced: None after %d asks, signal price kept OK"
          % broker._SNAPSHOT_TRIES)


def t4_failures_are_none_not_raised():
    # a failed GET: None at once, no further asks
    with Stubs([ib_orders.OrderError("GET failed: 500")]) as s:
        [tk] = broker.IB().reqTickers(stock())
    assert tk.last is None and len(s.gets) == 1 and not s.pauses, s.gets
    # unreadable or non-positive fields are no price
    with Stubs([[{"31": "C"}], [{"31": "0", "84": "-1"}], [{"31": "H12.5"}]]) as s:
        [tk] = broker.IB().reqTickers(stock())
    assert tk.last == 12.5 and len(s.gets) == 3, (tk.last, s.gets)
    # an error dict, not a list
    with Stubs([{"error": "no bridge"}]) as s:
        [tk] = broker.IB().reqTickers(stock())
    assert tk.last is None and len(s.gets) == broker._SNAPSHOT_TRIES, s.gets
    # no conid (the listing does not resolve): never asks for conid 0
    with Stubs([PREFLIGHT], resolve=None) as s:
        [tk] = broker.IB().reqTickers(stock(conid=0))
    assert tk.last is None and s.gets == [], s.gets
    print("t4 failures, junk fields and no conid all give None OK")


def t5_place_rebases_on_the_live_quote():
    """The guard this unblocks: a card price 4% off IB's quote is re-based."""
    sent = []

    class IB(broker.IB):
        def reqContractDetails(self, c):
            return [broker.ContractDetails(0.01)]

        def placeOrder(self, contract, order):
            t = broker.Trade(contract, order)
            sent.append(order.lmtPrice)
            t.orderStatus.status = "Submitted"
            return t

        def sleep(self, secs=0):
            pass

    c = broker.Stock("XYZ", "SMART", "USD")
    c.conId = 4242
    old = ib_bot.CONFIRM_FIRST
    try:
        ib_bot.CONFIRM_FIRST = False
        ib_bot._TICK_CACHE.clear()
        with Stubs([[{"conid": 4242}], [{"conid": 4242, "31": "100.00"}]]):
            ib_bot.place(IB(), c, "BUY", 10, 104.0, False)
    finally:
        ib_bot.CONFIRM_FIRST = old
    want = round(100.0 * (1 + ib_bot.LIMIT_BUFFER), 2)
    assert sent == [want], (sent, want)     # off IB's 100, not the card's 104
    print("t5 stale card 104 re-based on IB's 100 -> limit %s OK" % want)


if __name__ == "__main__":
    t1_preflight_then_price()
    t2_bid_or_ask_when_no_last()
    t3_never_priced_falls_back_to_none()
    t4_failures_are_none_not_raised()
    t5_place_rebases_on_the_live_quote()
    print("ALL WEB-QUOTE TESTS PASS")
