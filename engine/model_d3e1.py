"""D3 + E1: the second entry signal and the US earnings filter (MODEL "D3E1").

Owner decision 2026-10-26. Pure logic: no network, no numba, nothing written.
The only I/O is facts_for(), which READS the SEC files engine/sec_fetch.py
saved; production.analyze() never touches a file.

D3 - "held the 0.618": a pullback in a strong uptrend that touched the 0.618
retracement of a recent swing without breaking the 0.886 floor, and closed
back up. The research built it with numba (ideas/features.py fib + fib_hold);
fib_swing/fib_hold here are numpy ports that matched it on 889 tickers and
2,667,808 bars with 0 mismatches (a one-off check on 2026-10-26, re-run against
this module). Parameters: config.D3.

E1 - a FILTER on US E dips: keep the dip only if trailing-12-month EPS grew,
from SEC EDGAR companyconcept facts, point in time by filing date. Ported
from the research (ideas/lynch.py facts/prep_facts/ttm_growth, the reviewer's
lfix.quarters_asof_fix, lfix3.nice2 and lfix5.events5 split detection, plus
the 200-day age guard of live/live_e1.py 'fixed'); the port matched
the research's decisions on every US E bar it scored (5,937 of 5,937). A name
that cannot be scored is KEPT - the research did the same, and it means any
SEC failure falls back to today's plain E rule. Parameters: config.E1.
"""
import json
import re
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from config import D3, E1, MIN_ATR_BY_MARKET

# ------------------------------------------------------------------- D3 ----


def fib_swing(h, l, look_hi=D3["look_hi"], look_lo=D3["look_lo"], min_swing=D3["min_swing"]):
    """(SH, SL) per bar: SH = the highest High of the last look_hi bars (the
    FIRST one on ties, as argmax in the research), SL = the lowest Low of the
    look_lo+1 bars ending at that high. NaN where the swing is under
    min_swing, and before bar look_hi + look_lo (the research loop starts
    there). A NaN High or Low in a window gives no swing, as numba's min did."""
    h = np.asarray(h, dtype=float)
    l = np.asarray(l, dtype=float)
    m = len(h)
    SH = np.full(m, np.nan)
    SL = np.full(m, np.nan)
    start = look_hi + look_lo
    if m <= start:
        return SH, SL
    win = sliding_window_view(h, look_hi)              # win[k] = h[k : k + look_hi]
    jh = np.argmax(win, axis=1) + np.arange(len(win))   # first max (a NaN counts as max)
    lmin = pd.Series(l).rolling(look_lo + 1).min().to_numpy()   # min l[j - look_lo .. j]
    i = np.arange(start, m)
    j = jh[i - look_hi + 1]
    sh, sl = h[j], lmin[j]
    with np.errstate(invalid="ignore", divide="ignore"):
        ok = (sh - sl) / sl >= min_swing
    SH[i[ok]] = sh[ok]
    SL[i[ok]] = sl[ok]
    return SH, SL


def fib_hold(SH, SL, l, c, level=D3["level"], floor_level=D3["floor"], tol=D3["tol"],
             look=D3["look"]):
    """Bar i holds the level: the lowest Low of the last `look` bars is at most
    level x (1 + tol) and at least the floor, and the close is above the level
    and above the previous close. False for the first `look` bars, as the
    research loop never visited them."""
    l = np.asarray(l, dtype=float)
    c = np.asarray(c, dtype=float)
    m = len(c)
    out = np.zeros(m, bool)
    if m <= look:
        return out
    lo = pd.Series(l).rolling(look).min().to_numpy()
    rng = SH - SL
    lv = SH - level * rng
    fl = SH - floor_level * rng
    cp = np.concatenate([[np.nan], c[:-1]])
    with np.errstate(invalid="ignore"):
        out = (np.isfinite(SH) & np.isfinite(SL) & (lo <= lv * (1 + tol)) & (lo >= fl)
               & (c > lv) & (c > cp))
    out[:look] = False
    return out


