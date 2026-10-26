#!/usr/bin/env python3
"""Golden tests for parking idle USD in SGOV - SHADOW mode (owner decision 2026-10-26).

Run from this directory:  python test_park.py

Shadow means one log line per run on what a live mode WOULD park or unpark, and
nothing else: no order, no state, no file. What is locked down, through the
REAL run():

  * SGOV is never a stock slot - a holding of it and a working order for it
    are both left out of the 15 - and never a state['map'] entry, so no stock
    exit rule (regime, trailing, 60-bar time stop) can ever sell it; a signal
    row or an adopt.json line for it is refused;
  * the shadow line is measured where a live mode would act: after the entries,
    from SPENDABLE USD (what working orders and this run's entries claimed is
    netted out), keeping PARK_BUFFER_SLOTS slots' worth; it says why a live
    mode would hold off;
  * PARK_MODE "off" is silent, any mode not built ("live") places nothing and
    says so, and a parking report that raises never costs the run its save
    and publish.

Nothing here touches /root: every path is repointed before ib_bot is imported.
"""
import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

os.environ.setdefault("IB_BACKEND", "web")
import testenv                                     # noqa: E402
_TMP = testenv.isolate("mps-park-")
os.environ.pop("EXCLUDED_CASH", None)
for _v in ("MAX_ORDER_BASE", "TARGET_POSITIONS", "LIMIT_BUFFER", "HK_ENABLED"):
    os.environ.pop(_v, None)

import alerts                                      # noqa: E402
import broker                                      # noqa: E402
import ib_bot                                      # noqa: E402
testenv.assert_isolated()

BASE = ib_bot.BASE_CCY
RUN_AT = datetime(2026, 9, 16, 23, 35, 20, tzinfo=timezone.utc)
TODAY = date.today().isoformat()
HKD_PER = {"HKD": 1.0, "USD": 7.8, "JPY": 0.05, "EUR": 9.0}


def rate(ib_, a, b):
    if a == b:
        return 1.0
    return HKD_PER[a] / HKD_PER[b] if (a in HKD_PER and b in HKD_PER) else 0.0


class Patch:
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
        for reg in (ib_bot._FX_COMMITTED, ib_bot._FX_PENDING_CCY, ib_bot._FX_PENDING,
                    ib_bot._EARMARK_RUN, ib_bot._EXC_APPLIED, ib_bot._POCKET_RUN,
                    ib_bot._RESERVE_OK, ib_bot._RUN_CONFIG):
            reg.clear()
        del ib_bot.PLACED[:]


class Working:
    def __init__(self, sym, ccy, action, qty, lmt, status="PreSubmitted"):
        self.contract = broker.Contract(sym, "STK", ccy)
        self.order = broker.Order(action, qty, "LMT", lmt)
        self.orderStatus = broker._OrderStatus(status)
        self.log = []


class BookIB:
    def __init__(self, cash, held=(), working=()):
        self.cash, self.held, self.working = dict(cash), list(held), list(working)
        self.placed, self.qualified = [], []

    def accountValues(self):
        return ([broker.AccountValue("NetLiquidation", "220000", BASE)]
                + [broker.AccountValue("CashBalance", str(v), c) for c, v in self.cash.items()])

    def positions(self):
        return [broker.Position(broker.Contract(s, "STK", ccy), q, avg)
                for s, ccy, q, avg in self.held]

    def reqAllOpenOrders(self):
        pass

    def openTrades(self):
        return list(self.working)

    def sleep(self, *a):
        pass

    def qualifyContracts(self, *cs):
        self.qualified.extend(c.symbol for c in cs)
        return list(cs)

    def reqTickers(self, c):
        return [broker.Ticker()]

    def placeOrder(self, c, order):
        self.placed.append((order.action, order.totalQuantity, c.symbol))
        t = broker.Trade(c, order)
        t.orderStatus = broker._OrderStatus("Submitted")
        return t

    def disconnect(self):
        pass


def bot_run(ib, actions, state=None, **extra):
    d = Path(tempfile.mkdtemp(prefix="run-", dir=str(_TMP)))
    path = d / "state.json"
    path.write_text(json.dumps(state or {"map": {}, "pos": {}}), encoding="utf-8")
    alerts.DIR = d / "outbox"
    signals = {"generated": "2026-09-16", "actions": list(actions)}

    def get_json(url):
        if url == ib_bot.SIGNALS_URL:
            return signals
        raise AssertionError("unexpected fetch " + url)   # no card for SGOV, ever

    lines, published = [], []
    stubs = dict(
        STATE=path, IB=lambda: ib, connect_or_heal=lambda *a, **k: None,
        get_json=get_json, _now_utc=lambda: RUN_AT,
        publish_state=lambda ib_, st, nl: published.append(nl),
        fx_rate=rate, lot_size=lambda ib_, c: 1, min_tick=lambda ib_, c: 0.01,
        confirm=lambda msg: True, ADOPT=d / "adopt.json",
        EXIT_ATTEMPTS=d / "exit_attempts.json", FILLS_LEDGER=d / "fills.jsonl",
        log=lambda *a: lines.append(" ".join(str(x) for x in a)))
    stubs.update(extra)
    with Patch(ib_bot, **stubs):
        ib_bot.run(dry=False)
    return json.loads(path.read_text(encoding="utf-8")), lines, published


