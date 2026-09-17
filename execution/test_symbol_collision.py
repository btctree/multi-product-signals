#!/usr/bin/env python3
"""Golden tests: two listings that share one IB symbol never share one position.

Run from this directory:  python test_symbol_collision.py

Review 2026-09-17 ("SAN.MC and SAN.PA now resolve to two different companies
under the one IB symbol key 'SAN'"). The venue-exact resolver is right - SAN.MC
is Santander on BM, SAN.PA is Sanofi on SBF - but ib_bot keys held positions,
working orders and state['map'] by the bare IB symbol, and both are "SAN".
Reproduced with a fake IB and a stubbed secdef search:
  run 1, both BUY with free slots: BUY 91 of conId 12002 AND BUY 14 of conId
         12003 went out, and state['map']['SAN'] kept only 'SAN.PA';
  run 2, both filled, Santander listed last: SAN.PA's trailing stop fired and
         the bot sent SELL 91 of conId 12003 - Sanofi, 14 held - a short of 77,
         with 91 Santander left under no stop at all.
The same holds for the older pairs (DG/DG.PA, MC/MC.PA, 1928.HK/1928.T, ...).

What is locked down:
  * an IB symbol entered this run is not entered again for another listing,
    and a symbol held or working under another listing is skipped - each with
    a log line; a held name's own BUY/HOLD signal stays quiet as before;
  * a REFUSED entry does not take the symbol: nothing was bought;
  * an exit never sells a contract other than the position's own: when the
    card's conId differs, the web shim sells the HELD conId for the held
    quantity and says so loudly (log and alert); a backend that cannot route
    the held contract sends nothing and alerts; --dry only logs;
  * matching conIds sell the qualified contract exactly as before.
The resolver, broker.qualifyContracts and the conid cache are the REAL ones;
only IB's secdef search and the account are stubs. Nothing here touches /root:
every MPS_* path is set before any module is imported.
"""
import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

os.environ.setdefault("IB_BACKEND", "web")
_TMP = Path(tempfile.mkdtemp(prefix="mps-collide-"))
for _var, _name in (("MPS_EARMARK_DIR", "earmark"), ("MPS_ORDERS_LEDGER", "orders_ledger.jsonl"),
                    ("MPS_ALERT_DIR", "outbox"), ("MPS_EXIT_ATTEMPTS", "exit_attempts.json"),
                    ("MPS_FX_LAST_GOOD", "fx_last_good.json"),
                    ("MPS_CONID_CACHE", "conid_cache.json"), ("MPS_OAUTH_DIR", "oauth")):
    os.environ[_var] = str(_TMP / _name)
os.environ.pop("EXCLUDED_CASH", None)
(_TMP / "earmark").mkdir()

import alerts                                      # noqa: E402
import broker                                      # noqa: E402
import ib_bot                                      # noqa: E402
import ib_orders                                   # noqa: E402

assert str(ib_orders.CONID_CACHE).startswith(str(_TMP)), ib_orders.CONID_CACHE
assert str(alerts.DIR).startswith(str(_TMP)), alerts.DIR

SAN_ADR, SANTANDER, SANOFI = 12001, 12002, 12003
# Wed 2026-09-16 23:35 UTC, the evening run: every European bar is final.
RUN_AT = datetime(2026, 9, 16, 23, 35, 20, tzinfo=timezone.utc)
TODAY = date.today().isoformat()


def row(conid, exch, name):
    return {"conid": str(conid), "description": exch, "companyName": name,
            "sections": [{"secType": "STK"}]}


def secdef(path):
    assert path == "iserver/secdef/search?symbol=SAN", path
    return [row(SAN_ADR, "NYSE", "BANCO SANTANDER SA-SPON ADR"),
            row(SANTANDER, "BM", "BANCO SANTANDER SA"),
            row(SANOFI, "SBF", "SANOFI"),
            {"conid": "2147483647", "description": None, "sections": [{"secType": "BOND"}]}]


