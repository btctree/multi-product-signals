"""The per-country market trend filter's inputs: data.json["market_trend"].

    from market_trend import market_trend
    payload["market_trend"] = market_trend(data)     # build_dashboard.main()

The rule the owner asked to test (2026-10-26, "test the country filter only,
starting in log-only mode"), as backtested in the live simulator: no new buy
in a market while its home index - US ^GSPC, HK ^HSI, JP ^N225, EU ^STOXX50E -
closes below its 100-day simple moving average, judged on the last FINISHED
index bar. Exits are never gated. Measured with D3+E1: about the same return
as no gate, the worst falls 5-6 points smaller.

This module only publishes each index against its average. It decides nothing:
ib_bot reads the key in SHADOW mode (MARKET_FILTER = "shadow"), logs and tags
the orders the filter would have stopped, and sends exactly the orders it
would send without it. The digest and the page show the same four rows.

One row per market:
    {"index": "^GSPC", "name": "S&P 500", "close": 7623.7, "sma100": 7603.7,
     "pct_vs_sma100": 0.26, "below": false, "bar": "2026-10-23"}
pct_vs_sma100 is in PERCENT (-5.96 = 5.96% below the average); `below` is
close < sma100, worked out on the unrounded numbers, as the simulator did
(s < s.rolling(100).mean(), the average including the bar itself).

FINISHED BARS ONLY. Yahoo fills today's bar with the live price while a market
trades, and the hourly build downloads mid-session for some market at every
hour of the day. A bar counts only when the download behind the index's price
file STARTED at or after that market's close + 90 minutes - the same settle
rule the bot applies to every card (execution/market_clock.py, loaded here
from its file so there is one session table, not two copies that could
drift). A newer bar in the file is dropped, and the row is judged on the one
before it. The start is the index's own (data_fetch.download_started); a file
with no recorded start is not guessed at - that market gets no row.

READ ONLY, NEVER RAISES. It reads the frames the build already holds, else
the cached price file - it never downloads (fetch_one could). Any failure for
a market - no file, under 100 finished bars, a bad number, no settle time -
leaves that market out with a ::warning:: in the build log; the bot reads a
missing market as "unknown" and changes nothing. The build stays green: a
failed build leaves data.json stale, and the bot then defers every holding's
exit.
"""
import datetime as dt
import importlib.util
import math
from pathlib import Path

import pandas as pd

import data_fetch as D

# market -> (index, label). The index set is engine/universe.py INDICES, which
# the hourly build downloads with the rest of the universe.
MARKETS = {
    "US": ("^GSPC", "S&P 500"),
    "HK": ("^HSI", "Hang Seng"),
    "JP": ("^N225", "Nikkei 225"),
    "EU": ("^STOXX50E", "Euro Stoxx 50"),
}
SMA_DAYS = 100
# A newest finished bar more than this many weekdays before the market's last
# finished day means Yahoo stopped updating the index: the row is not vouched
# for. 5 clears the longest holiday runs (Golden Week, Lunar New Year).
MAX_BAR_AGE_WEEKDAYS = 5

# Each market's row in market_clock's session table, by Yahoo suffix: "" is
# New York, .HK Hong Kong (closing auction to 16:10), .T Tokyo, .DE Frankfurt
# 09:00-17:30 CET - the Euro Stoxx 50 is computed on its constituents' clock.
_SESSION_OF = {"US": "", "HK": ".HK", "JP": ".T", "EU": ".DE"}

_CLOCK = {}


