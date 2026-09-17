#!/usr/bin/env python3
"""Golden tests: an exception after an order is sent never costs that order its stops.

Run from this directory:  python test_run_abort.py

The failure (review 2026-09-17, reproduced three times with stubs): state.json
was written once, at the very end of run(). ensure_ccy's HKD branch had no
try/except, and every read in it is a fresh Web API call - the ledger behind
_spendable_base and fund_from_nonbase, iserver/currency/pairs behind
_fx_order_pair. At 23:35 a US BUY went out, the next candidate was an .HK name,
its ledger read got a 500, and run() raised past save_state. The US order filled
at the open with no state['map'] entry, and the exit loop skips an unmapped
holding without a word: no trailing stop, no regime exit, no time stop.

What is locked down:
  * a read error while funding an HK entry skips THAT candidate and the run
    carries on - later entries are placed and state.json holds them all;
  * any other exception after an order was sent still saves state.json before
    it propagates (live only), and publishes nothing;
  * --dry writes nothing on the exception path either;
  * a run that dies before state.json was read never writes it, so a torn file
    stays on disk for repair instead of being replaced;
  * (review 2026-09-17) an aborted run on which the kill switch tripped does not
    save today's _kill_noted, so the next run publishes the HALT row.
Nothing here touches /root: every path is repointed before ib_bot is imported.
"""
import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

os.environ.setdefault("IB_BACKEND", "web")
_TMP = Path(tempfile.mkdtemp(prefix="mps-abort-"))
os.environ["MPS_EARMARK_DIR"] = str(_TMP / "earmark")
os.environ["MPS_ORDERS_LEDGER"] = str(_TMP / "orders_ledger.jsonl")
os.environ["MPS_ALERT_DIR"] = str(_TMP / "outbox")
os.environ["MPS_EXIT_ATTEMPTS"] = str(_TMP / "exit_attempts.json")
os.environ["MPS_FX_LAST_GOOD"] = str(_TMP / "fx_last_good.json")
os.environ["MPS_CONID_CACHE"] = str(_TMP / "conid_cache.json")
os.environ.pop("EXCLUDED_CASH", None)
(_TMP / "earmark").mkdir()

import alerts                                      # noqa: E402
import broker                                      # noqa: E402
import earmark                                     # noqa: E402
import ib_bot                                      # noqa: E402
import ib_orders                                   # noqa: E402
import ib_web                                      # noqa: E402
from contracts import currency_of                  # noqa: E402

BASE = ib_bot.BASE_CCY
EDIR = _TMP / "earmark"
assert str(earmark.POCKET_FILE).startswith(str(EDIR)), earmark.POCKET_FILE
assert str(alerts.DIR).startswith(str(_TMP)), alerts.DIR
# Wed 2026-09-16 23:35 UTC, the evening run: US, EU, JP and HK all decidable.
RUN_AT = datetime(2026, 9, 16, 23, 35, 20, tzinfo=timezone.utc)
TODAY = date.today().isoformat()

CALM = {"price": 170, "sma200": 160, "atr": 10}     # AAPL: no exit rule fires
BREAK = {"price": 150, "sma200": 160, "atr": 10}    # AAPL: regime-break SELL


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
        ib_bot._FX_COMMITTED.clear()
        ib_bot._FX_PENDING_CCY.clear()
        ib_bot._FX_PENDING.clear()
        ib_bot._EARMARK_RUN.clear()
        ib_bot._EXC_APPLIED.clear()
        ib_bot._POCKET_RUN.clear()
        del ib_bot.PLACED[:]


class C:                                            # a contract
    def __init__(self, symbol, currency="USD", secType="STK"):
        self.symbol, self.currency, self.secType = symbol, currency, secType
        self.conId, self.exchange = abs(hash(symbol)) % 10 ** 6, "SMART"


class Status:
    def __init__(self, status):
        self.status = status


class Trade:
    def __init__(self, status="Submitted"):
        self.orderStatus, self.log = Status(status), []


class FakeIB:
    """Real-shaped account values (HKD 7, USD 10,000) behind the REAL
    cash_by_ccy, _spendable_base and ensure_ccy. ledger_500s: how many account
    reads fail, as a transient Web API 500, once a BUY has gone out."""

    def __init__(self, ledger_500s=0):
        self.placed, self.disconnected = [], False
        self.ledger_500s, self.raised = ledger_500s, 0

    def accountValues(self):
        if self.ledger_500s and any(p[0] == "BUY" for p in self.placed):
            self.ledger_500s -= 1
            self.raised += 1
            raise ib_web.IbWebError("GET portfolio/U1/ledger failed: 500 Internal "
                                    "Server Error")
        return [broker.AccountValue("NetLiquidation", "100000", BASE),
                broker.AccountValue("CashBalance", "7", BASE),
                broker.AccountValue("CashBalance", "10000", "USD")]

    def positions(self):
        return [broker.Position(C("AAPL"), 10, 190.0)]

    def reqAllOpenOrders(self):
        pass

    def openTrades(self):
        return []

    def sleep(self, *a):
        pass

    def qualifyContracts(self, *c):
        return list(c)

    def placeOrder(self, contract, order):
        self.placed.append((order.action, order.totalQuantity, contract.symbol))
        return Trade()

    def disconnect(self):
        self.disconnected = True


