#!/usr/bin/env python3
"""Golden tests: where the owner SEES the shadow market filter - the 23:40
digest line and the dashboard strip and History label.

Run from this directory:  python test_market_filter_views.py

The filter is log only (owner decision 2026-10-26): the bot still buys in
every market. So:

  * the digest carries ONE line, "Market filter (log only): US above (+0.3%) ·
    ...", from data.json "market_trend" - a market with no usable row reads
    "unknown", a build without the key says so, "off" drops the line - and its
    BUY list is NOT filtered: a JP buy in a JP-below week is still listed;
    daily_signal stays python 3.9-safe;
  * the page shows a small "Market filter · log only" strip on Positions -
    four chips US/HK/JP/EU, up or down, % against the 100-day average, the bar
    date - with the one-line note that the bot still buys everywhere, and a
    "filter would have skipped" label on History rows the bot tagged. Every
    value from JSON is escaped or passed through Number(), the chips are a
    4-column grid that shrinks to 320px, and a failure in the strip can never
    cost the positions list. node is not installed here, so these are text
    checks on the page, like the other page suites without node.

Nothing here touches /root or the network: every path is a temp file, every
fetch a stub.
"""
import ast
import io
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

import testenv                                     # noqa: E402
_TMP = testenv.isolate("mps-mfilter-views-")
REPO = _TMP / "repo"
(REPO / "execution").mkdir(parents=True)
(REPO / "data").mkdir()
os.environ.pop("EXCLUDED_CASH", None)
for _v in ("MAX_ORDER_BASE", "TARGET_POSITIONS", "HK_ENABLED", "DAILY_LOSS_KILL"):
    os.environ.pop(_v, None)
os.environ.setdefault("IB_BACKEND", "web")

import daily_signal as ds                          # noqa: E402
import ib_web                                      # noqa: E402
testenv.assert_isolated()
ds.log = lambda *a, **k: None

HERE = Path(__file__).resolve().parent
PAGE = HERE.parent / "docs" / "index.html"
AT = datetime(2026, 9, 16, 23, 40, tzinfo=timezone.utc)          # every market decided
BUILT = "2026-09-16T22:05:00Z"
FX = {"HKD=X": 7.8, "EURUSD=X": 1.1, "JPY=X": 150.0, "GBPUSD=X": 1.3}


def trend_row(index, close, sma, bar="2026-09-16"):
    return {"index": index, "name": index.strip("^"), "close": close, "sma100": sma,
            "pct_vs_sma100": round((close / sma - 1) * 100, 2), "below": close < sma,
            "bar": bar}


TREND = {"US": trend_row("^GSPC", 7623.68, 7603.69),
         "HK": trend_row("^HSI", 26000.0, 25000.0),
         "JP": trend_row("^N225", 62693.16, 66669.08),
         "EU": trend_row("^STOXX50E", 6068.22, 6303.68)}


class Patch:
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


# ------------------------------------------------------------------ digest --
def t1_the_digest_line():
    line = ds.market_filter_line({"market_trend": TREND})
    assert line == ("Market filter (log only): US above (+0.3%) · HK above (+4.0%) · "
                    "JP below (-6.0%) · EU below (-3.7%)"), line
    for sig in (None, {}, {"market_trend": None}, {"market_trend": {}},
                {"market_trend": "garbage"}, {"market_trend": ["US"]}, "garbage"):
        assert ds.market_filter_line(sig) == \
            "Market filter (log only): no index data in this build", sig
    part = {"market_trend": {"US": TREND["US"], "JP": dict(TREND["JP"], below="yes"),
                             "EU": dict(TREND["EU"], pct_vs_sma100=float("nan")),
                             "HK": "x"}}
    assert ds.market_filter_line(part) == ("Market filter (log only): US above (+0.3%) · "
                                           "HK unknown · JP unknown · EU unknown"), \
        ds.market_filter_line(part)
    with Patch(ds, MARKET_FILTER="off"):
        assert ds.market_filter_line({"market_trend": TREND}) is None
    print("t1 the digest line: four markets, unknown for a bad row, a missing key "
          "said, off drops it OK")


def report(actions, signals_extra):
    """build_report on 10 calm US holdings, LIVE from IB, USD 40,000 free."""
    held = ["H%02d" % k for k in range(10)]
    state = {"map": {s: s for s in held}, "_peak_netliq": 1,
             "pos": {s: {"entry": 90, "hw": 100, "stop": 80,
                         "entry_date": date.today().isoformat()} for s in held}}
    Path(ds.STATE).write_text(json.dumps(state), encoding="utf-8")
    Path(ds.BOT_STATE).write_text(json.dumps({"updated": "x", "netliq": 220000,
                                              "cash": {"USD": 40000.0},
                                              "positions": []}), encoding="utf-8")
    Path(ds.NETLIQ_HIST).write_text(json.dumps({"series": [{"d": "2026-07-20", "nl": 200000}],
                                                "flows": []}), encoding="utf-8")
    snap = {"netliq": 220000.0, "cash": {"USD": 40000.0, "JPY": 3000000.0},
            "positions": [{"ib_symbol": s, "qty": 10, "avg_cost": 90.0, "ccy": "USD",
                           "sec_type": "STK"} for s in held]}
    signals = {"generated_at": BUILT, "actions": list(actions)}
    signals.update(signals_extra)

    def get_json(url, timeout=30):
        for s in held:
            if url == ds.PRODUCTS + s + ".json":
                return {"card": {"price": 100.0, "sma200": 80.0, "atr": 2.0},
                        "generated_at": BUILT}
        if "interval=1m" in url:
            return {"chart": {"result": [{"timestamp": [1789570800],
                                          "indicators": {"quote": [{"close": [100.0]}]}}]}}
        for pair, v in FX.items():
            if "/chart/" + pair + "?" in url:
                return {"chart": {"result": [{"meta": {"regularMarketPrice": v}}]}}
        if url == ds.BASE + "data.json":
            return json.loads(json.dumps(signals))
        raise AssertionError("unexpected fetch " + url)

    with Patch(ds, get_json=get_json, _now_utc=lambda: AT), \
            Patch(ib_web, snapshot=lambda: snap):
        return ds.build_report(on_demand=True)[0]


