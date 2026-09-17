#!/usr/bin/env python3
"""Golden tests: a phone SELL never sells shares a working order is already selling.

Run from this directory:  python test_phone_sell.py

Review 2026-09-17: ib_commands sized every SELL as min(requested, position) and
never looked at working orders. A working SELL does not reduce the position
until it fills, so a 23:35 bot exit resting until the US open plus a morning
tap on the dashboard's Sell button (sitting right under the page's own red SELL
alert) would send a second full-size market SELL; so would two taps in one
poll (both reproduced against a fake IB). Both fill at the open, the margin
account ends short, and nothing closes a short - every exit path acts only on
qty > 0. What is locked down:

  * a working SELL (bot exit or earlier tap) is subtracted; covering it all
    sends nothing, marks the command done and says so in an alert;
  * two taps in one poll sell once - the poll counts what it has sent;
  * a partial working SELL leaves only the remainder to sell;
  * working orders that cannot be read send nothing and leave the command
    pending for the next poll, with one alert however many polls it takes;
  * "SELL: SYM" / "SELL: SYM 0" still mean everything (not already being sold);
  * nothing that can raise sits between placeOrder and the DONE save.

Review 2026-09-17, second pass (t6-t8 run through the real web shim, with only
ibind's HTTP client faked):
  * positions are read with IB's positions cache flushed, AFTER the book: a
    market-open exit that filled and left the book is not sold again off a
    cached position; a flush that fails sends nothing, leaves the command
    pending and alerts once;
  * a working SELL counts at its WHOLE order size, so a partial fill the
    positions read has not caught up with cannot over-sell;
  * a tap covered only by this poll's own REFUSED send is not reported as
    "a sell is already working" - the netting quantities are unchanged.

Nothing here touches /root: every path is repointed before anything runs.
"""
import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault("IB_BACKEND", "web")
# Every /root default at a temp path before import (review 2026-09-17, test
# isolation: the orders ledger, exit memo and FX memory were still /root here).
import testenv                                     # noqa: E402
_ROOT = testenv.isolate("mps-phonesell-")
(_ROOT / "earmark").mkdir(exist_ok=True)
os.environ.pop("EXCLUDED_CASH", None)

import alerts                                      # noqa: E402
import broker                                      # noqa: E402
import earmark                                     # noqa: E402
import ib_bot                                      # noqa: E402
import ib_commands                                 # noqa: E402
import ib_orders                                   # noqa: E402
import ib_web                                      # noqa: E402
testenv.assert_isolated()

assert str(earmark.MARKER_FILE).startswith(str(_ROOT)), earmark.MARKER_FILE
for _p in (ib_orders.CONID_CACHE, ib_orders.ORDERS_LEDGER, ib_web.ENVF,
           ib_bot.EXIT_ATTEMPTS, ib_bot.FX_LAST_GOOD, alerts.DIR):
    assert str(_p).startswith(str(_ROOT)), _p
alerts.DIR = _ROOT / "outbox"
ib_commands.DONE = _ROOT / "commands_done.json"


class Patch:
    """Swap attributes on one module for the duration of a block."""

    def __init__(self, mod, **kw):
        self.mod, self.kw, self.old = mod, kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(self.mod, k)
            setattr(self.mod, k, v)
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            setattr(self.mod, k, v)


def fresh():
    d = Path(tempfile.mkdtemp(prefix="t-", dir=str(_ROOT)))
    alerts.DIR = d / "outbox"
    ib_commands.DONE = d / "commands_done.json"
    return d


def queued():
    return [d["text"] for _, d in alerts._queued()]


def done_ids():
    p = ib_commands.DONE
    return json.loads(p.read_text()) if p.exists() else []


class C:
    def __init__(self, symbol, conId=0, secType="STK", currency="USD"):
        self.symbol, self.conId, self.secType = symbol, conId, secType
        self.currency, self.exchange = currency, "SMART"


