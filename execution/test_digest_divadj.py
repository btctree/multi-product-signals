#!/usr/bin/env python3
"""Golden tests: the digest's stop replay follows dividends and splits as the bot does.

Run from this directory:  python test_digest_divadj.py

Board review 2026-09-21. ib_bot's exit loop now rescales the high-water mark
and the stop by the factor the card's adjusted history moved since the run
that recorded px_ref, and anchors the tighten test on entry * adj
(div_adjust.py has the why). daily_signal.py - the digest behind the 23:40 UTC
action list and the /update tap - replays that loop for the operator to act on
by hand, and was left on the old rule: hw and stop straight from state.json,
the tighten test on the raw entry. On an ex-dividend day, and on any /update
between a build that carries the ex-date bar and the bot's next run, it
compared the dividend-dropped close with the pre-dividend stop and listed
"SELL X - trailing stop", a sale the bot itself will not make. What is locked
down:

  * t1: state recorded before a dividend, a card rescaled for it whose newest
    close fell by the dividend and nothing more - no trailing-stop SELL. The
    same close on a card that was NOT rescaled is a real fall: SELL, as before.
  * t2: a genuine fall below the RESCALED stop is still a SELL, at the
    rescaled stop.
  * t3: the tighten test anchors on entry * adj, not the raw entry.
  * t4: a row priced from Yahoo (no card) is replayed on its stored stop and
    never follows the previous row's card history.
  * t5: the digest writes nothing - state.json is untouched.

Nothing here touches /root or the network: every path is a temp file, every
fetch is a stub.
"""
import json
import os
import urllib.error
from datetime import date, datetime, timezone
from pathlib import Path

import testenv                                     # noqa: E402
_TMP = testenv.isolate("mps-digest-divadj-")
REPO = _TMP / "repo"
(REPO / "execution").mkdir(parents=True)
(REPO / "data").mkdir()
assert os.environ["MPS_REPO"] == str(REPO)
os.environ.pop("EXCLUDED_CASH", None)

import daily_signal as ds                          # noqa: E402
import div_adjust                                  # noqa: E402
import ib_web                                      # noqa: E402
testenv.assert_isolated()

ds.log = lambda *a, **k: None
# Wed 2026-09-16 23:40 UTC, the scheduled digest: New York's close has settled.
AT_2340 = datetime(2026, 9, 16, 23, 40, tzinfo=timezone.utc)
BUILT = "2026-09-16T22:05:00Z"                     # a build after that close
TODAY = date.today().isoformat()                   # 0 bars held: no time stop
TIMEOUT = urllib.error.URLError("timed out")
FX = {"HKD=X": 7.8, "EURUSD=X": 1.1, "JPY=X": 150.0, "GBPUSD=X": 1.3}
DIV = 0.98                                         # a 2% dividend


def write(path, obj):
    Path(path).write_text(json.dumps(obj), encoding="utf-8")


def series(closes):
    return [["2026-09-%02d" % (1 + i), c] for i, c in enumerate(closes)]


# The card as the bot's last run saw it: ten sessions, the newest close 100.0.
OLD = series([95.0, 96.0, 97.0, 98.0, 99.0, 99.5, 100.2, 100.4, 100.5, 100.0])
REF = div_adjust.px_ref(OLD)                       # what that run recorded
assert len(REF) == div_adjust.PX_REF_BARS, REF     # premise: enough bars to remember


def rescaled(newest):
    """The next build: Yahoo scaled every earlier bar by DIV for the ex-date,
    and the ex-date's own close is `newest`."""
    return [[d, round(c * DIV, 4)] for d, c in OLD] + [["2026-09-11", newest]]


def unscaled(newest):
    """The same session on a history nobody rescaled: no dividend, a real move."""
    return OLD + [["2026-09-11", newest]]


# Before the dividend: stop 99.0 sits just under the 100.0 close. entry 90 is
# the signal's price; atr 1.0, so the trail k*atr is 2.0 and hw - 2 = 98.5 is
# under the stop - the stored stop is what binds.
POS = {"entry": 90.0, "hw": 100.5, "stop": 99.0, "entry_date": TODAY, "px_ref": REF}


