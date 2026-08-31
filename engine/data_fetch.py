"""Download & cache daily OHLCV for the whole universe (10+ years).

Cache: data/prices/<safe_ticker>.csv  — refreshed if older than 1 day.
"""
import datetime as dt
import time
from pathlib import Path

import pandas as pd
import yfinance as yf  # noqa: batch + single fetch

from config import DATA_DIR, BACKTEST_YEARS
from universe import load_universe

PRICE_DIR = DATA_DIR / "prices"


def safe_name(ticker: str) -> str:
    return ticker.replace("^", "_IDX_").replace("=", "_EQ_").replace(".", "_")


def cache_path(ticker: str) -> Path:
    return PRICE_DIR / f"{safe_name(ticker)}.csv"


def fetch_one(ticker: str, force: bool = False) -> pd.DataFrame | None:
    PRICE_DIR.mkdir(parents=True, exist_ok=True)
    p = cache_path(ticker)
    if p.exists() and not force:
        age_h = (time.time() - p.stat().st_mtime) / 3600
        if age_h < 20:
            df = pd.read_csv(p, index_col=0, parse_dates=True)
            if len(df) > 100:
                return df
    start = (dt.date.today() - dt.timedelta(days=int(365.25 * (BACKTEST_YEARS + 1.2)))).isoformat()
    try:
        df = yf.download(ticker, start=start, interval="1d",
                         auto_adjust=True, progress=False, threads=False)
    except Exception as e:
        print(f"  ! {ticker}: {e}")
        return None
    if df is None or df.empty:
        print(f"  ! {ticker}: no data")
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = _clean(df, ticker)
    if df is None:
        print(f"  ! {ticker}: no usable rows after clean")
        return None
    df.to_csv(p)
    return df


def _live_price(ticker: str) -> float | None:
    """Last traded price from the quote endpoint (not the daily-bar series)."""
    try:
        fi = yf.Ticker(ticker).fast_info
    except Exception:
        return None
    for k in ("last_price", "lastPrice", "regular_market_price", "regularMarketPrice"):
        try:
            v = fi[k]
        except Exception:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v > 0:
            return v
    return None


def _fill_last_close(df: pd.DataFrame, ticker: str | None) -> pd.DataFrame:
    """Yahoo can leave the NEWEST daily bar's Close empty for hours after an
    exchange settles - Tokyo sat null for 11+ hours on 2026-08-31. dropna() then
    throws that finished session away and the engine reads the PREVIOUS day's
    close as "latest". That priced 5301.T at 1,681.5 instead of 1,811.0 and
    manufactured a trailing-stop exit (stop 1,686.65) on a position that was up
    7.7% and clear of its stop by 7%.

    Only the NEWEST row is touched, and only when its Close is missing - bars
    Yahoo has already finalised are never rewritten. Where an exchange is still
    open Yahoo already fills the in-progress bar with the live price, so this is
    a no-op there; it only stops a COMPLETED session from being discarded.
    """
    if not ticker or df is None or df.empty or "Close" not in df.columns:
        return df
    try:
        if not pd.isna(df["Close"].iloc[-1]):
            return df                        # newest bar already has a close
        px = _live_price(ticker)
        if not px:
            return df                        # no quote either - drop it as before
        i = df.index[-1]
        df.loc[i, "Close"] = px
        for c in ("Open", "High", "Low"):    # keep the row internally consistent
            if c in df.columns and pd.isna(df.at[i, c]):
                df.loc[i, c] = px
        print(f"  ~ {ticker}: newest bar had no close - filled from live quote {px}")
    except Exception as e:
        print(f"  ! {ticker}: live-close fill skipped ({e})")
    return df


def _clean(df: pd.DataFrame, ticker: str | None = None) -> pd.DataFrame | None:
    if df is None or df.empty:
        return None
    df = _fill_last_close(df, ticker)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
    return df if len(df) > 0 else None


def fetch_all(force: bool = False) -> dict[str, pd.DataFrame]:
    """Load fresh-cached tickers, then BATCH-download the misses in chunks of 80
    (one multi-ticker request instead of hundreds of singles) with a per-ticker
    fallback. Makes a ~1,000-product universe fetch in minutes."""
    import os
    max_h = float(os.environ.get("CACHE_MAX_H", 20))   # research: raise to reuse
    uni = load_universe()                              # day-old caches when Yahoo throttles
    out, need = {}, []
    for t in uni["tickers"]:
        p = cache_path(t)
        if p.exists() and not force:
            if (time.time() - p.stat().st_mtime) / 3600 < max_h:
                df = pd.read_csv(p, index_col=0, parse_dates=True)
                if len(df) > 260:
                    out[t] = df
                    continue
        need.append(t)

    start = (dt.date.today() - dt.timedelta(days=int(365.25 * (BACKTEST_YEARS + 1.2)))).isoformat()
    PRICE_DIR.mkdir(parents=True, exist_ok=True)
    CHUNK = 80
    for i in range(0, len(need), CHUNK):
        chunk = need[i:i + CHUNK]
        got = set()
        try:
            raw = yf.download(chunk, start=start, interval="1d", auto_adjust=True,
                              progress=False, group_by="ticker", threads=True)
            if raw is not None and not raw.empty:
                for t in chunk:
                    try:
                        sub = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
                        df = _clean(sub.copy(), t)
                        if df is not None and len(df) > 260:
                            df.to_csv(cache_path(t))
                            out[t] = df
                            got.add(t)
                    except Exception:
                        pass
        except Exception as e:
            print(f"  ! batch failed ({chunk[0]}..): {e}")
        for t in [x for x in chunk if x not in got]:   # per-ticker fallback
            df = fetch_one(t, force=force)
            if df is not None and len(df) > 260:
                out[t] = df
        print(f"  fetched {min(i + CHUNK, len(need))}/{len(need)} new "
              f"(+{len([t for t in out if t in uni['tickers']]) - 0} total ready)")
    print(f"Data ready: {len(out)}/{len(uni['tickers'])} tickers usable")
    return out


if __name__ == "__main__":
    fetch_all()
