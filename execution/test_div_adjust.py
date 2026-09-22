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
  * (board review 2026-09-21) A dividend Yahoo posts a few bars late is still
    followed, exactly once; an exact 50-for-1 split on 4 dp closes is followed
    while x0.01 is still refused; and a new position is seeded with the
    SIGNAL's own dividend memory, so an ex-date before its first exit check is
    followed too - only when the card's last close is the signal price.
"""
import io
import os
import sys
from datetime import date, timedelta

os.environ.setdefault("IB_BACKEND", "web")
import testenv                                      # noqa: E402
testenv.isolate("mps-divadj-")

import ib_bot                                       # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# ADAPTED 2026-09-21 (board review): px_ref now remembers the 3 closes BEFORE
# the newest PX_REF_SKIP (5) bars, so a late rescale still moves every
# remembered date together. The 3-4 bar series these tests were written with
# gave it nothing to remember - t2-t4 then passed without testing anything and
# t1, t5-t8 failed - so every series is now at least PX_REF_BARS + PX_REF_SKIP
# bars long, and each test first checks that a memory was actually taken.
NEED = ib_bot.PX_REF_BARS + ib_bot.PX_REF_SKIP      # 8 bars


def _series(closes, start=0):
    d0 = date(2026, 8, 1)
    return [[(d0 + timedelta(days=start + i)).isoformat(), c]
            for i, c in enumerate(closes)]


def _scaled(series, f):
    """The same dates, every close rescaled by f and rounded as the build does."""
    return [[d, round(c * f, 4)] for d, c in series]


def _next_bar(series, close):
    d = date.fromisoformat(series[-1][0]) + timedelta(days=1)
    return series + [[d.isoformat(), close]]


def _ref(series):
    ref = ib_bot.px_ref(series)
    assert len(ref) == ib_bot.PX_REF_BARS, "no memory taken - the test would be vacuous: %r" % ref
    return ref


# A gently rising series, 8 bars, the last close 107.
BASE = _series([100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0])
# ...and one whose last close is 100, for the stop cases.
PRE = _series([93.0, 94.0, 95.0, 96.0, 97.0, 98.0, 99.0, 100.0])


def t1_a_dividend_is_detected_from_the_history_itself():
    assert len(BASE) >= NEED
    ref = _ref(BASE)
    assert [d for d, _ in ref] == [d for d, _ in BASE[:3]], "remembers the bars before the newest 5"
    # the next run: Yahoo has scaled every earlier bar by 0.995 (a 0.5% dividend)
    after = _next_bar(_scaled(BASE, 0.995), 106.9)
    f = ib_bot.adjustment_since(ref, after)
    assert f is not None and abs(f - 0.995) < 1e-9, f
    print("t1 a 0.5%% dividend is read from the history's own rescale: x%.4f OK" % f)


def t2_no_event_moves_nothing():
    ref = _ref(BASE)
    assert ib_bot.adjustment_since(ref, _next_bar(BASE, 108.0)) is None
    assert ib_bot.adjustment_since([], BASE) is None, "no memory yet: nothing to compare"
    print("t2 an unchanged history, or no memory yet, moves nothing OK")


def t3_a_single_bad_bar_is_not_an_event():
    ref = _ref(BASE)
    # one remembered date moved and the others did not: a glitch, not a dividend
    glitch = [list(r) for r in BASE]
    glitch[1][1] = 95.0
    assert ib_bot.adjustment_since(ref, glitch) is None, \
        "a single bad bar must never move a live stop"
    # control: the same dates all moving together IS an event
    assert ib_bot.adjustment_since(ref, _scaled(BASE, 0.99)) is not None
    print("t3 evidence that disagrees moves nothing OK")


def t4_too_little_evidence_noise_and_implausible_factors():
    ref = _ref(BASE)
    only_one = [[BASE[2][0], 101.49]]            # two of three dates rolled away
    assert ib_bot.adjustment_since(ref, only_one) is None, "one date is not evidence"
    tiny = [[d, c + 0.00001] for d, c in BASE]
    assert ib_bot.adjustment_since(ref, tiny) is None, "rounding is not a dividend"
    crazy = _scaled(BASE, 0.01)                  # x0.01 - a damaged card
    assert ib_bot.adjustment_since(ref, crazy) is None, "implausible factor must not move a stop"
    print("t4 too little evidence, rounding noise and implausible factors move nothing OK")


def t5_a_split_is_followed_too():
    before = _series([200.0, 202.0, 204.0, 206.0, 208.0, 210.0, 212.0, 214.0])
    ref = _ref(before)
    after = _next_bar(_scaled(before, 0.5), 107.5)       # 2-for-1
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
    base = {"entry": 90.0, "hw": 100.0, "stop": 99.6, "adj": 1.0, "px_ref": _ref(PRE)}
    ex_day = _next_bar(_scaled(PRE, 0.995), 99.5)      # history x0.995, today 99.5

    old = dict(base)
    old.pop("px_ref")                                   # the old code had no memory
    old_sold = 99.5 <= max(old["stop"], max(old["hw"], 99.5) - 3.5 * 0.1)
    assert old_sold, "sanity: the old behaviour really did sell here"

    st = dict(base)
    sold = _ratchet(st, price=99.5, atr=0.1, prices=ex_day)
    assert not sold, "a position was sold on an ex-dividend drop that was not a loss"
    assert abs(st["adj"] - 0.995) < 1e-9, st
    assert st["stop"] < 99.5, st
    print("t6 an ex-dividend drop no longer sells: stop 99.60 -> %.2f, price 99.50 held OK"
          % st["stop"])


def t7_a_genuine_fall_still_sells():
    st = {"entry": 90.0, "hw": 100.0, "stop": 99.6, "adj": 1.0, "px_ref": _ref(PRE)}
    same = _next_bar(PRE, 98.0)                         # no event, a real 2% fall
    assert _ratchet(st, price=98.0, atr=0.1, prices=same), \
        "the cut-loss must still fire on a genuine fall"
    assert st["adj"] == 1.0, st
    print("t7 a genuine fall through the stop still sells OK")


def t8_the_signal_price_shown_to_the_owner_is_not_rewritten():
    st = {"entry": 90.0, "hw": 100.0, "stop": 80.0, "adj": 1.0, "px_ref": _ref(PRE)}
    _ratchet(st, price=99.5, atr=0.1, prices=_next_bar(_scaled(PRE, 0.995), 99.5))
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
    # ADDED 2026-09-21: the memory the published stop was scaled against goes
    # out beside adj, so the page can follow a rescale before the next run.
    assert '"px_ref": st.get("px_ref")' in bot, "ib_bot stopped publishing px_ref"
    # ...and the entry seed fetches its card on a LIVE run only (--dry fetches
    # and writes nothing extra)
    seed_at = bot.index("ref = _entry_px_ref(ysym, price)")
    guard = bot.rindex("if not dry:", 0, seed_at)
    assert seed_at - guard < 800 and \
        'state.setdefault("map", {})[c.symbol] = ysym' in bot[guard:seed_at], \
        "the entry seed and its card fetch must stay inside the live-only branch"
    web = io.open(os.path.join(HERE, "publish_web.py"), encoding="utf-8").read()
    assert '"adj": st.get("adj", 1.0)' in web, "publish_web stopped publishing adj"
    page = io.open(os.path.join(ROOT, "docs", "index.html"),
                   encoding="utf-8").read().replace(" ", "")
    assert "x.sig_entry*(x.adj||1)" in page, \
        "the dashboard's stop hint no longer anchors where the bot does"
    print("t9 bot, publishers and dashboard all anchor on entry x adj OK")


def _replay(runs, memory=None):
    """The exit loop's dividend step over a sequence of runs, each a card's
    price series as that run saw it. Returns (adj, [run index of each event]).
    `memory` replaces px_ref, for the control."""
    memory = memory or ib_bot.px_ref
    st, hits = {"adj": 1.0}, []
    for i, prices in enumerate(runs):
        f = ib_bot.adjustment_since(st.get("px_ref"), prices)
        if f is not None:
            st["adj"] *= f
            hits.append(i)
        ref = memory(prices)
        if ref:
            st["px_ref"] = ref
    return st["adj"], hits


def t10_a_dividend_posted_late_is_followed_once():
    """Board review 2026-09-21: the adjusted closes come from Yahoo's adjclose,
    built from the same dividend feed that is sometimes a day or more late. A
    run that remembered the ex-date bar itself saw [f, f, 1] when the rescale
    landed, rejected it as a glitch, and overwrote the memory - the dividend
    was lost for good and the stop stayed on pre-dividend prices.

    Here: 2.0 goes ex on bar 20, Yahoo applies it on bar 20 + late, two runs a
    bar (the first run's newest close a live-quote fill, the second the
    official close). It must be followed exactly once, on the run the rescale
    lands, and never again on the runs after it."""
    true = [100.0 + 0.1 * i for i in range(40)]
    ex, div = 20, 2.0
    f_true = 1.0 - div / true[ex - 1]
    closes = [c - div if i >= ex else c for i, c in enumerate(true)]   # the ex-date drop

    def card(t, late, live):
        s = _series(closes[:t + 1])
        if t >= ex + late:                              # Yahoo has rescaled
            s = [[d, round(c * f_true, 4)] if i < ex else [d, c]
                 for i, (d, c) in enumerate(s)]
        if live:
            s[-1] = [s[-1][0], round(s[-1][1] * 1.002, 4)]    # the 23:35 live-quote fill
        return s

    for late in (0, 1, 2, 3):
        runs, landed = [], None
        for t in range(NEED, 40):
            for live in (True, False):
                if landed is None and t >= ex + late:
                    landed = len(runs)
                runs.append(card(t, late, live))
        adj, hits = _replay(runs)
        assert hits == [landed], (late, hits, landed)
        assert abs(adj - f_true) < 1e-5, (late, adj, f_true)

    # CONTROL - the old memory, the 3 NEWEST closes, loses the late one for good
    def newest(p):
        return [[str(d), float(v)] for d, v in p][-3:]
    runs = [card(t, 1, live) for t in range(NEED, 40) for live in (True, False)]
    assert _replay(runs, memory=newest)[1] == [], "the control no longer shows the old loss"
    print("t10 a dividend posted 0-3 bars late is followed once, never twice "
          "(x%.6f) OK" % f_true)


def t11_an_exact_50_for_1_split_is_followed():
    """Board review 2026-09-21: the factor is read off closes rounded to 4 dp,
    and at exactly 50-for-1 it lands just under ADJ_MIN = 0.02 about half the
    time. Rejected, the stop stayed at ~2,700 against a card now at ~60, and
    the next close sold every share of a split that lost nothing. The band's
    edges now carry the agreement tolerance."""
    pre = _series([3005.4321, 3001.0101, 2998.0012, 3000.0, 3001.0, 3002.0, 3003.0, 3004.0])
    ref = _ref(pre)
    post = _next_bar([[d, round(c / 50.0, 4)] for d, c in pre], 60.1)
    ratios = sorted(dict(post)[d] / v for d, v in ref)
    assert ratios[1] < ib_bot.ADJ_MIN, "not the edge case: %r" % ratios
    f = ib_bot.adjustment_since(ref, post)
    assert f is not None and abs(f - 0.02) < 1e-6, f
    st = {"entry": 2950.0, "hw": 3004.0, "stop": 2700.0, "adj": 1.0, "px_ref": ref}
    assert not _ratchet(st, price=60.1, atr=3.0, prices=post), "a clean split sold the position"
    assert abs(st["stop"] - 54.0) < 0.01, st
    # ...while a damaged card is still refused
    for bad in (0.01, 0.0199):
        assert ib_bot.adjustment_since(ref, _scaled(pre, bad)) is None, bad
    print("t11 an exact 50-for-1 split on 4 dp closes is followed (stop 2700 -> %.2f); "
          "x0.01 and x0.0199 are still refused OK" % st["stop"])


class _Patch:
    def __init__(self, **kw):
        self.kw, self.old = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(ib_bot, k)
            setattr(ib_bot, k, v)

    def __exit__(self, *a):
        for k, v in self.old.items():
            setattr(ib_bot, k, v)


def t12_a_new_position_is_seeded_with_the_signals_memory():
    """Board review 2026-09-21: an entry got no px_ref, so the first exit check
    just recorded one from its own newer card, and a dividend going ex between
    the signal and that check was never followed. The seed now remembers the
    signal build's card - only when that card's last close IS the signal price
    (another build could be on another scale). A fetch that fails costs the
    memory, never the run. The run-level cases (the seed in state.json, --dry
    fetching nothing) are in test_run_abort.py t9."""
    docs = [{"card": {"price": 100.0}, "prices": PRE}]
    lines, urls = [], []

    def get_json(url):
        urls.append(url)
        return docs[0]

    with _Patch(get_json=get_json, log=lambda *a: lines.append(" ".join(map(str, a)))):
        ref = ib_bot._entry_px_ref("MSFT", 100)
        assert ref == ib_bot.px_ref(PRE) and len(ref) == 3, ref
        assert urls == [ib_bot.PRODUCTS_URL + "MSFT.json"], urls
        # another build: the card's last close is not the signal price
        assert ib_bot._entry_px_ref("MSFT", 99.0) is None
        assert any("is not the signal price" in l for l in lines), lines
        # no price history on the card
        docs[0] = {"card": {"price": 100.0}}
        assert ib_bot._entry_px_ref("MSFT", 100) is None
    # a fetch that fails: None, never an exception
    for err in (IOError("timed out"), ValueError("bad json"), KeyError("prices")):
        def boom(url, err=err):
            raise err
        with _Patch(get_json=boom, log=lambda *a: None):
            assert ib_bot._entry_px_ref("MSFT", 100) is None

    # End to end: the stock goes ex 2% before the first exit check. Seeded with
    # the memory, hw and stop follow; seeded without it (the old code), the stop
    # stays on the signal's pre-dividend price.
    first_check = _next_bar(_scaled(PRE, 0.98), 98.0)
    seeded = {"entry": 100.0, "hw": 100.0, "stop": 90.0, "adj": 1.0,
              "px_ref": ib_bot.px_ref(PRE)}
    _ratchet(seeded, price=98.0, atr=4.0, prices=first_check)
    assert abs(seeded["adj"] - 0.98) < 1e-9 and abs(seeded["stop"] - 88.2) < 1e-6, seeded
    old = {"entry": 100.0, "hw": 100.0, "stop": 90.0, "adj": 1.0}
    _ratchet(old, price=98.0, atr=4.0, prices=first_check)
    assert old["adj"] == 1.0 and old["stop"] == 90.0, old
    print("t12 an entry is seeded with the signal card's memory; the first check "
          "follows a dividend (stop 90.00 -> %.2f) OK" % seeded["stop"])


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
    t10_a_dividend_posted_late_is_followed_once()
    t11_an_exact_50_for_1_split_is_followed()
    t12_a_new_position_is_seeded_with_the_signals_memory()
    print("ALL DIVIDEND-ADJUST TESTS PASS")