class Pos:
    def __init__(self, contract, qty):
        self.contract, self.position, self.avgCost = contract, qty, 100.0


class O:
    def __init__(self, action, qty):
        self.action, self.totalQuantity = action, qty


class L:
    def __init__(self, message):
        self.message = message


class St:
    def __init__(self, status):
        self.status = status


class T:
    def __init__(self, contract, action, qty, status="PreSubmitted"):
        self.contract, self.order, self.orderStatus = contract, O(action, qty), St(status)
        self.log = []


class FakeIB:
    """positions: [Pos]; book: [T] as openTrades() returns them (the web shim
    maps PreSubmitted to "Submitted"; both are working). book_error: raise it
    from the order-book read. Every SELL is accepted and left working, except
    the first `refuse` of them, which IB refuses (Inactive, as broker.placeOrder
    marks one). A positions read that is not fresh fails the test: the cached
    read is the one that sold a filled exit twice."""

    def __init__(self, positions, book=(), book_error=None, refuse=0):
        self._positions, self.book, self.book_error = positions, list(book), book_error
        self.placed, self.book_reads, self.refuse = [], 0, refuse

    def connect(self, *a, **k):
        pass

    def disconnect(self):
        pass

    def sleep(self, *a):
        pass

    def positions(self, fresh=False):
        assert fresh, "a phone SELL read IB's CACHED positions"
        return self._positions

    def reqAllOpenOrders(self):
        self.book_reads += 1
        if self.book_error:
            raise self.book_error
        return self.book

    def openTrades(self):
        if self.book_error:
            raise self.book_error
        return self.book

    def qualifyContracts(self, c):
        return [c]

    def placeOrder(self, contract, order):
        self.placed.append((order.action, order.totalQuantity, contract.symbol))
        if self.refuse > 0:
            self.refuse -= 1
            t = T(contract, order.action, order.totalQuantity, "Inactive")
            t.log.append(L("order not accepted: timed out"))
            return t
        return T(contract, order.action, order.totalQuantity, "PreSubmitted")


def cmd(num, symbol, qty=None):
    return {"id": num, "kind": "sell", "symbol": symbol, "qty": qty, "amount": None}


def poll(ib, cmds, state=None):
    """One ib_commands.main() poll. Returns how many times it published."""
    published = []
    try:
        with Patch(ib_commands, fetch_commands=lambda: list(cmds), IB=lambda: ib), \
                Patch(ib_bot, load_state=lambda: dict(state or {"map": {}}),
                      net_liq=lambda ib_: 1.0,
                      publish_state=lambda ib_, s, nl: published.append(1)):
            ib_commands.main()
    finally:
        del ib_bot.PLACED[:]
    return len(published)


DELL = 1001


def t1_bot_exit_already_working_means_no_second_sell():
    fresh()
    # the 23:35 run's market-at-open exit, still resting at 10:00 UTC
    ib = FakeIB([Pos(C("DELL", DELL), 4)], book=[T(C("DELL", DELL), "SELL", 4)])
    assert poll(ib, [cmd(101, "DELL")]) == 1
    assert ib.placed == [], "a second SELL was sent against a working exit: %s" % ib.placed
    assert done_ids() == [101], "the command must be marked done"
    texts = queued()
    assert len(texts) == 1, texts
    assert "already working" in texts[0] and "sell of 4 DELL" in texts[0], texts[0]
    assert "issue #101" in texts[0] and "Nothing new was sent" in texts[0], texts[0]
    # the same holds for an explicit quantity, and when matched by symbol only
    fresh()
    ib = FakeIB([Pos(C("DELL", DELL), 4)], book=[T(C("DELL", 0), "SELL", 4)])
    poll(ib, [cmd(102, "DELL", 4)])
    assert ib.placed == [] and done_ids() == [102], ib.placed
    # history is not flight: a CANCELLED or FILLED sell, a BUY and an FX order
    # in the day's book do not block the sell
    fresh()
    ib = FakeIB([Pos(C("DELL", DELL), 4)],
                book=[T(C("DELL", DELL), "SELL", 4, "Cancelled"),
                      T(C("DELL", DELL), "SELL", 4, "Filled"),
                      T(C("DELL", DELL), "BUY", 4),
                      T(C("USD", 555, "CASH"), "SELL", 4)])
    poll(ib, [cmd(103, "DELL")])
    assert ib.placed == [("SELL", 4, "DELL")], ib.placed
    assert queued() == [], queued()
    # a working sell of ANOTHER instrument that shares the ticker is not this one
    fresh()
    ib = FakeIB([Pos(C("DELL", DELL), 4)], book=[T(C("DELL", 9999), "SELL", 4)])
    poll(ib, [cmd(104, "DELL")])
    assert ib.placed == [("SELL", 4, "DELL")], ib.placed
    print("t1 a working bot exit blocks a second sell, alerted and marked done OK")


