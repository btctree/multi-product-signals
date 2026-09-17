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

Review 2026-09-17, second pass: calendar batching left names alone in a batch
(ABI.BR, NDA-FI.HE) with no peer to rebuild the all-null newest bar yfinance
dropped, so the 5301.T fill could never fire for them. Both downloads now pass
keepna=True, so the evidence test decides for every name (t5); _clean still
drops every other empty row and any close <= 0 (t6); Euronext's venues share a
batch and each calendar splits into even batches (t3). And build_dashboard
stamps data.json and every card with "generated_at", the UTC time the price
download started, recorded beside the price files by data_fetch (t7).

Final review 2026-09-17: a newest bar Yahoo left with no close came out O=H=L=C.
auto_adjust multiplies Open/High/Low by AdjClose/Close, NaN without a close, so
the range Yahoo sent never reached the fill, and every EU bar still null-close
at the 23:35 decision cut ATR by 3-6%. The fill now takes the quote's day
high/low when the quote is the bar's own day, else px as before (t8).

Offline: yfinance is replaced by a stub before data_fetch is imported, so no
test can reach Yahoo, and every price file and dashboard file goes to a temp
dir.
"""
import datetime as dt
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


US_FAMILY = {"US", "ETF", "BOND", "LEV", "MACRO"}       # all NYSE/Nasdaq listings
EURONEXT = {"PA", "AS", "BR", "LS"}                     # one harmonised calendar


def calendar(t):                                        # independent of data_fetch
    m = market_of(t)
    if m in US_FAMILY:
        return "US"
    if m == "EU":
        suffix = t.rsplit(".", 1)[1]
        return "EURONEXT" if suffix in EURONEXT else "EU" + suffix
    if m in ("INDEX", "COMMODITY"):
        return t
    return m


def yahoo_like(frames, tickers, keepna):
    """What yfinance 1.5.1's download() hands back for these tickers: each
    ticker's own rows first (history(): Volume.fillna(0), then with
    keepna=False every row whose fields are all NaN or zero is DROPPED), then
    - for a list - reindex_dfs() onto the union of their dates."""
    own = {}
    for t in ([tickers] if isinstance(tickers, str) else tickers):
        df = frames[t].copy()
        df["Volume"] = df["Volume"].fillna(0)
        if not keepna:
            cols = ["Open", "High", "Low", "Close", "Volume"]
            df = df[~(df[cols].isna() | (df[cols] == 0)).all(axis=1)]
        own[t] = df
    if isinstance(tickers, str):
        return own[tickers]
    return union_batch(own)


def t3_chunks_never_mix_markets():
    uni = json.loads((Path(__file__).resolve().parent.parent / "data" / "universe.json")
                     .read_text(encoding="utf-8"))["tickers"]      # read only
    chunks = D._download_chunks(uni, 80)
    flat = [t for c in chunks for t in c]
    assert sorted(flat) == sorted(uni) and len(flat) == len(uni), "every ticker exactly once"
    assert max(map(len, chunks)) <= 80
    for c in chunks:
        assert len({calendar(t) for t in c}) == 1, c[:6]
        assert c == [t for t in uni if t in set(c)], "universe order kept"

    # EVEN batches: ceil(n/80) per calendar, sizes within one of each other -
    # no tail of 2 or 3 names (review 2026-09-17: the last 3 HK names, RKLB and
    # NBIS, and ABI.BR alone)
    by_cal = {}
    for c in chunks:
        by_cal.setdefault(calendar(c[0]), []).append(len(c))
    for cal, sizes in by_cal.items():
        n = sum(sizes)
        assert len(sizes) == -(-n // 80), (cal, sizes)
        assert max(sizes) - min(sizes) <= 1, (cal, sizes)
    assert by_cal["HK"] == [42, 41] and len(by_cal["US"]) == 8, (by_cal["HK"], by_cal["US"])
    # Euronext pooled: ABI.BR (Brussels) shares a batch with Paris and Amsterdam
    abi = [c for c in chunks if "ABI.BR" in c][0]
    assert len(abi) > 1 and any(t.endswith(".PA") for t in abi) \
        and any(t.endswith(".AS") for t in abi), abi
    assert D._calendar_group("NDA-FI.HE") == "HE", "Helsinki keeps its own calendar"

    must_differ = ["AAPL", "0700.HK", "7203.T", "SAP.DE", "AZN.L", "NESN.SW", "ASML.AS",
                   "BTC-USD", "^GSPC", "^HSI", "^N225", "GC=F", "KC=F", "EURUSD=X",
                   "000001.SS", "NDA-FI.HE", "ISP.MI", "SAN.MC", "EQNR.OL", "OMV.VI"]
    assert len({D._calendar_group(t) for t in must_differ}) == len(must_differ)
    assert {D._calendar_group(t) for t in
            ("AAPL", "SPY", "TLT", "TQQQ", "CPER", "BRK-B", "BF-B")} == {"US"}
    assert {D._calendar_group(t) for t in
            ("MC.PA", "ASML.AS", "ABI.BR", "EDP.LS", "ai.pa")} == {"EURONEXT"}
    assert D._calendar_group("NOVO-B.CO") == "CO" and D._calendar_group("ETH-USD") == "CRYPTO"
    # the split itself: 81 -> 41+40, 160 -> 80+80, 161 -> 54+54+53, 1 -> 1
    names = [f"N{k}" for k in range(161)]
    assert [len(c) for c in D._download_chunks(names[:81], 80)] == [41, 40]
    assert [len(c) for c in D._download_chunks(names[:160], 80)] == [80, 80]
    assert [len(c) for c in D._download_chunks(names, 80)] == [54, 54, 53]
    assert D._download_chunks(names[:1], 80) == [["N0"]]
    assert D._download_chunks([], 80) == []

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

    calls, singles, keepnas = [], [], []

    def fake_download(tickers, **kw):
        keepnas.append(kw.get("keepna"))
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
    assert singles and keepnas and all(k is True for k in keepnas), \
        "both yf.download calls must pass keepna=True: %s" % keepnas
    for c in calls:
        assert len(c) <= 80 and len({calendar(t) for t in c}) == 1, c[:5]
    assert sorted(t for c in calls for t in c) == sorted(order)
    assert sorted(singles) == sorted(t for t in order if t.endswith(".HK")), "fallback kept"
    assert set(out) == set(order)
    for t in order:
        assert len(out[t]) == len(frames[t]) and out[t].index[-1] == frames[t].index[-1], t
    assert FakeTicker.looked_up == [], "no quote lookups: no batch produced an empty row"
    assert all(p.parent == D.PRICE_DIR for p in D.PRICE_DIR.iterdir())
    print("t3 chunks never mix calendars; even batches <= 80, Euronext pooled, keepna, fallback OK")


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


def null_newest(last_day, drop_day, seed, px=20.0):
    """bars up to drop_day's session, then a newest row for last_day with EVERY
    price null - the 5301.T 31 Aug shape (Volume is NaN; yfinance fills it 0)."""
    df = bars(drop_day, seed=seed, px=px)
    df.loc[pd.Timestamp(last_day)] = [np.nan] * 5
    return df


def t5_singleton_null_newest_bar_is_decided_by_evidence():
    # NDA-FI.HE (Helsinki) is the only name on its calendar: a batch of one, no
    # peer to rebuild a row yfinance drops. Yahoo leaves its 16 Sep bar all-null.
    frames = {"NDA-FI.HE": null_newest("2026-09-16", "2026-09-15", seed=21, px=11.0),
              "ABI.BR": null_newest("2026-09-16", "2026-09-15", seed=22, px=55.0),
              "OR.PA": bars("2026-09-16", seed=23, px=400.0),
              "AAPL": bars("2026-09-16", seed=24, px=230.0)}
    helsinki = {"px": 11.37, "regularMarketTime": epoch("2026-09-16 15:30"),
                "exchangeTimezoneName": "Europe/Helsinki"}
    brussels = {"px": 57.2, "regularMarketTime": epoch("2026-09-16 15:35"),
                "exchangeTimezoneName": "Europe/Brussels"}

    def run(q, fail_batches=False):
        calls = []

        def download(tickers, **kw):
            calls.append((tickers if isinstance(tickers, str) else list(tickers),
                          kw.get("keepna")))
            if fail_batches and not isinstance(tickers, str):
                raise RuntimeError("Yahoo 429")
            return yahoo_like(frames, tickers, kw.get("keepna", False))

        with Patch(D, PRICE_DIR=Path(tempfile.mkdtemp(prefix="prices_t5_")),
                   load_universe=lambda: {"tickers": list(frames)}), \
                Patch(D.yf, download=download, Ticker=quotes(**q)):
            return D.fetch_all(force=True), calls

    # same-day quote: the singleton's own null bar is filled (batch path)
    out, calls = run({"NDA-FI_HE": helsinki, "ABI_BR": brussels})
    assert ["NDA-FI.HE"] in [c for c, _ in calls], calls        # alone in its batch
    assert ["ABI.BR", "OR.PA"] in [c for c, _ in calls], calls  # Euronext pooled
    nda = out["NDA-FI.HE"]
    assert nda.index[-1] == pd.Timestamp("2026-09-16"), nda.tail(2)
    assert list(nda.iloc[-1][["Open", "High", "Low", "Close"]]) == [11.37] * 4
    assert out["ABI.BR"].index[-1] == pd.Timestamp("2026-09-16")
    assert out["ABI.BR"]["Close"].iloc[-1] == 57.2
    assert out["AAPL"].index[-1] == pd.Timestamp("2026-09-16")
    # the same through the per-ticker fallback (fetch_one's download)
    out, calls = run({"NDA-FI_HE": helsinki, "ABI_BR": brussels}, fail_batches=True)
    assert ("NDA-FI.HE", True) in calls, calls
    assert out["NDA-FI.HE"]["Close"].iloc[-1] == 11.37
    # no same-day quote (last trade 15 Sep): the empty row is dropped, not filled
    stale = dict(helsinki, regularMarketTime=epoch("2026-09-15 15:30"))
    out, _ = run({"NDA-FI_HE": stale, "ABI_BR": dict(brussels, regularMarketTime=epoch(
        "2026-09-15 15:35"))})
    for t in ("NDA-FI.HE", "ABI.BR"):
        assert out[t].index[-1] == pd.Timestamp("2026-09-15"), (t, out[t].tail(2))
        assert len(out[t]) == len(frames[t]) - 1 and not out[t].isna().any().any()
    # ...and with no quote at all
    out, _ = run({})
    assert out["NDA-FI.HE"].index[-1] == pd.Timestamp("2026-09-15")
    print("t5 a singleton's own all-null newest bar: same-day quote fills it, else dropped OK")


def t6_clean_drops_every_other_empty_or_non_positive_row():
    df = bars("2026-09-16", seed=31, px=50.0)
    days = list(df.index)
    df.loc[days[100]] = [np.nan] * 5                     # Yahoo's all-null holiday row
    df.loc[days[150]] = [np.nan, np.nan, np.nan, np.nan, 0.0]
    df.loc[days[200]] = [0.0, 0.0, 0.0, 0.0, 0.0]        # all-zero row
    df.loc[days[210]] = [50.0, 51.0, 49.0, 0.0, 1000.0]  # a zero close with volume
    df.loc[days[220], "Close"] = -1.0
    df.loc[days[230], ["Open", "High", "Low"]] = np.nan  # a close alone is still a bar
    got = D._clean(yahoo_like({"X.HE": df}, "X.HE", keepna=True), "X.HE")
    dropped = {days[100], days[150], days[200], days[210], days[220]}
    assert list(got.index) == [d for d in days if d not in dropped], set(days) - set(got.index)
    assert not got["Close"].isna().any() and (got["Close"] > 0).all()
    assert got.index[-1] == days[-1] and days[230] in got.index
    # a newest row of O=H=L=0 and no close (keepna now lets it through): zeros are
    # no evidence of a session, and a fill never keeps a zero low
    zero = bars("2026-09-15", seed=32, px=50.0)
    zero.loc[pd.Timestamp("2026-09-16")] = [0.0, 0.0, 0.0, np.nan, 0.0]
    q = {"px": 52.0, "exchangeTimezoneName": "Europe/Helsinki"}
    with Patch(D.yf, Ticker=quotes(**{"Z_HE": dict(q, regularMarketTime=epoch(
            "2026-09-15 15:30"))})):
        out = D._clean(zero.copy(), "Z.HE")
    assert out.index[-1] == pd.Timestamp("2026-09-15"), out.tail(2)
    with Patch(D.yf, Ticker=quotes(**{"Z_HE": dict(q, regularMarketTime=epoch(
            "2026-09-16 15:30"))})):
        out = D._clean(zero.copy(), "Z.HE")
    assert list(out.iloc[-1][["Open", "High", "Low", "Close"]]) == [52.0] * 4, out.tail(1)
    print("t6 _clean drops mid-series empty rows and closes <= 0; zeros are no evidence OK")


STAMP_RE = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"


def t7_build_stamps_generated_at_with_the_download_start():
    import contextlib
    import io
    import re
    import build_dashboard as B
    import universe

    def utc(s):
        return dt.datetime.strptime(s, D.STAMP_FMT).replace(tzinfo=dt.timezone.utc)

    frames = {"AAPL": bars("2026-09-16", seed=41, px=230.0),
              "7203.T": bars("2026-09-17", seed=42, px=2800.0),
              "0700.HK": bars("2026-09-17", seed=43, px=600.0)}
    order = list(frames)
    tmp = Path(tempfile.mkdtemp(prefix="build_t7_"))
    uni = {"tickers": order, "updated": "2026-09-17", "added_log": [], "removed_log": []}
    # the download step's start: two hours ago, so this machine's clock at
    # build time is later, as it is on CI
    STEP1 = D.utc_stamp(dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2))

    def download(tickers, **kw):
        return yahoo_like(frames, tickers, kw.get("keepna", False))

    def build():
        """build_dashboard.py as CI runs it, in its own step: its fetch_all
        reads the price files step 1 wrote. Returns (data.json, cards, window)."""
        buf = io.StringIO()
        before = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        with contextlib.redirect_stdout(buf):
            B.main()
        after = dt.datetime.now(dt.timezone.utc)
        data = json.loads((tmp / "docs" / "data.json").read_text(encoding="utf-8"))
        cards = {p.name: json.loads(p.read_text(encoding="utf-8"))
                 for p in (tmp / "docs" / "products").glob("*.json")}
        return data, cards, (before, after), buf.getvalue()

    prices = tmp / "prices"
    with Patch(D, PRICE_DIR=prices, load_universe=lambda: dict(uni)), \
            Patch(D.yf, download=download, Ticker=quotes()), \
            Patch(B, load_universe=lambda: dict(uni), DOCS=tmp / "docs",
                  PROD_DIR=tmp / "docs" / "products", DATA_DIR=tmp / "data",
                  POS_FILE=tmp / "data" / "positions.json",
                  LOG_FILE=tmp / "data" / "trade_log.json",
                  NAMES_CACHE=tmp / "data" / "company_names.json"), \
            Patch(universe, prune_dead=lambda *a, **k: dict(uni)), \
            Patch(sys, argv=["build_dashboard.py"]):
        # step "Download prices": its start is recorded beside the price files
        with Patch(D, utc_stamp=lambda when=None: STEP1):
            D.fetch_all(force=True)
        assert D.download_started(order) == STEP1
        data, cards, (before, after), out = build()
        assert data["generated_at"] == STEP1, data["generated_at"]
        assert "falling back" not in out and "generated_at " + STEP1 in out, out[-400:]
        assert re.match(STAMP_RE, data["generated_at"])
        assert utc(data["generated_at"]) <= after, "later than the build"
        assert data["generated"] == dt.date.today().isoformat(), "date field unchanged"
        assert len(cards) == 3 and all(c["generated_at"] == STEP1 for c in cards.values()), \
            {k: c.get("generated_at") for k, c in cards.items()}

        # a later fetch that adds a name does not make the build look newer
        frames["SAP.DE"] = bars("2026-09-17", seed=44, px=240.0)
        uni["tickers"] = order + ["SAP.DE"]
        data, cards, _, _ = build()
        assert D.download_started(uni["tickers"]) == STEP1
        assert data["generated_at"] == STEP1 and len(cards) == 4
        assert all(c["generated_at"] == STEP1 for c in cards.values())
        starts = json.loads((prices / D.STARTS_NAME).read_text(encoding="utf-8"))
        assert starts["SAP.DE"] > STEP1 and re.match(STAMP_RE, starts["SAP.DE"]), starts

        # no record of when the download began: build time, and it says so
        for broken in (None, "{not json", json.dumps({"AAPL": STEP1}),
                       json.dumps(dict(starts, AAPL="2026-09-17 06:35")),
                       json.dumps({t: "2999-01-01T00:00:00Z" for t in uni["tickers"]})):
            if broken is None:
                (prices / D.STARTS_NAME).unlink()
            else:
                (prices / D.STARTS_NAME).write_text(broken, encoding="utf-8")
            data, cards, (before, after), out = build()
            stamp = data["generated_at"]
            assert re.match(STAMP_RE, stamp), stamp
            assert before <= utc(stamp) <= after, (broken, stamp, before, after)
            assert all(c["generated_at"] == stamp for c in cards.values())
            assert "falling back to the BUILD time" in out, (broken, out[-400:])
    # the helper alone, on a fixed clock
    now = dt.datetime(2026, 9, 17, 9, 0, 20, 700000, tzinfo=dt.timezone.utc)
    with Patch(D, PRICE_DIR=Path(tempfile.mkdtemp(prefix="prices_t7_"))):
        with contextlib.redirect_stdout(io.StringIO()):
            assert B.generated_at(["AAPL"], now=now) == "2026-09-17T09:00:20Z"
        D._record_download_start(["AAPL", "7203.T"], "2026-09-17T07:35:02Z")
        D._record_download_start(["0700.HK"], "2026-09-17T08:05:00Z")
        assert B.generated_at(["AAPL", "7203.T", "0700.HK"], now=now) == "2026-09-17T07:35:02Z"
        assert B.generated_at(["0700.HK"], now=now) == "2026-09-17T08:05:00Z"
        assert D.download_started([]) is None
    print("t7 data.json and every card carry generated_at = the download start, "
          "never after the build OK")


def auto_adjusted(raw):
    """What yf.download(auto_adjust=True) does to a raw frame (yfinance 1.5.1
    utils.auto_adjust): ratio = Adj Close / Close, Open/High/Low times it, Close
    replaced by Adj Close. A row with no close gets ratio NaN, so the O/H/L Yahoo
    DID send come out NaN; Volume is left alone."""
    df = raw.copy()
    ratio = df["Adj Close"] / df["Close"]
    for c in ("Open", "High", "Low"):
        df[c] = df[c] * ratio
    df["Close"] = df["Adj Close"]
    return df.drop(columns=["Adj Close"])


def t8_null_close_bar_keeps_its_session_range():
    # DBK.DE, 2026-09-16, as Yahoo still served it 11 hours after the close:
    # O 33.675, H 33.845, L 33.185, V 3,205,426, close and adjclose None
    hist = bars("2026-09-15", n=320, seed=51, px=33.0)
    scale = 33.5 / float(hist["Close"].iloc[-1])      # Tuesday closed at 33.50
    for c in ("Open", "High", "Low", "Close"):
        hist[c] = hist[c] * scale
    raw = hist.copy()
    raw["Adj Close"] = raw["Close"]
    day = pd.Timestamp("2026-09-16")
    raw.loc[day] = [33.675, 33.845, 33.185, np.nan, 3_205_426.0, np.nan]
    adj = auto_adjusted(raw)
    assert adj.loc[day, ["Open", "High", "Low", "Close"]].isna().all(), "premise: O/H/L wiped"
    assert adj.loc[day, "Volume"] == 3_205_426.0
    dbk = {"px": 33.63, "regularMarketTime": epoch("2026-09-16 15:35"),
           "exchangeTimezoneName": "Europe/Berlin",
           "regularMarketDayHigh": 33.845, "regularMarketDayLow": 33.185}

    def clean(q, frame=adj):
        with Patch(D.yf, Ticker=quotes(DBK_DE=q)):
            return D._clean(frame.copy(), "DBK.DE")

    # same-day quote with the day range: the real range, not a flat bar
    out = clean(dbk)
    row = out.iloc[-1]
    assert out.index[-1] == day and len(out) == len(hist) + 1
    assert (row["Close"], row["High"], row["Low"], row["Open"]) == (33.63, 33.845, 33.185, 33.63), row
    assert round(row["High"] - row["Low"], 6) == 0.66
    pd.testing.assert_frame_equal(out.iloc[:-1], hist, check_freq=False, check_names=False)
    # ...so ATR is what Yahoo's own High/Low give, and the flat fill understated it
    truth = hist.copy()
    truth.loc[day] = [33.675, 33.845, 33.185, 33.63, 3_205_426.0]
    flat = hist.copy()
    flat.loc[day] = [33.63, 33.63, 33.63, 33.63, 3_205_426.0]
    a_out, a_true, a_flat = (float(ind_atr(f, 14).iloc[-1]) for f in (out, truth, flat))
    assert abs(a_out - a_true) < 1e-12, (a_out, a_true)
    assert a_flat < a_true, (a_flat, a_true)
    assert analyze("DBK.DE", out)["atr"] == analyze("DBK.DE", truth)["atr"]
    # a print outside the reported range widens it; never narrows it
    row = clean(dict(dbk, px=33.95)).iloc[-1]
    assert (row["High"], row["Low"], row["Close"]) == (33.95, 33.185, 33.95), row
    row = clean(dict(dbk, px=33.10)).iloc[-1]
    assert (row["High"], row["Low"], row["Close"]) == (33.845, 33.10, 33.10), row
    # a day open, when the metadata has one, is used - inside the filled range
    row = clean(dict(dbk, regularMarketOpen=33.675)).iloc[-1]
    assert row["Open"] == 33.675, row
    row = clean(dict(dbk, regularMarketOpen=40.0)).iloc[-1]
    assert row["Open"] == 33.845, row

    # no usable day range: every missing field falls back to px, as before
    for label, extra in (("absent", {"regularMarketDayHigh": None, "regularMarketDayLow": None}),
                         ("zero", {"regularMarketDayHigh": 0, "regularMarketDayLow": 0.0}),
                         ("negative", {"regularMarketDayHigh": -1.0, "regularMarketDayLow": -2}),
                         ("NaN", {"regularMarketDayHigh": np.nan, "regularMarketDayLow": float("nan")}),
                         ("junk", {"regularMarketDayHigh": "n/a", "regularMarketDayLow": [1]})):
        q = {k: v for k, v in dict(dbk, **extra).items() if v is not None}
        row = clean(q).iloc[-1]
        assert list(row[["Open", "High", "Low", "Close"]]) == [33.63] * 4, (label, row)
    # half a range: the half that is there is used
    row = clean(dict(dbk, regularMarketDayLow=0)).iloc[-1]
    assert (row["High"], row["Low"]) == (33.845, 33.63), row

    # a quote from ANOTHER day uses none of its range: Thursday's 10:05 Berlin
    # print and Thursday's high/low are not Wednesday's bar. The volume still
    # proves Wednesday traded, so the close is filled, flat, as before.
    thu = dict(dbk, px=33.9, regularMarketTime=epoch("2026-09-17 08:05"),
               regularMarketDayHigh=34.4, regularMarketDayLow=33.7)
    out = clean(thu)
    assert out.index[-1] == day
    assert list(out.iloc[-1][["Open", "High", "Low", "Close"]]) == [33.9] * 4, out.iloc[-1]
    # ...and with no trade time at all, the same
    q = {k: v for k, v in dbk.items() if k != "regularMarketTime"}
    assert list(clean(q).iloc[-1][["Open", "High", "Low", "Close"]]) == [33.63] * 4

    # parts Yahoo did send are never overwritten by the quote's range
    part = adj.copy()
    part.loc[day, "High"] = 33.9
    row = clean(dbk, part).iloc[-1]
    assert (row["High"], row["Low"], row["Open"]) == (33.9, 33.185, 33.63), row

    # the evidence test is unchanged: a day range is no evidence of a session.
    # An all-null union row whose quote is from another day is still dropped...
    empty = hist.copy()
    empty.loc[day] = [np.nan] * 5
    out = clean(dict(thu), empty)
    assert out.index[-1] == pd.Timestamp("2026-09-15") and len(out) == len(hist), out.tail(2)
    # ...and an all-null row of the bar's own day (the 5301.T shape) gets the range
    row = clean(dbk, empty).iloc[-1]
    assert (row["High"], row["Low"], row["Close"]) == (33.845, 33.185, 33.63), row

    # _live_quote itself: one metadata read gives the price, the day and the range
    with Patch(D.yf, Ticker=quotes(DBK_DE=dbk)):
        assert D._live_quote("DBK.DE") == (
            33.63, dt.date(2026, 9, 16), {"High": 33.845, "Low": 33.185, "Open": None})

    class NoMetadata(FakeTicker):
        def get_history_metadata(self):
            raise RuntimeError("metadata unavailable")

    with Patch(D.yf, Ticker=lambda t: NoMetadata(t, {"DBK.DE": dbk})):
        assert D._live_quote("DBK.DE") == (33.63, None, {"High": None, "Low": None, "Open": None})
    with Patch(D.yf, Ticker=quotes()):
        assert D._live_quote("DBK.DE") == (None, None, {"High": None, "Low": None, "Open": None})
    print(f"t8 a null-close bar keeps its session range: ATR {a_out:.6f} "
          f"(flat fill gave {a_flat:.6f}) OK")


if __name__ == "__main__":
    t1_mixed_calendar_union_row_is_not_filled()
    t2_tokyo_null_close_is_still_filled()
    t3_chunks_never_mix_markets()
    t4_atr_unchanged_by_phantom_union_row()
    t5_singleton_null_newest_bar_is_decided_by_evidence()
    t6_clean_drops_every_other_empty_or_non_positive_row()
    t7_build_stamps_generated_at_with_the_download_start()
    t8_null_close_bar_keeps_its_session_range()
    print("ALL DATA-FETCH TESTS PASS")
