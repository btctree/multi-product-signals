#!/usr/bin/env python3
"""Golden tests: data.json "market_trend" (engine/market_trend.py).

Run from this directory:  python test_market_trend.py

The per-country market filter (owner decision 2026-10-26, log only) needs
each home index against its 100-day simple average on its last FINISHED bar.
What must stay true:

  * FINISHED BARS ONLY: a bar is used only when the download behind the
    index's file STARTED at or after that market's close + 90 minutes
    (execution/market_clock.py's settle rule, the bot's own); a newer bar - the
    session still trading, or settling - is dropped and the row is judged on
    the bar before. A file with no recorded download start is not guessed at;
  * close < sma100 is "below", on the unrounded numbers, with the average
    taken over the last 100 finished closes INCLUDING the bar itself, as the
    simulator did; pct_vs_sma100 is in percent;
  * NEVER RAISES, never downloads: a missing file, under 100 bars, bad
    numbers, an exception from the start lookup - that market is left out with
    a ::warning::; no market at all gives {} and build_dashboard then writes
    no key. The build's frames are used first, the cached file otherwise.

Offline: yfinance is a stub that fails the test if called; every price file
goes to a temp dir.
"""
import contextlib
import datetime as dt
import io
import sys
import tempfile
import types
from pathlib import Path

import pandas as pd

_yf = types.ModuleType("yfinance")                  # never the network in a test


def _no_network(*a, **k):
    raise AssertionError("test reached the yfinance network stub")


_yf.download = _no_network
_yf.Ticker = _no_network
sys.modules["yfinance"] = _yf

import data_fetch as D                              # noqa: E402
import market_trend as M                            # noqa: E402

D.PRICE_DIR = Path(tempfile.mkdtemp(prefix="trend_prices_"))   # never data/prices
HERE = Path(__file__).resolve().parent


def utc(s):
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc)


def weekday_closes(last_day, n, px=100.0, last_px=None):
    """n weekday closes ending on last_day, all px except the newest."""
    days, d = [], dt.date.fromisoformat(last_day)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= dt.timedelta(days=1)
    days.reverse()
    vals = [px] * n
    if last_px is not None:
        vals[-1] = last_px
    return pd.DataFrame({"Open": vals, "High": vals, "Low": vals, "Close": vals,
                         "Volume": [0] * n}, index=pd.to_datetime(days))


