#!/usr/bin/env python3
"""Golden tests: the live cut-loss follows dividends and splits.

Run from this directory:  python test_div_adjust.py

THE PROBLEM. The engine prices everything dividend- and split-ADJUSTED, so on an
ex-dividend day its series shows no drop - it scales every earlier bar down
instead - and the validated backtest's trailing stop never sees one. The live
bot keeps its high-water mark and stop in a state file, ratcheted up run after
run, and until 2026-09-21 nothing ever scaled them down. On an ex-dividend day
the price fell by the dividend against a stop still set on pre-dividend prices,
so a position near its stop could be sold on a drop that was not a loss.

Backtested before changing anything: 61 of 2,089 exits changed; the effect on
returns is noise with an unstable sign, but single trades moved up to 15.8
points either way, and rescaling reproduces the validated run exactly.

What must stay true:
  * A dividend is DETECTED from the card's own adjusted history - every
    remembered date moves by the same factor - and hw and stop follow it.
  * A position near its stop is NOT sold on an ex-dividend drop, and IS still
    sold when the price genuinely falls through the stop.
  * A split is followed the same way (a 2-for-1 halves the stop too).
  * Evidence that DISAGREES - one bad bar - moves nothing. So does too little
    evidence, noise below the rounding floor, or an implausible factor: a live
    stop is never moved on a damaged card.
  * The signal price shown to the owner is NOT rewritten; the tighten test
    scales it instead, and the dashboard's hint scales it the same way.
"""
import io
import os
import sys

os.environ.setdefault("IB_BACKEND", "web")
import testenv                                      # noqa: E402
testenv.isolate("mps-divadj-")

import ib_bot                                       # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _series(closes, start=1):
    return [["2026-09-%02d" % (start + i), c] for i, c in enumerate(closes)]


def t1_a_dividend_is_detected_from_the_history_itself():
    before = _series([100.0, 101.0, 102.0])
    ref = ib_bot.px_ref(before)
    # the next run: Yahoo has scaled every earlier bar by 0.995 (a 0.5% dividend)
    after = _series([99.5, 100.495, 101.49, 101.0])
    f = ib_bot.adjustment_since(ref, after)
    assert f is not None and abs(f - 0.995) < 1e-9, f
    print("t1 a 0.5%% dividend is read from the history's own rescale: x%.4f OK" % f)


def t2_no_event_moves_nothing():
    s = _series([100.0, 101.0, 102.0])
    assert ib_bot.adjustment_since(ib_bot.px_ref(s), s + [["2026-09-04", 103.0]]) is None
    assert ib_bot.adjustment_since([], s) is None, "no memory yet: nothing to compare"
    print("t2 an unchanged history, or no memory yet, moves nothing OK")


def t3_a_single_bad_bar_is_not_an_event():
    ref = ib_bot.px_ref(_series([100.0, 101.0, 102.0]))
    # one remembered date moved and the others did not: a glitch, not a dividend
    glitch = _series([100.0, 95.0, 102.0])
    assert ib_bot.adjustment_since(ref, glitch) is None, \
        "a single bad bar must never move a live stop"
    print("t3 evidence that disagrees moves nothing OK")


def t4_too_little_evidence_noise_and_implausible_factors():
    ref = ib_bot.px_ref(_series([100.0, 101.0, 102.0]))
    only_one = [["2026-09-03", 101.49]]          # two of three dates rolled away
    assert ib_bot.adjustment_since(ref, only_one) is None, "one date is not evidence"
    tiny = _series([100.00001, 101.00001, 102.00001])
    assert ib_bot.adjustment_since(ref, tiny) is None, "rounding is not a dividend"
    crazy = _series([1.0, 1.01, 1.02])           # x0.01 - a damaged card
    assert ib_bot.adjustment_since(ref, crazy) is None, "implausible factor must not move a stop"
    print("t4 too little evidence, rounding noise and implausible factors move nothing OK")


def t5_a_split_is_followed_too():
    ref = ib_bot.px_ref(_series([200.0, 202.0, 204.0]))
    after = _series([100.0, 101.0, 102.0])       # 2-for-1
    f = ib_bot.adjustment_since(ref, after)
    assert f is not None and abs(f - 0.5) < 1e-9, f
    print("t5 a 2-for-1 split halves the factor: x%.2f OK" % f)


def _ratchet(st, price, atr, prices):
    """The exit loop's own arithmetic, in the order ib_bot runs it."""
    f = ib_bot.adjustment_since(st.get("px_ref"), prices)
    if f is not None:
        for key in ("hw", "stop"):
            if st.get(key):
                st[key] = st[key] * f
        st["adj"] = st.get("adj", 1.0) * f
    ref = ib_bot.px_ref(prices)
    if ref:
        st["px_ref"] = ref
    hw = max(st.get("hw", price), price)
    entry_eff = st["entry"] * st.get("adj", 1.0)
    k = 2.0 if price >= entry_eff + 1.5 * atr else 3.5
    trail = max(st.get("stop", 0), hw - k * atr)
    st.update(hw=hw, stop=trail)
    return price <= trail