def report(rows):
    """build_report over `rows`: [(ysym, state entry, card prices or None,
    card price, card atr)]. A None history means no card: Yahoo prices it."""
    state_p, bot_p = _TMP / "state.json", _TMP / "bot_state.json"
    write(state_p, {"map": {y: y for y, *_ in rows}, "_peak_netliq": 1,
                    "pos": {y: st for y, st, *_ in rows}})
    write(bot_p, {"updated": "2026-09-16 23:20 UTC", "netliq": 250000,
                  "cash": {"USD": 30000},
                  "positions": [{"symbol": y, "qty": 10, "avg_cost": 90.0, "ccy": "USD"}
                                for y, *_ in rows]})
    cards = {ds.safe_name(y): (prices, px, atr) for y, st, prices, px, atr in rows
             if prices is not None}
    yahoo_px = {y: px for y, st, prices, px, atr in rows if prices is None}

    def get_json(url, timeout=30):
        if url.startswith(ds.PRODUCTS):
            name = url[len(ds.PRODUCTS):-len(".json")]
            if name not in cards:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            prices, px, atr = cards[name]
            return {"generated_at": BUILT, "prices": prices,
                    "card": {"price": px, "sma200": px * 0.5, "atr": atr}}   # no regime break
        if "range=1y" in url:                       # the Yahoo fallback
            sym = url.split("/chart/")[1].split("?")[0]
            return {"chart": {"result": [{"meta": {"regularMarketPrice": yahoo_px[sym]},
                                          "indicators": {"quote": [{"close": [50.0] * 200}]}}]}}
        if "interval=1m" in url:
            raise TIMEOUT                           # marks fall back to the close
        for pair, v in FX.items():
            if "/chart/" + pair + "?" in url:
                return {"chart": {"result": [{"meta": {"regularMarketPrice": v}}]}}
        if url == ds.BASE + "data.json":
            return {"generated_at": BUILT, "actions": []}
        raise AssertionError("unexpected fetch " + url)

    saved = {k: getattr(ds, k) for k in ("get_json", "_now_utc", "STATE", "BOT_STATE")}
    snap = ib_web.snapshot
    before = state_p.read_bytes()
    try:
        ds.get_json, ds._now_utc = get_json, (lambda: AT_2340)
        ds.STATE, ds.BOT_STATE = str(state_p), str(bot_p)
        ib_web.snapshot = no_ib
        msg = ds.build_report(on_demand=True)[0]
    finally:
        for k, v in saved.items():
            setattr(ds, k, v)
        ib_web.snapshot = snap
    assert state_p.read_bytes() == before, "the digest must write nothing to state.json"
    return msg


def no_ib():
    raise TIMEOUT                                   # the book comes from bot_state


def sells(msg):
    return [l for l in msg.splitlines() if l.startswith("<code>SELL ")]


def t1_an_ex_dividend_drop_is_not_a_trailing_stop_sell():
    # The ex-date close is 98.0: the 100.0 close less the 2% dividend. Nothing
    # was lost. The bot rescales its stop to 99.0 * 0.98 = 97.02 and holds.
    assert div_adjust.adjustment_since(REF, rescaled(98.0)) is not None
    msg = report([("KO", dict(POS), rescaled(98.0), 98.0, 1.0)])
    assert sells(msg) == [], msg
    assert "trailing stop" not in msg, msg
    assert "POSITIONS (1)" in msg and "used Yahoo" not in msg, msg
    # CONTROL: the same 98.0 on a history nobody rescaled is a real 2% fall
    # through the 99.0 stop - the rule still fires, exactly as before.
    msg = report([("KO", dict(POS), unscaled(98.0), 98.0, 1.0)])
    assert sells(msg) == ["<code>SELL KO 10 @ MKT</code>"], msg
    assert "trailing stop 99.00" in msg, msg
    # ...and so does the old replay on the rescaled card: this is the false SELL
    real = ds.div_adjust.adjustment_since
    try:
        ds.div_adjust.adjustment_since = lambda ref, prices: None
        msg = report([("KO", dict(POS), rescaled(98.0), 98.0, 1.0)])
    finally:
        ds.div_adjust.adjustment_since = real
    assert "trailing stop 99.00" in msg, "premise: without the rescale it sold"
    print("t1 an ex-dividend drop is not a trailing-stop SELL; an unscaled fall still is OK")