def t2_two_taps_in_one_poll_sell_once():
    fresh()
    ib = FakeIB([Pos(C("DELL", DELL), 4)])
    assert poll(ib, [cmd(201, "DELL"), cmd(202, "DELL")]) == 1
    assert ib.placed == [("SELL", 4, "DELL")], ib.placed
    assert done_ids() == [201, 202], done_ids()
    assert ib.book_reads == 1, "the order book is read once per poll"
    texts = queued()
    assert len(texts) == 1 and "issue #202" in texts[0] and "already working" in texts[0], texts
    # the ysym form of the symbol is the same position
    fresh()
    ib = FakeIB([Pos(C("700", 70), 200, )])
    ib._positions[0].contract.currency = "HKD"
    poll(ib, [cmd(203, "0700.HK"), cmd(204, "700")], state={"map": {"700": "0700.HK"}})
    assert ib.placed == [("SELL", 200, "700")], ib.placed
    # two partial taps that together fit are both sent
    fresh()
    ib = FakeIB([Pos(C("DELL", DELL), 10)])
    poll(ib, [cmd(205, "DELL", 5), cmd(206, "DELL", 5), cmd(207, "DELL", 5)])
    assert ib.placed == [("SELL", 5, "DELL"), ("SELL", 5, "DELL")], ib.placed
    assert done_ids() == [205, 206, 207]
    assert "issue #207" in queued()[0] and "sell of 10 DELL" in queued()[0], queued()
    print("t2 two taps in one poll sell once; partial taps that fit both go OK")


def t3_partial_working_sell_leaves_only_the_remainder():
    for requested, want in ((None, 6), (10, 6), (6, 6), (3, 3), (0, 6)):
        fresh()
        ib = FakeIB([Pos(C("DELL", DELL), 10)], book=[T(C("DELL", DELL), "SELL", 4)])
        poll(ib, [cmd(301, "DELL", requested)])
        assert ib.placed == [("SELL", want, "DELL")], (requested, ib.placed)
        assert queued() == [], (requested, queued())
    # two working sells add up; a working order and this poll's own sell add up
    fresh()
    ib = FakeIB([Pos(C("DELL", DELL), 10)],
                book=[T(C("DELL", DELL), "SELL", 4), T(C("DELL", DELL), "SELL", 3)])
    poll(ib, [cmd(302, "DELL"), cmd(303, "DELL")])
    assert ib.placed == [("SELL", 3, "DELL")], ib.placed
    assert "sell of 10 DELL" in queued()[0], queued()
    # a working SELL whose size IB did not report is taken as covering it all
    fresh()
    ib = FakeIB([Pos(C("DELL", DELL), 10)], book=[T(C("DELL", DELL), "SELL", 0)])
    poll(ib, [cmd(304, "DELL")])
    assert ib.placed == [] and done_ids() == [304], ib.placed
    assert "an unreported quantity" in queued()[0], queued()
    print("t3 a partial working sell leaves only the remainder to sell OK")


