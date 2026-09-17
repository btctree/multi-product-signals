#!/usr/bin/env python3
"""Golden tests: a batch download never invents a price bar, and Tokyo's null
close is still filled.

Run from this directory:  python test_data_fetch.py

Review 2026-09-17: fetch_all downloaded 80-ticker chunks in universe order
(chunk 0 = 50 US + 30 HK). yf.download reindexes a batch onto the UNION of its
tickers' dates, so between the Hong Kong and US opens every one of those US
names got an all-NaN row dated today, and _fill_last_close (8b98d34, the
5301.T fix) filled it O=H=L=C from yesterday's close. ATR fell by 1/14 and
ib_bot saved a trailing stop 0.25 ATR tighter. Now batches never mix calendars,
and a missing close is only filled with evidence the ticker's own exchange
traded that day.

Offline: yfinance is replaced by a stub before data_fetch is imported, so no
test can reach Yahoo, and every price file goes to a temp dir.
"""
import json
import sys
import tempfile
import types
from pathlib import Path

import numpy as np
import pandas as pd

_yf = types.ModuleType("yfinance")                  # never the network in a test


def _no_network(*a, **k):
    raise AssertionError("test reached the yfinance network stub")


_yf.download = _no_network
_yf.Ticker = _no_network
sys.modules["yfinance"] = _yf

import data_fetch as D                              # noqa: E402
from indicators import atr as ind_atr              # noqa: E402
from production import analyze                     # noqa: E402
from universe import market_of                     # noqa: E402

D.PRICE_DIR = Path(tempfile.mkdtemp(prefix="prices_"))   # never data/prices


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


def bars(last_day, n=300, seed=1, px=100.0, weekdays=True):
    """Synthetic daily OHLCV ending on last_day (naive dates, as yf.download
    returns them for daily bars)."""
    freq = "B" if weekdays else "D"
    idx = pd.date_range(end=pd.Timestamp(last_day), periods=n, freq=freq)
    rng = np.random.default_rng(seed)
    close = px * np.exp(np.cumsum(rng.normal(0.0008, 0.015, n)))
    open_ = close * (1 + rng.normal(0, 0.004, n))
    high = np.maximum(open_, close) * (1 + rng.uniform(0.001, 0.02, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0.001, 0.02, n))
    vol = rng.integers(1_000_000, 5_000_000, n).astype(float)
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close,
                         "Volume": vol}, index=idx)


def union_batch(frames):
    """What yfinance 1.5.1's download() builds for group_by='ticker':
    reindex_dfs() puts every ticker on the union of all dates, then concat."""
    idx = None
    for df in frames.values():
        idx = df.index if idx is None else idx.union(df.index)
    return pd.concat([df.reindex(idx) for df in frames.values()], axis=1,
                     keys=list(frames.keys()), names=["Ticker", "Price"])


class FakeTicker:
    """yf.Ticker as _live_quote reads it: fast_info + history metadata."""
    looked_up = []

    def __init__(self, ticker, quotes):
        FakeTicker.looked_up.append(ticker)
        q = quotes.get(ticker)
        if q is None:
            raise RuntimeError("no quote")
        self.fast_info = {"last_price": q["px"]}
        self._md = {k: v for k, v in q.items() if k != "px"}

    def get_history_metadata(self):
        return self._md


def quotes(**by_ticker):
    FakeTicker.looked_up = []
    table = {k.replace("_", "."): v for k, v in by_ticker.items()}
    return lambda t: FakeTicker(t, table)


def epoch(utc):
    return int(pd.Timestamp(utc, tz="UTC").timestamp())


