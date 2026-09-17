#!/usr/bin/env python3
"""Golden tests for order alerts: refused exits, unfinished exits, phone sells.

Run from this directory:  python test_alerts.py

A refused exit used to be a REJECTED dashboard row and nothing else. BEN's
trailing-stop exit was refused 5 times over ~35 h before a manual rerun sold it;
XYZ's was refused twice, its rule stopped firing, and 22 shares sat on with no
stop for two weeks. No alert went out for any of them. What is locked down:

  * enqueue never raises and never leaves half a file; once=True alerts once.
  * a refusal episode is one full alert, then one short line per later run.
  * drain escapes IB's HTML, batches, and deletes only what was delivered.
  * ib_bot alerts on a refused exit and on an exit that never completed, and
    stays silent while an exit is still working - without ever changing what
    it trades, and without an alert failure being able to stop a run.
  * ib_commands alerts on a refused or unmatched phone SELL only AFTER the DONE
    save, and on a command an exception cut short exactly once.
  * telegram_poll delivers before getUpdates can return early or fail.

Nothing here touches /root: every path is repointed before anything runs.
"""
import json
import os
import re
import tempfile
from datetime import date
from pathlib import Path

os.environ.setdefault("IB_BACKEND", "web")
import alerts                                      # noqa: E402
import earmark                                     # noqa: E402
import ib_bot                                      # noqa: E402
import ib_commands                                 # noqa: E402
import telegram_poll                               # noqa: E402

_ROOT = Path(tempfile.mkdtemp(prefix="mps-alerts-"))
earmark.MARKER_FILE = _ROOT / "excluded_cash"      # run() reads it
alerts.DIR = _ROOT / "outbox"                      # never touch /root in a test
ib_bot.EXIT_ATTEMPTS = _ROOT / "exit_attempts.json"
ib_commands.DONE = _ROOT / "commands_done.json"
telegram_poll.OFFSET_FILE = str(_ROOT / "telegram_offset.json")
telegram_poll.LOCK = str(_ROOT / "tg_poll.lock")

HTML_ERR = "<h4>Market Order Confirmation</h4>&nbsp;declined by IB"


def fresh():
    """A new, empty spool and memo for one test."""
    d = Path(tempfile.mkdtemp(prefix="mps-alerts-", dir=str(_ROOT)))
    alerts.DIR = d / "outbox"
    ib_bot.EXIT_ATTEMPTS = d / "exit_attempts.json"
    ib_commands.DONE = d / "commands_done.json"
    return d


def queued():
    """Queued alert texts, oldest first, exactly as drain would read them."""
    return [d["text"] for _, d in alerts._queued()]


def spool_names():
    return sorted(p.name for p in alerts.DIR.iterdir()) if alerts.DIR.exists() else []


def set_created(order):
    """Pin queue order: enqueue stamps time.time(), which two quick calls can tie."""
    for i, (p, d) in enumerate(sorted(alerts._queued(), key=lambda r: order.index(r[1]["key"]))):
        d["created"] = 1000.0 + i
        p.write_text(json.dumps(d), encoding="utf-8")


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


# ---------------- alerts.py on its own ----------------

def t1_enqueue_writes_one_whole_file_per_key():
    fresh()
    assert alerts.enqueue("exit-BEN-1", "first") is True
    assert alerts.enqueue("exit-BEN-1", "second") is True    # same key: replaced
    names = spool_names()
    assert len(names) == 1 and names[0].startswith("a-exit-BEN-1-"), names
    assert queued() == ["second"], queued()
    assert not [n for n in names if n.endswith(".tmp")], names
    # keys that sanitise to the same readable name must not overwrite each other
    assert alerts.enqueue("cmd a/b", "x") and alerts.enqueue("cmd_a_b", "y")
    assert sorted(queued()) == ["second", "x", "y"], queued()
    print("t1 enqueue writes one whole file per key OK")


