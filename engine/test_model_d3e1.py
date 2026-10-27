#!/usr/bin/env python3
"""Tests for MODEL "D3E1" (owner decision 2026-10-26): the D3 second signal,
the E1 earnings filter on US dips, and how analyze / scan_actions /
build_dashboard / sec_fetch carry them.

Run from this directory:  python test_model_d3e1.py

What must hold, and why:
- D3 here is a numpy port of the research's numba loops (ideas/features.py
  fib + fib_hold). t1 checks it against a literal loop port of those loops on
  random walks and the edge cases (ties, NaN, short series, a swing of
  exactly 10%); t2 checks the card's last-bar shortcut gives the same answer.
- E1 is a port of the research filter (lynch / lfix / lfix3 / lfix5 + the
  200-day age guard). t3 runs it on hand-made SEC companyconcept fixtures:
  growth keeps, decline blocks, every unscorable shape is "unscored" (kept),
  Q4 = FY - 9M, a 2:1 split restatement is found and divided out, and a
  filing dated after the signal day is never read.
- analyze (t4): price, atr and sma200 - everything the bot's exits read - are
  identical whatever the model and the resolver do; a kept dip is today's
  card plus the new keys; a vetoed dip is a WATCH; a D3 bar is a tier-1 BUY
  with the dip's own 12%-floor stop; an exception anywhere in the D3/E1 block
  still gives a card (a ticker without one is pruned from the universe and a
  held name without one gets no exits); ETFs, bonds and crypto never get D3;
  MODEL "E" builds every card as before.
- scan_actions (t5) writes E rows before D3 rows, each by score, so the first
  20 the build keeps are the tested order. Equal scores (many BUYs tie at
  100) break on the card's raw 90-day momentum, highest first (t5b, owner
  decision 2026-10-27); a card without "mom" keeps the old order, and the
  bot's and the digest's suites pin the same golden rows to the same order.
- build_dashboard (t6) reads E1 only from the SEC step's files and never
  raises; the header shows the owner's research figure with its wording;
  data.json carries "model". sec_fetch (t7) makes no request without a
  User-Agent, is bounded and polite, and always exits 0.

Offline: yfinance is replaced by a stub that fails any call, SEC by a fake
opener, and every file goes to a temp dir.
"""
import contextlib
import io
import json
import shutil
import sys
import tempfile
import types
import urllib.error
from pathlib import Path

import numpy as np
import pandas as pd

_yf = types.ModuleType("yfinance")                  # never the network in a test


def _no_network(*a, **k):
    raise AssertionError("test reached the yfinance network stub")


_yf.download = _no_network
_yf.Ticker = _no_network
sys.modules["yfinance"] = _yf

import config                                       # noqa: E402
import model_d3e1 as M                              # noqa: E402
import production as P                              # noqa: E402

HERE = Path(__file__).resolve().parent
EXIT_KEYS = ("price", "atr", "sma200")


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


# ------------------------------------------------------------- fixtures ----

def _ohlc(close, rng, spread=(0.004, 0.012)):
    open_ = close * (1 + rng.normal(0, 0.003, len(close)))
    high = np.maximum(open_, close) * (1 + rng.uniform(*spread, len(close)))
    low = np.minimum(open_, close) * (1 - rng.uniform(*spread, len(close)))
    return open_, high, low


def frame(close, high, low, open_=None, last_day="2026-10-23"):
    idx = pd.bdate_range(end=last_day, periods=len(close))
    open_ = close if open_ is None else open_
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close,
                         "Volume": np.full(len(close), 2e6)}, index=idx)


def walk(n=600, seed=1, px=100.0, drift=0.0006, vol=0.018):
    rng = np.random.default_rng(seed)
    close = px * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    o, h, l = _ohlc(close, rng)
    return frame(close, h, l, o)


def e_dip(seed=0, drift=0.005, n=420, px=50.0):
    """A steady strong uptrend, then three sharp down closes: RSI(3) far
    below 25, momentum ~ exp(90 x drift - 7.5%), ~7% off the high."""
    rng = np.random.default_rng(seed)
    r = drift + rng.normal(0, 0.003, n)
    r[-3:] = [-0.03, -0.025, -0.02]
    close = px * np.exp(np.cumsum(r))
    o, h, l = _ohlc(close, rng)
    return frame(close, h, l, o)


def d3_bar(seed=0, n=420, base=100.0, flat=1.0):
    """Rise to `base`, flat for 62 bars (the swing low), rally to ~2x base,
    a 10-bar pullback whose lows touch the 0.618 retracement, then an up
    close back above it: a D3 signal on the last bar, far below the 52-week
    high, so never also an E dip."""
    rng = np.random.default_rng(seed)
    n1 = n - 132
    p1 = base * 0.8 * np.exp(np.linspace(0, np.log(1 / 0.8), n1))
    p2 = base * flat * (1 + rng.normal(0, 0.002, 62))
    p3 = base * flat * np.exp(np.linspace(0, np.log(2.0), 60))[1:]
    close = np.concatenate([p1, p2, p3])
    o, h, l = _ohlc(close, rng, (0.002, 0.006))
    k = len(close)                                  # the swing high is in here
    sh = h[k - 40:].max()
    sl = l[k - 121:].min()
    lv = sh - 0.618 * (sh - sl)
    pull = np.linspace(close[-1], lv * 1.004, 11)[1:]
    pc = np.concatenate([pull, [lv * 1.03]])
    ph = pc * 1.006
    pl = np.concatenate([pull * 0.999, [lv * 1.012]])
    pl[-3] = lv * 1.002                             # the touch: low within 1% of the level
    close = np.concatenate([close, pc])
    high = np.concatenate([h, ph])
    low = np.concatenate([l, pl])
    open_ = np.concatenate([o, pc])
    assert len(close) == n, len(close)
    return frame(close, high, low, open_)


def feat(df):
    return P._feat(df)


# ------------------------------------------- research loops, ported literally

def fib_loop(h, l, look_hi=40, look_lo=120):
    """ideas/features.py fib, line for line, in plain Python."""
    m = len(h)
    SH = np.full(m, np.nan)
    SL = np.full(m, np.nan)
    for i in range(look_hi + look_lo, m):
        jh = i - look_hi + 1 + int(np.argmax(h[i - look_hi + 1:i + 1]))
        sl = l[jh - look_lo:jh + 1].min()
        with np.errstate(invalid="ignore", divide="ignore"):
            if (h[jh] - sl) / sl >= 0.10:
                SH[i], SL[i] = h[jh], sl
    return SH, SL