def t1_mixed_calendar_union_row_is_not_filled():
    us = bars("2026-09-16", seed=2)                  # Wed close; US not open yet
    hk = bars("2026-09-17", seed=3)                  # HK already trading Thu
    sub = union_batch({"AAPL": us, "0700.HK": hk})["AAPL"]
    assert len(sub) == len(us) + 1 and sub.iloc[-1].isna().all(), "premise: phantom row"

    last = float(us["Close"].iloc[-1])
    with Patch(D.yf, Ticker=quotes(AAPL={"px": last,
                                         "regularMarketTime": epoch("2026-09-16 20:00"),
                                         "exchangeTimezoneName": "America/New_York"})):
        out = D._clean(sub.copy(), "AAPL")
    assert out.index[-1] == pd.Timestamp("2026-09-16"), out.tail(2)
    assert len(out) == len(us), (len(out), len(us))
    pd.testing.assert_frame_equal(out, us, check_freq=False, check_names=False)

    # weekend: an HK name batched with crypto gets a Saturday row
    hk2 = bars("2026-09-18", seed=4)
    btc = bars("2026-09-19", seed=5, weekdays=False)
    sub = union_batch({"BTC-USD": btc, "0700.HK": hk2})["0700.HK"]
    assert sub.index[-1] == pd.Timestamp("2026-09-19") and sub.iloc[-1].isna().all()
    with Patch(D.yf, Ticker=quotes(**{"0700_HK": {
            "px": float(hk2["Close"].iloc[-1]),
            "regularMarketTime": epoch("2026-09-18 08:08"),
            "exchangeTimezoneName": "Asia/Hong_Kong"}})):
        out = D._clean(sub.copy(), "0700.HK")
    assert out.index[-1] == pd.Timestamp("2026-09-18") and len(out) == len(hk2)

    # a quote with no trade time is no evidence either, nor is a zero volume
    # (a yfinance that zero-filled Volume after the reindex must not reopen this)
    zero = union_batch({"AAPL": us, "0700.HK": hk})["AAPL"].copy()
    zero.iloc[-1, zero.columns.get_loc("Volume")] = 0.0
    with Patch(D.yf, Ticker=quotes(AAPL={"px": last})):
        out = D._clean(union_batch({"AAPL": us, "0700.HK": hk})["AAPL"].copy(), "AAPL")
        assert len(out) == len(us)
        out = D._clean(zero, "AAPL")
    assert len(out) == len(us)
    print("t1 an empty row made by batching with another market is dropped, not filled OK")