def t2_enqueue_never_raises_and_never_leaves_half_a_file():
    d = fresh()
    # a failed atomic replace leaves NO alert and NO tmp behind
    real_replace = alerts.os.replace

    def boom(*a, **k):
        raise OSError("disk full")
    alerts.os.replace = boom
    try:
        assert alerts.enqueue("exit-X-1", "text") is False
    finally:
        alerts.os.replace = real_replace
    assert spool_names() == [], spool_names()
    # an unusable spool directory: a FILE sits where the directory should be
    blocker = d / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    alerts.DIR = blocker / "outbox"
    assert alerts.enqueue("exit-X-1", "text") is False
    assert alerts.enqueue("cmd-1", "text", once=True) is False
    assert alerts.exit_refused("X", 1, "r", "e", "run") is False
    alerts.clear_episodes({"Y"})                                   # returns, no raise
    assert alerts.drain(lambda t: None) == 0

    class Unprintable:
        def __str__(self):
            raise ValueError("no text")
    fresh()
    assert alerts.enqueue("exit-X-2", Unprintable()) is False
    print("t2 enqueue never raises and leaves no torn file OK")


def t3_once_alerts_only_once_even_after_delivery():
    fresh()
    assert alerts.enqueue("cmd-unrun-7", "could not run", once=True) is True
    assert alerts.enqueue("cmd-unrun-7", "could not run", once=True) is False   # queued
    sent = []
    assert alerts.drain(sent.append) == 1
    assert alerts.enqueue("cmd-unrun-7", "could not run", once=True) is False   # delivered
    assert alerts.enqueue("cmd-unrun-8", "another issue", once=True) is True
    assert queued() == ["another issue"], queued()
    # the registry is never mistaken for an alert
    assert alerts.ONCE_NAME in spool_names()
    sent = []
    alerts.drain(sent.append)
    assert sent == ["another issue"], sent
    print("t3 once=True alerts once, queued or delivered OK")


def t4_refusal_episode_first_repeat_and_clear():
    fresh()
    assert alerts.exit_refused("BEN", 22, "trailing stop 44.10", HTML_ERR, "run1")
    first = queued()
    assert len(first) == 1, first
    for part in ("EXIT REFUSED", "BEN", "22", "trailing stop 44.10", HTML_ERR):
        assert part in first[0], (part, first[0])
    # the same run cannot add a second line
    assert alerts.exit_refused("BEN", 22, "trailing stop 44.10", HTML_ERR, "run1") is False
    assert len(queued()) == 1
    assert alerts.exit_refused("BEN", 22, "trailing stop 44.10", HTML_ERR, "run2")
    assert alerts.exit_refused("BEN", 22, "trailing stop 44.10", HTML_ERR, "run3")
    texts = queued()
    assert len(texts) == 3, texts
    for n, t in ((2, texts[1]), (3, texts[2])):
        assert "BEN" in t and ("still refused - attempt %d since" % n) in t, t
        assert "\n" not in t and HTML_ERR not in t, "a repeat is ONE short line: %r" % t
    # still held: the episode survives; gone: it closes, and a new refusal is news
    alerts.clear_episodes({"BEN", "AAPL"})
    assert alerts.exit_refused("BEN", 22, "trailing stop 44.10", HTML_ERR, "run4")
    assert "attempt 4" in queued()[-1], queued()[-1]
    alerts.clear_episodes({"AAPL"})
    assert alerts.exit_refused("BEN", 5, "time stop (60 bars >= 60)", "no", "run5")
    assert "EXIT REFUSED" in queued()[-1] and "time stop" in queued()[-1], queued()[-1]
    print("t4 refusal episode: one full alert, then one line per run, cleared when gone OK")


