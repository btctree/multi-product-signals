#!/usr/bin/env python3
"""Golden tests for the kill-switch's peak (execution/kill_switch.py).

Run from this directory:  python test_kill_switch.py

The 8% gate blocks new entries while NetLiq is far below its peak. Until
2026-10-23 that peak was the highest NetLiq ever seen and nothing lowered it for
a withdrawal: it was taken while the owner's 23,746 HKD deposit sat in the
account, 32,000 HKD then left on 2026-08-02, and the gate blocked entries on
28 Sep-1 Oct and from 15 Oct while the real fall from the real high (230,661 on
13 Oct) never passed 5.4%. These tests replay that history and pin down that a
recorded deposit or withdrawal is never a high or a fall, that a deposit
RECORDED LATE heals itself (board review 2026-10-23: a remembered peak froze it
in for good), and that a real 8% fall still blocks.
"""
import io
import json
import os

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
_n = [0]


def history(series=SERIES, flows=FLOWS, raw=None):
    _n[0] += 1
    p = os.path.join(str(_TMP), "h%d.json" % _n[0])
    io.open(p, "w", encoding="utf-8").write(
        raw if raw is not None else json.dumps({"series": series, "flows": flows}))
    return p


def t1_the_withdrawal_no_longer_blocks_entries():
    st = {"_peak_netliq": STALE}
    killed, peak, basis, warn = kill_switch.check(st, 217656, path=history(),
                                                  today="2026-10-16")
    assert not killed, "a recorded withdrawal still reads as an 8% loss"
    assert abs(peak - 230661) < 1e-6, peak            # the real high, in today's money
    assert basis == "net of deposits/withdrawals" and not warn, (basis, warn)
    assert st["_peak_netliq"] == peak and "_peak_wealth" not in st, st
    print("t1 the 2026-10-16 case: not blocked, peak is the real 230,661 OK")


def t2_a_real_eight_percent_fall_still_blocks():
    p = history()
    assert not kill_switch.check({}, 230661 * 0.921, path=p, today="2026-10-16")[0]
    assert kill_switch.check({}, 230661 * 0.919, path=p, today="2026-10-16")[0], \
        "a genuine 8.1% fall from the real high did not block entries"
    print("t2 a genuine fall past 8% still blocks entries OK")


def t3_a_recorded_deposit_is_not_a_new_high():
    flows = FLOWS + [{"d": "2026-10-20", "amt": 20000, "note": "deposit"}]
    series = SERIES + [{"d": "2026-10-20", "nl": 250661}]
    killed, peak, _, _ = kill_switch.check({}, 250661, path=history(series, flows),
                                           today="2026-10-20")
    assert not killed and abs(peak - 250661) < 1e-6, peak
    print("t3 a recorded deposit raises NetLiq and the line together OK")


def t4_a_recorded_withdrawal_is_not_a_fall():
    flows = FLOWS + [{"d": "2026-10-20", "amt": -30000, "note": "withdrawal"}]
    series = SERIES + [{"d": "2026-10-20", "nl": 200661}]
    killed, peak, _, _ = kill_switch.check({}, 200661, path=history(series, flows),
                                           today="2026-10-20")
    assert not killed, "a recorded 30,000 withdrawal blocked entries"
    assert abs(peak - 200661) < 1e-6, peak
    print("t4 a recorded withdrawal is not a fall OK")


def t5_a_deposit_recorded_late_heals_itself():
    """Board review 2026-10-23, scenario A: a 23,746 deposit lands on 10-26 and
    the history's 10-26 row includes it, but flows only gets the entry on 10-27.
    A REMEMBERED peak would keep that day's inflated wealth for good; recomputed
    from the history, the line is right again as soon as the entry exists."""
    series = SERIES + [{"d": "2026-10-26", "nl": 221637 + 23746}]
    st = {}
    # 10-27, before the entry: the deposit reads as a gain (it IS unexplained)
    kill_switch.check(st, 221637 + 23746, path=history(series, FLOWS), today="2026-10-27")
    # 10-27, entry made and dated the day the money moved
    flows = FLOWS + [{"d": "2026-10-26", "amt": 23746, "note": "deposit"}]
    killed, peak, _, _ = kill_switch.check(st, 221637 + 23746, path=history(series, flows),
                                           today="2026-10-27")
    assert not killed, "the late-recorded deposit was frozen in as a new high"
    assert abs(peak - (230661 + 23746)) < 1e-6, peak
    print("t5 a deposit recorded a day late corrects itself on the next run OK")


def t6_an_explicit_date_re_risks_after_a_genuine_fall():
    series = SERIES + [{"d": "2026-11-02", "nl": 205000}]       # a real -11% fall
    p = history(series, FLOWS)
    assert kill_switch.check({}, 205000, path=p, today="2026-11-02")[0]
    st = {"_kill_peak_since": "2026-11-02"}
    killed, peak, _, warn = kill_switch.check(st, 205000, path=p, today="2026-11-02")
    assert not killed and abs(peak - 205000) < 1e-6 and not warn, (killed, peak, warn)
    bad = {"_kill_peak_since": "2 Nov"}
    assert kill_switch.check(bad, 205000, path=p, today="2026-11-02")[0]
    assert any("_kill_peak_since" in w for w in kill_switch.check(
        bad, 205000, path=p, today="2026-11-02")[3])
    # a typo'd year must not quietly turn the gate off
    typo = {"_kill_peak_since": "2027-11-02"}
    killed, _, _, warn = kill_switch.check(typo, 205000, path=p, today="2026-11-02")
    assert killed and any("future" in w for w in warn), (killed, warn)
    print("t6 _kill_peak_since re-risks from a chosen day; a bad or future value is refused OK")