def hold_loop(SH, SL, l, c, level=0.618, floor_level=0.886, tol=0.01, look=5):
    """ideas/features.py fib_hold, line for line, in plain Python."""
    m = len(c)
    out = np.zeros(m, bool)
    for i in range(look, m):
        if not (SH[i] == SH[i] and SL[i] == SL[i]):
            continue
        rng = SH[i] - SL[i]
        lv = SH[i] - level * rng
        fl = SH[i] - floor_level * rng
        lo = l[i - look + 1:i + 1].min()
        if lo <= lv * (1 + tol) and lo >= fl and c[i] > lv and c[i] > c[i - 1]:
            out[i] = True
    return out


def same(a, b):
    return np.array_equal(a, b, equal_nan=True)


def t1_d3_matches_the_research_loops():
    holds = swings = 0
    cases = []
    for seed in range(50):
        df = walk(n=int(np.random.default_rng(seed).integers(200, 900)), seed=seed,
                  drift=0.0004 + 0.0002 * (seed % 5), vol=0.012 + 0.002 * (seed % 7))
        cases.append((f"walk {seed}", df["High"].to_numpy(), df["Low"].to_numpy(),
                      df["Close"].to_numpy()))
    d = d3_bar()
    cases.append(("crafted D3", d["High"].to_numpy(), d["Low"].to_numpy(), d["Close"].to_numpy()))
    # ties: a flat ceiling over bars 200-259 with ever lower lows under it.
    # The FIRST equal high is the swing high, so at bar 240 the swing low is
    # read from bars 81-201 - a later tied high would have reached the lows
    # at 202+ instead.
    flat = walk(n=400, seed=99)
    h, l = flat["High"].to_numpy().copy(), flat["Low"].to_numpy().copy()
    top = h.max() * 1.01
    h[200:260] = top
    l[202:260] = l[:202].min() * 0.9
    SH, SL = M.fib_swing(h, l)
    assert SH[240] == top and SL[240] == l[81:202].min() > l[202], (SH[240], SL[240])
    cases.append(("tied highs", h, l, flat["Close"].to_numpy()))
    # NaN in a High and a Low: no swing through it in either
    nan = walk(n=400, seed=98)
    h, l = nan["High"].to_numpy().copy(), nan["Low"].to_numpy().copy()
    h[300], l[250] = np.nan, np.nan
    cases.append(("NaN high/low", h, l, nan["Close"].to_numpy()))
    # short series: nothing before bar 160, and nothing at all at <= 160 bars
    for n in (5, 100, 160, 161, 170):
        s = walk(n=n, seed=n)
        cases.append((f"{n} bars", s["High"].to_numpy(), s["Low"].to_numpy(), s["Close"].to_numpy()))
    for name, h, l, c in cases:
        SH, SL = M.fib_swing(h, l)
        SH0, SL0 = fib_loop(h, l)
        assert same(SH, SH0) and same(SL, SL0), name
        hold, hold0 = M.fib_hold(SH, SL, l, c), hold_loop(SH0, SL0, l, c)
        assert np.array_equal(hold, hold0), (name, np.flatnonzero(hold != hold0)[:5])
        assert not np.isfinite(SH[:160]).any(), name
        holds += int(hold.sum())
        swings += int(np.isfinite(SH).sum())
    assert holds >= 20 and swings > 1000, (holds, swings)     # the walks did exercise it

    # a swing of exactly 10% counts; one a hair under does not (both ports)
    n = 200
    h = np.full(n, 100.0)
    l = np.full(n, 100.0)
    for top, want in ((110.0, True), (109.999, False)):   # (110 - 100) / 100 = 0.10
        h[190] = top
        SH, SL = M.fib_swing(h, l)
        SH0, _ = fib_loop(h, l)
        assert same(SH, SH0)
        assert bool(np.isfinite(SH[195])) is want, (top, SH[195])
    print(f"t1 D3 equals the research loops on 50 walks + edge cases ({holds} holds, "
          f"{swings} swing bars) OK")


def t2_d3_last_is_the_series_end():
    checked = hits = 0
    frames = [walk(n=700, seed=s, drift=0.0012, vol=0.02) for s in range(12)]
    frames += [d3_bar(seed=s) for s in range(4)]
    for df in frames:
        f = feat(df)
        full = M.d3_series(f, "US")
        cuts = sorted(set([len(f)] + [int(x) + 1 for x in np.flatnonzero(full)[:6]]
                          + list(range(200, len(f), 37))))
        for cut in cuts:
            fc = feat(df.iloc[:cut])
            ok, lv = M.d3_last(fc, "US")
            assert ok == bool(M.d3_series(fc, "US")[-1]), cut
            checked += 1
            if ok:
                hits += 1
                SH, SL = M.fib_swing(fc["High"].to_numpy(), fc["Low"].to_numpy())
                rng = SH[-1] - SL[-1]
                assert lv["swing_high"] == round(SH[-1], 4) and lv["swing_low"] == round(SL[-1], 4)
                assert abs(lv["level_0618"] - (SH[-1] - 0.618 * rng)) < 1e-4
                assert abs(lv["floor_0886"] - (SH[-1] - 0.886 * rng)) < 1e-4
                assert lv["low5"] == round(fc["Low"].iloc[-5:].min(), 4)
                assert lv["floor_0886"] <= lv["low5"] <= lv["level_0618"] * 1.01 + 1e-4
            else:
                assert lv is None
    assert hits >= 4, hits
    # outside US/HK/JP/EU: never, on the very bar that is D3 for a stock
    f = feat(d3_bar())
    assert M.d3_last(f, "US")[0] and M.d3_last(f, "HK")[0] and M.d3_last(f, "EU")[0]
    for mkt in ("ETF", "BOND", "LEV", "CRYPTO", "INDEX", "FX", "MACRO", "COMMODITY"):
        assert M.d3_last(f, mkt) == (False, None) and not M.d3_series(f, mkt).any(), mkt
    # momentum NaN fails, and a missing ATR is no stand-in
    g = f.copy()
    g.loc[g.index[-1], "mom_90"] = np.nan
    assert not M.d3_last(g, "US")[0]
    g = f.copy()
    g.loc[g.index[-1], "atr"] = np.nan
    assert not M.d3_last(g, "US")[0]
    print(f"t2 d3_last == d3_series[-1] at {checked} cut points ({hits} signals) OK")


# --------------------------------------------------------------- E1 -------

def _q_period(y, k):
    """(start, end) of calendar quarter k (1-4) of year y."""
    starts = {1: "01-01", 2: "04-01", 3: "07-01", 4: "10-01"}
    ends = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}
    return f"{y}-{starts[k]}", f"{y}-{ends[k]}"


