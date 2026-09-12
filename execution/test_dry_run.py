#!/usr/bin/env python3
"""Golden tests for --dry: a preview must change NOTHING.

Run from this directory:  python test_dry_run.py

What this locks down. `--dry` exists so a run can be previewed on the LIVE VM
before a market is enabled or sizing is changed, so it has to be read-only in
every sense - it used to be read-only only about ORDERS:

  * place() returned before transmitting, but the entry loop still recorded
    {"entry", "hw", "stop", "entry_date"} for the order it had not sent. The
    next live run then ratcheted a trailing stop, and counted bars towards the
    time stop, for a position the account did not hold;
  * save_state() and publish_state() ran unconditionally at the end of run(),
    so a preview rewrote execution/state.json AND pushed a bot_state.json
    commit that the dashboard then displayed as fact.

Each dry assertion is paired with a LIVE control on the same scenario: without
one, a harness that silently stopped reaching the write path would make every
dry test pass for the wrong reason.
"""
import io
import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault("IB_BACKEND", "web")     # import without a live socket
import ib_bot                                  # noqa: E402

_real_load_state = ib_bot.load_state           # kept before any patching

# state.json as it sits on disk before the run: one held position, with a
# trailing stop the exit loop below WILL want to ratchet up (180 -> 215).
SEED = json.dumps({
    "map": {"AAPL": "AAPL"},
    "pos": {"AAPL": {"entry": 200, "hw": 250, "stop": 180,
                     "entry_date": "2026-01-02"}},
    "_peak_netliq": 100000,
}, indent=1)


class Patch:
    """Swap module attributes for the duration of a block."""

    def __init__(self, **kw):
        self.kw, self.old = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(ib_bot, k)
            setattr(ib_bot, k, v)
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            setattr(ib_bot, k, v)
        ib_bot._FX_COMMITTED.clear()
        ib_bot._FX_PENDING_CCY.clear()
        ib_bot._FX_PENDING.clear()
        ib_bot._EARMARK_RUN.clear()
        del ib_bot.PLACED[:]


class C:                                        # a contract
    def __init__(self, symbol, currency="USD", secType="STK"):
        self.symbol, self.currency, self.secType = symbol, currency, secType
        self.conId, self.exchange = abs(hash(symbol)) % 10 ** 6, "SMART"


class Pos:
    def __init__(self, contract, qty):
        self.contract, self.position, self.avgCost = contract, qty, 190.0


class Status:
    def __init__(self, status):
        self.status = status


class Trade:
    def __init__(self, status="Submitted"):
        self.orderStatus, self.log = Status(status), []


class FakeIB:
    """Records every order that reaches the wire. In a dry run it must stay empty."""

    def __init__(self):
        self.placed, self.disconnected = [], False

    def reqAllOpenOrders(self):
        pass

    def openTrades(self):
        return []

    def positions(self):
        return [Pos(C("AAPL"), 10)]

    def accountValues(self):
        return []

    def sleep(self, *a):
        pass

    def qualifyContracts(self, c):
        return [c]

    def placeOrder(self, contract, order):
        self.placed.append((order.action, order.totalQuantity, contract.symbol))
        return Trade()

    def disconnect(self):
        self.disconnected = True


def scenario(state_path):
    """The stubs run() needs: one held AAPL below its SMA200 (a regime-break
    SELL), one MSFT BUY candidate. Both reach place(); nothing else is real."""
    cards = {"AAPL": {"card": {"price": 150, "sma200": 160, "atr": 10}}}
    signals = {"generated": "2026-09-12", "actions": [
        {"symbol": "MSFT", "action": "BUY", "price": 100, "score": 5, "stop": 90}]}

    def get_json(url):
        if url == ib_bot.SIGNALS_URL:
            return signals
        for sym, card in cards.items():
            if url.endswith("/" + sym + ".json"):
                return card
        raise AssertionError("unexpected fetch " + url)

    fake, seen, published = FakeIB(), {}, []

    def spy_load_state():                # keep the dict run() mutates in place
        seen["state"] = _real_load_state()
        return seen["state"]

    stubs = dict(
        STATE=Path(state_path),
        IB=lambda: fake,
        connect_or_heal=lambda *a, **k: None,
        get_json=get_json,
        load_state=spy_load_state,
        publish_state=lambda ib, state, nl: published.append(dict(state)),
        net_liq=lambda ib: 100000.0,
        cash_by_ccy=lambda ib: {"HKD": 50000.0, "USD": 10000.0},
        _excluded_cash=lambda: 0.0,
        held_positions=lambda ib: {"AAPL": (Pos(C("AAPL"), 10), 10)},
        reserve_working_cash=lambda ib: None,
        to_ib=lambda ysym: C(ysym.split(".")[0]),
        currency_of=lambda ysym: "USD",
        entry_blocked_reason=lambda ysym, ccy: None,
        fx_rate=lambda ib, a, b: 7.8,
        ensure_ccy=lambda ib, ccy, need, dry: True,
        lot_size=lambda ib, c: 1,
        min_tick=lambda ib, c: 0.01,
        live_base_price=lambda ib, c, fallback: fallback,
        confirm=lambda msg: True,
    )
    return stubs, fake, seen, published


def seeded_state():
    fd, path = tempfile.mkstemp(suffix=".json", prefix="state_")
    os.close(fd)
    io.open(path, "w", encoding="utf-8").write(SEED)
    return path


