#!/usr/bin/env python3
"""Golden tests: the display-only simulated lines (engine/build_sim_lines.py).

Run from this directory:  python test_sim_lines.py

The Growth vs S&P card draws two SIMULATED lines beside the account (owner's
decision 2026-10-26): the research 70/30 with its crypto trend sleeve, and the
account with a Bitcoin slot run by M1. This file builds the sleeves' daily
equity in the hourly CI job. What must stay true:

  * M1's record maps to a LONG-ONLY slot: Buy opens it; Sell, Short and Cover
    all leave it flat; the last action of a day is that day's state.
  * The slot follows M1 one HONG KONG session late: a Saturday signal is first
    held on Monday's session, and an HK holiday pushes it on a day.
  * One 0.23% cost per change of the slot, on the day it changes, never on a
    day it does not; the 1%/yr fee only while held.
  * complete_through is the UTC day before the price download STARTED, so the
    bar still trading at download time never reaches the page.
  * FAIL SOFT: M1's site down gives "m1": null with the trend line still
    built, nothing is written but docs/sim_lines.json (atomically), and the
    script exits 0 whatever happens - this runs in the build that deploys
    data.json, whose failure makes the bot defer every holding's exit.

Offline: yfinance is replaced by a stub and urllib's opener by one that fails
the test, so nothing here can reach Yahoo or M1's site; every file goes to a
temp dir.
"""
import contextlib
import datetime as dt
import io
import json
import os
import sys
import tempfile
import types
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

_yf = types.ModuleType("yfinance")                  # never the network in a test


def _no_network(*a, **k):
    raise AssertionError("test reached the yfinance network stub")


_yf.download = _no_network
_yf.Ticker = _no_network
sys.modules["yfinance"] = _yf


def _no_urlopen(*a, **k):
    raise AssertionError("test reached the network (urlopen)")


urllib.request.urlopen = _no_urlopen

import data_fetch as D                              # noqa: E402
import build_sim_lines as B                         # noqa: E402

D.PRICE_DIR = Path(tempfile.mkdtemp(prefix="sim_prices_"))   # never data/prices
B.OUT = Path(tempfile.mkdtemp(prefix="sim_docs_")) / "sim_lines.json"


class Patch:
    def __init__(self, target, **kw):
        self.target, self.kw, self.old = target, kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(self.target, k)
            setattr(self.target, k, v)
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            setattr(self.target, k, v)


