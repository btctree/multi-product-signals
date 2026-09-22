#!/usr/bin/env python3
"""Golden tests: the trading run and the phone-command poller take turns.

Run from this directory:  python test_runlock.py

Board review 2026-09-21 ("Phone SELL and the bot's own exit can both be sent:
nothing serializes ib_commands with the 09:00/23:35 trading runs"). The owner
taps Sell at 08:55 on a name the dashboard flags; the 09:00 trading run and the
*/10 poll start in the same minute. ib_bot reads the order book once, before
its exit loop; the poller reads its own once, before it sends. If the poll's
SELL goes out after the bot's read and the poll's read comes before the bot's
exit, neither sees the other: SELL 100 plus SELL 100 against 100 held, both
fill at the open, and the account is short 100 shares no exit ever closes.

What is locked down:
  * runlock.hold() is exclusive - within one process and across processes -
    and released when the holder exits, even when it is killed;
  * a LIVE ib_bot run (main -> run_locked) holds the lock for all of run(),
    and one that cannot take it logs, alerts once, exits RUN_LOCK_EXIT and
    reads and sends nothing; --dry takes no lock;
  * the poller takes the lock only when it has commands to run, before it
    touches IB, never waits for it, and when a run holds it leaves every
    command pending - nothing marked done, nothing alerted;
  * nothing inside either locked section takes the lock again (a second
    hold() in the same process would fail against the first).
Nothing here touches /root: every path is repointed before any module is
imported (MPS_LOCK_FILE among them).
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("IB_BACKEND", "web")
import testenv                                     # noqa: E402
_TMP = testenv.isolate("mps-runlock-")
os.environ.pop("EXCLUDED_CASH", None)

import alerts                                      # noqa: E402
import ib_bot                                      # noqa: E402
import ib_commands                                 # noqa: E402
import runlock                                     # noqa: E402
testenv.assert_isolated()

HERE = os.path.dirname(os.path.abspath(__file__))
LOCK = runlock.LOCK_FILE
assert str(LOCK).startswith(str(_TMP)), LOCK
RUN_AT = datetime(2026, 9, 16, 8, 59, 50, tzinfo=timezone.utc)


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
    d = Path(tempfile.mkdtemp(prefix="t-", dir=str(_TMP)))
    alerts.DIR = d / "outbox"
    ib_commands.DONE = d / "commands_done.json"
    ib_commands.POLL_OK = d / "commands_poll_ok"
    return d


def queued():
    return [x["text"] for _, x in alerts._queued()]


def probe():
    """Could anyone else take the lock right now?"""
    with runlock.hold("probe", wait_s=0) as got:
        return got


def t1_the_lock_is_exclusive_and_released():
    assert probe() is True, "nobody holds it yet"
    with runlock.hold("first", wait_s=0) as got:
        assert got is True
        assert probe() is False, "a second holder got in"
    assert probe() is True, "released on exit"
    # the last holder is named in the file, for the operator and the alert
    with runlock.hold("first", wait_s=0):
        pass
    who = open(LOCK, encoding="utf-8").read()
    assert who.startswith("first %d " % os.getpid()), who
    # released when the block raises, too
    try:
        with runlock.hold("first", wait_s=0):
            raise ValueError("boom")
    except ValueError:
        pass
    assert probe() is True
    print("t1 the lock is exclusive in one process and released on exit OK")


def t2_another_process_holds_it_until_it_dies():
    code = ("import sys, runlock\n"
            "with runlock.hold('child', wait_s=0) as got:\n"
            "    print('held' if got else 'busy', flush=True)\n"
            "    sys.stdin.read()\n")
    env = dict(os.environ, PYTHONPATH=HERE)
    child = subprocess.Popen([sys.executable, "-c", code], cwd=HERE, env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "held"
        assert probe() is False, "another process holds it, yet we got it"
        child.kill()                                # no clean-up: the OS must release it
        child.wait(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
        child.stdout.close()
        child.stdin.close()
    assert probe() is True, "a killed holder left the lock taken"
    print("t2 a lock held by another process blocks us, and dies with it OK")


def t3_a_live_run_holds_the_lock_for_all_of_run():
    seen = []

    def run(dry=False):
        seen.append((dry, probe()))                 # inside the run: taken?
    with Patch(ib_bot, run=run, log=lambda *a: None):
        ib_bot.main([])
        assert seen == [(False, False)], seen
        # --dry takes no lock at all
        ib_bot.main(["--dry"])
        assert seen[-1] == (True, True), seen
    assert probe() is True, "released after the run"
    print("t3 a live run holds the lock for all of run(); --dry takes none OK")


def t4_a_run_that_cannot_take_the_lock_does_nothing_and_says_so():
    fresh()
    calls, lines = [], []
    with Patch(ib_bot, run=lambda dry=False: calls.append(dry),
               log=lambda *a: lines.append(" ".join(map(str, a))),
               RUN_LOCK_WAIT_S=0, _now_utc=lambda: RUN_AT):
        with runlock.hold("ib_commands", wait_s=0) as got:
            assert got
            for _ in range(2):                      # the same hour: one alert
                try:
                    ib_bot.main([])
                    raise AssertionError("a run without the lock must exit non-zero")
                except SystemExit as e:
                    assert e.code == ib_bot.RUN_LOCK_EXIT and e.code != 0, e.code
            # --dry is not held up by it
            ib_bot.main(["--dry"])
    assert calls == [True], "a live run read or sent something without the lock: %s" % calls
    assert any(l.startswith("!! could not take the run lock") and "NOTHING" in l
               for l in lines), lines
    texts = queued()
    assert len(texts) == 1, texts
    assert "Trading run SKIPPED at 08:59 UTC" in texts[0] and "Nothing was read or sent" in texts[0]
    assert not [t for t in texts if "DIED" in t], "a lock refusal is not a crash"
    print("t4 a run that cannot take the lock exits %d, alerts once, sends nothing OK"
          % ib_bot.RUN_LOCK_EXIT)


class CmdIB:
    """Enough of IB for one phone SELL of 4 DELL."""

    def __init__(self, on_connect=None):
        self.on_connect, self.placed = on_connect, []

    def connect(self, *a, **k):
        if self.on_connect:
            self.on_connect()

    def reqAllOpenOrders(self):
        return []

    def openTrades(self):
        return []

    def positions(self, fresh=False):
        class C:
            symbol, conId, secType, currency, exchange = "DELL", 1001, "STK", "USD", "SMART"

        class P:
            contract, position, avgCost = C(), 4, 100.0
        return [P()]

    def qualifyContracts(self, c):
        return [c]

    def placeOrder(self, contract, order):
        self.placed.append((order.action, order.totalQuantity, contract.symbol))

        class S:
            status = "PreSubmitted"

        class T:
            orderStatus, log = S(), []
        return T()

    def sleep(self, *a):
        pass

    def disconnect(self):
        pass


def _sell(num):
    return {"id": num, "kind": "sell", "symbol": "DELL", "qty": None, "amount": None}


def poll(ib, cmds, lines):
    made = []

    def make():
        made.append(1)
        return ib
    try:
        with Patch(ib_commands, fetch_commands=lambda: list(cmds), IB=make,
                   log=lambda *a: lines.append(" ".join(map(str, a)))), \
                Patch(ib_bot, load_state=lambda: {"map": {}}, net_liq=lambda ib_: 1.0,
                      publish_state=lambda ib_, s, nl: None):
            ib_commands.main()
    finally:
        del ib_bot.PLACED[:]
    return bool(made)


def t5_the_poller_leaves_its_commands_pending_while_a_run_trades():
    fresh()
    ib, lines = CmdIB(), []
    with runlock.hold("ib_bot", wait_s=0) as got:
        assert got
        made = poll(ib, [_sell(501)], lines)
    assert not made and ib.placed == [], "the poller touched IB during a trading run"
    assert "trading run in progress - commands left pending for the next poll" in lines, lines
    assert not ib_commands.DONE.exists(), "nothing may be marked done"
    assert queued() == [], "a skipped turn is routine, not an alert: %s" % queued()
    # the next poll, the run finished: the SELL goes out and is marked done
    lines = []
    assert poll(ib, [_sell(501)], lines)
    assert ib.placed == [("SELL", 4, "DELL")], ib.placed
    assert json.loads(ib_commands.DONE.read_text()) == [501]
    # a poll with nothing to run takes no lock, so it never contends with a run
    lines = []
    with runlock.hold("ib_bot", wait_s=0):
        poll(ib, [_sell(501)], lines)                # 501 is done: nothing to run
    assert not [l for l in lines if "trading run in progress" in l], lines
    print("t5 while a run holds the lock the poller sends nothing and marks nothing "
          "done; the next poll runs it OK")


def t6_the_poller_holds_the_lock_while_it_trades():
    fresh()
    during = []
    ib = CmdIB(on_connect=lambda: during.append(probe()))
    poll(ib, [_sell(601)], [])
    assert during == [False], "the poller reached IB without the lock: %s" % during
    assert ib.placed == [("SELL", 4, "DELL")] and probe() is True
    print("t6 the poller holds the lock from before IB until it is done OK")


def t7_nothing_inside_a_locked_section_takes_the_lock_again():
    bot = open(os.path.join(HERE, "ib_bot.py"), encoding="utf-8").read()
    cmd = open(os.path.join(HERE, "ib_commands.py"), encoding="utf-8").read()
    assert bot.count("runlock.hold(") == 1, "ib_bot takes the lock in more than one place"
    locked = bot[bot.index("def run_locked():"):bot.index("def main(argv=None):")]
    assert "runlock.hold(" in locked and "return run(dry=False)" in locked
    for fn in ("def run(dry=False):", "def publish_state(", "def publish_only("):
        body = bot[bot.index(fn):]
        body = body[:body.index("\ndef ", 1)]
        assert "runlock." not in body and ".hold(" not in body, fn
    assert cmd.count("runlock.hold(") == 1, "ib_commands takes the lock in more than one place"
    body = cmd[cmd.index("def _execute("):cmd.index('if __name__ == "__main__":')]
    for word in ("runlock", "run_locked", "ib_bot.run(", "ib_bot.main("):
        assert word not in body, "%s inside the poller's locked section" % word
    # ...and the poller takes it AFTER its list is known, BEFORE IB
    main = cmd[cmd.index("def main():"):cmd.index("def _execute(")]
    assert main.index("if not todo:") < main.index("runlock.hold(") < main.index("_execute(todo, done)")
    assert "IB()" not in main
    # the lock path is overridable, so no test ever touches the live one
    assert "MPS_LOCK_FILE" in testenv.PATH_VARS
    print("t7 each process takes the lock once; nothing inside takes it again OK")


if __name__ == "__main__":
    t1_the_lock_is_exclusive_and_released()
    t2_another_process_holds_it_until_it_dies()
    t3_a_live_run_holds_the_lock_for_all_of_run()
    t4_a_run_that_cannot_take_the_lock_does_nothing_and_says_so()
    t5_the_poller_leaves_its_commands_pending_while_a_run_trades()
    t6_the_poller_holds_the_lock_while_it_trades()
    t7_nothing_inside_a_locked_section_takes_the_lock_again()
    print("ALL RUN-LOCK TESTS PASS")