def t5_drain_escapes_batches_and_deletes_after_success():
    fresh()
    alerts.enqueue("k1", "EXIT REFUSED: SELL 22 BEN\nIB said: " + HTML_ERR)
    alerts.enqueue("k2", "second & last <b>")
    alerts.enqueue("k3", "third")
    set_created(["k1", "k2", "k3"])
    sent = []
    assert alerts.drain(sent.append) == 3
    assert len(sent) == 1, sent                                   # one message fits all
    msg = sent[0]
    assert "<h4>" not in msg and "<b>" not in msg, msg
    assert "&lt;h4&gt;Market Order Confirmation&lt;/h4&gt;&amp;nbsp;" in msg, msg
    assert "second &amp; last &lt;b&gt;" in msg, msg
    assert msg.index("BEN") < msg.index("second") < msg.index("third"), "oldest first"
    assert [n for n in spool_names() if n.startswith("a-")] == [], spool_names()
    # batching: as few messages as fit, none over the cap
    fresh()
    keys = ["b%d" % i for i in range(5)]
    for k in keys:
        alerts.enqueue(k, k + ":" + "x" * 37)                     # 40 chars each
    set_created(keys)
    sent = []
    assert alerts.drain(sent.append, max_chars=100) == 5
    assert [len(m) <= 100 for m in sent] == [True] * 3, [len(m) for m in sent]
    assert "\n\n".join(sent).count(":") == 5
    assert sent[0].startswith("b0:") and sent[-1].startswith("b4:"), sent
    # one alert bigger than the cap is cut on the PLAIN text: never mid-entity
    fresh()
    alerts.enqueue("huge", "&" * 500)
    sent = []
    assert alerts.drain(sent.append, max_chars=100) == 1
    assert len(sent[0]) <= 100 and sent[0].endswith(" ..."), sent
    assert re.sub("&amp;", "", sent[0][:-4]) == "", "a cut landed inside an entity: %r" % sent[0]
    print("t5 drain escapes, batches, deletes after success OK")


def t6_drain_keeps_what_it_could_not_deliver():
    fresh()
    alerts.enqueue("f1", "a" * 60)
    alerts.enqueue("f2", "b" * 60)
    set_created(["f1", "f2"])

    def down(text):
        raise RuntimeError("telegram rejected: 502")
    assert alerts.drain(down) == 0
    assert len(queued()) == 2, "a failed send must keep every alert"
    # first message delivered, second refused: only the first is deleted
    calls = []

    def flaky(text):
        calls.append(text)
        if len(calls) > 1:
            raise RuntimeError("429 too many requests")
    assert alerts.drain(flaky, max_chars=100) == 1
    assert queued() == ["b" * 60], queued()
    sent = []
    assert alerts.drain(sent.append) == 1 and sent == ["b" * 60]
    # an alert re-queued under the same key WHILE its old version was being sent
    # is a new alert: it must survive the old version's deletion
    fresh()
    alerts.enqueue("cmd-9", "old text")

    def requeue(text):
        alerts.enqueue("cmd-9", "new text")
    assert alerts.drain(requeue) == 1
    assert queued() == ["new text"], queued()
    # a malformed file is set aside; it must never block the alerts behind it
    fresh()
    alerts.enqueue("good", "still delivered")
    (alerts.DIR / "a-torn-0.json").write_text('{"text": "x', encoding="utf-8")
    (alerts.DIR / "a-odd-0.json").write_text('{"text": "x", "created": "soon"}', encoding="utf-8")
    sent = []
    assert alerts.drain(sent.append) == 1 and sent == ["still delivered"], sent
    assert sorted(n for n in spool_names() if n.startswith("a-")) == ["a-odd-0.bad", "a-torn-0.bad"], \
        spool_names()
    print("t6 drain keeps undelivered and re-queued alerts OK")


# ---------------- ib_bot wiring ----------------

class C:                                            # a contract
    def __init__(self, symbol, currency="USD", secType="STK"):
        self.symbol, self.currency, self.secType = symbol, currency, secType
        self.conId, self.exchange = abs(hash(symbol)) % 10 ** 6, "SMART"


class Pos:
    def __init__(self, contract, qty):
        self.contract, self.position, self.avgCost = contract, qty, 100.0


class Status:
    def __init__(self, status):
        self.status = status


class Entry:
    def __init__(self, message):
        self.message = message


class Order:
    def __init__(self, action):
        self.action = action


class Trade:
    def __init__(self, contract, status, action="SELL", message=""):
        self.contract, self.order = contract, Order(action)
        self.orderStatus, self.log = Status(status), []
        if message:
            self.log.append(Entry(message))