def t2_the_digest_shows_it_and_still_lists_every_market():
    acts = [{"symbol": "NEWA", "action": "BUY", "price": 100.0, "score": 90, "market": "US"},
            {"symbol": "7203.T", "action": "BUY", "price": 2900.0, "score": 80, "market": "JP"}]
    msg = report(acts, {"market_trend": TREND})
    notes = msg.split("<b>ℹ️ Notes</b>")[1]
    assert ("• Market filter (log only): US above (+0.3%) · HK above (+4.0%) · "
            "JP below (-6.0%) · EU below (-3.7%)") in notes, notes
    buys = [l.split()[1] for l in msg.splitlines() if l.startswith("<code>BUY ")]
    assert buys == ["NEWA", "7203.T"], "log only: a JP-below week still lists the JP buy"
    msg = report(acts, {})
    assert "• Market filter (log only): no index data in this build" in msg
    with Patch(ds, MARKET_FILTER="off"):
        assert "Market filter" not in report(acts, {"market_trend": TREND})
    # 3.9-safe, still no ib_bot import
    src = (HERE / "daily_signal.py").read_text(encoding="utf-8")
    tree = ast.parse(src, feature_version=(3, 9))
    names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert "ib_bot" not in names, names
    print("t2 the digest's Notes carry the line; its BUY list is not filtered; "
          "3.9-safe OK")


# -------------------------------------------------------------------- page --
def _page():
    return io.open(PAGE, encoding="utf-8").read()


def _fn(page, name):
    """The source of one top-level page function, to its closing brace."""
    i = page.index("function %s(" % name)
    j = page.index("\n}\n", i)
    return page[i:j + 2]


def t3_the_positions_strip():
    page = _page()
    fn = _fn(page, "marketFilterHTML")
    assert "Market filter · log only" in fn
    assert ("Log only: the bot still buys in every market; this shows what the filter "
            "would have blocked.") in fn
    # four fixed markets, never keys from the JSON
    assert "const MF_MKTS=['US','HK','JP','EU'];" in page
    assert "MF_MKTS.map(m=>" in fn and "Object.keys(src)" not in fn
    # newest build first, else what the bot read; mode off shows nothing
    assert "DATA.market_trend" in fn and "mf.markets" in fn and "mf.mode==='off'" in fn
    # up/down, % vs the 100-day average, the bar date
    for bit in ("'mf-up'", "'mf-dn'", "'▲'", "'▼'", "x.pct.toFixed(1)+'%'", "mfDay(r.bar)"):
        assert bit in fn, bit
    # every JSON value escaped or numeric
    assert "Number(r.pct_vs_sma100)" in fn and "title=\"${escH(x.tip)}\"" in fn
    assert "${escH(x.day)}" in fn and "escH(days[0])" in fn and "escH(first)" in fn
    assert "${r." not in fn and "${mf." not in fn, "a raw JSON value reaches the HTML"
    # phone width: a shrinking 4-column grid, nothing forcing a width
    assert ".mf-chips{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));" in page
    assert ".mf-chip{min-width:0;overflow:hidden;" in page
    # placed under net worth, and guarded: the strip can never cost the list
    rp = _fn(page, "renderPositions")
    assert "try{ mfHtml=marketFilterHTML(); }catch(e){ mfHtml=''; }" in rp
    assert "el.innerHTML=staleNote+botNote+mfHtml+`<div id=\"bench-card\"></div>`" in rp
    print("t3 the Positions strip: four chips, the note, escaped, 320px-safe, "
          "guarded OK")


def t4_the_history_label():
    page = _page()
    fn = _fn(page, "renderActivityMode")
    assert ("${x.shadow?` <span class=\"pill\" style=\"background:rgba(255,193,77,.16);"
            "color:var(--amber)\">filter would have skipped</span>`:''}") in fn
    # the label is fixed text: the tag's own value never reaches the page
    assert "x.shadow}" not in fn and "esc(x.shadow)" not in fn
    assert "<i>filter would have skipped</i> = a buy the market filter would have " \
           "stopped" in fn, "the legend must explain the label"
    print("t4 History rows the bot tagged carry a 'filter would have skipped' "
          "label, explained in the legend OK")


if __name__ == "__main__":
    t1_the_digest_line()
    t2_the_digest_shows_it_and_still_lists_every_market()
    t3_the_positions_strip()
    t4_the_history_label()
    print("ALL MARKET FILTER VIEW TESTS PASS")
