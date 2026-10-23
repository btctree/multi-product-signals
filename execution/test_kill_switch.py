#!/usr/bin/env python3
"""Golden tests for the kill-switch's peak (execution/kill_switch.py).

Run from this directory:  python test_kill_switch.py

The 8% gate blocks new entries while NetLiq is far below its peak. Until
2026-10-23 that peak was the highest NetLiq ever seen and nothing lowered it for
a withdrawal: it was taken while the owner's 23,746 HKD deposit sat in the
account, 32,000 HKD then left on 2026-08-02, and the gate blocked entries on
28 Sep-1 Oct and from 15 Oct while the real fall from the real high (230,661 on
13 Oct) never passed 5.4%. These tests replay that history and pin down that a
recorded deposit or withdrawal is never a high or a fall, while a real 8% fall
still blocks.
"""
import io
import json
import os
import sys
import tempfile

os.environ.setdefault("IB_BACKEND", "web")
import testenv                                     # noqa: E402
_TMP = testenv.isolate("mps-kill-")

import ib_bot                                      # noqa: E402
import kill_switch                                 # noqa: E402
testenv.assert_isolated()

FLOWS = [{"d": "2026-07-31", "amt": 23746, "note": "deposit GBP 2,250"},
         {"d": "2026-08-02", "amt": -32000, "note": "cash withdrawal"}]
SERIES = [{"d": "2026-07-30", "nl": 212275}, {"d": "2026-07-31", "nl": 235861},
          {"d": "2026-08-01", "nl": 235881}, {"d": "2026-08-02", "nl": 203882},
          {"d": "2026-09-22", "nl": 217026}, {"d": "2026-10-13", "nl": 230661},
          {"d": "2026-10-16", "nl": 217656}]
STALE = 240603                       # the _peak_netliq the live bot carried


def history(series=SERIES, flows=FLOWS, name="h.json"):
    p = os.path.join(str(_TMP), name)
    io.open(p, "w", encoding="utf-8").write(json.dumps({"series": series, "flows": flows}))
    return p


def t1_the_withdrawal_no_longer_blocks_entries():
    st = {"_peak_netliq": STALE}
    killed, peak, basis = kill_switch.check(st, 217656, path=history(), today="2026-10-16")
    assert not killed, "a recorded withdrawal still reads as an 8% loss"
    assert abs(peak - 230661) < 1e-6, peak            # the real high, in today's money
    assert basis == "net of deposits/withdrawals"
    assert abs(st["_peak_wealth"] - 238915) < 1e-6, st  # 230,661 + the 8,254 net out
    print("t1 the 2026-10-16 case: not blocked, peak is the real 230,661 OK")


def t2_a_real_eight_percent_fall_still_blocks():
    st = {}
    p = history()
    assert not kill_switch.check(st, 230661 * 0.921, path=p, today="2026-10-16")[0]
    assert kill_switch.check(st, 230661 * 0.919, path=p, today="2026-10-16")[0], \
        "a genuine 8.1% fall from the real high did not block entries"
    print("t2 a genuine fall past 8% still blocks entries OK")


def t3_a_recorded_deposit_is_not_a_new_high_and_moves_the_line_with_it():
    flows = FLOWS + [{"d": "2026-10-20", "amt": 20000, "note": "deposit"}]
    p = history(flows=flows, name="dep.json")
    st = {}
    killed, peak, _ = kill_switch.check(st, 230661 + 20000, path=p, today="2026-10-20")
    assert not killed and abs(peak - 250661) < 1e-6, peak
    assert abs(st["_peak_wealth"] - 238915) < 1e-6, "the deposit was booked as a new high"
    print("t3 a recorded deposit raises NetLiq and the line together OK")


def t4_a_recorded_withdrawal_is_not_a_fall():
    flows = FLOWS + [{"d": "2026-10-20", "amt": -30000, "note": "withdrawal"}]
    p = history(flows=flows, name="wd.json")
    killed, peak, _ = kill_switch.check({}, 230661 - 30000, path=p, today="2026-10-20")
    assert not killed, "a recorded 30,000 withdrawal blocked entries"
    assert abs(peak - 200661) < 1e-6, peak
    print("t4 a recorded withdrawal is not a fall OK")


