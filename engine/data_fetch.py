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


def _live_quote(ticker: str) -> tuple[float | None, dt.date | None]:
    """Last traded price from the quote endpoint (not the daily-bar series), and
    the DATE of that trade on the exchange's own clock (None if Yahoo gives no
    time). One Ticker object serves both: fast_info's price lookup already loads
    the history metadata that carries regularMarketTime, so the date normally
    costs no extra request."""
    try:
        tk = yf.Ticker(ticker)
        fi = tk.fast_info
    except Exception:
        return None, None
    px = None
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
            px = v
            break
    if px is None:
        return None, None
    day = None
    try:
        md = tk.get_history_metadata() or {}
        ts = md.get("regularMarketTime")
        if ts is not None:
            # epoch seconds from the chart API; some yfinance builds pre-format it
            t = (pd.Timestamp(int(ts), unit="s", tz="UTC") if pd.api.types.is_number(ts)
                 else pd.Timestamp(ts))
            if t.tzinfo is None:
                t = t.tz_localize("UTC")
            day = t.tz_convert(md.get("exchangeTimezoneName") or "UTC").date()
    except Exception:
        day = None
    return px, day


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

    A missing close is NOT proof of a session (review 2026-09-17). yf.download
    lines a batch up on the UNION of its tickers' dates, so a US name batched
    with HK names got an all-NaN row for a day only Hong Kong had traded, and
    this function filled it O=H=L=C from yesterday's close: a flat bar that
    never happened. Its true range is 0, so ATR fell by 1/14 (7.1%), and
    ib_bot's max(stop, hw - k*ATR) saved a trail 0.25 ATR tighter into
    state.json for good. So a row is only filled when its OWN exchange traded
    that day:
      - Yahoo sent part of the bar (an Open, High, Low or non-zero Volume) -
        a union row never has any of these; or
      - the quote's last regular-session trade is dated that row's day on the
        exchange's clock.
    The second test is what keeps the Tokyo fix alive. Only the null close of
    5301.T's 31 Aug bar was ever inspected, and 8b98d34 was verified on a bar
    with EVERY field null. yfinance (keepna=False) drops an all-null row
    outright, so a bar of that shape can only reach here as a union row - from
    the Tokyo names that did have 31 Aug in 5301.T's all-Tokyo batch, where the
    fix took live. An emptiness test alone would have dropped it; its quote was
    stamped 2026-08-31 06:30 UTC, 15:30 JST, the same day. A row with neither
    keeps its NaN close and _clean's dropna removes it, exactly as before
    8b98d34.
    """
    if not ticker or df is None or df.empty or "Close" not in df.columns:
        return df
    try:
        if not pd.isna(df["Close"].iloc[-1]):
            return df                        # newest bar already has a close
        i = df.index[-1]
        partial = any(c in df.columns and not pd.isna(df.at[i, c])
                      for c in ("Open", "High", "Low"))
        if not partial and "Volume" in df.columns:
            v = df.at[i, "Volume"]
            partial = not pd.isna(v) and float(v) > 0
        px, day = _live_quote(ticker)
        if not px:
            return df                        # no quote either - drop it as before
        bar_day = pd.Timestamp(i).date()
        if not partial and day != bar_day:
            print(f"  ~ {ticker}: newest row {bar_day} is empty and the last trade "
                  f"was {day} - no session that day, row dropped")
            return df                        # NaN close -> dropna removes it
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


def _calendar_group(ticker: str) -> str:
    """Which trading calendar a ticker's daily bars follow, as a download-batch
    key. Yahoo suffixes name the exchange (.HK, .T, .DE, .L ...), so every EU
    venue keeps its own holidays (LSE shut on 31 Aug 2026 while Xetra traded).
    Unsuffixed tickers are US listings - stocks, ETFs and bond/leveraged ETFs all
    share the NYSE/Nasdaq calendar. Indices and futures each keep a group of
    their own: ^GSPC/^HSI/^N225 or CME/ICE contracts do not share holidays."""
    t = ticker.upper()
    if t.startswith("^") or t.endswith("=F"):
        return t
    if t.endswith("-USD"):
        return "CRYPTO"                      # trades every day, weekends included
    if t.endswith("=X"):
        return "FX"
    if "." in t:
        return t.rsplit(".", 1)[1]
    return "US"


def _download_chunks(tickers: list[str], size: int) -> list[list[str]]:
    """Batches of at most `size` that never mix calendars (review 2026-09-17).
    yf.download reindexes every ticker in a batch onto the union of all their
    dates, so a mixed batch hands a ticker an empty row for any day only another
    market traded - chunk 0 was 50 US + 30 HK, so every build between the Asian
    and US opens gave each of those US names a phantom row for today, and the
    crypto in chunk 160 did the same to its equities every weekend. Seen live at
    01:10 UTC on 2026-09-17: AAPL and 0700.HK batched with 7203.T each got an
    empty 17 Sep row, and the old fill cut both ATRs by 7.14%. Universe order is
    kept within a group."""
    groups: dict[str, list[str]] = {}
    for t in tickers:
        groups.setdefault(_calendar_group(t), []).append(t)
    return [g[i:i + size] for g in groups.values() for i in range(0, len(g), size)]


def fetch_all(force: bool = False) -> dict[str, pd.DataFrame]:
    """Load fresh-cached tickers, then BATCH-download the misses in chunks of 80
    (one multi-ticker request instead of hundreds of singles) with a per-ticker
    fallback. Makes a ~1,000-product universe fetch in minutes. A chunk only
    ever holds tickers from ONE trading calendar - see _download_chunks."""
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
    done = 0
    for chunk in _download_chunks(need, CHUNK):
        done += len(chunk)
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
        print(f"  fetched {done}/{len(need)} new "
              f"(+{len([t for t in out if t in uni['tickers']]) - 0} total ready)")
    print(f"Data ready: {len(out)}/{len(uni['tickers'])} tickers usable")
    return out


if __name__ == "__main__":
    fetch_all()