def quiet(fn, *a, **k):
    """fn's result and what it printed."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = fn(*a, **k)
    return out, buf.getvalue()


# --------------------------------------------------------------- finished bars --
def t1_a_bar_still_in_session_is_dropped():
    # Monday 2026-10-26 (BST over, Europe on CET; New York still on EDT until
    # 1 Nov). Every index's newest bar is Monday's, a 10% drop below a flat 100.
    frames = {t: weekday_closes("2026-10-26", 150, 100.0, 90.0)
              for t, _ in M.MARKETS.values()}
    cases = [
        # (download start UTC, market, judged on bar)
        ("2026-10-26 07:00", "JP", "2026-10-23"),   # Tokyo 16:00, settles 17:00 JST
        ("2026-10-26 08:00", "JP", "2026-10-26"),   # 17:00 JST: final
        ("2026-10-26 09:00", "HK", "2026-10-23"),   # HK 17:00, settles 17:40 HKT
        ("2026-10-26 09:40", "HK", "2026-10-26"),
        ("2026-10-26 17:59", "EU", "2026-10-23"),   # 18:59 CET, settles 19:00
        ("2026-10-26 18:00", "EU", "2026-10-26"),
        ("2026-10-26 21:29", "US", "2026-10-23"),   # 17:29 EDT, settles 17:30
        ("2026-10-26 21:30", "US", "2026-10-26"),
        ("2026-10-25 12:00", "US", "2026-10-23"),   # a Sunday build: Friday's bar
    ]
    for at, market, bar in cases:
        out, _ = quiet(M.market_trend, frames, started_of=lambda t, at=at: utc(at))
        row = out[market]
        assert row["bar"] == bar, (at, market, row)
        # the finished bar decides: Friday's flat close sits ON its average,
        # Monday's drop sits 10% under the average that includes it
        if bar == "2026-10-26":
            assert row["below"] is True and row["close"] == 90.0, row
            assert row["sma100"] == 99.9 and row["pct_vs_sma100"] == -9.91, row
        else:
            assert row["below"] is False and row["close"] == 100.0, row
            assert row["pct_vs_sma100"] == 0.0, row
    print("t1 a bar still in session or settling is dropped; the row is judged on "
          "the last finished bar OK")


def t2_below_and_above_on_the_unrounded_numbers():
    s = weekday_closes("2026-10-23", 120, 100.0)
    s.iloc[-100:, s.columns.get_loc("Close")] = [100.0 + k for k in range(100)]
    frames = {"^GSPC": s}
    at = lambda t: utc("2026-10-24 12:00")
    out, _ = quiet(M.market_trend, frames, started_of=at)
    us = out["US"]
    # last 100 closes 100..199, mean 149.5, close 199: +33.11%
    assert us == {"index": "^GSPC", "name": "S&P 500", "close": 199.0, "sma100": 149.5,
                  "pct_vs_sma100": 33.11, "below": False, "bar": "2026-10-23"}, us
    # below by less than the rounding: still below, as the simulator says
    s2 = weekday_closes("2026-10-23", 120, 100.0, 99.999)
    out, _ = quiet(M.market_trend, {"^N225": s2}, started_of=at)
    jp = out["JP"]
    assert jp["below"] is True and jp["close"] == 100.0 and jp["pct_vs_sma100"] == -0.0, jp
    # exactly on the average is not below
    out, _ = quiet(M.market_trend, {"^HSI": weekday_closes("2026-10-23", 120)}, started_of=at)
    assert out["HK"]["below"] is False, out["HK"]
    print("t2 below = close < its 100-day average (the bar included), unrounded; "
          "pct in percent OK")


def t3_missing_or_bad_data_leaves_the_market_out_and_never_raises():
    at = lambda t: utc("2026-10-24 12:00")
    good = weekday_closes("2026-10-23", 150, 100.0, 95.0)
    nan = good.copy()
    nan["Close"] = float("nan")
    cases = {
        "no frame, no file": ({}, at, "FileNotFoundError"),
        "too few bars": ({"^GSPC": weekday_closes("2026-10-23", 99)}, at, "only 99"),
        "no recorded start": ({"^GSPC": good}, lambda t: None, "no recorded download start"),
        "start lookup raises": ({"^GSPC": good}, lambda t: 1 / 0, "ZeroDivisionError"),
        "no Close column": ({"^GSPC": good.drop(columns=["Close"])}, at, "KeyError"),
        "all NaN": ({"^GSPC": nan}, at, "only 0"),
        "not a frame": ({"^GSPC": "garbage"}, at, "Error"),
        # Yahoo stopped updating: newest bar 14 Oct, last finished day 23 Oct
        "stale index": ({"^GSPC": weekday_closes("2026-10-14", 150)}, at, "stopped updating"),
    }
    for name, (frames, started, why) in cases.items():
        out, printed = quiet(M.market_trend, frames, started_of=started)
        assert "US" not in out, (name, out)
        line = [l for l in printed.splitlines() if l.startswith("::warning::market trend: US")]
        assert line and why in line[0], (name, printed)
    # a holiday-length gap (5 weekdays) is still a current bar
    out, printed = quiet(M.market_trend, {"^GSPC": weekday_closes("2026-10-16", 150)}, started_of=at)
    assert "US" in out and "::warning::market trend: US" not in printed, printed
    # one market's trouble never costs another its row
    out, _ = quiet(M.market_trend, {"^GSPC": good, "^N225": "garbage"}, started_of=at)
    assert "US" in out and "JP" not in out, out
    # nothing judged at all: {} - and build_dashboard writes the key only when
    # there is something in it
    out, printed = quiet(M.market_trend, {}, started_of=at)
    assert out == {} and printed.count("::warning::") == 4, printed
    src = (HERE / "build_dashboard.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert "    if trend:" in src and 'payload["market_trend"] = trend' in src, \
        "build_dashboard must leave the key out when no market was judged"
    assert "trend = market_trend(data)" in src and "trend = {}" in src, \
        "build_dashboard must guard the call and fall back to no key"
    print("t3 a missing file, short history, bad numbers or no download start "
          "leaves that market out with a warning; never raises OK")


def t4_the_cached_file_is_read_and_nothing_is_downloaded():
    df = weekday_closes("2026-10-23", 130, 100.0, 120.0)
    df.to_csv(D.cache_path("^STOXX50E"))
    # the recorded start, as data_fetch writes it
    D._record_download_start(["^STOXX50E"], "2026-10-24T08:00:00Z")
    out, printed = quiet(M.market_trend)                  # no frames: the cache
    eu = out["EU"]
    assert eu["index"] == "^STOXX50E" and eu["close"] == 120.0 and eu["below"] is False, eu
    assert eu["bar"] == "2026-10-23", eu
    # the build's own frame wins over the file
    out, _ = quiet(M.market_trend, {"^STOXX50E": weekday_closes("2026-10-23", 130, 100.0, 80.0)})
    assert out["EU"]["close"] == 80.0 and out["EU"]["below"] is True, out["EU"]
    # a cached file without a recorded start is not guessed at
    weekday_closes("2026-10-23", 130).to_csv(D.cache_path("^HSI"))
    out, printed = quiet(M.market_trend)
    assert "HK" not in out and "no recorded download start" in printed, printed
    print("t4 the cached price file is read when the build has no frame; nothing is "
          "downloaded OK")


def t5_the_session_table_is_the_bots():
    mc = M._market_clock()
    import importlib.util
    assert mc.SESSION_SETTLE_MIN == 90
    assert mc.MARKET_SESSIONS[".T"] == ("Asia/Tokyo", (9, 0), (15, 30))
    # loaded by path: nothing under execution/ joined the build's import path
    assert not any(str(p).replace("\\", "/").rstrip("/").endswith("/execution")
                   for p in sys.path), sys.path
    assert "market_clock" not in sys.modules, "loaded under its own name"
    assert importlib.util.find_spec("_mps_market_clock") is None
    print("t5 the settle rule is execution/market_clock.py's own, loaded by path OK")


if __name__ == "__main__":
    t1_a_bar_still_in_session_is_dropped()
    t2_below_and_above_on_the_unrounded_numbers()
    t3_missing_or_bad_data_leaves_the_market_out_and_never_raises()
    t4_the_cached_file_is_read_and_nothing_is_downloaded()
    t5_the_session_table_is_the_bots()
    print("ALL MARKET-TREND TESTS PASS")