class Status:
    def __init__(self, status):
        self.status = status


class Entry:
    def __init__(self, message):
        self.message = message


class Trade:
    def __init__(self, status, message=""):
        self.orderStatus, self.log = Status(status), []
        if message:
            self.log.append(Entry(message))


class FakeIB:
    """positions: [(conId, qty)] in the order IB lists them. refuse: conIds
    whose BUY IB refuses. Unqualified contracts go through the real shim."""

    def __init__(self, positions=(), refuse=()):
        self._positions, self.refuse, self.placed = list(positions), set(refuse), []

    def positions(self):
        return [broker.Position(broker.Contract("SAN", "STK", "EUR", conId=cid), q, 10.0)
                for cid, q in self._positions]

    def qualifyContracts(self, c):
        if getattr(c, "conId", 0):
            return [c]
        return broker.IB().qualifyContracts(c)

    def reqAllOpenOrders(self):
        pass

    def openTrades(self):
        return []

    def sleep(self, *a):
        pass

    def placeOrder(self, contract, order):
        self.placed.append((order.action, order.totalQuantity, contract.symbol, contract.conId))
        if contract.conId in self.refuse:
            return Trade("Inactive", "order refused by IB")
        return Trade("PreSubmitted")

    def disconnect(self):
        pass


BUY_BOTH = [{"symbol": "SAN.MC", "action": "BUY", "price": 12.14, "score": 9, "stop": 11},
            {"symbol": "SAN.PA", "action": "BUY", "price": 76.0, "score": 8, "stop": 70}]
# SAN.PA above its SMA200 but under its trailing stop: a trailing-stop SELL
SANOFI_STOPPED = {"price": 75.0, "sma200": 70.0, "atr": 1.0}
SANOFI_POS = {"entry": 76.0, "hw": 80.0, "stop": 78.0, "entry_date": TODAY}


def bot_run(ib, state, actions=(), cards=None, dry=False, backend=None):
    """One ib_bot.run(). Returns (state on disk, log lines, queued alert texts)."""
    d = Path(tempfile.mkdtemp(prefix="run-", dir=str(_TMP)))
    state_path = d / "state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    alerts.DIR = d / "outbox"
    cards = cards or {}
    signals = {"generated": "2026-09-16", "actions": list(actions)}

    def get_json(url):
        if url == ib_bot.SIGNALS_URL:
            return signals
        for ysym, card in cards.items():
            if url.endswith("/" + ib_bot.safe_name(ysym) + ".json"):
                return {"card": card}
        raise AssertionError("unexpected fetch " + url)

    lines = []
    stubs = dict(
        STATE=state_path, IB=lambda: ib, connect_or_heal=lambda *a, **k: None,
        get_json=get_json, _now_utc=lambda: RUN_AT,
        publish_state=lambda ib_, st, nl: None,
        net_liq=lambda ib_: 300000.0,
        cash_by_ccy=lambda ib_: {"HKD": 50000.0, "EUR": 20000.0},
        reserve_working_cash=lambda ib_: None, warm_fx_memory=lambda ib_, a: None,
        fx_rate=lambda ib_, a, b: 9.047, ensure_ccy=lambda ib_, ccy, need, dry_: True,
        lot_size=lambda ib_, c: 1, min_tick=lambda ib_, c: 0.01,
        live_base_price=lambda ib_, c, fallback: fallback,
        confirm=lambda msg: True, EXIT_ATTEMPTS=d / "exit_attempts.json",
        FILLS_LEDGER=d / "fills.jsonl",
        log=lambda *a: lines.append(" ".join(str(x) for x in a)),
    )
    old = {k: getattr(ib_bot, k) for k in stubs}
    real_get, real_backend = ib_orders._get, broker.BACKEND
    try:
        for k, v in stubs.items():
            setattr(ib_bot, k, v)
        ib_orders._get = secdef
        if backend:
            broker.BACKEND = backend
        ib_bot.run(dry=dry)
    finally:
        for k, v in old.items():
            setattr(ib_bot, k, v)
        ib_orders._get, broker.BACKEND = real_get, real_backend
        del ib_bot.PLACED[:]
        ib_bot._FX_COMMITTED.clear()
        ib_bot._EARMARK_RUN.clear()
        ib_bot._POCKET_RUN.clear()
    queued = [q["text"] for _, q in alerts._queued()] if alerts.DIR.exists() else []
    return json.loads(state_path.read_text(encoding="utf-8")), lines, queued