class FakeIB:
    """refuse: the actions IB refuses (as the web shim reports it: Inactive plus
    IB's message). Everything else is accepted and left working."""

    def __init__(self, refuse=(), open_trades=()):
        self.refuse, self.open_trades = set(refuse), list(open_trades)
        self.placed = []

    def reqAllOpenOrders(self):
        pass

    def openTrades(self):
        return self.open_trades

    def sleep(self, *a):
        pass

    def qualifyContracts(self, c):
        return [c]

    def placeOrder(self, contract, order):
        self.placed.append((order.action, order.totalQuantity, contract.symbol))
        if order.action in self.refuse:
            return Trade(contract, "Inactive", order.action, HTML_ERR)
        return Trade(contract, "PreSubmitted", order.action)

    def disconnect(self):
        pass


def bot_run(d, held, cards, pos_state, ib, actions=(), dry=False):
    """One ib_bot.run() against stubs. held: {symbol: qty}; cards: {symbol:
    card, or None to make the card fetch fail}. Returns (published, state)."""
    state_path = d / "state.json"
    if not state_path.exists():
        state_path.write_text(json.dumps({
            "map": {s: s for s in pos_state}, "pos": pos_state,
            "_peak_netliq": 100000}), encoding="utf-8")
    signals = {"generated": "2026-09-17", "actions": list(actions)}

    def get_json(url):
        if url == ib_bot.SIGNALS_URL:
            return signals
        for sym, card in cards.items():
            if url.endswith("/" + sym + ".json"):
                if card is None:
                    raise IOError("404")
                return {"card": card}
        raise AssertionError("unexpected fetch " + url)

    published = []
    stubs = dict(
        STATE=state_path, IB=lambda: ib, connect_or_heal=lambda *a, **k: None,
        get_json=get_json,
        publish_state=lambda ib_, state, nl: published.append(json.loads(json.dumps(state))),
        net_liq=lambda ib_: 100000.0,
        cash_by_ccy=lambda ib_: {"HKD": 50000.0, "USD": 10000.0},
        held_positions=lambda ib_: {s: (Pos(C(s), q), q) for s, q in held.items()},
        reserve_working_cash=lambda ib_: None,
        warm_fx_memory=lambda ib_, a: None,
        to_ib=lambda ysym: C(ysym), currency_of=lambda ysym: "USD",
        entry_blocked_reason=lambda ysym, ccy: None, fx_rate=lambda ib_, a, b: 7.8,
        ensure_ccy=lambda ib_, ccy, need, dry_: True, lot_size=lambda ib_, c: 1,
        min_tick=lambda ib_, c: 0.01, live_base_price=lambda ib_, c, fallback: fallback,
        confirm=lambda msg: True,
    )
    try:
        with Patch(ib_bot, **stubs):
            ib_bot.run(dry=dry)
    finally:
        del ib_bot.PLACED[:]
        ib_bot._FX_COMMITTED.clear()
        ib_bot._EARMARK_RUN.clear()
    return published, json.loads(state_path.read_text(encoding="utf-8"))


TODAY = date.today().isoformat()
# AAPL below its SMA200: the regime-break exit fires
BREAK = {"price": 150, "sma200": 160, "atr": 10}
# AAPL above its SMA200 and its trail (150): no exit rule fires
CALM = {"price": 170, "sma200": 160, "atr": 10}
CALM_POS = {"AAPL": {"entry": 150, "hw": 170, "stop": 100, "entry_date": TODAY}}