def t1_dry_run_leaves_state_json_byte_identical():
    path = seeded_state()
    try:
        before = io.open(path, "rb").read()
        stubs, fake, seen, published = scenario(path)
        with Patch(**stubs):
            ib_bot.run(dry=True)
        assert io.open(path, "rb").read() == before, "dry run rewrote state.json"
        assert published == [], "dry run published to the dashboard"
        assert fake.placed == [], fake.placed
        assert fake.disconnected
    finally:
        os.unlink(path)
    print("t1 dry run leaves state.json byte-identical OK")


def t2_live_run_does_write_state_and_publish():
    # The control for t1: the same scenario, not dry, MUST reach both writes -
    # otherwise t1 proves only that the stubs never got that far.
    path = seeded_state()
    try:
        before = io.open(path, "rb").read()
        stubs, fake, seen, published = scenario(path)
        with Patch(**stubs):
            ib_bot.run(dry=False)
        after = io.open(path, "rb").read()
        assert after != before, "live run did not write state.json"
        st = json.loads(after.decode("utf-8"))
        assert st["map"].get("MSFT") == "MSFT", st["map"]
        assert st["pos"]["MSFT"]["entry"] == 100, st["pos"]["MSFT"]
        assert len(published) == 1, published
        assert ("SELL", 10, "AAPL") in fake.placed, fake.placed
        assert ("BUY", 8, "MSFT") in fake.placed, fake.placed
    finally:
        os.unlink(path)
    print("t2 live run writes state and publishes (control) OK")


def t3_dry_run_records_no_phantom_position():
    # The bug this file exists for. Independent of save_state: even with the
    # file write skipped, recording the entry in the live state dict would put
    # a position the account does not hold in front of publish_state - and,
    # once the write came back, in front of the trailing and time stops.
    path = seeded_state()
    try:
        stubs, fake, seen, published = scenario(path)
        with Patch(**stubs):
            ib_bot.run(dry=True)
        state = seen["state"]
        assert "MSFT" not in state.get("map", {}), state["map"]
        assert "MSFT" not in state.get("pos", {}), list(state["pos"])
        # and no order was sent for it, which is why the entry must not exist
        assert fake.placed == [], fake.placed
    finally:
        os.unlink(path)
    print("t3 dry run records no phantom position OK")


def t4_dry_run_does_not_persist_ratcheted_stops():
    # The exit loop ratchets hw/stop and backfills entry_date on the state it
    # loaded. Those are decisions about the CURRENT run, not facts: a preview
    # must not leave the next live run a stop it never actually tightened.
    path = seeded_state()
    try:
        stubs, fake, seen, published = scenario(path)
        with Patch(**stubs):
            ib_bot.run(dry=True)
        on_disk = json.loads(io.open(path, encoding="utf-8").read())
        assert on_disk["pos"]["AAPL"]["stop"] == 180, on_disk["pos"]["AAPL"]
        assert on_disk["pos"]["AAPL"]["hw"] == 250, on_disk["pos"]["AAPL"]
    finally:
        os.unlink(path)
    # control: the ratchet is real, and a live run does persist it
    path = seeded_state()
    try:
        stubs, fake, seen, published = scenario(path)
        with Patch(**stubs):
            ib_bot.run(dry=False)
        on_disk = json.loads(io.open(path, encoding="utf-8").read())
        assert on_disk["pos"]["AAPL"]["stop"] == 215, on_disk["pos"]["AAPL"]
    finally:
        os.unlink(path)
    print("t4 dry run does not persist ratcheted stops OK")


def t5_publish_only_still_publishes():
    # --publish-only is the hourly dashboard refresh and has no dry mode; the
    # guard added to run() must not have touched it. It writes no state.json
    # either - that was already true and is what the hourly cron relies on.
    path = seeded_state()
    try:
        before = io.open(path, "rb").read()
        stubs, fake, seen, published = scenario(path)
        with Patch(**stubs):
            ib_bot.publish_only()
        assert len(published) == 1, published
        assert published[0]["pos"]["AAPL"]["stop"] == 180, published[0]
        assert io.open(path, "rb").read() == before
        assert fake.placed == [], fake.placed
        assert fake.disconnected
    finally:
        os.unlink(path)
    print("t5 publish-only still publishes OK")


def t6_dry_wins_over_publish_only_on_the_command_line():
    # Board finding: the --publish-only branch never consulted args.dry, so
    # `--publish-only --dry` ran the FULL publish - commit and push included -
    # for an operator who had just been told --dry writes nothing. Exercised
    # through main() the way the shell reaches it, not by calling publish_only.
    path = seeded_state()
    try:
        before = io.open(path, "rb").read()
        stubs, fake, seen, published = scenario(path)
        for flags, expect in ((["--publish-only", "--dry"], 0),
                              (["--publish-only"], 1)):          # control
            del published[:]
            with Patch(**stubs):
                ib_bot.main(flags)
            assert len(published) == expect, (flags, published)
        assert io.open(path, "rb").read() == before
        assert fake.placed == [], fake.placed
    finally:
        os.unlink(path)
    print("t6 --dry wins over --publish-only OK")


if __name__ == "__main__":
    t1_dry_run_leaves_state_json_byte_identical()
    t2_live_run_does_write_state_and_publish()
    t3_dry_run_records_no_phantom_position()
    t4_dry_run_does_not_persist_ratcheted_stops()
    t5_publish_only_still_publishes()
    t6_dry_wins_over_publish_only_on_the_command_line()
    print("ALL DRY RUN TESTS PASS")