def t1_resolver_really_splits_san():
    # The premise: without it the rest proves nothing.
    assert ib_bot.to_ib("SAN.MC").symbol == ib_bot.to_ib("SAN.PA").symbol == "SAN"
    real_get = ib_orders._get
    ib_orders._get = secdef
    try:
        mc = broker.IB().qualifyContracts(ib_bot.to_ib("SAN.MC"))[0]
        pa = broker.IB().qualifyContracts(ib_bot.to_ib("SAN.PA"))[0]
    finally:
        ib_orders._get = real_get
    assert (mc.conId, pa.conId) == (SANTANDER, SANOFI), (mc.conId, pa.conId)
    print("t1 SAN.MC and SAN.PA qualify to two conIds under one IB symbol OK")


def t2_run1_buys_one_listing_per_ib_symbol():
    ib = FakeIB()
    st, lines, queued = bot_run(ib, {"map": {}, "pos": {}, "_peak_netliq": 300000}, BUY_BOTH)
    assert ib.placed == [("BUY", 182, "SAN", SANTANDER)], ib.placed     # never SANOFI too
    assert st["map"] == {"SAN": "SAN.MC"}, st["map"]
    assert "SAN.MC" in st["pos"] and "SAN.PA" not in st["pos"], st["pos"]
    skip = [l for l in lines if "skip SAN.PA: IB symbol SAN is already taken by SAN.MC" in l]
    assert len(skip) == 1, lines
    # --dry takes the symbol the same way, so the preview matches the live run
    ib = FakeIB()
    st, lines, queued = bot_run(ib, {"map": {}, "pos": {}, "_peak_netliq": 300000}, BUY_BOTH,
                                dry=True)
    assert ib.placed == [] and st["map"] == {}, (ib.placed, st)
    assert [l for l in lines if "BUY 182 SAN @" in l], lines             # SAN.MC's size
    assert not [l for l in lines if "BUY 29 SAN @" in l], lines          # SAN.PA's size
    assert [l for l in lines if "skip SAN.PA: IB symbol SAN is already taken by SAN.MC" in l], lines
    print("t2 run 1: only SAN.MC is bought, SAN.PA skipped with a log line OK")


def t3_refused_entry_does_not_take_the_symbol():
    ib = FakeIB(refuse={SANTANDER})
    st, lines, queued = bot_run(ib, {"map": {}, "pos": {}, "_peak_netliq": 300000}, BUY_BOTH)
    assert [p[3] for p in ib.placed] == [SANTANDER, SANOFI], ib.placed
    assert st["map"] == {"SAN": "SAN.PA"}, st["map"]
    assert not [l for l in lines if "already taken" in l], lines
    print("t3 a refused BUY leaves the IB symbol free for the next listing OK")


def t4_held_or_working_under_another_listing_is_skipped_loudly():
    # Santander held under SAN.MC; SAN.PA signals BUY; SAN.MC's own BUY/HOLD too
    ib = FakeIB(positions=[(SANTANDER, 181)])
    state = {"map": {"SAN": "SAN.MC"}, "_peak_netliq": 300000,
             "pos": {"SAN.MC": {"entry": 12.14, "hw": 12.5, "stop": 10, "entry_date": TODAY}}}
    actions = [dict(BUY_BOTH[0], action="BUY/HOLD"), BUY_BOTH[1]]
    st, lines, queued = bot_run(ib, state, actions,
                                cards={"SAN.MC": {"price": 12.4, "sma200": 11, "atr": 0.2}})
    assert ib.placed == [], ib.placed
    assert [l for l in lines if "skip SAN.PA: IB symbol SAN is already taken by SAN.MC" in l], lines
    assert not [l for l in lines if "skip SAN.MC" in l], "a held name's own signal stays quiet"
    assert st["map"] == {"SAN": "SAN.MC"}, st["map"]
    print("t4 an IB symbol held under another listing is skipped with a log line OK")