def memo():
    try:
        return json.loads(ib_bot.EXIT_ATTEMPTS.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def seed_memo(book):
    ib_bot.EXIT_ATTEMPTS.write_text(json.dumps(book), encoding="utf-8")


def t7_refused_exit_is_alerted_then_summarised():
    d = fresh()
    ib = FakeIB(refuse={"SELL"})
    published, state = bot_run(d, {"AAPL": 10}, {"AAPL": BREAK}, CALM_POS, ib)
    assert ("SELL", 10, "AAPL") in ib.placed, ib.placed
    texts = queued()
    assert len(texts) == 1, texts
    for part in ("EXIT REFUSED", "SELL 10 AAPL", "regime break", HTML_ERR):
        assert part in texts[0], (part, texts[0])
    assert memo()["AAPL"]["status"] == "REJECTED", memo()
    assert len(published) == 1, "the run must still publish"
    # next run, refused again: one short line, not a second full alert
    published, state = bot_run(d, {"AAPL": 10}, {"AAPL": BREAK}, CALM_POS, ib)
    texts = queued()
    assert len(texts) == 2 and "AAPL exit still refused - attempt 2 since" in texts[1], texts
    # and it did not change what was traded: one SELL per run, as before
    assert [p for p in ib.placed if p[0] == "SELL"] == [("SELL", 10, "AAPL")] * 2, ib.placed
    print("t7 refused exit alerted in full, then one line per run OK")


def t8_alert_failure_cannot_stop_the_run():
    d = fresh()
    ib = FakeIB(refuse={"SELL"})

    def boom(*a, **k):
        raise RuntimeError("spool on fire")
    with Patch(alerts, enqueue=boom, exit_refused=boom, clear_episodes=boom):
        published, state = bot_run(
            d, {"AAPL": 10}, {"AAPL": BREAK}, CALM_POS, ib,
            actions=[{"symbol": "MSFT", "action": "BUY", "price": 100, "score": 5, "stop": 90}])
    assert ("SELL", 10, "AAPL") in ib.placed and ("BUY", 8, "MSFT") in ib.placed, ib.placed
    assert len(published) == 1, "publish_state must still run"
    assert state["map"].get("MSFT") == "MSFT", "save_state must still run"
    print("t8 an alert failure cannot stop exits, entries, save or publish OK")


def t9_accepted_exit_that_vanished_is_reported():
    d = fresh()
    seed_memo({"AAPL": {"time": "2026-09-16 23:35 UTC", "status": "sent", "qty": 10,
                        "reason": "regime break (close < SMA200)"}})
    ib = FakeIB()                                           # accepts this run
    bot_run(d, {"AAPL": 10}, {"AAPL": BREAK}, CALM_POS, ib)
    texts = queued()
    assert len(texts) == 1, texts
    assert "AAPL" in texts[0] and "accepted at 2026-09-16 23:35 UTC" in texts[0], texts[0]
    assert "did not complete" in texts[0] and "10 still held" in texts[0], texts[0]
    assert memo()["AAPL"]["status"] == "sent" and memo()["AAPL"]["time"] != "2026-09-16 23:35 UTC"
    # ...but when THIS run's exit is refused, the refusal alert speaks alone
    d = fresh()
    seed_memo({"AAPL": {"time": "2026-09-16 23:35 UTC", "status": "sent", "qty": 10,
                        "reason": "regime break (close < SMA200)"}})
    bot_run(d, {"AAPL": 10}, {"AAPL": BREAK}, CALM_POS, FakeIB(refuse={"SELL"}))
    texts = queued()
    assert len(texts) == 1 and "EXIT REFUSED" in texts[0], texts
    # ...and a refused exit that IB accepts on the next run is simply progress
    d = fresh()
    seed_memo({"AAPL": {"time": "2026-09-16 23:35 UTC", "status": "REJECTED", "qty": 10,
                        "reason": "regime break (close < SMA200)"}})
    bot_run(d, {"AAPL": 10}, {"AAPL": BREAK}, CALM_POS, FakeIB())
    assert queued() == [], queued()
    assert memo()["AAPL"]["status"] == "sent", memo()
    print("t9 accepted-then-gone exit reported; a fresh refusal is not doubled OK")


def t10_lapsed_exit_is_reported_and_forgotten():
    # The XYZ case: refused, then the rule stopped firing, position kept forever.
    d = fresh()
    seed_memo({"AAPL": {"time": "2026-09-01 23:35 UTC", "status": "REJECTED", "qty": 10,
                        "reason": "trailing stop 78.15"}})
    ib = FakeIB()
    bot_run(d, {"AAPL": 10}, {"AAPL": CALM}, CALM_POS, ib)
    assert ib.placed == [], "the alert must not turn into an order: %s" % ib.placed
    texts = queued()
    assert len(texts) == 1, texts
    for part in ("AAPL", "2026-09-01 23:35 UTC", "REJECTED", "trailing stop 78.15",
                 "never completed", "condition has cleared", "keeping the position"):
        assert part in texts[0], (part, texts[0])
    assert "AAPL" not in memo(), memo()
    bot_run(d, {"AAPL": 10}, {"AAPL": CALM}, CALM_POS, ib)
    assert len(queued()) == 1, "a lapsed exit is reported once"
    print("t10 lapsed exit reported once, record dropped, nothing traded OK")


def t11_working_exit_is_not_alerted():
    # A 23:35 US exit is legitimately still working at 09:00.
    d = fresh()
    rec = {"AAPL": {"time": "2026-09-16 23:35 UTC", "status": "sent", "qty": 10,
                    "reason": "regime break (close < SMA200)"}}
    seed_memo(rec)
    ib = FakeIB(open_trades=[Trade(C("AAPL"), "PreSubmitted", "SELL")])
    bot_run(d, {"AAPL": 10}, {"AAPL": BREAK}, CALM_POS, ib)
    assert ib.placed == [], ib.placed
    assert queued() == [], queued()
    assert memo() == rec, memo()
    # a CONDITION-cleared symbol with a working order is left alone too
    bot_run(d, {"AAPL": 10}, {"AAPL": CALM}, CALM_POS, ib)
    assert queued() == [] and memo() == rec, (queued(), memo())
    print("t11 an exit still working is neither alerted nor forgotten OK")


def t12_gone_dropped_quietly_skipped_left_alone():
    d = fresh()
    rec = {"time": "2026-09-16 23:35 UTC", "status": "sent", "qty": 5, "reason": "r"}
    seed_memo({"SOLD": dict(rec), "AAPL": dict(rec), "NOMAP": dict(rec), "NULLPX": dict(rec)})
    pos_state = dict(CALM_POS)
    pos_state["NULLPX"] = {"entry": 10, "hw": 10, "stop": 1, "entry_date": "2020-01-02"}
    # AAPL's card fetch fails; NOMAP is held with no state.map entry; NULLPX's
    # card has no price (its time stop is due but deferred). None can be judged.
    state_path = d / "state.json"
    state_path.write_text(json.dumps({"map": {"AAPL": "AAPL", "NULLPX": "NULLPX"},
                                      "pos": pos_state, "_peak_netliq": 100000}),
                          encoding="utf-8")
    ib = FakeIB()
    bot_run(d, {"AAPL": 10, "NOMAP": 3, "NULLPX": 4},
            {"AAPL": None, "NULLPX": {"price": None, "sma200": None, "atr": 0}},
            pos_state, ib)
    assert ib.placed == [], ib.placed
    assert queued() == [], queued()
    assert sorted(memo()) == ["AAPL", "NOMAP", "NULLPX"], memo()   # SOLD dropped
    print("t12 sold symbol dropped quietly; unjudgeable records left untouched OK")


def t13_dry_run_writes_no_alert_state():
    d = fresh()
    seed_memo({"SOLD": {"time": "t", "status": "sent", "qty": 1, "reason": "r"}})
    before = ib_bot.EXIT_ATTEMPTS.read_bytes()
    ib = FakeIB(refuse={"SELL"})
    published, _ = bot_run(d, {"AAPL": 10}, {"AAPL": BREAK}, CALM_POS, ib, dry=True)
    assert ib.placed == [] and published == []
    assert spool_names() == [], spool_names()
    assert ib_bot.EXIT_ATTEMPTS.read_bytes() == before
    # the lapsed-exit path too: a cleared condition with a record on file
    d = fresh()
    seed_memo({"AAPL": {"time": "t", "status": "REJECTED", "qty": 10, "reason": "r"}})
    before = ib_bot.EXIT_ATTEMPTS.read_bytes()
    published, _ = bot_run(d, {"AAPL": 10}, {"AAPL": CALM}, CALM_POS, FakeIB(), dry=True)
    assert spool_names() == [], spool_names()
    assert ib_bot.EXIT_ATTEMPTS.read_bytes() == before
    print("t13 --dry queues nothing and leaves the memo alone OK")


# ---------------- ib_commands ----------------

class CmdIB:
    def __init__(self, positions, refuse=True, connect_error=None):
        self._positions, self.refuse = positions, refuse
        self.connect_error, self.placed = connect_error, []

    def connect(self, *a, **k):
        if self.connect_error:
            raise self.connect_error

    def positions(self):
        return self._positions

    def qualifyContracts(self, c):
        return [c]

    def placeOrder(self, contract, order):
        self.placed.append((order.action, order.totalQuantity, contract.symbol))
        if self.refuse:
            return Trade(contract, "Inactive", "SELL", HTML_ERR)
        return Trade(contract, "PreSubmitted", "SELL")

    def sleep(self, *a):
        pass

    def disconnect(self):
        pass


def _cmd(num, symbol, qty=None):
    return {"id": num, "kind": "sell", "symbol": symbol, "qty": qty, "amount": None}


def t14_phone_sell_refused_and_unmatched_are_alerted_after_done():
    fresh()
    ib = CmdIB([Pos(C("AAPL"), 10)])
    published = []
    at_enqueue = []
    real_enqueue = alerts.enqueue

    def spy(key, text, once=False):
        # what DONE held at the moment the alert was queued
        done = json.loads(ib_commands.DONE.read_text()) if ib_commands.DONE.exists() else []
        at_enqueue.append((key, list(done)))
        return real_enqueue(key, text, once)
    try:
        with Patch(ib_commands, fetch_commands=lambda: [_cmd(101, "AAPL", 5), _cmd(102, "NOPE")],
                   IB=lambda: ib), \
                Patch(ib_bot, load_state=lambda: {"map": {}}, net_liq=lambda ib_: 1.0,
                      publish_state=lambda ib_, s, nl: published.append(1)), \
                Patch(alerts, enqueue=spy):
            ib_commands.main()
    finally:
        del ib_bot.PLACED[:]
    assert ib.placed == [("SELL", 5, "AAPL")], ib.placed
    texts = queued()
    assert len(texts) == 2, texts
    assert "PHONE SELL REFUSED" in texts[0] and "SELL 5 AAPL" in texts[0], texts[0]
    assert "issue #101" in texts[0] and HTML_ERR in texts[0], texts[0]
    assert "no held position matches NOPE" in texts[1] and "issue #102" in texts[1], texts[1]
    assert at_enqueue == [("cmd-101", [101]), ("cmd-102", [101, 102])], at_enqueue
    assert published == [1]
    # an accepted sell alerts nothing
    fresh()
    ib = CmdIB([Pos(C("AAPL"), 10)], refuse=False)
    try:
        with Patch(ib_commands, fetch_commands=lambda: [_cmd(103, "AAPL")], IB=lambda: ib), \
                Patch(ib_bot, load_state=lambda: {"map": {}}, net_liq=lambda ib_: 1.0,
                      publish_state=lambda ib_, s, nl: None):
            ib_commands.main()
    finally:
        del ib_bot.PLACED[:]
    assert ib.placed == [("SELL", 10, "AAPL")] and queued() == [], (ib.placed, queued())
    # static guard: nothing alert-related between placeOrder and the DONE save
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ib_commands.py"),
               encoding="utf-8").read()
    body = src[src.index("trade = ib.placeOrder("):]
    window = body[:body.index("DONE.write_text(")]
    code = "\n".join(line.split("#")[0] for line in window.splitlines())
    assert "alert" not in code, "an alert call sits in the place->DONE window"
    assert "refusal = (qty, p.contract.symbol, err)" in code, "the window holds only a tuple"
    print("t14 refused and unmatched phone sells alerted, after the DONE save OK")