def t4_unreadable_order_book_sends_nothing_and_retries():
    fresh()
    boom = RuntimeError("cannot read working orders (orders snapshot never ready) - "
                        "refusing to trade blind")
    ib = FakeIB([Pos(C("DELL", DELL), 4)], book_error=boom)
    earmark_cmd = {"id": 400, "kind": "earmark", "symbol": "", "qty": None, "amount": 5000.0}
    delivered = []
    for _ in range(3):                                      # three 10-minute polls
        assert poll(ib, [earmark_cmd, cmd(401, "DELL"), cmd(402, "BEN", 3)]) == 1
        alerts.drain(lambda text: delivered.extend(text.split("\n\n")))
    assert ib.placed == [], ib.placed
    assert done_ids() == [400], "sells must stay pending; the earmark still applies"
    assert earmark.marker() == 5000.0
    assert len(delivered) == 2, delivered                   # once per command, not per poll
    assert "issue #401" in delivered[0] and "was NOT sent" in delivered[0], delivered
    assert "snapshot never ready" in delivered[0] and "SELL BEN 3" in delivered[1], delivered
    assert ib.book_reads == 3, "read once per poll, not once per command"
    # the book comes back: the pending command runs on the next poll
    ib.book_error = None
    poll(ib, [cmd(401, "DELL")])
    assert ib.placed == [("SELL", 4, "DELL")] and done_ids() == [400, 401], ib.placed
    earmark.MARKER_FILE.unlink()
    print("t4 unreadable working orders send nothing, stay pending, alert once OK")


def t5_nothing_raisable_between_place_and_done_save():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ib_commands.py"),
               encoding="utf-8").read()
    body = src[src.index("def main():"):]
    place_at = body.index("trade = ib.placeOrder(")
    window = body[place_at:body.index("DONE.write_text(", place_at)]
    code = "\n".join(line.split("#")[0] for line in window.splitlines())
    for word in ("alert", "working_sells", "openTrades", "sent.append", "_on_its_way_out",
                 "refused.append", "fresh_positions"):
        assert word not in code, "%s sits in the placeOrder -> DONE save window" % word
    # the book is read before positions are, and before any order is placed;
    # positions only ever through the cache-flushing read
    assert body.index("book = working_sells(ib)") < body.index("held = fresh_positions(ib)")
    assert "ib.positions(" not in body[:body.index('if __name__ == "__main__":')], \
        "main() reads positions without flushing IB's cache"
    # an unreadable book leaves the command un-done: no DONE save on that path
    unread = body[body.index("if book_err is not None:"):body.index("held = fresh_positions(ib)")]
    assert "continue" in unread and "done.add" not in unread, unread
    # ...and so do positions that cannot be read fresh
    unread = body[body.index("held = fresh_positions(ib)"):body.index("placed = False")]
    assert "continue" in unread and "done.add" not in unread, unread
    assert "alert_positions_unread" in unread, unread
    print("t5 place -> DONE save window still holds nothing that can raise OK")


# ---------------- through the real web shim ----------------
# broker.IB, ib_web and ib_orders run for real; only ibind's HTTP client, the
# brokerage-session handshake and the order POST are replaced.

ACCT = "U7654321"                                  # a made-up live-shaped id


class Resp:
    def __init__(self, data):
        self.data = data