RATES = {("USD", BASE): 7.8, (BASE, "USD"): 1 / 7.8, ("EUR", BASE): 9.1,
         ("JPY", BASE): 0.053, ("GBP", BASE): 10.6}


def seed(pos_aapl):
    d = Path(tempfile.mkdtemp(prefix="case-", dir=str(_TMP)))
    path = d / "state.json"
    path.write_text(json.dumps({"map": {"AAPL": "AAPL"}, "pos": {"AAPL": pos_aapl},
                                "_peak_netliq": 100000}, indent=1), encoding="utf-8")
    return d, path


def bot_run(d, path, ib, card, actions, dry=False, **extra):
    """ib_bot.run() with the real funding path; only the edges are stubbed.
    Returns (raised exception or None, published states, log lines)."""
    signals = {"generated": "2026-09-16", "actions": list(actions)}

    def get_json(url):
        if url == ib_bot.SIGNALS_URL:
            return signals
        if url.endswith("/AAPL.json"):
            return {"card": card}
        raise AssertionError("unexpected fetch " + url)

    published, lines = [], []
    alerts.DIR = d / "outbox"
    stubs = dict(
        STATE=path, IB=lambda: ib, connect_or_heal=lambda *a, **k: None,
        get_json=get_json,
        publish_state=lambda ib_, st, nl: published.append(json.loads(json.dumps(st))),
        to_ib=lambda ysym: C(ysym.split(".")[0], currency_of(ysym)),
        fx_rate=lambda ib_, a, b: 1.0 if a == b else RATES.get((a, b), 0.0),
        lot_size=lambda ib_, c: 100 if c.currency == BASE else 1,
        min_tick=lambda ib_, c: 0.01,
        live_base_price=lambda ib_, c, fallback: fallback,
        confirm=lambda msg: True,
        EXIT_ATTEMPTS=d / "exit_attempts.json",
        FILLS_LEDGER=d / "fills_ledger.jsonl",
        HK_ENABLED=True,
        _now_utc=lambda: RUN_AT,
        log=lambda *a: lines.append(" ".join(str(x) for x in a)),
    )
    stubs.update(extra)
    err = None
    with Patch(ib_bot, **stubs):
        try:
            ib_bot.run(dry=dry)
        except BaseException as e:            # KeyboardInterrupt included (t5)
            err = e
        assert not ib_bot._POCKET_RUN.get("active"), "pocket left active"
    return err, published, lines


def on_disk(path):
    return json.loads(path.read_text(encoding="utf-8"))


def edir_names():
    return sorted(p.name for p in EDIR.iterdir())


def clean_edir():
    for f in EDIR.iterdir():
        f.unlink()


MSFT = {"symbol": "MSFT", "action": "BUY", "price": 100, "score": 9, "stop": 90}
HK = {"symbol": "0700.HK", "action": "BUY", "price": 5.0, "score": 5, "stop": 4}
NVDA = {"symbol": "NVDA", "action": "BUY", "price": 100, "score": 3, "stop": 90}
GOOG = {"symbol": "GOOG", "action": "BUY", "price": 100, "score": 5, "stop": 90}
CALM_POS = {"entry": 150, "hw": 170, "stop": 100, "entry_date": TODAY}
BREAK_POS = {"entry": 200, "hw": 250, "stop": 180, "entry_date": "2026-01-02"}


