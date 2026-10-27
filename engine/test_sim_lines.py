#!/usr/bin/env python3
"""Golden tests: the display-only simulated lines (engine/build_sim_lines.py).

Run from this directory:  python test_sim_lines.py

The Growth vs S&P card draws SIMULATED lines beside the account (owner's
decisions 2026-10-26 and 2026-10-27): the research 70/30 with its crypto trend
sleeve, the account with a Bitcoin slot run by M1, and the account with the
bot buys the 100-day market filter would have skipped taken out. This file
builds what each line needs in the hourly CI job. What must stay true:

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
  * The filter line's figure c (engine/filter_lots.py): a held skipped lot is
    its share of the position's HKD value less its HKD cost; once sold, its
    realised profit stays; a day it cannot price has no figure (never a
    guess) and the series goes on past it - only a hole at the end stops it,
    and the build log warns of every hole but the routine one, the newest
    breakdown still intraday; with nothing skipped every day is 0. Bot lots
    are the bot's own BUY orders paired with their fills - never a hand
    trade, a rejected or an unfilled order - and sales close shares first in,
    first out.
  * The lots come from data/filter_backfill.json and the bot's shadow record,
    once each; any record that cannot be read gives "filter": null and leaves
    the trend and M1 lines exactly as they were.
  * The committed data/filter_backfill.json agrees with the ledger and with
    the rule it states (build_filter_backfill.py judges an order on the index
    bar that had FINISHED when it was placed).

Offline: yfinance is replaced by a stub and urllib's opener by one that fails
the test, so nothing here can reach Yahoo or M1's site; every file goes to a
temp dir. The one exception is t11, which only READS the committed
data/filter_backfill.json and data/fills_ledger.jsonl.
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
import build_filter_backfill as BF                  # noqa: E402
import filter_lots as FL                            # noqa: E402

REPO_DATA = B.DATA                                  # read by t11 only, never written
D.PRICE_DIR = Path(tempfile.mkdtemp(prefix="sim_prices_"))   # never data/prices
B.OUT = Path(tempfile.mkdtemp(prefix="sim_docs_")) / "sim_lines.json"
B.DATA = Path(tempfile.mkdtemp(prefix="sim_data_"))           # never the repo's data/


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
    _filter_data(B.DATA)
    # one more account day, 26 Oct: past the Bitcoin lines' complete_through
    hist = json.loads((B.DATA / "netliq_history.json").read_text(encoding="utf-8"))
    hist["series"].append({"d": "2026-10-26", "nl": 201000, "exc": 0})
    (B.DATA / "netliq_history.json").write_text(json.dumps(hist), encoding="utf-8")
    parts = json.loads((B.DATA / "day_parts.json").read_text(encoding="utf-8"))
    parts["days"]["2026-10-26"] = dict(parts["days"]["2026-10-25"], ts="2026-10-26T23:25:12Z")
    (B.DATA / "day_parts.json").write_text(json.dumps(parts), encoding="utf-8")
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
    # the filter line rides in the same file, on the account's own days - the
    # Bitcoin lines' complete_through does not cut it
    fl = data["filter"]
    assert fl and [r[0] for r in fl["series"]] == FDAYS + ["2026-10-26"], data.get("filter_error")
    assert data["complete_through"] == "2026-10-25"
    # it wrote one file and touched no price file
    assert sorted(os.listdir(out_dir)) == ["sim_lines.json"], os.listdir(out_dir)
    assert _snapshot(D.PRICE_DIR) == before, "a price file was written"
    print("t4 complete through the day before the download began (2026-10-25), both lines "
          "normalised, the trend sleeve is run_trend's own, the filter line beside them, "
          "one file written OK")

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


# --------------------------------------------------------- the filter line --
def _part(day, pos, fx=None, ts=None):
    """One day_parts row: pos {SYM: [qty, hkd value, ccy]}, Yahoo-keyed, with
    the IB -> Yahoo map the publisher writes."""
    return {"ts": ts or f"{day}T23:25:10Z", "nl": 200000, "exc": 0,
            "fx": dict({"HKD": 1.0, "USD": 7.8, "JPY": 0.05}, **(fx or {})),
            "ib": {k.split(".")[0]: k for k in pos if "." in k},
            "pos": {k: list(v) for k, v in pos.items()}, "cash": {}, "src": "live"}


def _fill(fid, day, sym, side, qty, px, ccy, com, clock="00:00", com_ccy=None):
    return {"execId": fid, "date": day, "ts": f"{day} {clock}", "symbol": sym,
            "sec_type": "STK", "side": side, "qty": qty, "price": px, "ccy": ccy,
            "commission": com, "commission_ccy": com_ccy or ccy, "source": "api"}


def _lot(fills, sales=(), sym="7733"):
    """A lot as filter_lots builds it: fills/sales [id, date, qty, px, com, com_ccy, ccy]."""
    return {"key": "x", "order": {"time": "2026-09-01 23:35 UTC", "symbol": sym},
            "fills": [list(f) for f in fills], "sales": [list(s) for s in sales]}


def t7_the_filter_figure_held_sold_partial_and_never_guessed():
    ds = days("2026-09-01", "2026-09-08")
    # bought 2 Sep: 100 x 2,000 JPY + 160 JPY commission at 0.05 = 10,008 HKD;
    # sold 5 Sep: 100 x 2,100 - 168 at THAT day's 0.051 = 10,701.432 HKD
    lot = _lot([["f1", "2026-09-02", 100, 2000.0, 160.0, "JPY", "JPY"]],
               [["s1", "2026-09-05", 100, 2100.0, 168.0, "JPY", "JPY"]])
    parts = {d: _part(d, {}) for d in ds}
    parts["2026-09-02"] = _part("2026-09-02", {"7733.T": [100, 10200.0, "JPY"]})
    parts["2026-09-03"] = _part("2026-09-03", {"7733.T": [100, 9500.0, "JPY"]})
    parts["2026-09-04"] = _part("2026-09-04", {"7733.T": [100, 9900.0, "JPY"]})
    parts["2026-09-05"] = _part("2026-09-05", {}, fx={"JPY": 0.051})
    for d in ("2026-09-06", "2026-09-07"):
        del parts[d]                         # no breakdown at all: nothing held, no matter
    rows, stop = FL.filter_series(ds, [lot], parts)
    c = dict(rows)
    assert stop is None and [r[0] for r in rows] == ds, (rows, stop)
    assert c["2026-09-01"] == 0.0, "a lot counted before its fill"
    # held: the position's HKD value less the HKD cost, marked to market each day
    assert c["2026-09-02"] == round(10200 - 10008, 2)
    assert c["2026-09-03"] == round(9500 - 10008, 2) and c["2026-09-04"] == round(9900 - 10008, 2)
    # sold: proceeds at the sale day's rate less the cost, constant from then on
    real = round(100 * 2100 * 0.051 - 168 * 0.051 - 10008, 2)
    assert c["2026-09-05"] == c["2026-09-06"] == c["2026-09-07"] == c["2026-09-08"] == real, c

    # a partial position: the lot's share of it, by shares (hand shares beside it)
    parts2 = {"2026-09-02": _part("2026-09-02", {"7733.T": [300, 30600.0, "JPY"]})}
    rows, stop = FL.filter_series(["2026-09-02"], [lot], parts2)
    assert rows == [["2026-09-02", round(30600 / 3 - 10008, 2)]] and stop is None, rows
    # ...and part of the lot sold: the rest held, the sold part realised
    half = _lot([["f1", "2026-09-02", 100, 2000.0, 160.0, "JPY", "JPY"]],
                [["s1", "2026-09-03", 40, 2100.0, 67.2, "JPY", "JPY"]])
    p3 = {"2026-09-02": _part("2026-09-02", {"7733.T": [100, 10200.0, "JPY"]}),
          "2026-09-03": _part("2026-09-03", {"7733.T": [60, 6300.0, "JPY"]})}
    rows, _ = FL.filter_series(["2026-09-02", "2026-09-03"], [half], p3)
    want = round(6300 + (40 * 2100 - 67.2) * 0.05 - 10008, 2)
    assert rows[1] == ["2026-09-03", want], (rows, want)

    # never guessed: a held day with no row, without the position, or with
    # fewer shares than the lot has no figure - it is left out, the days
    # around it keep theirs exactly, and the series goes on (the page steps
    # across it); `holes` names it
    full = FL.filter_series(ds, [lot], parts)[0]
    for broken, why in (
            ({}, "no row"),
            ({"2026-09-03": _part("2026-09-03", {"CRM": [5, 9000.0, "USD"]})}, "not in"),
            ({"2026-09-03": _part("2026-09-03", {"7733.T": [50, 5000.0, "JPY"]})}, "shares"),
    ):
        p = dict(parts)
        p.pop("2026-09-03")
        p.update(broken)
        holes = []
        rows, stop = FL.filter_series(ds, [lot], p, holes)
        assert rows == [r for r in full if r[0] != "2026-09-03"] and stop is None, \
            (why, rows, stop)
        assert [h[0] for h in holes] == ["2026-09-03"] and why in holes[0][1], (why, holes)
        # ...and the same hole on the last day ends the series there, saying why
        holes = []
        rows, stop = FL.filter_series(ds[:3], [lot], p, holes)
        assert [r[0] for r in rows] == ["2026-09-01", "2026-09-02"] and stop, (why, rows, stop)
        assert why in stop and holes == [["2026-09-03", stop]], (why, stop, holes)
    # no rate for the fill: no day from the fill on can be priced - every one
    # of them is a hole to the end, so the series stops after the day before
    p = dict(parts)
    p["2026-09-02"] = dict(p["2026-09-02"], fx={"HKD": 1.0, "USD": 7.8})
    holes = []
    rows, stop = FL.filter_series(ds, [lot], p, holes)
    assert [r[0] for r in rows] == ["2026-09-01"] and "no JPY rate" in stop, (rows, stop)
    assert [h[0] for h in holes] == ds[1:], holes
    # the newest breakdown counts only once it is the day-end one
    p = {d: parts[d] for d in ("2026-09-02", "2026-09-03")}
    p["2026-09-03"] = dict(p["2026-09-03"], ts="2026-09-03T18:25:11Z")
    rows, stop = FL.filter_series(["2026-09-02", "2026-09-03"], [lot], p)
    assert [r[0] for r in rows] == ["2026-09-02"] and "day-end" in stop, (rows, stop)
    assert FL.INTRADAY in stop, stop
    p["2026-09-03"]["ts"] = "2026-09-03T23:25:11Z"
    assert len(FL.filter_series(["2026-09-02", "2026-09-03"], [lot], p)[0]) == 2

    # nothing skipped: every day 0 - no breakdown needed - and the line is the account
    rows, stop = FL.filter_series(ds, [], {})
    assert rows == [[d, 0.0] for d in ds] and stop is None
    print("t7 the filter figure: a held lot marked to market, its share of a bigger "
          "position, realised once sold (sale-day rate), a day it cannot price left out "
          "(only a hole at the end stops it), the newest breakdown only once day-end, "
          "nothing skipped = zeros OK")


def _row(time, sym, qty, ccy="USD", status="sent", **kw):
    return dict({"time": time, "action": "BUY", "qty": qty, "symbol": sym, "limit": 1.0,
                 "ccy": ccy, "reason": "entry signal E, score 90", "status": status,
                 "error": ""}, **kw)


def t8_bot_lots_are_the_bots_own_filled_orders():
    rows = [
        _row("2026-07-20 09:00 UTC", "MS", 8, status=None),   # the first day: no status yet
        _row("2026-07-20 18:35 UTC", "MS", 8, status=None),   # placed again: this one filled
        _row("2026-07-20 18:35 UTC", "MS", 8, status=None),   # the same row in a later snapshot
        _row("2026-07-21 05:50 UTC", "WST", 5, status="ok"),
        _row("2026-07-21 05:55 UTC", "WST", 5, status="ok"),
        _row("2026-07-28 09:00 UTC", "UNH", 4, status="REJECTED"),
        _row("2026-08-21 23:35 UTC", "NTAP", 9, status="ok"),
        _row("2026-08-24 13:30 UTC", "NTAP", 9, status="ok", manual=True),   # its fill, by hand
        _row("2026-08-26 14:05 UTC", "URI", 1, status="ok", manual=True),    # a hand trade
        _row("2026-09-13 23:35 UTC", "BAYN", 33, ccy="EUR"),                # never filled
        dict(_row("2026-09-14 09:00 UTC", "X", 1), action="SELL"),
        dict(_row("2026-09-14 09:00 UTC", "Y", 1), reason="HKD->USD"),
    ]
    orders = FL.bot_orders(rows)
    assert [(o["time"][:16], o["symbol"]) for o in orders] == [
        ("2026-07-20 09:00", "MS"), ("2026-07-20 18:35", "MS"), ("2026-07-21 05:50", "WST"),
        ("2026-07-21 05:55", "WST"), ("2026-08-21 23:35", "NTAP"),
        ("2026-09-13 23:35", "BAYN")], orders
    ledger = FL.read_ledger("\n".join(r if isinstance(r, str) else json.dumps(r) for r in [
        _fill("e-ms", "2026-07-20", "MS", "BOT", 8, 211.0, "USD", 1.0),
        _fill("e-wst", "2026-07-21", "WST", "BOT", 25, 350.0, "USD", 1.0),
        _fill("m-ntap", "2026-08-24", "NTAP", "BOT", 9, 191.05, "USD", 1.0, "14:00"),
        _fill("m-uri", "2026-08-26", "URI", "BOT", 1, 1057.05, "USD", 1.0, "14:00"),
        _fill("x-wst", "2026-07-22", "WST", "SLD", 20, 357.2, "USD", 1.0),
        _fill("y-wst", "2026-07-27", "WST", "SLD", 5, 329.24, "USD", 1.0, "13:30"),
        {"execId": "fx", "date": "2026-07-23", "sec_type": "CASH", "side": "SLD",
         "symbol": "EUR", "qty": 895.3, "price": 1.1, "ccy": "USD"},
        "not json at all"]))
    assert [f["id"] for f in ledger] == ["e-ms", "e-wst", "x-wst", "y-wst", "m-ntap", "m-uri"]
    lots, hand = FL.match_lots(orders, ledger)
    got = {lot["key"]: [(f[0], f[2]) for f in lot["fills"]] for lot in lots}
    assert got == {
        "2026-07-20 18:35 UTC|MS": [("e-ms", 8)],          # the newer of the two orders
        "2026-07-21 05:55 UTC|WST": [("e-wst", 5)],        # one fill split over two orders
        "2026-07-21 05:50 UTC|WST": [("e-wst", 5)],
        "2026-08-21 23:35 UTC|NTAP": [("m-ntap", 9)]}, got   # the bot's order, booked by hand
    # the rest of WST's fill and URI are not the bot's; BAYN bought nothing
    assert hand == [["e-wst", "2026-07-21", "WST", 15], ["m-uri", "2026-08-26", "URI", 1]], hand
    # a fill the week after: no order of eight days before claims it
    late = FL.read_ledger(json.dumps(_fill("z", "2026-09-22", "BAYN", "BOT", 33, 48.0, "EUR", 3)))
    assert FL.match_lots(orders, late) == ([], [["z", "2026-09-22", "BAYN", 33]])
    # ...nor a fill timed BEFORE the order on the same day
    early = FL.read_ledger(json.dumps(_fill("w", "2026-09-13", "BAYN", "BOT", 33, 48.0, "EUR",
                                            3, "09:00")))
    assert FL.match_lots(orders, early)[0] == []
    # first in, first out: the bot's 2 x 5 WST go with the first sale
    FL.attach_sales(lots, ledger)
    sold = {lot["key"]: [(s[0], s[2]) for s in lot["sales"]] for lot in lots}
    assert sold["2026-07-21 05:55 UTC|WST"] == [("x-wst", 5)]
    assert sold["2026-07-21 05:50 UTC|WST"] == [("x-wst", 5)]
    assert sold["2026-07-20 18:35 UTC|MS"] == [] and sold["2026-08-21 23:35 UTC|NTAP"] == []
    # a hand lot bought FIRST is sold first: the bot's lot after it stays held
    led2 = FL.read_ledger("\n".join(json.dumps(r) for r in [
        _fill("h", "2026-10-01", "CRM", "BOT", 3, 240.0, "USD", 1.0, "14:00"),
        _fill("b", "2026-10-02", "CRM", "BOT", 7, 239.0, "USD", 1.0, "13:30"),
        _fill("s", "2026-10-05", "CRM", "SLD", 3, 250.0, "USD", 1.0, "13:30")]))
    lots2, hand2 = FL.match_lots([dict(_row("2026-10-01 23:35 UTC", "CRM", 7),
                                       qty=7.0, symbol="CRM")], led2)
    FL.attach_sales(lots2, led2)
    assert hand2 == [["h", "2026-10-01", "CRM", 3]] and lots2[0]["sales"] == [], lots2
    # the shadow record's symbols are Yahoo's; the History's are IB's
    assert [FL.ib_symbol(s) for s in ("7272.T", "0700.HK", "DBK.DE", "NOVO-B.CO", "MSFT")] == \
        ["7272", "700", "DBK", "NOVO-B", "MSFT"]
    assert [FL.ccy_of(s) for s in ("7272.T", "0700.HK", "DBK.DE", "AZN.L", "MSFT")] == \
        ["JPY", "HKD", "EUR", "GBP", "USD"]
    print("t8 bot lots: entry orders only (no rejected, hand-marked or unfilled), the "
          "newest order takes a fill, a split fill, a late or early fill unclaimed, hand "
          "trades left out, sales first in first out OK")


FDAYS = days("2026-10-19", "2026-10-25")


def _filter_data(folder=None, backfill=None, state=None):
    """A small, consistent set of the five files filter_line reads. Lots:
      7733 (JP) - in the backfill, skipped by the rule: bought 20 Oct, sold 22 Oct
      AAA (US)  - in the backfill, NOT skipped: bought 20 Oct, held
      7272 (JP) - after the backfill, in the bot's shadow record only: bought 23 Oct
      BBB (US)  - after the backfill, not in the shadow record (the bot let it be)
    plus a hand AAA buy that no order claims."""
    folder = Path(folder or tempfile.mkdtemp(prefix="sim_fdata_"))
    folder.mkdir(parents=True, exist_ok=True)
    bf = {"schema": 1, "orders_through": "2026-10-21 23:40 UTC", "first_live": None,
          "lots": [
              {"order": "2026-10-19 23:35 UTC", "symbol": "7733", "ccy": "JPY",
               "order_qty": 100, "skipped": True, "market": "JP", "pct": -1.25,
               "judged_by": "rule"},
              {"order": "2026-10-19 23:35 UTC", "symbol": "AAA", "ccy": "USD",
               "order_qty": 10, "skipped": False, "market": "US", "pct": 2.0,
               "judged_by": "rule"}],
          "unfilled": [{"order": "2026-10-20 23:35 UTC", "symbol": "CCC", "ccy": "EUR",
                        "qty": 5, "status": "sent"}], "not_bot": []}
    st = {"updated": "2026-10-25 23:25 UTC", "activity": [
              _row("2026-10-19 23:35 UTC", "7733", 100, ccy="JPY"),   # also in the backfill
              _row("2026-10-22 23:35 UTC", "7272", 100, ccy="JPY",
                   shadow="market filter would have skipped"),
              _row("2026-10-22 23:35 UTC", "BBB", 4)],
          "market_filter": {"mode": "shadow", "at": "2026-10-25 23:35 UTC", "markets": {},
                            "would_have_skipped": [{"time": "2026-10-22 23:35 UTC",
                                                    "symbol": "7272.T", "market": "JP",
                                                    "pct": -5.96}]}}
    led = [_fill("f-7733", "2026-10-20", "7733", "BOT", 100, 2000.0, "JPY", 160.0),
           _fill("f-aaa", "2026-10-20", "AAA", "BOT", 10, 100.0, "USD", 1.0, "13:30"),
           _fill("h-aaa", "2026-10-20", "AAA", "BOT", 5, 101.0, "USD", 1.0, "15:00"),
           _fill("s-7733", "2026-10-22", "7733", "SLD", 100, 2100.0, "JPY", 168.0),
           _fill("f-7272", "2026-10-23", "7272", "BOT", 100, 1500.0, "JPY", 120.0),
           _fill("f-bbb", "2026-10-23", "BBB", "BOT", 4, 50.0, "USD", 1.0, "13:30")]
    parts = {}
    for i, d in enumerate(FDAYS):
        pos = {}
        if "2026-10-20" <= d < "2026-10-22":
            pos["7733.T"] = [100, 10100.0 + 100 * i, "JPY"]
        if d >= "2026-10-20":
            pos["AAA"] = [15, 11800.0, "USD"]
        if d >= "2026-10-23":
            pos["7272.T"] = [100, 7600.0 + 50 * i, "JPY"]
            pos["BBB"] = [4, 1600.0, "USD"]
        parts[d] = _part(d, pos)
    hist = {"series": [{"d": d, "nl": 200000 + 100 * i, "exc": 0} for i, d in enumerate(FDAYS)],
            "flows": []}
    files = {"filter_backfill.json": bf if backfill is None else backfill,
             "bot_state.json": st if state is None else state,
             "day_parts.json": {"days": parts}, "netliq_history.json": hist}
    for name, obj in files.items():
        (folder / name).write_text(obj if isinstance(obj, str) else json.dumps(obj),
                                   encoding="utf-8")
    (folder / "fills_ledger.jsonl").write_text("\n".join(json.dumps(r) for r in led) + "\n",
                                               encoding="utf-8")
    return folder


def t9_the_filter_line_from_both_records():
    data = _filter_data()
    before = _snapshot(data)
    f = B.filter_line("2026-01-01", data)
    assert _snapshot(data) == before, "a data file was written"
    c = dict(f["series"])
    assert [r[0] for r in f["series"]] == FDAYS and f["stopped"] is None, f
    # 7733 by the backfill's rule, 7272 by the bot's shadow record - once each,
    # though both appear in the History too; AAA (above) and BBB (not tagged) are not
    assert [(l["sym"], l["mkt"], l["by"]) for l in f["lots"]] == [
        ("7733", "JP", "rule"), ("7272", "JP", "shadow")], f["lots"]
    cost33, cost72 = 100 * 2000 * 0.05 + 160 * 0.05, 100 * 1500 * 0.05 + 120 * 0.05
    real33 = 100 * 2100 * 0.05 - 168 * 0.05 - cost33
    assert c["2026-10-19"] == 0.0
    assert c["2026-10-20"] == round(10200.0 - cost33, 2), c
    assert c["2026-10-21"] == round(10300.0 - cost33, 2), c
    assert c["2026-10-22"] == round(real33, 2), "the sale is not realised on its day"
    assert c["2026-10-23"] == round(real33 + 7600 + 50 * 4 - cost72, 2), c
    assert c["2026-10-25"] == round(real33 + 7600 + 50 * 6 - cost72, 2), c
    l33, l72 = f["lots"]
    assert l33["sold"] == "2026-10-22" and l33["fill"] == "2026-10-20" and l33["qty"] == 100
    assert l33["pl"] == round(real33) and l72["sold"] is None
    assert l72["pl"] == round(7600 + 300 - cost72) and l72["pct"] == -5.96
    assert f["judged_through"] == "2026-10-21 23:40 UTC" and f["first_live"] is None
    assert f["shadow_mode"] == "shadow"

    # where both records list a lot, it is still one lot (the shadow's verdict)
    bf = json.loads((data / "filter_backfill.json").read_text(encoding="utf-8"))
    bf["lots"].append({"order": "2026-10-22 23:35 UTC", "symbol": "7272", "ccy": "JPY",
                       "order_qty": 100, "skipped": True, "market": "JP", "pct": -5.96,
                       "judged_by": "shadow"})
    f2 = B.filter_line("2026-01-01", _filter_data(backfill=bf))
    assert f2["series"] == f["series"] and len(f2["lots"]) == 2, f2["lots"]

    # nothing skipped anywhere: all zeros, so the page draws the account itself
    bf = json.loads((data / "filter_backfill.json").read_text(encoding="utf-8"))
    bf["lots"][0]["skipped"] = False
    st = json.loads((data / "bot_state.json").read_text(encoding="utf-8"))
    del st["market_filter"]                            # before the shadow bot's first run
    f3 = B.filter_line("2026-01-01", _filter_data(backfill=bf, state=st))
    assert f3["series"] == [[d, 0.0] for d in FDAYS] and f3["lots"] == [], f3
    assert f3["shadow_mode"] is None
    # the window: days before `first` are not published
    assert [r[0] for r in B.filter_line("2026-10-23", data)["series"]] == FDAYS[4:]
    print("t9 the filter line: the backfill's rule lot and the shadow record's lot, once "
          "each, marked to market and realised; nothing skipped = all zeros OK")


def _quiet_filter_line(data, first="2026-01-01"):
    """filter_line's result and what it printed."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        f = B.filter_line(first, data)
    return f, buf.getvalue()