class FakeHTTP:
    """ibind's client as ib_web and ib_orders call it.

    portfolio/<acct>/positions/0 answers from IB's backend CACHE, which only
    POST .../positions/invalidate refreshes - IBKR's documented behaviour, and
    the one the review reproduced. `live` is what the account really holds
    (None: IB's cache still lags even after the flush). `orders` is the raw
    /iserver/account/orders list."""

    def __init__(self, cached, live=None, orders=(), invalidate_error=None):
        self.cached, self.live, self.orders = list(cached), live, list(orders)
        self.invalidate_error, self.calls = invalidate_error, []

    def get(self, path):
        self.calls.append(("GET", path))
        if path == "portfolio/accounts":
            return Resp([{"accountId": ACCT}])
        if path == "portfolio/%s/positions/0" % ACCT:
            return Resp(self.cached)
        if path == "iserver/account/orders":
            return Resp({"snapshot": True, "orders": self.orders})
        raise AssertionError("unexpected GET " + path)

    def post(self, path, params=None):
        self.calls.append(("POST", path))
        if path == "portfolio/%s/positions/invalidate" % ACCT:
            if self.invalidate_error:
                raise self.invalidate_error
            if self.live is not None:
                self.cached = list(self.live)
            return Resp({"message": "success"})
        raise AssertionError("unexpected POST " + path)

    def index(self, method, path):
        return self.calls.index((method, path))


def pos_row(qty, conid=7733, ticker="7733", ccy="JPY"):
    return {"conid": conid, "ticker": ticker, "contractDesc": ticker, "position": qty,
            "avgCost": 1800.0, "currency": ccy, "assetClass": "STK"}


def order_row(status, remaining, total, conid=7733, ticker="7733", side="S"):
    return {"orderId": 55, "conid": conid, "ticker": ticker, "side": side,
            "remainingQuantity": remaining, "totalSize": total,
            "filledQuantity": total - remaining, "status": status,
            "secType": "STK", "currency": "JPY"}


class _NoSleep:
    @staticmethod
    def sleep(*a):
        pass


def web_poll(http, cmds, state=None):
    """One ib_commands.main() poll over the real shim. Returns the orders
    ib_orders.place was asked for, as (side, qty, conid)."""
    placed = []

    def place(conid, action, qty, **kw):
        placed.append((action, qty, conid))
        return {"order_id": "o%d" % len(placed), "coid": "mps-test"}

    ib = broker.IB()
    old_client = ib_web._client
    try:
        ib_web._client = http
        with Patch(ib_orders, ensure_session=lambda *a, **k: True, place=place,
                   poll_status=lambda *a, **k: ("ok", "PreSubmitted", "")), \
                Patch(broker, time=_NoSleep):
            poll(ib, cmds, state=state or {"map": {"7733": "7733.T"}})
    finally:
        ib_web._client = old_client
    return placed


def t6_filled_open_exit_is_not_sold_again_off_a_cached_position():
    # 23:35 run: market-at-open SELL 100 7733. The operator taps Sell at 23:55.
    # The 00:00 poll reads the book after the opening auction filled the exit,
    # so it is Filled (history, not working) - while IB's positions cache
    # still shows the 100 shares the auction already sold.
    fresh()
    http = FakeHTTP(cached=[pos_row(100)], live=[],
                    orders=[order_row("Filled", 0.0, 100.0)])
    placed = web_poll(http, [cmd(601, "7733.T")])
    assert placed == [], "a filled exit was sold again off a cached position: %s" % placed
    assert done_ids() == [601]
    texts = queued()
    assert len(texts) == 1 and "no held position matches 7733.T" in texts[0], texts
    flush = ("POST", "portfolio/%s/positions/invalidate" % ACCT)
    read = ("GET", "portfolio/%s/positions/0" % ACCT)
    book = ("GET", "iserver/account/orders")
    assert flush in http.calls and read in http.calls, http.calls
    last_read = len(http.calls) - 1 - http.calls[::-1].index(read)
    assert http.index(*book) < http.index(*flush) < last_read, \
        "positions must be flushed and read AFTER the order book: %s" % http.calls
    # the shim's plain read (ib_bot's runs, publish_state) does not flush
    ib = broker.IB()
    ib._acct = ACCT
    http2 = FakeHTTP(cached=[pos_row(100)], live=[])
    old_client = ib_web._client
    try:
        ib_web._client = http2
        assert [p.position for p in ib.positions()] == [100]
        assert http2.calls == [read], http2.calls
        assert [p.position for p in ib.positions(fresh=True)] == []
        assert http2.calls == [read, flush, read], http2.calls
    finally:
        ib_web._client = old_client
    print("t6 a filled open exit is not sold again off IB's cached positions OK")