def t5_run2_never_sells_a_contract_the_position_is_not():
    # The state the unfixed run 1 left: map SAN -> SAN.PA, both filled, and IB
    # lists Santander last, so held['SAN'] is 181 Santander.
    state = {"map": {"SAN": "SAN.PA"}, "pos": {"SAN.PA": dict(SANOFI_POS)},
             "_peak_netliq": 300000}
    ib = FakeIB(positions=[(SANOFI, 19), (SANTANDER, 181)])
    st, lines, queued = bot_run(ib, state, cards={"SAN.PA": SANOFI_STOPPED})
    sells = [p for p in ib.placed if p[0] == "SELL"]
    assert not [p for p in sells if p[3] == SANOFI], "sold Sanofi for Santander's qty: %s" % sells
    assert sells == [("SELL", 181, "SAN", SANTANDER)], sells          # the held conId, its qty
    loud = [l for l in lines if "CONTRACT MISMATCH" in l]
    assert len(loud) == 1 and str(SANOFI) in loud[0] and str(SANTANDER) in loud[0], lines
    assert "Selling the HELD contract" in loud[0], loud[0]
    mism = [q for q in queued if "two instruments share one IB symbol" in q]
    assert len(mism) == 1 and "sold the HELD contract" in mism[0], queued

    # a backend that cannot route the held contract by conId: NO order, alert
    ib = FakeIB(positions=[(SANOFI, 19), (SANTANDER, 181)])
    st, lines, queued = bot_run(ib, state, cards={"SAN.PA": SANOFI_STOPPED}, backend="socket")
    assert ib.placed == [], ib.placed
    assert [l for l in lines if "CONTRACT MISMATCH" in l and "NO order" in l], lines
    mism = [q for q in queued if "two instruments share one IB symbol" in q]
    assert len(mism) == 1 and "sent NO order" in mism[0], queued
    assert st["map"] == {"SAN": "SAN.PA"}, "the map is the operator's to fix: %s" % st["map"]

    # --dry: logged, nothing sent, nothing queued
    ib = FakeIB(positions=[(SANOFI, 19), (SANTANDER, 181)])
    st, lines, queued = bot_run(ib, state, cards={"SAN.PA": SANOFI_STOPPED}, dry=True)
    assert ib.placed == [] and queued == [], (ib.placed, queued)
    assert [l for l in lines if "CONTRACT MISMATCH" in l], lines

    # CONTROL - IB lists Sanofi last, the conIds match: the qualified contract is
    # sold exactly as before, with no mismatch said
    ib = FakeIB(positions=[(SANTANDER, 181), (SANOFI, 19)])
    st, lines, queued = bot_run(ib, state, cards={"SAN.PA": SANOFI_STOPPED})
    assert ib.placed == [("SELL", 19, "SAN", SANOFI)], ib.placed
    assert not [l for l in lines if "MISMATCH" in l] and not [q for q in queued if "share one" in q]
    print("t5 run 2: an exit sells only the held conId, never the card's other listing OK")


if __name__ == "__main__":
    t1_resolver_really_splits_san()
    t2_run1_buys_one_listing_per_ib_symbol()
    t3_refused_entry_does_not_take_the_symbol()
    t4_held_or_working_under_another_listing_is_skipped_loudly()
    t5_run2_never_sells_a_contract_the_position_is_not()
    print("ALL SYMBOL-COLLISION TESTS PASS")