def t1_hk_funding_read_error_skips_that_entry_and_the_run_carries_on():
    # The reviewer's probe: BUY MSFT is sent, then 0700.HK's ledger read 500s.
    d, path = seed(CALM_POS)
    ib = FakeIB(ledger_500s=1)
    err, published, lines = bot_run(d, path, ib, CALM, [MSFT, HK, NVDA])
    assert err is None, "the run aborted: %r" % err
    assert ib.raised == 1, "the ledger read never failed - the probe proves nothing"
    assert ib.placed == [("BUY", 8, "MSFT"), ("BUY", 8, "NVDA")], ib.placed
    st = on_disk(path)
    assert st["map"].get("MSFT") == "MSFT" and st["map"].get("NVDA") == "NVDA", st["map"]
    assert "0700" not in st["map"] and "0700.HK" not in st["pos"], st
    assert st["pos"]["MSFT"] == {"entry": 100, "hw": 100, "stop": 90,
                                 "entry_date": TODAY}, st["pos"]["MSFT"]
    assert len(published) == 1, published
    assert any("HKD funding skipped" in l and "500" in l for l in lines), lines
    assert any("skip 0700.HK: HKD funding did not complete" in l for l in lines), lines

    # The other read on that path: iserver/currency/pairs behind _fx_order_pair
    # (fund_from_nonbase gets that far - USD 10,000 covers HK$6,500 x 1.03).
    def pairs_503(a, b):
        raise ib_orders.OrderError("GET iserver/currency/pairs?currency=USD: 503")
    d, path = seed(CALM_POS)
    ib = FakeIB()
    with Patch(ib_orders, fx_pair_conid=pairs_503):
        err, published, lines = bot_run(d, path, ib, CALM, [MSFT, HK, NVDA])
    assert err is None, "the run aborted: %r" % err
    assert ib.placed == [("BUY", 8, "MSFT"), ("BUY", 8, "NVDA")], ib.placed   # no FX sent
    assert any("funding HKD from USD" in l for l in lines), "never reached the pairs read"
    assert any("HKD funding skipped" in l and "503" in l for l in lines), lines
    st = on_disk(path)
    assert set(st["map"]) == {"AAPL", "MSFT", "NVDA"}, st["map"]
    assert len(published) == 1
    print("t1 an HK funding read error skips that entry; the run carries on and saves OK")


def t2_abort_after_an_order_still_saves_state_live():
    clean_edir()                          # t1's finished runs wrote a pocket file
    d, path = seed(BREAK_POS)
    ib = FakeIB()

    def lot_size(ib_, c):
        if c.symbol == "GOOG":
            raise RuntimeError("simulated crash after an order went out")
        return 1
    err, published, lines = bot_run(d, path, ib, BREAK, [MSFT, GOOG], lot_size=lot_size)
    assert isinstance(err, RuntimeError) and "simulated crash" in str(err), repr(err)
    assert ib.placed == [("SELL", 10, "AAPL"), ("BUY", 8, "MSFT")], ib.placed
    st = on_disk(path)
    # the entry that went out keeps its map entry and its stop...
    assert st["map"] == {"AAPL": "AAPL", "MSFT": "MSFT"}, st["map"]
    assert st["pos"]["MSFT"] == {"entry": 100, "hw": 100, "stop": 90,
                                 "entry_date": TODAY}, st["pos"]["MSFT"]
    # ...and the exit loop's ratchet before it is kept too
    assert st["pos"]["AAPL"]["stop"] == 215, st["pos"]["AAPL"]
    assert st["_peak_netliq"] == 100000
    assert published == [], "an aborted run must not commit and push"
    assert ib.disconnected
    assert any("run aborted (RuntimeError" in l and "state.json saved" in l for l in lines), lines
    # unchanged semantics: the exit memo is written as the exit is sent, the
    # pocket file only at the end of a run that finished
    memo = json.loads((d / "exit_attempts.json").read_text(encoding="utf-8"))
    assert memo["AAPL"]["status"] == "sent", memo
    assert "earmark_pocket.json" not in edir_names(), edir_names()
    print("t2 an abort after an order still saves state.json on a live run OK")


def t3_dry_abort_writes_nothing():
    clean_edir()
    d, path = seed(BREAK_POS)
    before = path.read_bytes()
    ib = FakeIB()

    def lot_size(ib_, c):
        if c.symbol == "GOOG":
            raise RuntimeError("simulated crash in a preview")
        return 1
    err, published, lines = bot_run(d, path, ib, BREAK, [MSFT, GOOG], dry=True,
                                    lot_size=lot_size)
    assert isinstance(err, RuntimeError), repr(err)
    assert path.read_bytes() == before, "--dry rewrote state.json on the way out"
    assert ib.placed == [] and published == [], (ib.placed, published)
    assert sorted(p.name for p in d.iterdir()) == ["state.json"], list(d.iterdir())
    assert edir_names() == [], edir_names()
    assert any("--dry: run aborted (RuntimeError) - state.json NOT written" in l
               for l in lines), lines
    print("t3 --dry writes nothing on the exception path either OK")


def t4_death_before_state_is_read_never_writes_it():
    # (a) the account read fails before load_state: nothing new to keep
    d, path = seed(BREAK_POS)
    before = path.read_bytes()

    def net_liq(ib_):
        raise ib_web.IbWebError("GET portfolio/U1/ledger failed: timeout")
    err, published, lines = bot_run(d, path, FakeIB(), BREAK, [MSFT], net_liq=net_liq)
    assert isinstance(err, ib_web.IbWebError), repr(err)
    assert path.read_bytes() == before
    # (b) a torn state.json: it must stay for repair, not be replaced by {}
    d, path = seed(BREAK_POS)
    path.write_text('{"map": {"AAPL": "AA', encoding="utf-8")
    err, published, lines = bot_run(d, path, FakeIB(), BREAK, [MSFT])
    assert isinstance(err, ValueError), repr(err)
    assert path.read_text(encoding="utf-8") == '{"map": {"AAPL": "AA'
    print("t4 a run that dies before reading state.json never writes it OK")