def _edit_json(path, fn):
    obj = json.loads(Path(path).read_text(encoding="utf-8"))
    fn(obj)
    Path(path).write_text(json.dumps(obj), encoding="utf-8")


def t9b_a_hole_is_stepped_over_and_the_intraday_row_is_routine():
    ref, _ = _quiet_filter_line(_filter_data())
    # the newest breakdown (25 Oct) is still the intraday one while 7272 is
    # held: the series reaches the 24th, "stopped" says why - and no warning,
    # this is every hour of every day until the 23:25 day-end publish
    data = _filter_data()
    _edit_json(data / "day_parts.json",
               lambda o: o["days"]["2026-10-25"].update(ts="2026-10-25T18:25:11Z"))
    f, log = _quiet_filter_line(data)
    assert [r[0] for r in f["series"]] == FDAYS[:-1] and f["series"] == ref["series"][:-1], f
    assert f["stopped"] and FL.INTRADAY in f["stopped"] and f["holes"] == [], f["stopped"]
    assert "::warning::" not in log, log
    # a real hole while 7272 is held (no breakdown for the 24th): the day is
    # left out, every other day keeps its figure, the line runs to the end -
    # and the build log says so
    data = _filter_data()
    _edit_json(data / "day_parts.json", lambda o: o["days"].pop("2026-10-24"))
    f, log = _quiet_filter_line(data)
    assert f["series"] == [r for r in ref["series"] if r[0] != "2026-10-24"], f["series"]
    assert f["stopped"] is None and [h[0] for h in f["holes"]] == ["2026-10-24"], f
    assert log.count("::warning::") == 1 and "2026-10-24" in log and "steps across" in log, log
    # ...the same hole on the newest day is not the routine case: it stops the
    # line and warns
    data = _filter_data()
    _edit_json(data / "day_parts.json", lambda o: o["days"].pop("2026-10-25"))
    f, log = _quiet_filter_line(data)
    assert [r[0] for r in f["series"]] == FDAYS[:-1] and "no row" in f["stopped"], f
    assert "::warning::sim lines: the market filter line stops after 2026-10-24" in log, log
    # ...and so is an intraday newest row with a hole before it: both warned
    data = _filter_data()
    _edit_json(data / "day_parts.json", lambda o: (
        o["days"].pop("2026-10-24"), o["days"]["2026-10-25"].update(ts="2026-10-25T18:25:11Z")))
    f, log = _quiet_filter_line(data)
    assert [r[0] for r in f["series"]] == FDAYS[:-2] and f["stopped"], f
    assert "stops after 2026-10-23" in log, log
    print("t9b a missing breakdown inside the line is left out and stepped across (warned); "
          "the newest day still intraday stops it quietly; a hole at the end warns OK")


