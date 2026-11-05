"""Fresh prices from Yahoo, EPS facts from SEC, and the store that keeps a name alive after Yahoo forgets it.

THE STORE. data/forward_scoreboard/store/bars_<close>.csv.xz: each reading saves the bars no earlier file has, as compact text
(7 significant digits), so a name that later vanishes from Yahoo does not silently leave the test. Prices are kept AS TRADED,
with dividends, splits and Yahoo's back-adjustment steps as events, so files saved months apart join cleanly whatever split came
between them. The next reading merges: fresh bars win on any day they cover, except that a dividend Yahoo has since lost is kept
(its events do come and go); a name with no fresh bars at all, or fresh bars ending before the store's, is carried and named.

No personal contact anywhere: Yahoo gets a generic client name; SEC gets SEC_USER_AGENT from the environment or a generic name.
"""
import calendar
import hashlib
import json
import lzma
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from . import model_d3e1_frozen as MD
from .rules import split_factors

COLS = ["Open", "High", "Low", "Close", "Dividends", "Splits", "AdjStep"]
YAHOO_AGENT = "Mozilla/5.0"
SEC_GENERIC_AGENT = "MultiProductResearch/1.0 (forward scoreboard; no contact supplied)"
SEC_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{:010d}/us-gaap/{}.json"
STORE_FROM = "2025-09-15"   # enough warm-up for a carried name (260 bars before the first session in every market); keeps the first file near 2 MB


class FetchFailed(Exception):
    """Yahoo or SEC could not be asked (network, rate limit). Not the same as 'no such name'."""


def to_actual(df):
    """Yahoo's split-adjusted bars -> as traded. Dividends too: Yahoo reports them split-adjusted. AdjStep is Yahoo's own
    back-adjustment at each ex-date, adjusted close / close on the bar before over the same on this bar; 1 elsewhere."""
    df = df[df["Close"] > 0].dropna(subset=["Close"])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    F = split_factors(df["Splits"].fillna(0.0).to_numpy(dtype=float))
    out = pd.DataFrame({c: df[c].to_numpy(dtype=float) * F for c in ("Open", "High", "Low", "Close", "Dividends")}, index=df.index)
    out["Splits"] = df["Splits"].fillna(0.0).to_numpy(dtype=float)
    ratio = (df["AdjClose"] / df["Close"]).ffill().bfill().fillna(1.0).to_numpy(dtype=float)
    step = np.ones(len(ratio))
    step[1:] = ratio[:-1] / ratio[1:]
    step[np.abs(step - 1.0) < 1e-5] = 1.0        # Yahoo's numbers are float32: this far from 1 is rounding, not an event
    out["AdjStep"] = step
    return out[COLS]


def bar_days(stamps, meta):
    """Yahoo's bar times as dates on the exchange's own clock. meta["gmtoffset"] is today's offset, not the bar's: FX bars open at
    00:00 London, 23:00 UTC in summer, so one offset dates every summer FX bar a day early when it is read in winter."""
    tz = meta.get("exchangeTimezoneName") or "UTC"
    return pd.DatetimeIndex(pd.to_datetime(stamps, unit="s", utc=True).tz_convert(tz).tz_localize(None).normalize())