def t7_partial_fill_cannot_over_sell():
    # the exit of 100 has filled 40: IB reports 60 remaining of 100
    for label, cached, live in (
            ("positions caught up (60 held)", [pos_row(100)], [pos_row(60)]),
            ("positions still lag after the flush (100 held)", [pos_row(100)], None)):
        fresh()
        http = FakeHTTP(cached=cached, live=live,
                        orders=[order_row("PreSubmitted", 60.0, 100.0)])
        placed = web_poll(http, [cmd(701, "7733.T")])
        assert placed == [], "%s: over-sold %s" % (label, placed)
        assert done_ids() == [701]
        texts = queued()
        assert len(texts) == 1 and "already working" in texts[0], (label, texts)
    # the shim keeps both sizes: ib_bot's cash reserve still reads what is LEFT
    ib = broker.IB()
    ib._acct = ACCT
    old_client = ib_web._client
    try:
        ib_web._client = FakeHTTP(cached=[], orders=[order_row("PreSubmitted", 60.0, 100.0)])
        with Patch(ib_orders, ensure_session=lambda *a, **k: True):
            (t,) = ib.openTrades()
    finally:
        ib_web._client = old_client
    assert t.order.totalQuantity == 60.0 and t.order.totalSize == 100.0, vars(t.order)
    # netting by the whole order still sells what no order covers
    fresh()
    http = FakeHTTP(cached=[pos_row(100)], live=[pos_row(100)],
                    orders=[order_row("PreSubmitted", 30.0, 30.0)])
    assert web_poll(http, [cmd(702, "7733.T")]) == [("SELL", 70, 7733)]
    assert queued() == []
    print("t7 a partial fill counts at the whole order size and cannot over-sell OK")


def t8_positions_flush_failure_sends_nothing_and_retries():
    fresh()
    boom = RuntimeError("503 Service Unavailable for portfolio/%s/positions/invalidate" % ACCT)
    # nothing working, cached positions say 100: the cached read WOULD sell
    http = FakeHTTP(cached=[pos_row(100)], live=[pos_row(100)], invalidate_error=boom)
    delivered = []
    for _ in range(3):                                      # three 10-minute polls
        assert web_poll(http, [cmd(801, "7733.T")]) == []
        alerts.drain(lambda text: delivered.extend(text.split("\n\n")))
    assert done_ids() == [], "the command must stay pending"
    read = ("GET", "portfolio/%s/positions/0" % ACCT)
    assert read not in http.calls, "fell back to the cached positions read: %s" % http.calls
    assert len(delivered) == 1, delivered                   # once per command
    assert "issue #801" in delivered[0] and "was NOT sent" in delivered[0], delivered
    assert "positions could not be refreshed" in delivered[0], delivered
    assert ACCT not in delivered[0] and "U***" in delivered[0], delivered
    # IB answers the flush with an error body: the same, never a silent pass
    fresh()
    http = FakeHTTP(cached=[pos_row(100)], live=[pos_row(100)])
    http.post = lambda path, params=None: (http.calls.append(("POST", path))
                                           or Resp({"error": "not available"}))
    assert web_poll(http, [cmd(802, "7733.T")]) == [] and done_ids() == []
    assert read not in http.calls and "not available" in queued()[0], queued()
    # the flush works again: the pending command runs on the next poll
    fresh()
    http = FakeHTTP(cached=[pos_row(100)], live=[pos_row(100)], invalidate_error=boom)
    web_poll(http, [cmd(803, "7733.T")])
    http.invalidate_error = None
    assert web_poll(http, [cmd(803, "7733.T")]) == [("SELL", 100, 7733)]
    assert done_ids() == [803]
    print("t8 a positions flush that fails sends nothing, stays pending, alerts once OK")