def _plus(day, n):
    return (pd.Timestamp(day) + pd.Timedelta(days=n)).date().isoformat()


def company(eps, years, last=(2026, 3), split=None, ratio=2.0, drop=(), no_9m=False):
    """SEC companyconcept JSON as EDGAR sends it, for a calendar-year filer.
    eps(y, k) = the true EPS of quarter k of year y in ORIGINAL share units.
    10-Qs (filed 40 days after Q1-Q3) report the quarter and last year's same
    quarter; the Q3 10-Q also the 9-month YTD (and last year's). The 10-K
    (60 days after Dec 31) reports FY and last FY only, so Q4 must come from
    FY - 9M. Values filed on/after `split` are in post-split units (/ ratio),
    comparatives included - which is what a restatement looks like."""
    rows = []

    def val(v, filed):
        return round(v / (ratio if split and filed >= split else 1.0), 2)

    gone = {_q_period(y, k) for y, k in drop}

    def add(start, end, v, filed, form):
        if (start, end) in gone:             # missing from every filing, comparatives too
            return
        rows.append({"start": start, "end": end, "val": val(v, filed), "filed": filed,
                     "form": form, "accn": f"0000-{filed}", "fy": int(filed[:4]), "fp": "Q"})
    for y in years:
        for k in (1, 2, 3, 4):
            if (y, k) > last:
                break
            s, e = _q_period(y, k)
            if k < 4:
                filed = _plus(e, 40)
                add(s, e, eps(y, k), filed, "10-Q")
                if y - 1 in years:
                    ps, pe = _q_period(y - 1, k)
                    add(ps, pe, eps(y - 1, k), filed, "10-Q")
                if k == 3 and not no_9m:
                    add(f"{y}-01-01", e, sum(eps(y, j) for j in (1, 2, 3)), filed, "10-Q")
                    if y - 1 in years:
                        add(f"{y-1}-01-01", f"{y-1}-09-30",
                            sum(eps(y - 1, j) for j in (1, 2, 3)), filed, "10-Q")
            else:
                filed = _plus(e, 60)
                add(f"{y}-01-01", e, sum(eps(y, j) for j in (1, 2, 3, 4)), filed, "10-K")
                if y - 1 in years:
                    add(f"{y-1}-01-01", f"{y-1}-12-31",
                        sum(eps(y - 1, j) for j in (1, 2, 3, 4)), filed, "10-K")
    return {"cik": 1, "tag": "EarningsPerShareDiluted", "units": {"USD/shares": rows}}


def rising(y, k):
    return 1.00 + 0.05 * (4 * (y - 2023) + k)


def falling(y, k):
    return 3.00 - 0.05 * (4 * (y - 2023) + k)


YEARS = (2023, 2024, 2025, 2026)
ASOF = "2026-11-20"                    # Q3 2026 filed 2026-11-09


def decide(doc, asof=ASOF):
    return M.e1_asof(M.facts_from_json(doc), asof)