def t6_the_ex_dividend_drop_no_longer_sells_a_position():
    """The case the whole change exists for, end to end.

    A stock at 100 with its stop at 99.6 goes ex-dividend for 0.5 and closes at
    99.5 - exactly the dividend, no real move at all. Before: 99.5 <= 99.6, the
    bot sells. After: the stop follows the dividend to ~99.10 and it holds.
    """
    pre = _series([98.0, 99.0, 100.0])
    base = {"entry": 90.0, "hw": 100.0, "stop": 99.6, "adj": 1.0,
            "px_ref": ib_bot.px_ref(pre)}
    ex_day = _series([97.51, 98.505, 99.5, 99.5])      # history x0.995, today 99.5

    old = dict(base)
    old.pop("px_ref")                                   # the old code had no memory
    old_sold = 99.5 <= max(old["stop"], max(old["hw"], 99.5) - 3.5 * 0.1)
    assert old_sold, "sanity: the old behaviour really did sell here"

    st = dict(base)
    sold = _ratchet(st, price=99.5, atr=0.1, prices=ex_day)
    assert not sold, "a position was sold on an ex-dividend drop that was not a loss"
    assert abs(st["stop"] - 99.6 * 0.995) < 1e-9 or st["stop"] < 99.5, st
    print("t6 an ex-dividend drop no longer sells: stop 99.60 -> %.2f, price 99.50 held OK"
          % st["stop"])


def t7_a_genuine_fall_still_sells():
    pre = _series([98.0, 99.0, 100.0])
    st = {"entry": 90.0, "hw": 100.0, "stop": 99.6, "adj": 1.0,
          "px_ref": ib_bot.px_ref(pre)}
    same = _series([98.0, 99.0, 100.0, 98.0])          # no event, a real 2% fall
    assert _ratchet(st, price=98.0, atr=0.1, prices=same), \
        "the cut-loss must still fire on a genuine fall"
    print("t7 a genuine fall through the stop still sells OK")


def t8_the_signal_price_shown_to_the_owner_is_not_rewritten():
    pre = _series([98.0, 99.0, 100.0])
    st = {"entry": 90.0, "hw": 100.0, "stop": 80.0, "adj": 1.0,
          "px_ref": ib_bot.px_ref(pre)}
    _ratchet(st, price=99.5, atr=0.1, prices=_series([97.51, 98.505, 99.5, 99.5]))
    assert st["entry"] == 90.0, "the signal price the owner sees must stay as proposed"
    assert abs(st["adj"] - 0.995) < 1e-9, "the adjustment is carried instead"
    print("t8 the displayed signal price stays 90.00, the anchor scales by adj OK")


def t9_the_code_and_the_page_agree():
    bot = io.open(os.path.join(HERE, "ib_bot.py"), encoding="utf-8").read()
    assert "f = adjustment_since(st.get(\"px_ref\"), prices_now)" in bot, \
        "the exit loop no longer follows dividends"
    assert bot.index("adjustment_since(st.get") < bot.index("hw = max(st.get(\"hw\""), \
        "the rescale must run BEFORE the ratchet, or the ex-date drop is already a hit"
    assert "entry_eff = (st[\"entry\"] * st.get(\"adj\", 1.0))" in bot
    assert '"adj": st.get("adj", 1.0)' in bot, "ib_bot stopped publishing adj"
    web = io.open(os.path.join(HERE, "publish_web.py"), encoding="utf-8").read()
    assert '"adj": st.get("adj", 1.0)' in web, "publish_web stopped publishing adj"
    page = io.open(os.path.join(ROOT, "docs", "index.html"),
                   encoding="utf-8").read().replace(" ", "")
    assert "x.sig_entry*(x.adj||1)" in page, \
        "the dashboard's stop hint no longer anchors where the bot does"
    print("t9 bot, publishers and dashboard all anchor on entry x adj OK")


if __name__ == "__main__":
    t1_a_dividend_is_detected_from_the_history_itself()
    t2_no_event_moves_nothing()
    t3_a_single_bad_bar_is_not_an_event()
    t4_too_little_evidence_noise_and_implausible_factors()
    t5_a_split_is_followed_too()
    t6_the_ex_dividend_drop_no_longer_sells_a_position()
    t7_a_genuine_fall_still_sells()
    t8_the_signal_price_shown_to_the_owner_is_not_rewritten()
    t9_the_code_and_the_page_agree()
    print("ALL DIVIDEND-ADJUST TESTS PASS")