def t15_command_cut_short_by_an_exception_alerts_once():
    fresh()
    ib = CmdIB([], connect_error=RuntimeError("no bridge"))
    delivered = []
    for _poll in range(3):                                  # three 10-minute polls
        try:
            with Patch(ib_commands, fetch_commands=lambda: [_cmd(201, "AAPL", 5),
                                                            _cmd(202, "BEN")],
                       IB=lambda: ib), \
                    Patch(ib_bot, load_state=lambda: {"map": {}}):
                ib_commands.main()
            raise AssertionError("connect error must propagate to the handler")
        except RuntimeError as e:
            ib_commands.alert_unrun(e)                      # what __main__ does
        # telegram_poll delivers between polls. Without this a repeat would just
        # overwrite the same queued file and "once" would go untested.
        alerts.drain(lambda text: delivered.extend(text.split("\n\n")))
    texts = delivered
    assert len(texts) == 2, texts
    assert "did not complete: SELL AAPL 5" in texts[0] and "issue #201" in texts[0], texts[0]
    assert "no bridge" in texts[0] and "SELL BEN (all)" in texts[1], texts
    # a command already done by this poll is not reported
    ib_commands._POLL["todo"] = [_cmd(301, "X")]
    ib_commands._POLL["done"] = {301}
    ib_commands.alert_unrun(RuntimeError("late"))
    assert queued() == [], queued()
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ib_commands.py"),
               encoding="utf-8").read()
    handler = src[src.index('if __name__ == "__main__":'):]
    assert "alert_unrun(e)" in handler, "the __main__ handler must queue the alert"
    print("t15 command cut short by an exception alerts once per issue OK")