def t9_refused_same_poll_send_is_not_called_a_working_sell():
    # 4 DELL held, two taps in one poll, IB refuses the first
    fresh()
    ib = FakeIB([Pos(C("DELL", DELL), 4)], refuse=1)
    poll(ib, [cmd(901, "DELL"), cmd(902, "DELL")])
    assert ib.placed == [("SELL", 4, "DELL")], "netting changed: %s" % ib.placed
    assert done_ids() == [901, 902]
    delivered = []
    alerts.drain(lambda text: delivered.extend(text.split("\n\n")))
    assert len(delivered) == 2, delivered
    assert "PHONE SELL REFUSED" in delivered[0] and "issue #901" in delivered[0], delivered
    second = delivered[1]
    assert "issue #902" in second and "issue #901" in second, second
    assert "refused by IB" in second and "may still exist at IB" in second, second
    assert "Check IB" in second and "before tapping Sell again" in second, second
    for false_claim in ("already working", "is working", "cancelled or expires"):
        assert false_claim not in second, (false_claim, second)
    # netting quantities are unchanged: a refused tap of 2 still holds 2 back
    fresh()
    ib = FakeIB([Pos(C("DELL", DELL), 4)], refuse=1)
    poll(ib, [cmd(903, "DELL", 2), cmd(904, "DELL")])
    assert ib.placed == [("SELL", 2, "DELL"), ("SELL", 2, "DELL")], ib.placed
    # a working order AND a refused tap together cover it: both are named
    fresh()
    ib = FakeIB([Pos(C("DELL", DELL), 4)], book=[T(C("DELL", DELL), "SELL", 2)], refuse=1)
    poll(ib, [cmd(905, "DELL", 2), cmd(906, "DELL")])
    assert ib.placed == [("SELL", 2, "DELL")], ib.placed
    text = queued()[-1]
    assert "issue #906" in text and "a sell of 2 DELL is working at IB" in text, text
    assert "issue #905" in text and "refused by IB" in text, text
    assert "already working" not in text, text
    # a refusal of ANOTHER instrument, and a working order that alone covers
    # this one: the working sell is what blocked it, and it says so
    fresh()
    ib = FakeIB([Pos(C("DELL", DELL), 4), Pos(C("AAPL", 2002), 3)],
                book=[T(C("DELL", DELL), "SELL", 4)], refuse=1)
    poll(ib, [cmd(909, "AAPL"), cmd(910, "DELL")])
    assert ib.placed == [("SELL", 3, "AAPL")], ib.placed
    text = [t for t in queued() if "issue #910" in t][0]
    assert "already working" in text and "refused" not in text, text
    # a working order of unreported size covers everything on its own
    fresh()
    ib = FakeIB([Pos(C("DELL", DELL), 4)], book=[T(C("DELL", DELL), "SELL", 0)])
    poll(ib, [cmd(913, "DELL")])
    assert ib.placed == [] and "an unreported quantity" in queued()[0], queued()
    # an ACCEPTED earlier tap still reads as a working sell
    fresh()
    ib = FakeIB([Pos(C("DELL", DELL), 4)])
    poll(ib, [cmd(911, "DELL"), cmd(912, "DELL")])
    text = queued()[0]
    assert "already working" in text and "refused" not in text, text
    print("t9 a tap covered by this poll's refused send is not called a working sell OK")


if __name__ == "__main__":
    t1_bot_exit_already_working_means_no_second_sell()
    t2_two_taps_in_one_poll_sell_once()
    t3_partial_working_sell_leaves_only_the_remainder()
    t4_unreadable_order_book_sends_nothing_and_retries()
    t5_nothing_raisable_between_place_and_done_save()
    t6_filled_open_exit_is_not_sold_again_off_a_cached_position()
    t7_partial_fill_cannot_over_sell()
    t8_positions_flush_failure_sends_nothing_and_retries()
    t9_refused_same_poll_send_is_not_called_a_working_sell()
    print("ALL PHONE SELL TESTS PASS")