def d3_series(f: pd.DataFrame, mkt: str) -> np.ndarray:
    """D3 on every bar of an add_features() frame. All False outside D3's
    markets. The trend uses the RAW ATR (it must be finite - the card's 2%
    stand-in for a missing ATR does not qualify) and momentum with NaN as a
    fail, exactly as the research did."""
    n = len(f)
    if mkt not in D3["markets"] or n == 0:
        return np.zeros(n, bool)
    h = f["High"].to_numpy(dtype=float)
    l = f["Low"].to_numpy(dtype=float)
    c = f["Close"].to_numpy(dtype=float)
    SH, SL = fib_swing(h, l)
    st = f["sma_trend"].to_numpy(dtype=float)
    sf = f["sma_fast"].to_numpy(dtype=float)
    atr = f["atr"].to_numpy(dtype=float)
    mom = f["mom_90"].to_numpy(dtype=float)
    mom = np.where(np.isfinite(mom), mom, -1.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        trend = (np.isfinite(st) & (c > st) & (sf > st) & (mom > D3["min_mom"])
                 & np.isfinite(atr)
                 & (atr / c > MIN_ATR_BY_MARKET.get(mkt, D3["min_atr_pct"])))
    return fib_hold(SH, SL, l, c) & trend


# The last bar only needs look_hi + look_lo bars behind it (its swing high is
# at most look_hi - 1 bars back, and that high's low window look_lo more), so
# the tail gives exactly the full-series answer at a fraction of the cost.
_D3_TAIL = D3["look_hi"] + D3["look_lo"] + 40


def d3_last(f: pd.DataFrame, mkt: str):
    """(True, levels) when the LAST bar of an add_features() frame is a D3
    signal, else (False, None). levels = the card's "d3" block: swing_high,
    swing_low, level_0618, floor_0886, low5. Equal to d3_series(f, mkt)[-1]:
    the indicators come from the whole frame, only the swing scan is cut."""
    if mkt not in D3["markets"] or len(f) == 0:
        return False, None
    t = f.tail(_D3_TAIL)
    if not bool(d3_series(t, mkt)[-1]):
        return False, None
    SH, SL = fib_swing(t["High"].to_numpy(dtype=float), t["Low"].to_numpy(dtype=float))
    sh, sl = float(SH[-1]), float(SL[-1])
    rng = sh - sl
    low5 = float(t["Low"].to_numpy(dtype=float)[-D3["look"]:].min())
    return True, {"swing_high": round(sh, 4), "swing_low": round(sl, 4),
                  "level_0618": round(sh - D3["level"] * rng, 4),
                  "floor_0886": round(sh - D3["floor"] * rng, 4),
                  "low5": round(low5, 4)}


# ------------------------------------------------------------------- E1 ----

CONCEPTS = ("EarningsPerShareDiluted", "EarningsPerShareBasic")
# split ratios the restatement detector may recognise (forward and reverse)
_FWD = [1.5] + list(range(2, 11)) + [12, 15, 20, 25, 30, 40, 50, 100]
_NICE = _FWD + [1 / k for k in range(2, 21)]


def sec_file(sec_dir, ticker: str, concept: str) -> Path:
    """Where sec_fetch.py saves one ticker's concept and the build reads it.
    Its own naming (data/sec is not the price cache): anything outside
    letters, digits, '_' and '-' becomes '_'."""
    return Path(sec_dir) / f"{re.sub(r'[^A-Za-z0-9_-]', '_', ticker)}_{concept}.json"


def facts_from_json(d):
    """The usable 10-Q/10-K rows of one companyconcept response as a frame,
    or None when there are fewer than E1["min_rows"] (lynch.facts)."""
    if not isinstance(d, dict):
        return None
    u = d.get("units", {})
    if not isinstance(u, dict):
        return None
    rows = u.get("USD/shares") or next(iter(u.values()), [])
    rows = [r for r in rows if isinstance(r, dict)
            and str(r.get("form", "")).startswith(("10-Q", "10-K")) and "start" in r]
    return pd.DataFrame(rows) if len(rows) >= E1["min_rows"] else None


def facts_for(sec_dir, ticker: str):
    """(frame, concept) from the saved SEC files: diluted, or basic when
    diluted has too few rows - as the research chose. (None, None) when
    neither file exists or has enough rows. Reads local files only."""
    for concept in CONCEPTS:
        p = sec_file(sec_dir, ticker, concept)
        if not p.exists():
            continue
        df = facts_from_json(json.loads(p.read_text(encoding="utf-8")))
        if df is not None:
            return df, concept
    return None, None


def prep_facts(df):
    df = df.copy()
    for k in ("start", "end", "filed"):
        df[k] = pd.to_datetime(df[k])
    df["dur"] = (df["end"] - df["start"]).dt.days
    df["kind"] = np.select([df.dur.between(80, 100), df.dur.between(170, 190),
                            df.dur.between(260, 285), df.dur.between(350, 380)],
                           ["Q", "H", "9M", "FY"], "x")
    return df[df.kind != "x"].sort_values("filed")


def nice2(vp, vn, known):
    """The closest split ratio n with vp / known ~= n x vn, allowing for both
    values being rounded to 2 decimals; None if none fits (lfix3.nice2)."""
    r = vp / vn / known
    if r <= 0:
        return None
    n = min(_NICE + [1.0], key=lambda x: abs(np.log(r / x)))
    ok = abs(vp / known - n * vn) <= n * 0.0051 + 0.0051 / known + 0.015 * abs(vp / known)
    return n if ok else None


def pairs(df):
    """{filing date: [(previous filing date, previous value, new value)]} for
    every period a later filing reported again (lfix5.pairs)."""
    out = {}
    for _, x in df.groupby(["start", "end", "kind"]):
        if len(x) < 2:
            continue
        x = x.sort_values("filed")
        v, f = x.val.to_numpy(), x.filed.to_numpy()
        for a in range(1, len(v)):
            if abs(v[a]) < 0.03 or abs(v[a - 1]) < 0.03 or v[a] * v[a - 1] <= 0:
                continue
            out.setdefault(pd.Timestamp(f[a]), []).append((pd.Timestamp(f[a - 1]), v[a - 1], v[a]))
    return out


def events5(df):
    """Split restatements as [(filing date, ratio)], oldest first (lfix5):
    two or more periods restated by one ratio in one filing make an event; a
    single one is tentative until a later filing within 200 days agrees."""
    P = pairs(df)
    conf, tent = [], []

    def resid(evs, fd):
        return [nice2(vp, vn, float(np.prod([n for d, n in evs if fp < d <= fd])) if evs else 1.0)
                for fp, vp, vn in P[fd]]
    for fd in sorted(P):
        tent = [(d, n) for d, n in tent if (fd - d).days <= 200]
        for T in list(tent):
            span = [k for k, (fp, vp, vn) in enumerate(P[fd]) if fp < T[0] <= fd]
            if not span:
                continue
            r_all = resid(conf + tent, fd)
            ok = sum(r_all[k] == 1.0 for k in span)
            if ok * 2 > len(span):
                conf.append(T)
            tent.remove(T)
        r = resid(conf + tent, fd)
        vals = [x for x in r if x is not None and x != 1.0]
        if vals:
            n, c = Counter(vals).most_common(1)[0]
            (conf if c >= 2 else tent).append((fd, n))
    return sorted(conf)


def quarters_asof_fix(df, d, ev):
    """{quarter end: EPS} known on day d: the latest filing of each period
    filed <= d, split-restated values divided back, Q4 = FY - 9M (or FY minus
    the three quarters inside it) (lfix.quarters_asof_fix)."""
    k = df[df.filed <= d]
    if k.empty:
        return {}
    last = k.groupby(["start", "end", "kind"], as_index=False).last()
    if ev:
        fac = np.ones(len(last))
        for ed, n in ev:
            if ed <= d:
                fac = np.where(last.filed.to_numpy() < np.datetime64(ed), fac * n, fac)
        last = last.assign(val=last.val / fac)
    q = {r.end: r.val for r in last[last.kind == "Q"].itertuples()}
    for r in last[last.kind == "FY"].itertuples():
        if r.end in q:
            continue
        nine = last[(last.kind == "9M") & (last.start == r.start)]
        if len(nine):
            q[r.end] = r.val - nine.val.iloc[-1]
            continue
        inside = [v for e, v in q.items() if r.start < e < r.end]
        if len(inside) == 3:
            q[r.end] = r.val - sum(inside)
    return q


def ttm_growth(q):
    """(growth, ttm now, ttm a year before, newest quarter end) from the last
    8 quarters, or None: fewer than 8, a gap outside 60-120 days, or a prior
    TTM of exactly 0 (lynch.ttm_growth)."""
    ends = sorted(q)
    if len(ends) < 8:
        return None
    e = ends[-8:]
    gaps = np.diff([x.toordinal() for x in e])
    if not all(60 <= g <= 120 for g in gaps):
        return None
    now = sum(q[x] for x in e[-4:])
    then = sum(q[x] for x in e[:4])
    if then == 0:
        return None
    return (now - then) / abs(then), now, then, e[-1]


def _unscored(why):
    return {"decision": "unscored", "growth": None, "qend": None, "why": why}


def _day(asof):
    """A bar date as the naive Timestamp the filing dates are (a price index
    read with a timezone must not make every comparison raise)."""
    t = pd.Timestamp(asof)
    return t.tz_localize(None) if t.tzinfo is not None else t


def e1_decision(df, ev, asof, max_age=E1["max_age_days"]):
    """E1 on day `asof` from prepped facts and their split events:
    {"decision": "keep" | "block" | "unscored", "growth", "qend", "why",
    "ttm_now", "ttm_prior"}. Unscored (kept) when ttm_growth has no answer or
    the newest quarter ended more than max_age days before asof."""
    asof = _day(asof)
    b = ttm_growth(quarters_asof_fix(df, asof, ev))
    if b is None:
        return _unscored("fewer than 8 regular quarters filed, or a prior TTM of 0")
    growth, now, then, qend = b
    if (asof - qend).days > max_age:
        return _unscored(f"newest quarter ended {qend.date()}, over {max_age} days before")
    return {"decision": "keep" if growth > 0 else "block",
            "growth": round(float(growth), 4), "qend": qend.date().isoformat(),
            "why": "TTM EPS growth", "ttm_now": round(float(now), 4),
            "ttm_prior": round(float(then), 4)}


def e1_asof(df, asof, max_age=E1["max_age_days"]):
    """E1 for one ticker on day `asof` from its raw facts frame (facts_for):
    strictly point in time - only filings dated <= asof are read, split
    detection included. The research ran split detection on every filing it
    had; a live build has nothing filed later than today anyway, so this only
    keeps a filing that lands between the bar and the build out of it."""
    if df is None:
        return _unscored("no SEC data")
    asof = _day(asof)
    p = prep_facts(df)
    p = p[p.filed <= asof]
    return e1_decision(p, events5(p), asof, max_age)


def card_e1(r):
    """The card's "e1" block (the shared data.json contract): decision,
    growth and qend only."""
    return {"decision": r.get("decision", "unscored"), "growth": r.get("growth"),
            "qend": r.get("qend")}
