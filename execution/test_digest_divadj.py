#!/usr/bin/env python3
"""Golden test: the digest reads each holding against its OWN card, or none.

Run from this directory:  python test_digest_divadj.py

Board review 2026-09-21. build_report walks the held positions and reads each
one's product card; a holding with no card is priced from Yahoo instead. The
card it read (`product`) is reset per symbol, so a row priced from Yahoo is
never read against the PREVIOUS symbol's card. What is locked down:

  * t1: a card-priced holding, then one with no card. The second is priced
    from Yahoo and replayed on its own stored stop - a SELL there - while the
    first holds on its own card. The digest writes nothing: state.json is
    byte-for-byte unchanged.

(The file name is historical.) Nothing here touches /root or the network:
every path is a temp file, every fetch is a stub.
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
import ib_web                                      # noqa: E402
testenv.assert_isolated()

ds.log = lambda *a, **k: None
# Wed 2026-09-16 23:40 UTC, the scheduled digest: New York's close has settled.
AT_2340 = datetime(2026, 9, 16, 23, 40, tzinfo=timezone.utc)
BUILT = "2026-09-16T22:05:00Z"                     # a build after that close
TODAY = date.today().isoformat()                   # 0 bars held: no time stop
TIMEOUT = urllib.error.URLError("timed out")
FX = {"HKD=X": 7.8, "EURUSD=X": 1.1, "JPY=X": 150.0, "GBPUSD=X": 1.3}


def write(path, obj):
    Path(path).write_text(json.dumps(obj), encoding="utf-8")


# stop 99.0 under a 100.0 close; entry 90 and atr 1.0 make the trail
# 100.5 - 2 = 98.5, under the stop - the stored stop is what binds.
POS = {"entry": 90.0, "hw": 100.5, "stop": 99.0, "entry_date": TODAY}


def report(rows):
    """build_report over `rows`: [(ysym, state entry, has card, price, atr)].
    A row with no card is priced from Yahoo."""
    state_p, bot_p = _TMP / "state.json", _TMP / "bot_state.json"
    write(state_p, {"map": {y: y for y, *_ in rows}, "_peak_netliq": 1,
                    "pos": {y: st for y, st, *_ in rows}})
    write(bot_p, {"updated": "2026-09-16 23:20 UTC", "netliq": 250000,
                  "cash": {"USD": 30000},
                  "positions": [{"symbol": y, "qty": 10, "avg_cost": 90.0, "ccy": "USD"}
                                for y, *_ in rows]})
    cards = {ds.safe_name(y): (px, atr) for y, st, has, px, atr in rows if has}
    yahoo_px = {y: px for y, st, has, px, atr in rows if not has}

    def get_json(url, timeout=30):
        if url.startswith(ds.PRODUCTS):
            name = url[len(ds.PRODUCTS):-len(".json")]
            if name not in cards:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            px, atr = cards[name]
            return {"generated_at": BUILT,
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


def t1_a_yahoo_priced_row_is_read_on_its_own():
    # KO holds on its card: 100.0 is above its 99.0 stop. PEP has NO card
    # (Yahoo prices it at 98.0) and the same stored record: replayed on its
    # own stop, 99.0, it is a SELL.
    msg = report([("KO", dict(POS), True, 100.0, 1.0),
                  ("PEP", dict(POS), False, 98.0, 0)])
    assert "PEP: no dashboard card, used Yahoo" in msg, msg
    assert "KO: no dashboard card" not in msg, msg
    assert sells(msg) == ["<code>SELL PEP 10 @ MKT</code>"], msg
    assert "trailing stop 99.00" in msg, msg
    assert not Path(ds.PREV).exists(), "build_report must not move the P&L baseline"
    print("t1 a Yahoo-priced row is replayed on its own stored stop OK")


if __name__ == "__main__":
    t1_a_yahoo_priced_row_is_read_on_its_own()
    print("ALL DIGEST CARD-RESET TESTS PASS")