def yahoo_bars(ticker, from_day, now):
    """As-traded daily bars from from_day on, None when Yahoo has no such name, FetchFailed when it could not be asked."""
    p1 = calendar.timegm(pd.Timestamp(from_day).timetuple())
    p2 = calendar.timegm(now.timetuple()) + 86400
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(ticker, safe="")
           + "?period1=%d&period2=%d&interval=1d&events=div%%7Csplit" % (p1, p2))
    last = None
    for k in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": YAHOO_AGENT})
            with urllib.request.urlopen(req, timeout=60) as h:
                d = json.loads(h.read())["chart"]
            if d.get("error"):
                return None
            r = d["result"][0]
            ts = r.get("timestamp")
            if not ts:
                return None
            q = r["indicators"]["quote"][0]
            day = lambda x: bar_days([x], r["meta"])[0]
            days = bar_days(ts, r["meta"])
            ac = (r["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q["close"]
            df = pd.DataFrame({"Open": q["open"], "High": q["high"], "Low": q["low"], "Close": q["close"], "AdjClose": ac}, index=days, dtype=float)
            df["Dividends"], df["Splits"] = 0.0, 0.0
            for e in (r.get("events", {}).get("dividends") or {}).values():
                if day(e["date"]) in df.index:
                    df.loc[day(e["date"]), "Dividends"] += float(e["amount"])
            for e in (r.get("events", {}).get("splits") or {}).values():
                if day(e["date"]) in df.index and e.get("denominator"):
                    df.loc[day(e["date"]), "Splits"] = float(e["numerator"]) / float(e["denominator"])
            return to_actual(df)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last = "HTTP %d" % e.code
        except Exception as e:
            last = repr(e)[:100]
        time.sleep(1.5 * (k + 1))
    raise FetchFailed(last)


def fetch_all(tickers, from_day, now, bars_fn=yahoo_bars, workers=4):
    """({ticker: bars}, {ticker: why it has none}). An error here is kept apart from a name Yahoo simply no longer has."""

    def one(t):
        try:
            return t, bars_fn(t, from_day, now), None
        except FetchFailed as e:
            return t, None, "fetch failed: %s" % e

    bars, why = {}, {}
    with ThreadPoolExecutor(workers) as ex:
        for t, b, err in ex.map(one, tickers):
            if b is not None and len(b):
                bars[t] = b
            else:
                why[t] = err or "no longer on Yahoo"
    return bars, why


def sec_facts(ticker, cik):
    """The research's choice of EPS facts (diluted, or basic when diluted has too few rows), or None. Raises when SEC cannot be asked."""
    c = cik.get(ticker)
    if c is None:
        return None
    agent = os.environ.get("SEC_USER_AGENT") or SEC_GENERIC_AGENT
    for concept in MD.CONCEPTS:
        body, last = None, None
        for k in range(3):
            try:
                time.sleep(0.15)
                req = urllib.request.Request(SEC_URL.format(int(c), concept), headers={"User-Agent": agent, "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as h:
                    body = json.loads(h.read())
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    body = {}
                    break
                last = "HTTP %d" % e.code
            except Exception as e:
                last = repr(e)[:100]
            time.sleep(1.0 * (k + 1))
        if body is None:
            raise FetchFailed(last)
        df = MD.facts_from_json(body)
        if df is not None:
            return df
    return None


# ------------------------------------------------------------------- the store
def _num(x):
    return "" if x != x else "%.7g" % x


def _store_files(store_dir):
    return sorted(Path(store_dir).glob("bars_*.csv.xz"))


def load_store(store_dir):
    """{ticker: as-traded bars} from every store file; a later file wins on a day two files share."""
    frames = [pd.read_csv(f, compression="xz", parse_dates=["date"], dtype={"ticker": str}, keep_default_na=False,
                          na_values={c: [""] for c in ("open", "high", "low", "close", "div", "split", "adj")})
              for f in _store_files(store_dir)]
    if not frames:
        return {}
    a = pd.concat(frames, ignore_index=True).drop_duplicates(["ticker", "date"], keep="last")
    a = a.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "div": "Dividends", "split": "Splits", "adj": "AdjStep"})
    a[["Dividends", "Splits"]] = a[["Dividends", "Splits"]].fillna(0.0)
    a["AdjStep"] = a["AdjStep"].fillna(1.0)
    return {t: g.set_index("date")[COLS].sort_index().rename_axis(None) for t, g in a.groupby("ticker", sort=False)}


def merge(fresh, stored, tickers, why, end):
    """(bars cut at the last finished close, {carried name: why}, [names with no bars at all])."""
    end = pd.Timestamp(end)
    bars, carried, missing = {}, {}, []
    for t in tickers:
        f, s = fresh.get(t), stored.get(t)
        if f is None and s is None:
            missing.append(t)
            continue
        if f is None:
            carried[t] = why.get(t, "no fresh bars")
            b = s
        elif s is None:
            b = f
        else:
            if s.index[-1] > f.index[-1] and s.index[-1] <= end:
                carried[t] = "fresh bars end %s, the store's later" % f.index[-1].date()
            b = f.combine_first(s)[COLS]
            both = f.index.intersection(s.index)
            lost = both[(f.loc[both, "Dividends"] == 0) & (s.loc[both, "Dividends"] > 0)]
            b.loc[lost, ["Dividends", "AdjStep"]] = s.loc[lost, ["Dividends", "AdjStep"]]
        b = b[b.index <= end]
        if t.endswith("=X"):
            b = b[b.index.weekday < 5]      # the first store holds FX rows the old date code dated a day early, a Sunday for a Monday; none after 2026-10-25
        if len(b):
            bars[t] = b
        else:
            missing.append(t)
    return bars, carried, missing


def write_store(store_dir, bars, stored, end, close_only=()):
    """Save the bars no earlier file has. close_only: rate and FX tickers, and funds, where the open, high and low are never read."""
    rows = ["ticker,date,open,high,low,close,div,split,adj"]
    n = 0
    for t in sorted(bars):
        b = bars[t]
        s = stored.get(t)
        b = b[b.index > s.index[-1]] if s is not None else b[b.index >= STORE_FROM]
        o, h, l, c, dv, sp, st = (b[k].to_numpy(dtype=float) for k in COLS)
        days = b.index.strftime("%Y-%m-%d")
        for i in range(len(b)):
            ohl = ("", "", "") if t in close_only else (_num(o[i]), _num(h[i]), _num(l[i]))
            rows.append(",".join((t, days[i]) + ohl + (_num(c[i]), _num(dv[i]) if dv[i] else "", _num(sp[i]) if sp[i] else "", "" if st[i] == 1.0 else _num(st[i]))))
            n += 1
    Path(store_dir).mkdir(parents=True, exist_ok=True)
    path = Path(store_dir) / ("bars_%s.csv.xz" % end)
    body = lzma.compress(("\n".join(rows) + "\n").encode("ascii"), preset=9 | lzma.PRESET_EXTREME)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(body)
    os.replace(tmp, path)
    return {"file": path.name, "bytes": len(body), "rows": n, "sha256": hashlib.sha256(body).hexdigest()}


def store_bytes(store_dir):
    return sum(f.stat().st_size for f in _store_files(store_dir))
