"""Download & cache daily OHLCV for the whole universe (10+ years).

Cache: data/prices/<safe_ticker>.csv  — refreshed if older than 1 day.
"""
import datetime as dt
import time
from pathlib import Path

import pandas as pd
import yfinance as yf

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
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
    df.to_csv(p)
    return df


def fetch_all(force: bool = False) -> dict[str, pd.DataFrame]:
    uni = load_universe()
    out = {}
    n = len(uni["tickers"])
    for i, t in enumerate(uni["tickers"], 1):
        df = fetch_one(t, force=force)
        if df is not None and len(df) > 260:  # need at least ~1y of history
            out[t] = df
        if i % 20 == 0:
            print(f"  fetched {i}/{n}")
    print(f"Data ready: {len(out)}/{n} tickers usable")
    return out


if __name__ == "__main__":
    fetch_all()