def t5_ctrl_c_at_a_confirm_after_an_order_saves_state():
    d, path = seed(CALM_POS)
    ib = FakeIB()

    def confirm(msg):
        if "GOOG" in msg:
            raise KeyboardInterrupt()
        return True
    err, published, lines = bot_run(d, path, ib, CALM, [MSFT, GOOG], confirm=confirm)
    assert isinstance(err, KeyboardInterrupt), repr(err)
    assert ib.placed == [("BUY", 8, "MSFT")], ib.placed
    assert on_disk(path)["map"].get("MSFT") == "MSFT"
    assert published == []
    print("t5 Ctrl-C at a CONFIRM after an order still saves state.json OK")


class BlindIB(FakeIB):
    """The working-orders read fails, as broker.openTrades does on purpose."""

    def openTrades(self):
        raise RuntimeError("cannot read working orders (500) - refusing to trade blind: "
                           "an empty list here would duplicate live orders")


def t6_aborted_kill_switch_run_leaves_the_halt_row_to_the_next_run():
    # Review 2026-09-17: the kill switch trips (NetLiq 100,000 against a peak of
    # 200,000), _kill_noted was stamped today, then openTrades raised. The abort
    # saved the stamp but published nothing, so the same UTC day's next run
    # skipped the HALT row and the dashboard never showed entries were halted.
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def halt_rows(rows):
        return [r for r in rows if r.get("action") == "HALT" and r.get("symbol") == "ENTRIES"]

    for seeded in (None, "2026-09-15"):                   # never noted / noted a past day
        clean_edir()
        d, path = seed(CALM_POS)
        st = on_disk(path)
        st["_peak_netliq"] = 200000
        if seeded:
            st["_kill_noted"] = seeded
        path.write_text(json.dumps(st, indent=1), encoding="utf-8")
        pub = []

        def publish(ib_, state, nl):
            pub.append((json.loads(json.dumps(state)), [dict(r) for r in ib_bot.PLACED]))

        err, _, lines = bot_run(d, path, BlindIB(), CALM, [MSFT], publish_state=publish)
        assert isinstance(err, RuntimeError) and "trade blind" in str(err), repr(err)
        assert any("KILL-SWITCH" in l for l in lines), "the kill switch never tripped"
        saved = on_disk(path)
        assert saved["_peak_netliq"] == 200000, saved
        assert saved.get("_kill_noted") == seeded, "abort saved a stamp with no row: %s" % saved
        assert pub == [], "an aborted run must not publish"

        # the next run, same UTC day, healthy: the HALT row is published now
        ib = FakeIB()
        err, _, lines = bot_run(d, path, ib, CALM, [MSFT], publish_state=publish)
        assert err is None, repr(err)
        assert ib.placed == [], "entries are blocked: %s" % ib.placed
        assert len(pub) == 1, pub
        state_pub, placed_pub = pub[0]
        rows = halt_rows(placed_pub)
        assert len(rows) == 1 and "kill-switch: NetLiq 100000 vs peak 200000" in rows[0]["reason"], placed_pub
        assert state_pub["_kill_noted"] == today_utc, state_pub
        assert on_disk(path)["_kill_noted"] == today_utc

        # CONTROL: a third run the same day adds no second row
        err, _, lines = bot_run(d, path, FakeIB(), CALM, [MSFT], publish_state=publish)
        assert err is None and len(pub) == 2 and halt_rows(pub[1][1]) == [], pub[1]
    # --dry never stamps it either
    clean_edir()
    d, path = seed(CALM_POS)
    st = on_disk(path)
    st["_peak_netliq"] = 200000
    path.write_text(json.dumps(st, indent=1), encoding="utf-8")
    before = path.read_bytes()
    err, published, lines = bot_run(d, path, FakeIB(), CALM, [MSFT], dry=True)
    assert err is None and published == [] and path.read_bytes() == before
    print("t6 an aborted kill-switch run leaves _kill_noted, so the next run adds the HALT row OK")


if __name__ == "__main__":
    t1_hk_funding_read_error_skips_that_entry_and_the_run_carries_on()
    t2_abort_after_an_order_still_saves_state_live()
    t3_dry_abort_writes_nothing()
    t4_death_before_state_is_read_never_writes_it()
    t5_ctrl_c_at_a_confirm_after_an_order_saves_state()
    t6_aborted_kill_switch_run_leaves_the_halt_row_to_the_next_run()
    print("ALL RUN-ABORT TESTS PASS")
