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

Nothing here touches /root: every path is repointed before anything runs.
"""
import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault("IB_BACKEND", "web")
_ROOT = Path(tempfile.mkdtemp(prefix="mps-phonesell-"))
os.environ["MPS_EARMARK_DIR"] = str(_ROOT / "earmark")
(_ROOT / "earmark").mkdir()
os.environ.pop("EXCLUDED_CASH", None)

import alerts                                      # noqa: E402
import earmark                                     # noqa: E402
import ib_bot                                      # noqa: E402
import ib_commands                                 # noqa: E402

assert str(earmark.MARKER_FILE).startswith(str(_ROOT)), earmark.MARKER_FILE
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
    from the order-book read. Every SELL is accepted and left working."""

    def __init__(self, positions, book=(), book_error=None):
        self._positions, self.book, self.book_error = positions, list(book), book_error
        self.placed, self.book_reads = [], 0

    def connect(self, *a, **k):
        pass

    def disconnect(self):
        pass

    def sleep(self, *a):
        pass

    def positions(self):
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
    for word in ("alert", "working_sells", "openTrades", "sent.append", "_on_its_way_out"):
        assert word not in code, "%s sits in the placeOrder -> DONE save window" % word
    # the book is read before positions are, and before any order is placed
    assert body.index("book = working_sells(ib)") < body.index("for p in ib.positions()")
    # an unreadable book leaves the command un-done: no DONE save on that path
    unread = body[body.index("if book_err is not None:"):body.index("placed = False")]
    assert "continue" in unread and "done.add" not in unread, unread
    print("t5 place -> DONE save window still holds nothing that can raise OK")


if __name__ == "__main__":
    t1_bot_exit_already_working_means_no_second_sell()
    t2_two_taps_in_one_poll_sell_once()
    t3_partial_working_sell_leaves_only_the_remainder()
    t4_unreadable_order_book_sends_nothing_and_retries()
    t5_nothing_raisable_between_place_and_done_save()
    print("ALL PHONE SELL TESTS PASS")