def t7_bad_rows_are_skipped_never_poison_the_peak():
    flows = FLOWS + [{"d": "2026-09-01", "amt": float("nan")},
                     {"d": "23/10/2026", "amt": 5000}, {"amt": 1}]
    series = SERIES + [{"d": "2026-09-30", "nl": float("inf")}]
    p = history(raw=json.dumps({"series": series, "flows": flows}))   # NaN/Infinity tokens
    killed, peak, basis, warn = kill_switch.check({}, 230661 * 0.5, path=p,
                                                  today="2026-10-16")
    assert killed, "a NaN row left the gate open at half the peak"
    assert abs(peak - 230661) < 1e-6 and basis.startswith("net"), (peak, basis)
    assert len(warn) == 4, warn                  # three flows rows and the inf day
    print("t7 NaN, Infinity and malformed dates are skipped with a warning OK")


def t8_an_unusable_history_falls_back_and_never_raises():
    for raw in ('{"series": [', '{"series": 5, "flows": []}', '[1, 2]', ''):
        st = {"_peak_netliq": STALE}
        killed, peak, basis, warn = kill_switch.check(st, 217656, path=history(raw=raw))
        assert killed and peak == STALE and basis.startswith("raw") and warn, (raw, basis)
    for junk in ("garbage", float("nan"), 10 ** 400, True, None):
        st = {"_peak_netliq": junk}
        killed, peak, basis, _ = kill_switch.check(st, 217656, path=history(raw='{'))
        assert not killed and peak == 217656 and basis.startswith("raw"), (junk, peak)
    # the bot's own wrapper survives the same junk
    st = {"_peak_netliq": 10 ** 400}
    ib_bot.kill_switch_check(st, 217656, path=history(raw='{'))
    print("t8 an unusable history keeps the old rule, labelled, and never raises OK")


def t9_the_digest_reads_the_same_peak_and_writes_nothing():
    st = {"_peak_netliq": STALE}
    before = dict(st)
    p = history()
    rep, warn = kill_switch.peak_for_report(st, 217656, path=p, today="2026-10-16")
    assert st == before, "the digest's read changed the state"
    assert abs(rep - kill_switch.check(dict(before), 217656, path=p,
                                       today="2026-10-16")[1]) < 1e-6 and not warn
    rep2, warn2 = kill_switch.peak_for_report(st, 217656, path=history(raw='{'))
    assert rep2 == STALE and warn2, "an unusable history was not reported to the digest"
    print("t9 the digest reports the bot's peak, writes nothing, flags a bad history OK")


def t10_the_bot_gates_through_this_rule():
    st = {"_peak_netliq": STALE}
    killed, peak, basis = ib_bot.kill_switch_check(st, 217656, path=history(),
                                                   today="2026-10-16")
    assert not killed and abs(peak - 230661) < 1e-6
    here = os.path.dirname(os.path.abspath(__file__))
    src = io.open(os.path.join(here, "ib_bot.py"), encoding="utf-8").read()
    assert "killed, peak, kill_basis = kill_switch_check(state, nl)" in src
    assert "peak = max(state.get(\"_peak_netliq\", nl), nl)" not in src
    ds = io.open(os.path.join(here, "daily_signal.py"), encoding="utf-8").read()
    assert "kill_switch.peak_for_report(state, est_netliq)" in ds
    print("t10 the bot and the digest both go through kill_switch OK")


def t11_the_live_history_if_present():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", "netliq_history.json")
    if not os.path.exists(p):
        print("t11 no local history file - skipped")
        return
    h = json.loads(io.open(p, encoding="utf-8-sig").read())
    last = h["series"][-1]
    killed, peak, basis, warn = kill_switch.check({}, last["nl"], path=p, today=last["d"])
    fall = last["nl"] / peak - 1
    assert killed == (fall < -0.08) and basis.startswith("net"), (fall, killed, basis, warn)
    print("t11 published history: %s NetLiq %d vs real peak %d (%+.1f%%), blocked=%s OK"
          % (last["d"], last["nl"], peak, fall * 100, killed))


if __name__ == "__main__":
    t1_the_withdrawal_no_longer_blocks_entries()
    t2_a_real_eight_percent_fall_still_blocks()
    t3_a_recorded_deposit_is_not_a_new_high()
    t4_a_recorded_withdrawal_is_not_a_fall()
    t5_a_deposit_recorded_late_heals_itself()
    t6_an_explicit_date_re_risks_after_a_genuine_fall()
    t7_bad_rows_are_skipped_never_poison_the_peak()
    t8_an_unusable_history_falls_back_and_never_raises()
    t9_the_digest_reads_the_same_peak_and_writes_nothing()
    t10_the_bot_gates_through_this_rule()
    t11_the_live_history_if_present()
    print("ALL KILL-SWITCH TESTS PASS")