def t5_the_peak_only_ratchets_up():
    st = {}
    p = history()
    kill_switch.check(st, 240000, path=p, today="2026-10-16")    # a real new high
    hi = st["_peak_wealth"]
    kill_switch.check(st, 225000, path=p, today="2026-10-17")
    assert st["_peak_wealth"] == hi, "a lower day lowered the peak"
    assert abs(hi - (240000 + 8254)) < 1e-6
    print("t5 the wealth peak ratchets up and never down OK")


def t6_unreadable_history_keeps_the_old_rule_and_says_so():
    bad = os.path.join(str(_TMP), "torn.json")
    io.open(bad, "w", encoding="utf-8").write('{"series": [')
    st = {"_peak_netliq": STALE}
    killed, peak, basis = kill_switch.check(st, 217656, path=bad)
    assert killed and peak == STALE and basis.startswith("raw"), (killed, peak, basis)
    assert "_peak_wealth" not in st
    print("t6 an unreadable history falls back to the raw peak, labelled OK")


def t7_the_digest_reads_the_same_peak_and_writes_nothing():
    st = {"_peak_netliq": STALE}
    before = dict(st)
    p = history()
    rep = kill_switch.peak_for_report(st, 217656, path=p, today="2026-10-16")
    assert st == before, "the digest's read changed the state"
    assert abs(rep - kill_switch.check(dict(before), 217656, path=p,
                                       today="2026-10-16")[1]) < 1e-6
    print("t7 the digest reports the bot's peak and writes nothing OK")


def t8_the_bot_gates_through_this_rule():
    st = {"_peak_netliq": STALE}
    killed, peak, basis = ib_bot.kill_switch_check(st, 217656, path=history(),
                                                   today="2026-10-16")
    assert not killed and abs(peak - 230661) < 1e-6
    src = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ib_bot.py"),
                  encoding="utf-8").read()
    assert "killed, peak, kill_basis = kill_switch_check(state, nl)" in src, \
        "run() no longer gates on kill_switch_check"
    assert "peak = max(state.get(\"_peak_netliq\", nl), nl)" not in src, \
        "the cash-flow-naive peak is back in ib_bot"
    ds = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "daily_signal.py"),
                 encoding="utf-8").read()
    assert "kill_switch.peak_for_report(state, est_netliq)" in ds, \
        "the digest no longer reports the bot's peak"
    print("t8 the bot and the digest both go through kill_switch OK")


def t9_the_live_history_if_present():
    """The real published file: today's account must not read as killed when its
    real fall from its real high is under 8%."""
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", "netliq_history.json")
    if not os.path.exists(p):
        print("t9 no local history file - skipped")
        return
    h = json.loads(io.open(p, encoding="utf-8-sig").read())
    last = h["series"][-1]
    net_in, best = kill_switch.flows_and_history_peak(p, last["d"])
    peak = best + net_in
    real_fall = last["nl"] / peak - 1
    killed = kill_switch.check({}, last["nl"], path=p, today=last["d"])[0]
    assert killed == (real_fall < -0.08), (real_fall, killed)
    print("t9 published history: %s NetLiq %d vs real peak %d (%+.1f%%), blocked=%s OK"
          % (last["d"], last["nl"], peak, real_fall * 100, killed))


if __name__ == "__main__":
    t1_the_withdrawal_no_longer_blocks_entries()
    t2_a_real_eight_percent_fall_still_blocks()
    t3_a_recorded_deposit_is_not_a_new_high_and_moves_the_line_with_it()
    t4_a_recorded_withdrawal_is_not_a_fall()
    t5_the_peak_only_ratchets_up()
    t6_unreadable_history_keeps_the_old_rule_and_says_so()
    t7_the_digest_reads_the_same_peak_and_writes_nothing()
    t8_the_bot_gates_through_this_rule()
    t9_the_live_history_if_present()
    print("ALL KILL-SWITCH TESTS PASS")