def days(first, last):
    d0, d1 = dt.date.fromisoformat(first), dt.date.fromisoformat(last)
    return [(d0 + dt.timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


def weekdays(first, last, holidays=()):
    return [d for d in days(first, last)
            if dt.date.fromisoformat(d).weekday() < 5 and d not in holidays]


def closes(first, last, px=100.0, step=0.0):
    ds = days(first, last)
    return pd.Series([px * (1 + step) ** i for i in range(len(ds))],
                     index=pd.to_datetime(ds))


def acts(*pairs):
    return B.validate_m1([{"date": d, "price": 1.0, "action": a, "engine": "X"}
                          for d, a in pairs])


def by_day(rows):
    return {r[0]: r for r in rows}


# ------------------------------------------------------------------ mapping --
def t1_actions_map_to_a_long_only_slot():
    at = B.m1_long_at(acts(("2026-10-01", "Buy"), ("2026-10-03", "Sell"),
                           ("2026-10-05", "Short"), ("2026-10-07", "Cover"),
                           ("2026-10-07", "Buy"),            # Cover then Buy: long
                           ("2026-10-09", "Buy"), ("2026-10-09", "Short")))   # flat
    want = {"2026-09-30": 0, "2026-10-01": 1, "2026-10-02": 1, "2026-10-03": 0,
            "2026-10-04": 0, "2026-10-05": 0, "2026-10-06": 0, "2026-10-07": 1,
            "2026-10-08": 1, "2026-10-09": 0, "2026-10-20": 0}
    got = {d: at(d) for d in want}
    assert got == want, got
    # a Short is flat for this slot, not a position the other way
    assert B.M1_LONG == {"Buy": 1, "Sell": 0, "Short": 0, "Cover": 0}

    # all or nothing: a record whose shape changed is not drawn from
    for bad in ([], {}, "x", [{"date": "2026-10-01", "action": "Hold"}],
                [{"date": "2026-13-01", "action": "Buy"}],
                [{"date": "01/10/2026", "action": "Buy"}],
                [{"date": "2026-10-01"}], ["Buy"]):
        try:
            B.validate_m1(bad)
        except ValueError:
            continue
        raise AssertionError("accepted a malformed M1 record: %r" % (bad,))
    # sorted by day, same-day order kept (the later action is the close)
    got = B.validate_m1([{"date": "2026-10-02", "action": "Sell"},
                         {"date": "2026-10-01", "action": "Cover"},
                         {"date": "2026-10-01", "action": "Buy"}])
    assert [(a["date"], a["action"]) for a in got] == [
        ("2026-10-01", "Cover"), ("2026-10-01", "Buy"), ("2026-10-02", "Sell")], got
    print("t1 Buy holds; Sell, Short and Cover are flat; a day's last action wins; "
          "a malformed record is refused OK")


def t2_a_weekend_signal_is_first_held_on_mondays_session():
    cl = closes("2026-10-15", "2026-11-06", step=0.01)
    hk = weekdays("2026-10-01", "2026-11-30", holidays={"2026-11-02"})
    # Buy on Saturday 24 Oct; Sell on Wednesday 28 Oct; Buy on Saturday 31 Oct
    # with Monday 2 Nov an HK holiday
    rows = by_day(B.m1_slot(cl, acts(("2026-10-24", "Buy"), ("2026-10-28", "Sell"),
                                     ("2026-10-31", "Buy")), hk))
    held = {d: rows[d][2] for d in days("2026-10-23", "2026-11-04")}
    assert held == {
        "2026-10-23": 0,                        # Fri
        "2026-10-24": 0, "2026-10-25": 0,       # the signal day and Sunday: no session
        "2026-10-26": 1,                        # Mon: the ETF is bought, Sunday's close -> Monday's
        "2026-10-27": 1, "2026-10-28": 1,       # Wed: M1 sells at its close...
        "2026-10-29": 1,                        # ...the slot sells at Thursday's session (exit cost)
        "2026-10-30": 0,
        "2026-10-31": 0, "2026-11-01": 0,
        "2026-11-02": 0,                        # Monday is an HK holiday
        "2026-11-03": 1,                        # Tuesday's session buys it
        "2026-11-04": 1}, held
    # ...and the slot only earns Bitcoin's move on days it actually held it
    g = lambda d: rows[d][1]
    assert g("2026-10-25") == g("2026-10-24") == g("2026-10-23"), "earned a move before the session"
    assert g("2026-10-27") > g("2026-10-26"), "a held day did not earn Bitcoin's move"
    assert g("2026-11-01") == g("2026-10-31") == g("2026-10-30"), "earned a move while flat"
    print("t2 a Saturday signal is held from Monday's HK session, a holiday pushes it to "
          "Tuesday, a Wednesday sale leaves at Thursday's OK")


def t3_one_cost_per_change_and_the_fee_only_while_held():
    cl = closes("2026-10-01", "2026-10-31")             # Bitcoin flat: only costs move g
    hk = weekdays("2026-09-01", "2026-11-30")
    rows = B.m1_slot(cl, acts(("2026-10-05", "Buy"), ("2026-10-12", "Sell"),
                              ("2026-10-14", "Short"), ("2026-10-16", "Cover")), hk)
    fee = 1 - B.M1_FEE_YR / 365.0
    cost = 1 - B.M1_COST
    changes, prev = [], None
    for (d0, g0, _), (d1, g1, in1) in zip(rows, rows[1:]):
        ratio = g1 / g0
        if abs(ratio - 1) < 1e-15:
            assert in1 == 0, "%s: in without being held or paying" % d1
        elif abs(ratio - fee) < 1e-12:
            assert in1 == 1, "%s: fee charged on a day not held" % d1
        elif abs(ratio - fee * cost) < 1e-12:
            changes.append(("in", d1))                  # bought: fee + one cost
        elif abs(ratio - cost) < 1e-12:
            changes.append(("out", d1))                 # sold: one cost, no fee
            assert in1 == 1, "the exit day must count the slot as in"
        else:
            raise AssertionError("%s: unexpected step %.12f" % (d1, ratio))
    # M1 Buy Mon 5 Oct -> held from Tue 6 Oct; Sell Mon 12 Oct -> out Tue 13 Oct.
    # The Short and the Cover after it change nothing for a long-only slot.
    assert changes == [("in", "2026-10-06"), ("out", "2026-10-13")], changes
    g = by_day(rows)
    assert abs(g["2026-10-31"][1] / g["2026-10-01"][1] - cost * cost * fee ** 7) < 1e-12
    print("t3 two costs for one round trip (Short and Cover add none), the fee on the 7 "
          "held days only OK")


# ------------------------------------------------------------ the whole build --
def _bars(index, seed, px, drift):
    rng = np.random.default_rng(seed)
    close = px * np.exp(np.cumsum(rng.normal(drift, 0.02, len(index))))
    op = close * (1 + rng.normal(0, 0.004, len(index)))
    hi = np.maximum(op, close) * (1 + rng.uniform(0.001, 0.02, len(index)))
    lo = np.minimum(op, close) * (1 - rng.uniform(0.001, 0.02, len(index)))
    return pd.DataFrame({"Open": op, "High": hi, "Low": lo, "Close": close,
                         "Volume": rng.integers(1e6, 5e6, len(index)).astype(float)},
                        index=pd.DatetimeIndex(index, name="Date"))


START = "2026-10-26T09:05:00Z"          # the CI download began mid-morning UTC


def _write_prices(start=START, record=True):
    for p in D.PRICE_DIR.glob("*"):
        p.unlink()
    today = start[:10]
    cal = days("2024-01-01", today)                     # crypto: every day, today trading
    btc = _bars(cal, 1, 40000.0, 0.0015)
    btc.iloc[-1, btc.columns.get_loc("Close")] *= 3     # today's in-progress bar: must not count
    _bars(cal, 2, 2000.0, 0.0012).to_csv(D.cache_path("ETH-USD"))
    btc.to_csv(D.cache_path("BTC-USD"))
    _bars(weekdays("2024-01-01", today), 3, 20000.0, 0.0).to_csv(D.cache_path("^HSI"))
    if record:
        D._record_download_start(["BTC-USD", "ETH-USD", "^HSI"], start)


def _m1_record():
    return [{"date": "2026-09-19", "action": "Buy"}, {"date": "2026-10-03", "action": "Sell"},
            {"date": "2026-10-10", "action": "Short"}, {"date": "2026-10-17", "action": "Cover"},
            {"date": "2026-10-24", "action": "Buy"}]


def _snapshot(folder):
    return {p.name: (p.stat().st_mtime_ns, p.stat().st_size) for p in Path(folder).iterdir()}


def t4_the_build_publishes_complete_days_only():
    _write_prices()
    before = _snapshot(D.PRICE_DIR)
    out_dir = Path(tempfile.mkdtemp(prefix="sim_out_"))
    out = out_dir / "sim_lines.json"
    res = B.build(out_path=out, fetch=lambda url: B.validate_m1(_m1_record()))
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data == json.loads(json.dumps(res)), "the file is not what build() returned"
    assert data["complete_through"] == "2026-10-25", data["complete_through"]
    assert data["weights"] == {"trend": 0.30, "m1": 0.20}
    assert data["display_only"] and data["schema"] == 1
    t, m = data["trend"], data["m1"]
    assert t and m, (data.get("trend_error"), data.get("m1_error"))
    for s in (t["series"], m["series"]):
        ds = [r[0] for r in s]
        assert ds == days(ds[0], "2026-10-25"), "missing or extra days, or past complete_through"
        assert ds[0] >= B._shift("2026-10-25", -B.KEEP_DAYS)
        assert s[0][1] == 1.0, "not normalised on the first published day"
    assert "2026-10-26" not in json.dumps(t["series"]) + json.dumps(m["series"]), \
        "today's bar, still trading at download time, was published"
    assert m["source"] == B.M1_URL and m["fetched_at"].endswith("Z")
    assert m["last_action"] == {"date": "2026-10-24", "action": "Buy"}
    held = {r[0]: r[2] for r in m["series"]}
    # Buy Sat 19 Sep -> Mon 21 Sep; Sell Sat 3 Oct -> out at Mon 5 Oct (cost day)
    assert held["2026-09-20"] == 0 and held["2026-09-21"] == 1 and held["2026-10-04"] == 1
    assert held["2026-10-05"] == 1 and held["2026-10-06"] == 0
    # Buy Sat 24 Oct -> first held Mon 26 Oct, which is not complete yet
    assert held["2026-10-25"] == 0
    # the trend sleeve is the research run, cut and rescaled - nothing else
    eq = B.trend_sleeve({k: B.read_prices(k) for k in B.TREND_TICKERS})
    w = eq[(eq.index >= t["series"][0][0]) & (eq.index <= "2026-10-25")]
    assert max(abs(a[1] - round(v / w.iloc[0], 6)) for a, v in zip(t["series"], w)) < 1e-12
    # it wrote one file and touched no price file
    assert sorted(os.listdir(out_dir)) == ["sim_lines.json"], os.listdir(out_dir)
    assert _snapshot(D.PRICE_DIR) == before, "a price file was written"
    print("t4 complete through the day before the download began (2026-10-25), both lines "
          "normalised, the trend sleeve is run_trend's own, one file written OK")

    # no recorded start: the files' own write times stand in, and it says so
    _write_prices(record=False)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        res = B.build(out_path=out, fetch=lambda url: B.validate_m1(_m1_record()))
    # the files were written just now, so the day before today (UTC) - and
    # never past the last bar they hold
    yesterday = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)).isoformat()
    assert res["complete_through"] == min(yesterday, START[:10]), res["complete_through"]
    assert "no recorded download start" in buf.getvalue(), buf.getvalue()
    print("t4b without a recorded start the price files' write times are used, with a "
          "warning OK")