def t10_an_unreadable_record_is_filter_null_and_nothing_else():
    _write_prices()
    m1 = lambda url: B.validate_m1(_m1_record())
    good = _filter_data(B.DATA)
    out = Path(tempfile.mkdtemp(prefix="sim_out_")) / "sim_lines.json"
    ref = B.build(out_path=out, fetch=m1)
    assert ref["filter"] and ref["filter"]["series"], ref.get("filter_error")
    bad_rows = [{"time": "yesterday", "symbol": "7272.T", "market": "JP", "pct": -5.0},
                {"time": "2026-10-22 23:35 UTC", "symbol": "7272.T", "market": "MARS", "pct": 1},
                {"time": "2026-10-22 23:35 UTC", "symbol": "7272.T", "market": "JP",
                 "pct": "nan"}, "7272.T"]
    breaks = [("filter_backfill.json", "{not json"), ("filter_backfill.json", {"schema": 2}),
              ("filter_backfill.json", {"schema": 1, "lots": {}, "unfilled": []}),
              ("bot_state.json", [])]
    for r in bad_rows:
        st = json.loads((good / "bot_state.json").read_text(encoding="utf-8"))
        st["market_filter"]["would_have_skipped"] = [r]
        breaks.append(("bot_state.json", st))
    for name, content in breaks + [("day_parts.json", None), ("fills_ledger.jsonl", None)]:
        _filter_data(B.DATA)
        p = B.DATA / name
        if content is None:
            p.unlink()
        else:
            p.write_text(content if isinstance(content, str) else json.dumps(content),
                         encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            res = B.build(out_path=out, fetch=m1)
        assert res["filter"] is None and res.get("filter_error"), (name, content, res.get("filter"))
        assert "market filter line not built" in buf.getvalue(), buf.getvalue()
        same_m1 = {k: v for k, v in res["m1"].items() if k != "fetched_at"} == \
            {k: v for k, v in ref["m1"].items() if k != "fetched_at"}
        assert res["trend"] == ref["trend"] and same_m1, \
            "a broken filter record changed another line (%s)" % name
        assert res["complete_through"] == ref["complete_through"]
        assert json.loads(out.read_text(encoding="utf-8"))["filter"] is None
    print("t10 an unreadable backfill, shadow row, breakdown or ledger: filter null with a "
          "warning, the trend and M1 lines exactly as before OK")


def t11_the_committed_backfill_agrees_with_the_ledger_and_its_rule():
    bf = json.loads((REPO_DATA / "filter_backfill.json").read_text(encoding="utf-8"))
    assert bf["schema"] == 1 and bf["lots"] and bf["index"]["download_started"], bf.keys()
    ledger = FL.read_ledger((REPO_DATA / "fills_ledger.jsonl").read_text(encoding="utf-8"))
    orders = [{"time": r["order"], "symbol": r["symbol"], "ccy": r["ccy"],
               "qty": float(r.get("order_qty", r.get("qty")))}
              for r in bf["lots"] + bf["unfilled"]]
    orders.sort(key=lambda o: (FL.parse_time(o["time"]), o["symbol"]))
    lots, _hand = FL.match_lots(orders, ledger)
    again = {lot["key"]: [[f[0], f[1], round(f[2], 6)] for f in lot["fills"]] for lot in lots}
    for r in bf["lots"]:
        k = FL.order_key(r["order"], r["symbol"])
        assert again.get(k) == [[f[0], f[1], f[2]] for f in r["fills"]], \
            "%s: the ledger pairs other fills with this order now - re-run " \
            "build_filter_backfill.py" % k
        assert r["qty"] == round(sum(f[2] for f in r["fills"]), 6)
        # each verdict is what its own numbers say (close below the average)
        if r.get("judged_by") == "rule" and r.get("index"):
            assert r["skipped"] == (r["close"] < r["sma100"]) or \
                abs(r["close"] - r["sma100"]) < 0.01, r
            assert abs(r["pct"] - (r["close"] / r["sma100"] - 1) * 100) < 0.02, r
            assert r["index"] == BF.MT.MARKETS[r["market"]][0], r
            assert r["bar"] <= r["order"][:10], r
    # the owner's own buys are never bot lots
    keys = {(r["symbol"], r["fills"][0][1]) for r in bf["lots"]}
    for sym, day in (("URI", "2026-08-26"), ("MSFT", "2026-10-23")):
        assert (sym, day) not in keys, "%s on %s is a hand trade, not a bot lot" % (sym, day)
        assert any(h[2] == sym and h[1] == day for h in bf["not_bot"]), (sym, day)
    print("t11 the committed backfill: %d lots, each paired with the fills the ledger gives "
          "it, each verdict matching its own close and average, hand trades left out OK"
          % len(bf["lots"]))


def t12_the_backfill_judges_the_bar_finished_when_the_order_went_in():
    # S&P flat at 100 for 130 sessions, then 90 on Friday 23 Oct
    idx = pd.bdate_range(end="2026-10-23", periods=130)
    s = pd.Series([100.0] * 129 + [90.0], index=idx)
    closes = {m: s for m in ("US", "HK", "JP", "EU")}

    def o(t, ccy="USD", sym="CRM"):
        return {"time": t, "symbol": sym, "ccy": ccy, "qty": 1}
    # 19:00 UTC: New York still open (close 20:00 + 90 min) - Thursday's bar, level
    v = BF.judge(o("2026-10-23 19:00 UTC"), closes)
    assert v["bar"] == "2026-10-22" and v["skipped"] is False and v["pct"] == 0.0, v
    # 23:35 UTC: Friday's bar has settled - 90 against an average of 99.9: skipped
    v = BF.judge(o("2026-10-23 23:35 UTC"), closes)
    assert v["bar"] == "2026-10-23" and v["skipped"] is True and v["sma100"] == 99.9, v
    assert v["index"] == "^GSPC" and v["market"] == "US"
    # the market from the currency: every European one is EU, as market_of has it
    assert [BF.order_market(o("x", c, "X")) for c in
            ("JPY", "HKD", "EUR", "GBP", "CHF", "DKK", "SEK", "NOK", "AUD")] == \
        ["JP", "HK", "EU", "EU", "EU", "EU", "EU", "EU", None]
    # a US-listed fund has no home index in the backtest: never gated
    etf = BF.order_market(o("x", "USD", "SPY"))
    assert etf is None and BF.order_market(o("x", "USD", "CRM")) == "US", etf
    v = BF.judge(o("2026-10-23 23:35 UTC", "USD", "SPY"), closes)
    assert v["skipped"] is False and v["market"] is None
    # an index file that stopped updating: unknown, never gated (as the bot reads it)
    v = BF.judge(o("2026-11-20 23:35 UTC"), closes)
    assert v["skipped"] is False and "unknown" in v["why"], v
    print("t12 the backfill judges the index bar that had finished when the order went in, "
          "the market from the currency, funds and stale files never gated OK")


if __name__ == "__main__":
    t1_actions_map_to_a_long_only_slot()
    t2_a_weekend_signal_is_first_held_on_mondays_session()
    t3_one_cost_per_change_and_the_fee_only_while_held()
    t4_the_build_publishes_complete_days_only()
    t5_a_failed_fetch_is_m1_null_and_nothing_else()
    t6_the_ci_step_cannot_break_the_build()
    t7_the_filter_figure_held_sold_partial_and_never_guessed()
    t8_bot_lots_are_the_bots_own_filled_orders()
    t9_the_filter_line_from_both_records()
    t9b_a_hole_is_stepped_over_and_the_intraday_row_is_routine()
    t10_an_unreadable_record_is_filter_null_and_nothing_else()
    t11_the_committed_backfill_agrees_with_the_ledger_and_its_rule()
    t12_the_backfill_judges_the_bar_finished_when_the_order_went_in()
    print("ALL SIM-LINES TESTS PASS")