def _market_clock():
    """execution/market_clock.py, loaded once by path. Stdlib only, and nothing
    under execution/ joins sys.path - the build must not import bot code by
    accident through a shadowed module name."""
    if "m" not in _CLOCK:
        path = Path(__file__).resolve().parent.parent / "execution" / "market_clock.py"
        spec = importlib.util.spec_from_file_location("_mps_market_clock", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _CLOCK["m"] = mod
    return _CLOCK["m"]


def last_finished_day(market, started):
    """The newest local trading day whose bar was final when a download that
    began at `started` (aware UTC datetime) began: the day of the latest
    weekday close + market_clock.SESSION_SETTLE_MIN at or before it. None when
    the clock cannot say (no zone data) - the caller must not guess."""
    mc = _market_clock()
    suffix = _SESSION_OF[market]
    # market_clock keys on a symbol's suffix; "X" + suffix is not a ticker
    settle = mc.last_settled_close("X" + suffix, started)
    if settle is None:
        return None
    zone = mc.MARKET_SESSIONS[suffix][0]
    from zoneinfo import ZoneInfo
    close = settle - dt.timedelta(minutes=mc.SESSION_SETTLE_MIN)
    return close.astimezone(ZoneInfo(zone)).date()


def _closes(ticker, frames):
    """The index's daily closes from the build's own frame, else its cached
    price file - never a download."""
    df = (frames or {}).get(ticker)
    if df is None:
        p = D.cache_path(ticker)
        if not p.exists():
            raise FileNotFoundError(f"no cached price file for {ticker}")
        df = pd.read_csv(p, index_col=0, parse_dates=True)
    s = pd.to_numeric(df["Close"], errors="coerce")
    s.index = pd.to_datetime(s.index)
    s = s[~s.index.duplicated(keep="last")].sort_index().dropna()
    return s[s > 0]


def trend_row(market, closes, started):
    """One market's row from its daily closes and the download start. Raises
    on anything it cannot vouch for; market_trend() turns that into a warning."""
    ticker, name = MARKETS[market]
    if started is None:
        raise ValueError("no recorded download start for its price file - the "
                         "newest bar cannot be told finished, so it is not guessed at")
    last = last_finished_day(market, started)
    if last is None:
        raise ValueError("no settle time for this market (zone data unreadable)")
    done = closes[[d.date() <= last for d in closes.index]]
    if len(done) < SMA_DAYS:
        raise ValueError(f"only {len(done)} finished bars, {SMA_DAYS} needed")
    newest = done.index[-1].date()
    age = len(pd.bdate_range(newest, last)) - 1
    if age > MAX_BAR_AGE_WEEKDAYS:
        raise ValueError(f"newest bar {newest} is {age} weekdays before the last "
                         f"finished day {last} - the index stopped updating")
    close = float(done.iloc[-1])
    sma = float(done.iloc[-SMA_DAYS:].mean())
    if not (math.isfinite(close) and math.isfinite(sma) and close > 0 and sma > 0):
        raise ValueError(f"unusable numbers (close {close}, sma100 {sma})")
    return {"index": ticker, "name": name,
            "close": round(close, 2), "sma100": round(sma, 2),
            "pct_vs_sma100": round((close / sma - 1.0) * 100.0, 2),
            "below": bool(close < sma),
            "bar": done.index[-1].strftime("%Y-%m-%d")}


def _started(ticker):
    s = D.download_started([ticker])
    if s is None:
        return None
    return dt.datetime.strptime(s, D.STAMP_FMT).replace(tzinfo=dt.timezone.utc)


def market_trend(frames=None, started_of=None):
    """{"US": row, "HK": row, "JP": row, "EU": row}, leaving out any market
    that fails (each with a ::warning::). frames: the build's {ticker:
    DataFrame}; started_of(ticker) -> aware UTC datetime or None, the start of
    the download that wrote the ticker's file (default: data_fetch's record).
    Never raises."""
    started_of = started_of or _started
    out = {}
    for market, (ticker, _name) in MARKETS.items():
        try:
            out[market] = trend_row(market, _closes(ticker, frames), started_of(ticker))
        except Exception as e:
            print(f"::warning::market trend: {market} ({ticker}) left out - "
                  f"{type(e).__name__}: {str(e)[:160]}; the bot reads it as unknown")
    return out