def park_lines(lines):
    """The parking report's own lines (not the run's one-line rules summary)."""
    return [l for l in lines if l.startswith("park (") or "!! park:" in l]


def buy(sym, price, score):
    return {"symbol": sym, "action": "BUY", "price": price, "score": score,
            "stop": round(price * 0.88, 2), "market": "US"}


STOCKS = [("H%02d" % k, "USD", 1, 10.0) for k in range(13)]


def t1_the_plan_rules():
    p = ib_bot.park_plan
    # park what is above the buffer, sized at the limit so it never overspends
    assert p(10000.0, 5000.0, 100.5, 0) == ("BUY", int(5000.0 / (100.5 * 1.005)))
    assert p(5999.0, 5000.0, 100.5, 0) is None                 # under PARK_MIN_USD
    assert p(6000.0, 5000.0, 100.5, 0) == ("BUY", 9)
    # unpark only below HALF the buffer, and never more than is held
    assert p(2400.0, 5000.0, 100.5, 40) == ("SELL", 26)        # ceil(2,600 / 100.5)
    assert p(2400.0, 5000.0, 100.5, 10) == ("SELL", 10)
    assert p(2600.0, 5000.0, 100.5, 40) is None                # between: hold still
    assert p(-3000.0, 5000.0, 100.5, 0) is None                # nothing parked to sell
    for bad in ((None, 5000.0, 100.5, 0), (10000.0, 5000.0, 0, 0),
                (10000.0, 5000.0, "x", 0), (float("nan"), 5000.0, 100.5, 0)):
        assert p(*bad) is None, bad
    print("t1 park_plan: buy above the buffer, sell below half of it, else nothing OK")


def t2_sgov_is_never_a_stock_slot_nor_a_map_entry():
    held = STOCKS + [("SGOV", "USD", 30, 100.4)]
    working = [Working("SGOV", "USD", "BUY", 10, 100.6),
               Working("VLO", "USD", "BUY", 4, 421.38)]
    ib = BookIB({"USD": 20000.0}, held=held, working=working)
    state = {"map": {}, "pos": {}}
    st, lines, published = bot_run(ib, [buy("SGOV", 100.5, 99), buy("NEWA", 100.0, 90)],
                                   state=state)
    # 15 - 13 stocks - VLO = 1 free; SGOV's holding and working buy take none
    assert any("working BUY orders hold 1 slot(s): ['VLO']" in l for l in lines), lines
    sizing = [l for l in lines if l.startswith("sizing: ")]
    assert len(sizing) == 1 and "/ 1 free slot(s)" in sizing[0], sizing
    assert any("skip SGOV: the cash-parking ETF is never a stock entry" in l for l in lines)
    assert "SGOV" not in ib.qualified, ib.qualified
    assert [s for a, q, s in ib.placed] == ["NEWA"], ib.placed
    assert "SGOV" not in st["map"] and "SGOV" not in st["pos"], st
    assert not [l for l in lines if "SGOV" in l and "EXIT" in l], lines
    print("t2 SGOV takes no slot, gets no map entry and no stock exit rule OK")