def t2_a_genuine_fall_below_the_rescaled_stop_still_sells():
    # 96.5 on the rescaled card: the dividend AND a real fall, through 97.02.
    msg = report([("KO", dict(POS), rescaled(96.5), 96.5, 1.0)])
    assert sells(msg) == ["<code>SELL KO 10 @ MKT</code>"], msg
    assert "trailing stop 97.02" in msg, msg
    # a split is followed the same way: 2-for-1 halves the stop to 49.50
    split = [[d, round(c * 0.5, 4)] for d, c in OLD] + [["2026-09-11", 49.9]]
    msg = report([("KO", dict(POS), split, 49.9, 0.5)])
    assert sells(msg) == [], msg
    split[-1][1] = 49.4
    msg = report([("KO", dict(POS), split, 49.4, 0.5)])
    assert "trailing stop 49.50" in msg and sells(msg), msg
    print("t2 a genuine fall below the rescaled stop still sells, at 97.02 OK")


def t3_the_tighten_test_anchors_on_entry_times_adj():
    # No stored stop, so the trail alone decides. After the dividend hw 106.0
    # is 103.88 and entry 100.0 is 98.0. The close 100.0 clears
    # 98.0 + 1.5 * atr(1.0) = 99.5, so k tightens to 2: trail 101.88, a SELL
    # there. Anchored on the raw entry (100.0 < 101.5) k would stay 3.5 and
    # the trail would read 100.38.
    st = {"entry": 100.0, "hw": 106.0, "entry_date": TODAY, "px_ref": REF}
    msg = report([("KO", dict(st), rescaled(100.0), 100.0, 1.0)])
    assert sells(msg) == ["<code>SELL KO 10 @ MKT</code>"], msg
    assert "trailing stop 101.88" in msg, msg
    # an adj already on record from an earlier event is carried, not
    # replaced: 100 * 1.02 * 0.98 = 99.96, and 100.0 < 99.96 + 1.5, so k
    # stays 3.5 - trail 100.38
    msg = report([("KO", dict(st, adj=1.02), rescaled(100.0), 100.0, 1.0)])
    assert "trailing stop 100.38" in msg, msg
    print("t3 the tighten test anchors on entry * adj OK")


def t4_a_yahoo_priced_row_never_follows_another_rows_card():
    # KO has a card rescaled for its dividend. PEP has NO card (Yahoo prices
    # it at 98.0) and, to make a leak visible, the very same px_ref: were the
    # previous row's card reused, PEP's stop would follow KO's dividend down to
    # 97.02 and its SELL would vanish. Priced from Yahoo it is replayed on its
    # stored stop, 99.0 - a SELL.
    msg = report([("KO", dict(POS), rescaled(98.0), 98.0, 1.0),
                  ("PEP", dict(POS), None, 98.0, 0)])
    assert "PEP: no dashboard card, used Yahoo" in msg, msg
    assert sells(msg) == ["<code>SELL PEP 10 @ MKT</code>"], msg
    assert "trailing stop 99.00" in msg, msg
    print("t4 a Yahoo-priced row is replayed on its own stored stop OK")


def t5_nothing_is_written():
    # report() asserts state.json is byte-for-byte unchanged after every run.
    # Here the rescale certainly happened (t1), and the record read back is
    # still the pre-dividend one: hw, stop and px_ref as the bot left them.
    state_p = _TMP / "state.json"
    report([("KO", dict(POS), rescaled(98.0), 98.0, 1.0)])
    st = json.loads(state_p.read_text(encoding="utf-8"))["pos"]["KO"]
    assert st == POS, st
    assert not Path(ds.PREV).exists(), "build_report must not move the P&L baseline"
    print("t5 the digest writes nothing: state.json is untouched OK")


if __name__ == "__main__":
    t1_an_ex_dividend_drop_is_not_a_trailing_stop_sell()
    t2_a_genuine_fall_below_the_rescaled_stop_still_sells()
    t3_the_tighten_test_anchors_on_entry_times_adj()
    t4_a_yahoo_priced_row_never_follows_another_rows_card()
    t5_nothing_is_written()
    print("ALL DIGEST DIVIDEND TESTS PASS")