def t2_tokyo_null_close_is_still_filled():
    # 8b98d34's verified shape: 28 Aug 1,681.5, then a 31 Aug row with every
    # field null, which only survives yfinance as a union row from Tokyo peers
    jp = bars("2026-08-28", seed=6, px=1600.0)
    jp.iloc[-1, jp.columns.get_loc("Close")] = 1681.5
    peer = bars("2026-08-31", seed=7, px=3000.0)
    sub = union_batch({"5301.T": jp, "7203.T": peer})["5301.T"]
    assert sub.index[-1] == pd.Timestamp("2026-08-31") and sub.iloc[-1].isna().all()
    tokyo = {"px": 1811.0, "regularMarketTime": epoch("2026-08-31 06:30"),
             "exchangeTimezoneName": "Asia/Tokyo"}
    with Patch(D.yf, Ticker=quotes(**{"5301_T": tokyo})):
        out = D._clean(sub.copy(), "5301.T")
    assert out.index[-1] == pd.Timestamp("2026-08-31"), out.tail(2)
    assert len(out) == len(jp) + 1
    assert list(out.iloc[-1][["Open", "High", "Low", "Close"]]) == [1811.0] * 4
    assert out["Close"].iloc[-2] == 1681.5

    # the same, with regularMarketTime already turned into a Timestamp
    pre = dict(tokyo, regularMarketTime=pd.Timestamp("2026-08-31 15:30", tz="Asia/Tokyo"))
    with Patch(D.yf, Ticker=quotes(**{"5301_T": pre})):
        out = D._clean(sub.copy(), "5301.T")
    assert out["Close"].iloc[-1] == 1811.0

    # Yahoo sent part of the bar (Open/High/Low, no Close): filled even when
    # the quote carries no time, and the parts Yahoo sent are kept
    part = jp.copy()
    part.loc[pd.Timestamp("2026-08-31")] = [1700.0, 1820.0, 1695.0, np.nan, np.nan]
    with Patch(D.yf, Ticker=quotes(**{"5301_T": {"px": 1811.0}})):
        out = D._clean(part.copy(), "5301.T")
    assert list(out.iloc[-1][["Open", "High", "Low", "Close"]]) == [1700.0, 1820.0, 1695.0, 1811.0]

    # volume alone is evidence too
    vol = jp.copy()
    vol.loc[pd.Timestamp("2026-08-31")] = [np.nan, np.nan, np.nan, np.nan, 2_400_000.0]
    with Patch(D.yf, Ticker=quotes(**{"5301_T": {"px": 1811.0}})):
        out = D._clean(vol.copy(), "5301.T")
    assert out.index[-1] == pd.Timestamp("2026-08-31") and out["Close"].iloc[-1] == 1811.0

    # no quote at all: dropped, exactly as before 8b98d34
    with Patch(D.yf, Ticker=quotes()):
        out = D._clean(sub.copy(), "5301.T")
    assert out.index[-1] == pd.Timestamp("2026-08-28") and out["Close"].iloc[-1] == 1681.5

    # the date is read on the EXCHANGE's clock: 23:00 UTC on the 16th is the
    # 17th in Auckland, so a 17th row is that market's own session
    nz = bars("2026-09-16", seed=8, px=30.0)
    sub_nz = union_batch({"FPH.NZ": nz, "X.NZ": bars("2026-09-17", seed=9)})["FPH.NZ"]
    with Patch(D.yf, Ticker=quotes(**{"FPH_NZ": {
            "px": 31.2, "regularMarketTime": epoch("2026-09-16 23:00"),
            "exchangeTimezoneName": "Pacific/Auckland"}})):
        out = D._clean(sub_nz.copy(), "FPH.NZ")
    assert out.index[-1] == pd.Timestamp("2026-09-17") and out["Close"].iloc[-1] == 31.2

    # a name that traded TODAY never has today's price stamped on an empty
    # row for yesterday (halted yesterday, reopened this morning)
    halted = bars("2026-09-15", seed=10)
    sub_h = union_batch({"0005.HK": halted, "0700.HK": bars("2026-09-16", seed=11)})["0005.HK"]
    with Patch(D.yf, Ticker=quotes(**{"0005_HK": {
            "px": 99.0, "regularMarketTime": epoch("2026-09-17 02:00"),
            "exchangeTimezoneName": "Asia/Hong_Kong"}})):
        out = D._clean(sub_h.copy(), "0005.HK")
    assert out.index[-1] == pd.Timestamp("2026-09-15") and len(out) == len(halted)
    print("t2 Tokyo's null close is still filled from a same-day quote; no evidence, no bar OK")