class _Resp:
    def __init__(self, body, status=200):
        self.body, self.status = body, status

    def read(self, n=-1):
        return self.body if n < 0 else self.body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def t5_a_failed_fetch_is_m1_null_and_nothing_else():
    # fetch_m1 itself, against stub openers: a good body, then each way it fails
    seen = {}

    def good(req, timeout):
        seen["url"], seen["timeout"] = req.full_url, timeout
        seen["ua"] = req.get_header("User-agent")
        return _Resp(json.dumps(_m1_record()).encode())
    assert [a["action"] for a in B.fetch_m1(opener=good)] == [
        "Buy", "Sell", "Short", "Cover", "Buy"]
    assert seen["url"] == B.M1_URL and seen["timeout"] == B.M1_TIMEOUT_S and seen["ua"]
    for opener in (lambda req, timeout: _Resp(b"<html>404</html>"),
                   lambda req, timeout: _Resp(b"[]"),
                   lambda req, timeout: _Resp(b"{}", status=503),
                   lambda req, timeout: _Resp(b"[" + b" " * (B.M1_MAX_BYTES + 5) + b"]")):
        try:
            B.fetch_m1(opener=opener)
        except (ValueError, json.JSONDecodeError):
            continue
        raise AssertionError("fetch_m1 accepted a bad response")

    # the build: M1's site down -> m1 null, trend still built, exit code 0
    _write_prices()
    out_dir = Path(tempfile.mkdtemp(prefix="sim_out_"))

    def down(url):
        raise urllib.error.URLError("timed out")
    buf = io.StringIO()
    # as CI runs it: the default output path and the real fetch, which is down
    with contextlib.redirect_stdout(buf), Patch(B, OUT=out_dir / "sim_lines.json", fetch_m1=down):
        res = B.build()
    assert res["m1"] is None and "URLError" in res["m1_error"], res.get("m1_error")
    assert res["trend"] and len(res["trend"]["series"]) > 100, "the trend line went with M1"
    assert res["complete_through"] == "2026-10-25"
    assert "::warning::" in buf.getvalue() and "m1 is null" in buf.getvalue()
    assert json.loads((out_dir / "sim_lines.json").read_text(encoding="utf-8"))["m1"] is None
    assert sorted(os.listdir(out_dir)) == ["sim_lines.json"], os.listdir(out_dir)

    # main() never fails the build: no price files at all -> warning, exit 0,
    # and the file from before is left alone (the page draws what it had)
    for p in D.PRICE_DIR.glob("*"):
        p.unlink()
    out = out_dir / "sim_lines.json"
    old = out.read_text(encoding="utf-8")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), Patch(B, OUT=out, fetch_m1=down):
        rc = B.main()
    assert rc == 0, rc
    assert "::warning::sim lines not built" in buf.getvalue(), buf.getvalue()
    assert out.read_text(encoding="utf-8") == old
    assert sorted(os.listdir(out_dir)) == ["sim_lines.json"], os.listdir(out_dir)

    # ...and a write that fails leaves no temp file behind
    _write_prices()
    with Patch(B.os, replace=lambda a, b: (_ for _ in ()).throw(OSError("disk full"))):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), Patch(B, OUT=out, fetch_m1=down):
            assert B.main() == 0
    assert sorted(os.listdir(out_dir)) == ["sim_lines.json"], os.listdir(out_dir)
    assert out.read_text(encoding="utf-8") == old
    print("t5 M1 down: m1 null with the trend line kept; any failure: a warning, exit 0, "
          "no stray file OK")