# ---------------- telegram_poll ----------------

def t16_telegram_poll_drains_before_get_updates():
    fresh()
    alerts.enqueue("exit-refused-BEN-run1", "EXIT REFUSED: " + HTML_ERR)
    calls = []
    ds = telegram_poll.ds
    with Patch(ds, load_cfg=lambda: {"TELEGRAM_TOKEN": "t", "TELEGRAM_CHAT_ID": "42"},
               send_message=lambda tok, chat, text: calls.append(("send", chat, text)) or 1), \
            Patch(telegram_poll, get_updates=lambda tok, off: calls.append(("get",)) or []):
        assert telegram_poll.main() == 0
    assert [c[0] for c in calls] == ["send", "get"], calls
    assert calls[0][1] == "42" and "&lt;h4&gt;" in calls[0][2], calls
    assert queued() == [], "a delivered alert must be removed"
    # a failed send keeps the alert AND still lets the command handler run
    alerts.enqueue("exit-refused-BEN-run2", "again")
    calls = []

    def down(tok, chat, text):
        calls.append(("send",))
        raise RuntimeError("telegram rejected")
    with Patch(ds, load_cfg=lambda: {"TELEGRAM_TOKEN": "t", "TELEGRAM_CHAT_ID": "42"},
               send_message=down), \
            Patch(telegram_poll, get_updates=lambda tok, off: calls.append(("get",)) or []):
        assert telegram_poll.main() == 0
    assert [c[0] for c in calls] == ["send", "get"], calls
    assert queued() == ["again"], queued()
    # getUpdates failing cannot block delivery: the alert went out first
    calls = []

    def updates_down(tok, off):
        raise RuntimeError("getUpdates: 409")
    with Patch(ds, load_cfg=lambda: {"TELEGRAM_TOKEN": "t", "TELEGRAM_CHAT_ID": "42"},
               send_message=lambda tok, chat, text: calls.append(text) or 1), \
            Patch(telegram_poll, get_updates=updates_down):
        try:
            telegram_poll.main()
            raise AssertionError("getUpdates failure should still surface")
        except RuntimeError:
            pass
    assert calls == ["again"] and queued() == [], (calls, queued())
    assert not os.path.exists(telegram_poll.LOCK), "the lock must be released"
    print("t16 telegram_poll drains before getUpdates OK")


if __name__ == "__main__":
    t1_enqueue_writes_one_whole_file_per_key()
    t2_enqueue_never_raises_and_never_leaves_half_a_file()
    t3_once_alerts_only_once_even_after_delivery()
    t4_refusal_episode_first_repeat_and_clear()
    t5_drain_escapes_batches_and_deletes_after_success()
    t6_drain_keeps_what_it_could_not_deliver()
    t7_refused_exit_is_alerted_then_summarised()
    t8_alert_failure_cannot_stop_the_run()
    t9_accepted_exit_that_vanished_is_reported()
    t10_lapsed_exit_is_reported_and_forgotten()
    t11_working_exit_is_not_alerted()
    t12_gone_dropped_quietly_skipped_left_alone()
    t13_dry_run_writes_no_alert_state()
    t14_phone_sell_refused_and_unmatched_are_alerted_after_done()
    t15_command_cut_short_by_an_exception_alerts_once()
    t16_telegram_poll_drains_before_get_updates()
    print("ALL ALERT TESTS PASS")