def t3_chunks_never_mix_markets():
    US_FAMILY = {"US", "ETF", "BOND", "LEV", "MACRO"}   # all NYSE/Nasdaq listings

    def calendar(t):                                # independent of data_fetch
        m = market_of(t)
        if m in US_FAMILY:
            return "US"
        if m == "EU":
            return "EU" + t.rsplit(".", 1)[1]
        if m in ("INDEX", "COMMODITY"):
            return t
        return m

    uni = json.loads((Path(__file__).resolve().parent.parent / "data" / "universe.json")
                     .read_text(encoding="utf-8"))["tickers"]      # read only
    chunks = D._download_chunks(uni, 80)
    flat = [t for c in chunks for t in c]
    assert sorted(flat) == sorted(uni) and len(flat) == len(uni), "every ticker exactly once"
    assert max(map(len, chunks)) == 80
    for c in chunks:
        assert len({calendar(t) for t in c}) == 1, c[:6]
        assert c == [t for t in uni if t in set(c)], "universe order kept"

    must_differ = ["AAPL", "0700.HK", "7203.T", "SAP.DE", "AZN.L", "NESN.SW", "ASML.AS",
                   "BTC-USD", "^GSPC", "^HSI", "^N225", "GC=F", "KC=F", "EURUSD=X",
                   "000001.SS"]
    assert len({D._calendar_group(t) for t in must_differ}) == len(must_differ)
    assert {D._calendar_group(t) for t in
            ("AAPL", "SPY", "TLT", "TQQQ", "CPER", "BRK-B", "BF-B")} == {"US"}
    assert D._calendar_group("NOVO-B.CO") == "CO" and D._calendar_group("ETH-USD") == "CRYPTO"

    # fetch_all itself: an interleaved universe, a stubbed Yahoo that builds
    # batches the way yfinance does, and a failed batch still falls back
    frames, order = {}, []
    for k in range(90):
        order.append(f"U{k}")
        frames[f"U{k}"] = bars("2026-09-16", seed=100 + k)
        if k % 3 == 0:
            t = f"{1000 + k}.T"
            order.append(t)
            frames[t] = bars("2026-09-17", seed=300 + k)
        if k % 7 == 0:
            t = f"{k:04d}.HK"
            order.append(t)
            frames[t] = bars("2026-09-17", seed=500 + k)
    for k in range(55):
        frames[f"{2000 + k}.T"] = bars("2026-09-17", seed=700 + k)
        order.append(f"{2000 + k}.T")
    order += ["BTC-USD", "ETH-USD"]
    frames["BTC-USD"] = bars("2026-09-19", seed=900, weekdays=False)
    frames["ETH-USD"] = bars("2026-09-19", seed=901, weekdays=False)

    calls, singles = [], []

    def fake_download(tickers, **kw):
        if isinstance(tickers, str):                # fetch_one's per-ticker call
            singles.append(tickers)
            return frames[tickers].copy()
        calls.append(list(tickers))
        if any(t.endswith(".HK") for t in tickers):
            raise RuntimeError("Yahoo 429")
        return union_batch({t: frames[t] for t in tickers})

    with Patch(D, load_universe=lambda: {"tickers": order}), \
            Patch(D.yf, download=fake_download, Ticker=quotes()):
        out = D.fetch_all(force=True)
    for c in calls:
        assert len(c) <= 80 and len({calendar(t) for t in c}) == 1, c[:5]
    assert sorted(t for c in calls for t in c) == sorted(order)
    assert sorted(singles) == sorted(t for t in order if t.endswith(".HK")), "fallback kept"
    assert set(out) == set(order)
    for t in order:
        assert len(out[t]) == len(frames[t]) and out[t].index[-1] == frames[t].index[-1], t
    assert FakeTicker.looked_up == [], "no quote lookups: no batch produced an empty row"
    assert all(p.parent == D.PRICE_DIR for p in D.PRICE_DIR.iterdir())
    print("t3 download chunks never mix calendars; size 80 and per-ticker fallback kept OK")


def t4_atr_unchanged_by_phantom_union_row():
    us = bars("2026-09-16", n=320, seed=42, px=900.0)
    hk = bars("2026-09-17", n=320, seed=43)
    alone = D._clean(us.copy(), "LLY")
    sub = union_batch({"LLY": us, "1810.HK": hk})["LLY"]
    last = float(us["Close"].iloc[-1])
    with Patch(D.yf, Ticker=quotes(LLY={"px": last,
                                        "regularMarketTime": epoch("2026-09-16 20:00"),
                                        "exchangeTimezoneName": "America/New_York"})):
        batched = D._clean(sub.copy(), "LLY")
    a0, a1 = analyze("LLY", alone), analyze("LLY", batched)
    assert a1["atr"] == a0["atr"] and a1["price"] == a0["price"], (a0["atr"], a1["atr"])
    assert float(ind_atr(batched, 14).iloc[-1]) == float(ind_atr(alone, 14).iloc[-1])

    # what the guard prevents: the 8b98d34 fill on that row (O=H=L=C = last
    # close, true range 0) leaves the price alone but cuts ATR to 13/14
    legacy = sub.copy()
    legacy.loc[legacy.index[-1], ["Open", "High", "Low", "Close"]] = last
    legacy = legacy.dropna(subset=["Close"])
    a2 = analyze("LLY", legacy)
    assert a2["price"] == a0["price"]
    ratio = float(ind_atr(legacy, 14).iloc[-1]) / float(ind_atr(alone, 14).iloc[-1])
    assert abs(ratio - 13 / 14) < 1e-9, ratio
    print(f"t4 card ATR {a0['atr']} unchanged by a phantom row (legacy fill gave {a2['atr']}) OK")


if __name__ == "__main__":
    t1_mixed_calendar_union_row_is_not_filled()
    t2_tokyo_null_close_is_still_filled()
    t3_chunks_never_mix_markets()
    t4_atr_unchanged_by_phantom_union_row()
    print("ALL DATA-FETCH TESTS PASS")