def t6_the_ci_step_cannot_break_the_build():
    """The workflow step that runs this, read as text: after the dashboard
    build, before the Pages upload, continue-on-error with a timeout."""
    root = Path(__file__).resolve().parent.parent
    wf = (root / ".github" / "workflows" / "daily.yml").read_text(encoding="utf-8")
    i = wf.index("name: Simulated comparison lines (display only)")
    step = wf[i:wf.index("\n\n", i)]
    assert "continue-on-error: true" in step and "timeout-minutes: 3" in step, step
    assert "working-directory: engine" in step and "run: python build_sim_lines.py" in step
    assert wf.index("name: Build dashboard") < i < wf.index("actions/upload-pages-artifact"), \
        "the step must run after the dashboard build and before the Pages upload"
    ign = (root / ".gitignore").read_text(encoding="utf-8").split()
    assert "docs/sim_lines.json" in ign, "the built file must never be committed"
    print("t6 the CI step runs after the build, before the upload, continue-on-error with "
          "a 3-minute timeout; the file is gitignored OK")


if __name__ == "__main__":
    t1_actions_map_to_a_long_only_slot()
    t2_a_weekend_signal_is_first_held_on_mondays_session()
    t3_one_cost_per_change_and_the_fee_only_while_held()
    t4_the_build_publishes_complete_days_only()
    t5_a_failed_fetch_is_m1_null_and_nothing_else()
    t6_the_ci_step_cannot_break_the_build()
    print("ALL SIM-LINES TESTS PASS")