def t3_e1_on_sec_fixtures():
    up = decide(company(rising, YEARS))
    assert up["decision"] == "keep" and up["qend"] == "2026-09-30" and up["growth"] > 0, up
    # TTM Q4'25..Q3'26 against Q4'24..Q3'25: each quarter +0.20 a year
    assert abs(up["ttm_now"] - up["ttm_prior"] - 0.80) < 1e-6, up
    down = decide(company(falling, YEARS))
    assert down["decision"] == "block" and down["growth"] < 0, down
    assert set(M.card_e1(down)) == {"decision", "growth", "qend"}

    # Q4 = FY - 9M; and FY - Q1 - Q2 - Q3 when no 9M was filed
    for doc in (company(rising, YEARS), company(rising, YEARS, no_9m=True)):
        p = M.prep_facts(M.facts_from_json(doc))
        q = M.quarters_asof_fix(p, pd.Timestamp(ASOF), [])
        assert abs(q[pd.Timestamp("2025-12-31")] - rising(2025, 4)) < 0.011, q
    assert decide(company(rising, YEARS, no_9m=True))["decision"] == "keep"

    # unscored = KEPT: too few rows, irregular quarters, prior TTM of 0, stale
    few = company(rising, (2026,), last=(2026, 3))                 # 4 rows
    assert M.facts_from_json(few) is None
    assert decide(few)["decision"] == "unscored" and decide(few)["why"] == "no SEC data"
    gap = decide(company(rising, YEARS, drop={(2025, 2)}))
    assert gap["decision"] == "unscored", gap                   # Mar 31 -> Sep 30 = 183 days
    ends = [pd.Timestamp(_q_period(y, k)[1]) for y in (2024, 2025) for k in (1, 2, 3, 4)]
    assert M.ttm_growth({e: 1.0 for e in ends[:7]}) is None
    assert M.ttm_growth({e: 1.0 for e in ends}) == (0.0, 4.0, 4.0, ends[-1])
    zero = dict(zip(ends, [0.5, -0.5, 0.25, -0.25, 1, 1, 1, 1]))
    assert M.ttm_growth(zero) is None
    newest = pd.Timestamp("2026-09-30")
    ok200 = decide(company(rising, YEARS), (newest + pd.Timedelta(days=200)).date().isoformat())
    stale = decide(company(rising, YEARS), (newest + pd.Timedelta(days=201)).date().isoformat())
    assert ok200["decision"] == "keep" and stale["decision"] == "unscored", (ok200, stale)
    assert "over 200 days" in stale["why"] and stale["growth"] is None

    # a 2:1 split: every filing from the Q2 2026 10-Q (2026-08-09) on reports
    # half, comparatives restated. One restated period makes it tentative, the
    # Q3 10-Q's two confirm it; every older value is then divided back, so the
    # last 8 quarters are all in post-split units.
    doc = company(rising, YEARS, split="2026-08-09")
    p = M.prep_facts(M.facts_from_json(doc))
    ev = M.events5(p)
    assert ev == [(pd.Timestamp("2026-08-09"), 2.0)], ev
    q = M.quarters_asof_fix(p, pd.Timestamp(ASOF), ev)
    last8 = sorted(q)[-8:]
    assert all(abs(q[e] - rising(e.year, (e.month + 2) // 3) / 2) < 0.011 for e in last8), q
    blind = M.quarters_asof_fix(p, pd.Timestamp(ASOF), [])
    assert abs(blind[pd.Timestamp("2025-03-31")] - rising(2025, 1)) < 0.011   # units mixed
    # where it decides: flat EPS has no growth (block) - unless half the
    # quarters are left in pre-split units, which fakes a +50% rise (keep)
    doc = company(lambda y, k: 1.0, YEARS, split="2026-08-09")
    p = M.prep_facts(M.facts_from_json(doc))
    ev = M.events5(p)
    assert M.e1_decision(p, ev, ASOF)["decision"] == "block"
    assert M.e1_decision(p, ev, ASOF)["growth"] == 0.0
    assert M.e1_decision(p, [], ASOF)["decision"] == "keep"

    # point in time: a filing dated after the signal day is never read. A
    # shrinking company whose Q3 2026 jumps: blocked the day before that
    # 10-Q, kept on its filing day (filed <= day).
    def jump(y, k):
        return 9.0 if (y, k) == (2026, 3) else falling(y, k)
    doc = company(jump, YEARS)
    assert decide(doc, "2026-11-08")["decision"] == "block"
    assert decide(doc, "2026-11-08")["qend"] == "2026-06-30"
    assert decide(doc, "2026-11-09")["decision"] == "keep"
    # ... and split detection only sees filings up to the day too
    late = company(rising, YEARS, split="2026-08-09")      # the Q2 2026 10-Q
    assert M.events5(M.prep_facts(M.facts_from_json(late))) != []
    early = M.e1_asof(M.facts_from_json(late), "2026-08-08")
    assert early["decision"] == "keep" and early["qend"] == "2026-03-31", early

    # files: diluted first, basic when diluted has too few rows; tz-aware day
    tmp = Path(tempfile.mkdtemp(prefix="sec_t3_"))
    try:
        M.sec_file(tmp, "BRK-B", "EarningsPerShareDiluted").write_text(
            json.dumps(few), encoding="utf-8")
        M.sec_file(tmp, "BRK-B", "EarningsPerShareBasic").write_text(
            json.dumps(company(falling, YEARS)), encoding="utf-8")
        df, concept = M.facts_for(tmp, "BRK-B")
        assert concept == "EarningsPerShareBasic" and len(df) >= 8
        assert M.e1_asof(df, pd.Timestamp(ASOF, tz="UTC"))["decision"] == "block"
        assert M.sec_file(tmp, "BRK.B/x", "C").name == "BRK_B_x_C.json"
        assert M.facts_for(tmp, "NONE") == (None, None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("t3 E1 keeps growth, blocks decline, keeps the unscorable, finds a 2:1 split, "
          "reads nothing filed after the day OK")


# ------------------------------------------------------------- analyze ----

def keep_r(t, d):
    return {"decision": "keep", "growth": 0.12, "qend": "2026-09-30", "why": "x"}


def block_r(t, d):
    return {"decision": "block", "growth": -0.25, "qend": "2026-09-30", "why": "x"}


def boom_r(t, d):
    raise RuntimeError("SEC files unreadable")


def tickers_30():
    """30 synthetic products: US/HK/JP/EU dips, D3 bars and walks, plus the
    classes that must never get D3."""
    out = {}
    for i, t in enumerate(("NVDA", "AAPL", "0700.HK", "7203.T", "SAP.DE")):
        out[t] = e_dip(seed=i, drift=0.0045 + 0.0004 * i)
    for i, t in enumerate(("MSFT", "LLY", "9988.HK", "6758.T", "ASML.AS")):
        out[t] = d3_bar(seed=10 + i, flat=1.0 + 0.02 * i)
    for i, t in enumerate(("SPY", "TLT", "TQQQ", "BTC-USD", "UUP")):
        out[t] = d3_bar(seed=20 + i)                   # D3 shape, wrong class
    for i, t in enumerate(("QQQ", "HYG")):
        out[t] = e_dip(seed=30 + i)
    for i, t in enumerate(("AMZN", "META", "JPM", "V", "MA", "0005.HK", "8306.T",
                           "MC.PA", "SIE.DE", "^GSPC", "EURUSD=X", "GC=F", "ETH-USD")):
        out[t] = walk(n=500, seed=40 + i, drift=0.0003 * (i % 4), vol=0.015 + 0.002 * (i % 3))
    assert len(out) == 30
    return out


def t4_analyze_invariants():
    data = tickers_30()
    seen = []

    def spy_r(t, d):
        seen.append((t, pd.Timestamp(d)))
        return keep_r(t, d)

    n_e = n_d3 = n_block = 0
    for t, df in data.items():
        mkt = P.market_of(t)
        with Patch(P, MODEL="E"):
            old = P.analyze(t, df)
            assert P.analyze(t, df, e1=boom_r) == old, t      # MODEL E never asks E1
        base = P.analyze(t, df)
        cards = {"none": base, "keep": P.analyze(t, df, e1=keep_r),
                 "block": P.analyze(t, df, e1=block_r), "boom": P.analyze(t, df, e1=boom_r),
                 "spy": P.analyze(t, df, e1=spy_r)}
        for how, c in cards.items():
            assert all(c[k] == old[k] for k in EXIT_KEYS), (t, how)
            assert c["symbol"] == t and c["action"] in ("BUY", "BUY/HOLD", "WATCH", "AVOID")
        is_e = old["action"] == "BUY" and old["regime"].startswith("Uptrend — pullback")
        assert ("signal" in old or "tier" in old or "e1" in old or "d3" in old) is False, t
        if is_e:
            n_e += 1
            assert {k: v for k, v in base.items() if k not in ("signal", "tier")} == old, t
            assert base["signal"] == "E" and base["tier"] == 0 and "e1" not in base
            if mkt == "US":
                assert cards["keep"] == dict(base, e1={"decision": "keep", "growth": 0.12,
                                                       "qend": "2026-09-30"}), t
                b = cards["block"]
                n_block += 1
                assert b["action"] == "WATCH" and b["regime"] == "Uptrend — dip, EPS falling (filter)"
                assert b["stop"] == round(base["sma200"], 2) and b["entry"] is None
                assert b["e1"]["decision"] == "block" and "-25%" in b["reasons"][-1], b
                assert "signal" not in b and "tier" not in b
                # a resolver that raises: the dip stays a BUY, with a note
                assert cards["boom"] == dict(base, model_note=cards["boom"]["model_note"]), t
                assert "RuntimeError" in cards["boom"]["model_note"]
            else:
                # E1 is US only: never asked, the card is the plain E card
                for how in ("keep", "block", "boom", "spy"):
                    assert cards[how] == base, (t, how)
        elif base.get("signal") == "D3":
            n_d3 += 1
            assert mkt in ("US", "HK", "JP", "EU"), t
            f = feat(df)
            px, atr = float(f["Close"].iloc[-1]), float(f["atr"].iloc[-1])
            assert base["action"] == "BUY" and base["tier"] == 1 and base["confidence"] == "Medium"
            assert base["stop"] == round(max(px - 3.5 * atr, 0.88 * px), 2), (t, base["stop"])
            assert base["entry"] == round(px, 2) and base["target"] == round(px + 3.0 * atr, 2)
            assert base["score"] == P.momentum_score(float(f["mom_90"].iloc[-1]))
            assert set(base["d3"]) == {"swing_high", "swing_low", "level_0618", "floor_0886", "low5"}
            assert old["action"] == "WATCH", (t, old["regime"])   # what MODEL E showed
            for how in ("keep", "block", "boom", "spy"):
                assert cards[how] == base, (t, how)               # E1 is for dips only
        else:
            for how in ("keep", "block", "boom", "spy"):
                assert cards[how] == base, (t, how)
            assert {k: v for k, v in base.items()} == old, t
    assert n_e == 7 and n_d3 == 5 and n_block == 2, (n_e, n_d3, n_block)
    assert sorted(t for t, _ in seen) == ["AAPL", "NVDA"], seen     # US dips only
    assert all(d == data[t].index[-1] for t, d in seen)              # on the bar's own date

    # the classes that must never get D3, on the exact bars that are D3 for stocks
    for t in ("SPY", "TLT", "TQQQ", "BTC-USD", "UUP"):
        c = P.analyze(t, data[t])
        assert c.get("signal") != "D3" and "tier" not in c and "d3" not in c, (t, c.get("regime"))

    # an exception anywhere in the D3 block never costs the card: MODEL E's
    # card, plus the note
    def broken(*a, **k):
        raise ValueError("swing scan failed")
    # (D3 bars in three markets, under three resolvers, and a vetoed US dip,
    # which then looks for D3)
    for t, r in (("MSFT", keep_r), ("NVDA", block_r), ("6758.T", None), ("ASML.AS", boom_r)):
        with Patch(P, MODEL="E"):
            old = P.analyze(t, data[t])
        with Patch(P, d3_last=broken):
            c = P.analyze(t, data[t], e1=r)
        assert "ValueError" in c["model_note"], t
        rest = {k: v for k, v in c.items() if k not in ("model_note", "signal", "tier")}
        assert rest == old, t
        assert c["action"] == old["action"] and c["stop"] == old["stop"], t
    with Patch(P, d3_last=broken):                       # NVDA vetoed + broken = a BUY again
        assert P.analyze("NVDA", data["NVDA"], e1=block_r)["action"] == "BUY"
    print(f"t4 analyze: exits unchanged on 30 products x 5 resolvers; {n_e} E, {n_d3} D3, "
          f"{n_block} vetoed; errors fall back to E OK")


def t5_scan_actions_order():
    data = {}
    for i in range(6):
        data[f"E{i}"] = e_dip(seed=50 + i, drift=0.0042 + 0.0003 * i)
    for i in range(4):
        data[f"D{i}"] = d3_bar(seed=60 + i, flat=0.94 + 0.02 * i)    # higher momentum
    data["0388.HK"] = e_dip(seed=70, drift=0.0052)
    data["ZTEST"] = d3_bar(seed=71)
    rows = P.scan_actions(data)
    tiers = [c.get("tier", 0) for c in rows]
    assert tiers == sorted(tiers) and tiers.count(0) == 7 and tiers.count(1) == 5, tiers
    for t in (0, 1):
        sc = [c["score"] for c in rows if c.get("tier", 0) == t]
        assert sc == sorted(sc, reverse=True), (t, sc)
    d3_scores = [c["score"] for c in rows if c["signal"] == "D3"]
    e_scores = [c["score"] for c in rows if c["signal"] == "E"]
    assert max(d3_scores) > min(e_scores), "fixture must test tier over score"
    # E1 blocks US dips out of the actions; HK dips are never asked
    blocked = P.scan_actions(data, e1=block_r)
    assert [c["symbol"] for c in blocked if c["signal"] == "E"] == ["0388.HK"]
    assert [c["symbol"] for c in blocked if c["signal"] == "D3"] == \
        [c["symbol"] for c in rows if c["signal"] == "D3"]
    # 20 or more dips: the build's top 20 holds no D3 (live_sim cut the same way)
    many = {f"E{i}": e_dip(seed=100 + i, drift=0.0041 + 0.00005 * i) for i in range(21)}
    many.update({f"D{i}": d3_bar(seed=200 + i) for i in range(3)})
    top = P.scan_actions(many)[:20]
    assert len(top) == 20 and all(c["signal"] == "E" for c in top)
    # without tiers (MODEL E) the order is by score alone (ties: t5b)
    with Patch(P, MODEL="E"):
        old = P.scan_actions(data)
    assert all("tier" not in c for c in old)
    keyed = sorted(range(len(old)), key=lambda i: -old[i]["score"])
    assert keyed == list(range(len(old)))
    print(f"t5 scan_actions: {tiers.count(0)} E rows then {tiers.count(1)} D3 rows, each by "
          f"score; 21 dips push D3 out of the top 20 OK")


# The tie-break golden (owner decision 2026-10-27): (symbol, tier, score, mom).
# execution/test_sizing.py (the bot) and execution/test_digest_entries.py (the
# digest) pin these same rows to this same order, so the build, the bot and
# the digest agree row for row. None = a card with no "mom" key (an older
# data.json, or a bar without 90 days of history behind it).
TIE_ROWS = [("LOW", 0, 100, 0.55), ("D3HI", 1, 100, 1.4), ("TOP", 0, 100, 0.91),
            ("NOM", 0, 100, None), ("E90", 0, 90, 0.45), ("MID", 0, 100, 0.72),
            ("D3LO", 1, 100, 0.6), ("NOM2", 0, 100, None), ("D3NO", 1, 100, None)]
TIE_ORDER = ["TOP", "MID", "LOW", "NOM", "NOM2", "E90", "D3HI", "D3LO", "D3NO"]
# the same rows with no "mom" at all: the order before 2026-10-27, ties in
# input order
OLD_ORDER = ["LOW", "TOP", "NOM", "MID", "NOM2", "E90", "D3HI", "D3LO", "D3NO"]


def t5b_ties_break_on_momentum():
    # 1. the golden rows through scan_actions itself: analyze is stubbed to
    #    hand back each row's card, and any 260-bar frame passes the length check
    def card(sym, tier, score, mom):
        c = {"symbol": sym, "action": "BUY", "score": score, "tier": tier}
        if mom is not None:
            c["mom"] = mom
        return c

    stub = {s: card(s, t, sc, m) for s, t, sc, m in TIE_ROWS}
    bars = {s: walk(n=260, seed=9) for s, *_ in TIE_ROWS}
    with Patch(P, analyze=lambda t, df, e1=None: dict(stub[t])):
        got = [c["symbol"] for c in P.scan_actions(bars)]
        assert got == TIE_ORDER, got
        for c in stub.values():                  # an older build: no "mom" anywhere
            c.pop("mom", None)
        got = [c["symbol"] for c in P.scan_actions(bars)]
        assert got == OLD_ORDER, got
    # anything but a finite number ranks as "no momentum", never raises
    for bad in (None, "0.9", float("nan"), float("inf"), float("-inf"), True, [1], 10 ** 400):
        assert P.action_rank({"tier": 0, "score": 100, "mom": bad}) == (0, -100, float("inf"))
    assert P.action_rank({"score": 100, "mom": 0.8}) == (0, -100, -0.8)

    # 2. real cards: 24 dips all scoring 100, written in an order that is NOT
    #    their momentum order, and two D3 bars
    many = {f"Q{i:02d}": e_dip(seed=300 + i, drift=0.0060 + 0.00011 * ((i * 7) % 24))
            for i in range(24)}
    many.update({"DA": d3_bar(seed=61, flat=0.96), "DB": d3_bar(seed=62, flat=0.98)})
    for t, df in many.items():
        c = P.analyze(t, df)
        assert c["mom"] == round(float(feat(df)["mom_90"].iloc[-1]), 6), t
    rows = P.scan_actions(many)
    e_rows = [c for c in rows if c["signal"] == "E"]
    assert len(e_rows) == 24 and {c["score"] for c in e_rows} == {100}
    moms = [c["mom"] for c in e_rows]
    assert moms == sorted(moms, reverse=True), moms
    assert [c["symbol"] for c in e_rows] != [t for t in many if t.startswith("Q")], \
        "fixture must not already be in momentum order"
    assert [c["signal"] for c in rows[-2:]] == ["D3", "D3"]          # tiers still first
    keys = [P.action_rank(c) for c in rows]
    assert keys == sorted(keys), keys
    # the build's cut to 20 keeps the 20 strongest of the 24 ties
    top = P.scan_actions(many)[:20]
    assert sorted(c["mom"] for c in top) == sorted(moms)[-20:]
    src = (HERE / "build_dashboard.py").read_text(encoding="utf-8")
    assert "actions = scan_actions(data, e1=e1)[:20]" in src, "the cut must follow the sort"
    # MODEL "E" has no tiers: its ties break on momentum too
    with Patch(P, MODEL="E"):
        old = P.scan_actions(many)
    assert [c["mom"] for c in old] == moms
    # every kind of card carries "mom" when it has one; a short history has none
    assert isinstance(P.analyze("AMZN", walk(n=500, seed=5)).get("mom"), float)
    assert "mom" not in P.analyze("AMZN", walk(n=80, seed=5))
    print("t5b ties at score 100 break on raw 90-day momentum, tiers first; no mom keeps "
          "the old order; the top-20 cut keeps the strongest OK")


# ------------------------------------------------------- build_dashboard ----

def t6_build_dashboard_wiring():
    import build_dashboard as B
    import data_fetch as D
    import universe

    tmp = Path(tempfile.mkdtemp(prefix="build_d3e1_"))
    try:
        sec = tmp / "data" / "sec"
        sec.mkdir(parents=True)
        M.sec_file(sec, "NVDA", "EarningsPerShareDiluted").write_text(
            json.dumps(company(rising, YEARS)), encoding="utf-8")
        M.sec_file(sec, "AAPL", "EarningsPerShareDiluted").write_text(
            json.dumps(company(falling, YEARS)), encoding="utf-8")
        M.sec_file(sec, "META", "EarningsPerShareDiluted").write_text("{not json",
                                                                     encoding="utf-8")
        # the resolver: memoised, never raises
        e1 = B.e1_resolver(sec)
        asof = "2026-11-20"
        assert e1("NVDA", asof)["decision"] == "keep"
        assert e1("AAPL", asof)["decision"] == "block"
        assert e1("META", asof)["decision"] == "unscored"
        assert e1("META", asof)["why"].startswith("SEC file unreadable")
        assert e1("TSLA", asof) == dict(e1("TSLA", asof), decision="unscored", why="no SEC data")
        assert len(e1.seen) == 4 and e1("NVDA", asof) is e1("NVDA", asof)
        # status: missing, then as the SEC step wrote it
        st = B.sec_status(sec)
        assert st["state"] == "missing" and "unscored and kept" in st["message"]
        (sec / "_status.json").write_text(json.dumps(
            {"state": "no_user_agent", "message": "SEC_USER_AGENT is not set", "ran_at": "x"}))
        assert B.sec_status(sec)["state"] == "no_user_agent"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rep = B.model_report("D3E1", e1, [{"signal": "D3"}, {"signal": "E"}],
                                 B.sec_status(sec))
        assert rep["e1"]["checked"] == 4 and rep["e1"]["kept"] == 1
        assert rep["e1"]["blocked_symbols"] == ["AAPL"] and rep["d3_actions"] == 1
        assert rep["e1"]["unscored_symbols"] == ["META", "TSLA"]
        assert "::warning::E1: 2 US dip(s) unscored" in out.getvalue(), out.getvalue()
        assert B.model_report("E", None, [], {}) == {"name": "E", "code": "E"}

        # the header: the owner's figure and wording, from the committed file
        h = B.model_headline(json.loads((HERE.parent / "data" / "model_headline.json")
                                        .read_text(encoding="utf-8")))
        assert h == {"win": "51.6%", "cagr": "31.8%", "maxdd": "-28.7%",
                     "grows": "HK$150k -> 3.55M (11.5y backtest)", "years": "11.5",
                     "basis": "research engine: 70% stocks + 30% crypto (crypto on hold)"
                              " · 15 slots · HK$50k cap",
                     "stocks_only": "win 53.1%, CAGR 22.5%, maxDD -26.9%"}, h
        for bad in (None, {}, {"win": "x"}, {"win": 0.5, "cagr": float("nan"), "dd": -0.2,
                                             "start_hkd": 1, "final_hkd": 2, "years": 1,
                                             "slots": 15, "cap_hkd": 5e4}):
            assert B.model_headline(bad) is None, bad

        # end to end, as CI runs it after the SEC step: cards, actions, header, model
        frames = {"NVDA": e_dip(seed=1, drift=0.0050), "AAPL": e_dip(seed=2, drift=0.0049),
                  "MSFT": d3_bar(seed=3), "0700.HK": e_dip(seed=4, drift=0.0046),
                  "AMZN": walk(n=500, seed=5)}
        uni = {"tickers": list(frames), "updated": "2026-10-23", "added_log": [],
               "removed_log": []}
        prices = tmp / "prices"
        prices.mkdir()
        for t, df in frames.items():
            df.to_csv(prices / f"{D.safe_name(t)}.csv")
        shutil.copyfile(HERE.parent / "data" / "model_headline.json",
                        tmp / "data" / "model_headline.json")

        def build():
            buf = io.StringIO()
            with Patch(D, PRICE_DIR=prices, load_universe=lambda: dict(uni)), \
                    Patch(B, load_universe=lambda: dict(uni), DOCS=tmp / "docs",
                          PROD_DIR=tmp / "docs" / "products", DATA_DIR=tmp / "data",
                          POS_FILE=tmp / "data" / "positions.json",
                          LOG_FILE=tmp / "data" / "trade_log.json",
                          NAMES_CACHE=tmp / "data" / "company_names.json"), \
                    Patch(universe, prune_dead=lambda *a, **k: dict(uni)), \
                    Patch(sys, argv=["build_dashboard.py"]), contextlib.redirect_stdout(buf):
                B.main()
            data = json.loads((tmp / "docs" / "data.json").read_text(encoding="utf-8"))
            cards = {p.stem: json.loads(p.read_text(encoding="utf-8"))["card"]
                     for p in (tmp / "docs" / "products").glob("*.json")}
            return data, cards, buf.getvalue()

        data, cards, log = build()
        acts = [(a["symbol"], a.get("signal"), a.get("tier")) for a in data["actions"]]
        assert acts == [("NVDA", "E", 0), ("0700.HK", "E", 0), ("MSFT", "D3", 1)], acts
        assert data["actions"][0]["e1"]["decision"] == "keep"
        assert cards["AAPL"]["action"] == "WATCH" and cards["AAPL"]["e1"]["decision"] == "block"
        assert "e1" not in cards["0700_HK"] and cards["MSFT"]["d3"]["level_0618"] > 0
        assert data["headline"]["cagr"] == "31.8%" and data["headline"]["years"] == "11.5"
        assert data["product"].startswith("D3+E1: score>60 dips (US: EPS growing)")
        assert data["model"]["e1"]["checked"] == 2 and data["model"]["e1"]["blocked"] == 1
        assert data["model"]["d3_actions"] == 1
        assert data["model"]["e1"]["sec_status"]["state"] == "no_user_agent"
        assert "::warning::E1" not in log, log            # both US dips had files
        assert len(cards) == 5
        # the tie-break's number rides on every card and every action (t5b)
        assert all(isinstance(c.get("mom"), float) for c in cards.values()), cards
        assert [a["mom"] for a in data["actions"]] == \
            [cards[D.safe_name(a["symbol"])]["mom"] for a in data["actions"]]

        # no SEC files at all, no headline file: dips kept, arm-E header, a warning
        shutil.rmtree(sec)
        (tmp / "data" / "model_headline.json").unlink()
        data, cards, log = build()
        assert [a["symbol"] for a in data["actions"]] == ["NVDA", "AAPL", "0700.HK", "MSFT"]
        assert data["model"]["e1"]["unscored"] == 2
        assert data["model"]["e1"]["sec_status"]["state"] == "missing"
        assert "::warning::E1: 2 US dip(s) unscored" in log
        assert "model_headline.json missing or malformed" in log
        assert set(data["headline"]) == {"win", "cagr", "maxdd", "grows", "years", "basis"}
        # MODEL "E": the old product line, no tiers anywhere
        with Patch(B, MODEL="E"), Patch(P, MODEL="E"):
            data, cards, log = build()
        assert data["product"] == "D: score>60 dips + crypto trend · 15 positions (13+2)"
        assert data["model"] == {"name": "E", "code": "E"}
        assert all("tier" not in a and "signal" not in a for a in data["actions"])
        assert all("tier" not in c for c in cards.values())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("t6 build_dashboard: E1 from the SEC files only, never raises; header, product, "
          "model and tiered actions in data.json OK")


# ------------------------------------------------------------- sec_fetch ----

class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


class FakeSec:
    """An opener + clock for sec_fetch.Sec: answers by URL, counts calls."""

    def __init__(self, answers):
        self.answers, self.calls, self.now, self.slept = answers, [], 0.0, []

    def open(self, req, timeout=None):
        self.calls.append((req.full_url, req.get_header("User-agent"), timeout))
        self.now += 0.05
        a = self.answers.get(req.full_url, 404)
        if isinstance(a, list):
            a = a.pop(0) if len(a) > 1 else a[0]
        if isinstance(a, int):
            raise urllib.error.HTTPError(req.full_url, a, "err", {}, None)
        if isinstance(a, Exception):
            raise a
        return FakeResponse(a)

    def sleep(self, s):
        self.slept.append(s)
        self.now += s

    def clock(self):
        return self.now


def t7_sec_fetch():
    import sec_fetch as S
    tmp = Path(tempfile.mkdtemp(prefix="sec_t7_"))
    try:
        (tmp / "universe.json").write_text(json.dumps(
            {"tickers": ["NVDA", "AAPL", "MSFT", "NEWCO", "0700.HK", "SPY"]}))
        (tmp / "sec_cik.json").write_text(json.dumps(
            {"about": "x", "cik": {"NVDA": 1045810, "AAPL": 320193, "MSFT": 789019}}))
        frames = {"NVDA": e_dip(seed=1), "AAPL": e_dip(seed=2), "MSFT": d3_bar(seed=3),
                  "NEWCO": e_dip(seed=4), "0700.HK": e_dip(seed=5), "SPY": e_dip(seed=6)}
        sec_dir = tmp / "sec"

        def url(cik, concept):
            return S.FACT_URL.format(cik=cik, concept=concept)
        dil, bas = "EarningsPerShareDiluted", "EarningsPerShareBasic"
        fake = FakeSec({
            url(1045810, dil): json.dumps(company(rising, YEARS)).encode(),
            url(320193, dil): json.dumps({"units": {"USD/shares": []}}).encode(),   # unusable
            url(320193, bas): json.dumps(company(falling, YEARS)).encode(),
            S.TICKERS_URL: json.dumps({"0": {"cik_str": 999, "ticker": "NEWCO",
                                             "title": "New Co"}}).encode(),
            url(999, dil): 404, url(999, bas): 404})

        def factory(ua, budget=None):
            return S.Sec(ua, budget=budget, opener=fake.open, sleep=fake.sleep,
                         clock=fake.clock)

        def run(env, argv=(), model="D3E1"):
            out = io.StringIO()
            with Patch(S, SEC_DIR=sec_dir, CIK_FILE=tmp / "sec_cik.json",
                       UNIVERSE_FILE=tmp / "universe.json", MODEL=model), \
                    contextlib.redirect_stdout(out):
                rc = S.main(list(argv), load=lambda t: frames.get(t), sec_factory=factory,
                            env=env)
            st = json.loads((sec_dir / "_status.json").read_text(encoding="utf-8"))
            return rc, st, out.getvalue()

        # no User-Agent (or a header-splitting one): not one request, and says so
        for ua in ("", "   ", "name\r\nX-Evil: 1"):
            rc, st, log = run({"SEC_USER_AGENT": ua})
            assert rc == 0 and st["state"] == "no_user_agent" and fake.calls == [], (ua, st)
            assert "::warning::SEC step: no_user_agent" in log
        rc, st, _ = run({"SEC_USER_AGENT": "Signals ops@example.org"}, model="E")
        assert rc == 0 and st["state"] == "model_e" and fake.calls == []

        rc, st, log = run({"SEC_USER_AGENT": "Signals ops@example.org"})
        assert rc == 0 and st["state"] == "ok", st
        assert st["dips"] == ["NVDA", "AAPL", "NEWCO"], st["dips"]   # US E dips only
        got = [u for u, _, _ in fake.calls]
        assert got == [S.TICKERS_URL, url(1045810, dil), url(320193, dil), url(320193, bas),
                       url(999, dil), url(999, bas)], got
        assert all(h == "Signals ops@example.org" and t == config.SEC["timeout_s"]
                   for _, h, t in fake.calls)
        assert "ops@example.org" not in log                         # never printed
        assert all(abs(s - (config.SEC["spacing_s"] - 0.05)) < 1e-9 for s in fake.slept)
        assert M.facts_for(sec_dir, "NVDA")[1] == dil
        assert M.facts_for(sec_dir, "AAPL")[1] == bas
        assert json.loads(M.sec_file(sec_dir, "NEWCO", dil).read_text()) == {}
        assert st["fetched"] == 5 and st["not_found"] == 2 and st["requests"] == 6
        assert not list(sec_dir.glob("*.tmp"))

        # SEC refuses (429 then 403): retried once, recorded, still exit 0; the
        # earlier run's files for those names are gone, not reused
        fake.calls.clear()
        fake.answers[url(1045810, dil)] = [429, 403]
        rc, st, log = run({"SEC_USER_AGENT": "Signals ops@example.org"})
        assert rc == 0 and st["state"] == "partial", st
        assert "NVDA EarningsPerShareDiluted: HTTPError" in st["errors"]
        assert [u for u, _, _ in fake.calls].count(url(1045810, dil)) == 2
        assert M.facts_for(sec_dir, "NVDA") == (None, None)
        assert "::warning::SEC step: partial" in log

        # the time budget: stops between names, says which were skipped
        fake.calls.clear()
        fake.answers[url(1045810, dil)] = json.dumps(company(rising, YEARS)).encode()

        def slow_factory(ua, budget=None):
            s = factory(ua, budget=budget)
            s.t0 -= config.SEC["budget_s"] - 0.2       # 0.2 s of budget left
            return s
        with Patch(S, SEC_DIR=sec_dir, CIK_FILE=tmp / "sec_cik.json",
                   UNIVERSE_FILE=tmp / "universe.json"), contextlib.redirect_stdout(io.StringIO()):
            S.main([], load=lambda t: frames.get(t), sec_factory=slow_factory,
                   env={"SEC_USER_AGENT": "Signals ops@example.org"})
        st = json.loads((sec_dir / "_status.json").read_text(encoding="utf-8"))
        assert st["budget_hit"] and st["state"] == "partial" and st["skipped"], st

        # no price files at all; anything raising inside: status "failed", exit 0
        with Patch(S, SEC_DIR=sec_dir, UNIVERSE_FILE=tmp / "universe.json"), \
                contextlib.redirect_stdout(io.StringIO()):
            assert S.main([], load=lambda t: None, env={"SEC_USER_AGENT": "a b@c"}) == 0
        assert json.loads((sec_dir / "_status.json").read_text())["state"] == "failed"
        with Patch(S, SEC_DIR=sec_dir, UNIVERSE_FILE=tmp / "missing.json"), \
                contextlib.redirect_stdout(io.StringIO()):
            assert S.main([], env={"SEC_USER_AGENT": "a b@c"}) == 0
        st = json.loads((sec_dir / "_status.json").read_text())
        assert st["state"] == "failed" and "FileNotFoundError" in st["message"], st
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("t7 sec_fetch: no User-Agent = no request; dips only, basic only when needed, "
          "404/429/budget soft; always exits 0 OK")


def t8_committed_files():
    """The CIK map covers every US name of the universe, and the headline
    file is the owner's 2026-10-26 research figure."""
    from universe import market_of
    uni = json.loads((HERE.parent / "data" / "universe.json").read_text(encoding="utf-8"))
    cik = json.loads((HERE.parent / "data" / "sec_cik.json").read_text(encoding="utf-8"))["cik"]
    us = [t for t in uni["tickers"] if market_of(t) == "US"]
    missing = [t for t in us if t not in cik]
    # the universe grows daily; sec_fetch looks a newcomer up at run time
    assert len(missing) <= max(10, len(us) // 20), missing
    assert all(isinstance(v, int) and v > 0 for v in cik.values())
    h = json.loads((HERE.parent / "data" / "model_headline.json").read_text(encoding="utf-8"))
    assert h["model"] == "D3+E1" and h["slots"] == 15 and h["cap_hkd"] == 50000
    assert config.MODEL in ("D3E1", "E")
    print(f"t8 committed CIK map ({len(cik)} names, {len(missing)} US missing) and headline "
          f"file OK")


if __name__ == "__main__":
    t1_d3_matches_the_research_loops()
    t2_d3_last_is_the_series_end()
    t3_e1_on_sec_fixtures()
    t4_analyze_invariants()
    t5_scan_actions_order()
    t5b_ties_break_on_momentum()
    t6_build_dashboard_wiring()
    t7_sec_fetch()
    t8_committed_files()
    print("ALL MODEL-D3E1 TESTS PASS")