def t3_the_shadow_line_measures_spendable_usd_after_the_entries():
    working = [Working("VLO", "USD", "BUY", 4, 421.38)]
    ib = BookIB({"USD": 20000.0}, held=STOCKS, working=working)
    st, lines, published = bot_run(ib, [buy("NEWA", 100.0, 90)])
    assert [s for a, q, s in ib.placed] == ["NEWA"], ib.placed     # no SGOV order
    pl = park_lines(lines)
    assert len(pl) == 1, pl
    # free cash (20,000 - 1,685.52) x 7.8 = 142,853 over 1 slot -> 50k cap order
    newa = ib.placed[0][1]
    usd = 20000.0 - 4 * 421.38 - newa * 100.0 * 1.005
    slot = (20000.0 - 4 * 421.38) * 7.8            # the per-slot figure, 1 free slot
    keep = 3 * slot / 7.8
    n = int((usd - keep) // (ib_bot.PARK_REF_PX * 1.005)) if usd - keep >= 1000 else 0
    want = ("park (SHADOW - nothing placed): spendable USD %s after entries, keep %s "
            "(3 slots of %s HKD), SGOV held 0" % (format(usd, ",.0f"), format(keep, ",.0f"),
                                                 format(slot, ",.0f")))
    assert pl[0].startswith(want), (pl[0], want)
    assert n == 0 and pl[0].endswith("-> nothing to park or unpark"), pl[0]

    # With 3 free slots or fewer the buffer (3 slots of free cash / free) is
    # all of it: nothing is ever parked that the next entries need. A quiet
    # book - 2 holdings, 13 free slots, lots of idle USD, nothing to buy - WOULD
    # park what is above 3 slots' worth, and says so
    ib = BookIB({"USD": 60000.0}, held=STOCKS[:2])
    st, lines, published = bot_run(ib, [])
    pl = park_lines(lines)
    slot = 60000.0 * 7.8 / 13                      # 13 free slots
    keep = 3 * slot / 7.8
    n = int((60000.0 - keep) // (ib_bot.PARK_REF_PX * 1.005))
    assert len(pl) == 1 and ("-> would BUY %d SGOV" % n) in pl[0], (pl, n)
    assert n > 400, n
    assert ib.placed == [] and "SGOV" not in st["map"], (ib.placed, st)
    print("t3 the shadow line reads spendable USD after the entries and places nothing OK")


def t4_unpark_and_the_reasons_a_live_mode_would_hold_off():
    held = STOCKS + [("SGOV", "USD", 50, 100.4)]
    working = [Working("SGOV", "USD", "SELL", 10, 100.3)]
    ib = BookIB({"USD": 500.0}, held=held, working=working)
    st, lines, published = bot_run(ib, [])
    pl = park_lines(lines)
    assert len(pl) == 1 and "-> would SELL" in pl[0] and "SGOV held 50" in pl[0], pl
    assert "~100.4)" in pl[0], "the holding's own cost prices it: %s" % pl[0]
    assert "a live mode would hold off: a SGOV order is already working" in pl[0], pl[0]
    assert ib.placed == [], ib.placed
    print("t4 unpark below half the buffer, priced at the holding, hold-off reasons OK")


def t5_off_is_silent_and_an_unbuilt_mode_places_nothing():
    ib = BookIB({"USD": 60000.0}, held=STOCKS)
    st, lines, published = bot_run(ib, [], PARK_MODE="off")
    assert park_lines(lines) == [], park_lines(lines)
    assert any("cash parking off" in l for l in lines), "the rules line must say so"
    ib = BookIB({"USD": 60000.0}, held=STOCKS)
    st, lines, published = bot_run(ib, [], PARK_MODE="live")
    assert any("!! park: PARK_MODE 'live' is not built" in l for l in lines), lines
    assert ib.placed == [] and len(published) == 1, (ib.placed, published)
    assert ib_bot.PARK_MODE == "shadow", "the shipped mode must be shadow"
    print("t5 PARK_MODE off is silent; an unbuilt mode places nothing OK")


def t6_a_parking_report_that_raises_never_costs_the_run():
    def boom(*a, **k):
        raise RuntimeError("SGOV report on fire")
    ib = BookIB({"USD": 20000.0}, held=STOCKS)
    st, lines, published = bot_run(ib, [buy("NEWA", 100.0, 90)], park_idle_usd=boom)
    assert any("cash-parking (shadow) report skipped (SGOV report on fire)" in l
               for l in lines), lines
    assert [s for a, q, s in ib.placed] == ["NEWA"] and len(published) == 1
    assert st["map"].get("NEWA") == "NEWA", "state must still be saved"
    print("t6 a parking report that raises costs nothing: saved and published OK")


def t7_adopt_json_cannot_hand_sgov_to_the_stock_exits():
    d = Path(tempfile.mkdtemp(prefix="adopt-", dir=str(_TMP)))
    p = d / "adopt.json"
    p.write_text(json.dumps({"adopt": [{"symbol": "SGOV", "entry": 100.4, "stop": 95.0,
                                        "entry_date": "2026-10-20"}]}), encoding="utf-8")
    state, lines = {}, []
    held = {"SGOV": (broker.Position(broker.Contract("SGOV"), 50, 100.4), 50)}
    with Patch(ib_bot, log=lambda *a: lines.append(" ".join(str(x) for x in a))):
        done = ib_bot.adopt_holdings(state, held, dry=False, path=p,
                                     today=date(2026, 10, 26), lot_date=lambda s: None)
    assert done == [] and state.get("map", {}) == {} and state.get("pos", {}) == {}, state
    assert any("SGOV is the cash-parking ETF" in l and "NOT adopted" in l for l in lines), lines
    print("t7 adopt.json cannot hand SGOV to the stock exit rules OK")


if __name__ == "__main__":
    t1_the_plan_rules()
    t2_sgov_is_never_a_stock_slot_nor_a_map_entry()
    t3_the_shadow_line_measures_spendable_usd_after_the_entries()
    t4_unpark_and_the_reasons_a_live_mode_would_hold_off()
    t5_off_is_silent_and_an_unbuilt_mode_places_nothing()
    t6_a_parking_report_that_raises_never_costs_the_run()
    t7_adopt_json_cannot_hand_sgov_to_the_stock_exits()
    print("ALL PARK TESTS PASS")
