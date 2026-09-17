"""Download & cache daily OHLCV for the whole universe (10+ years).

Cache: data/prices/<safe_ticker>.csv  — refreshed if older than 1 day.
Download starts: data/prices/_download_started.json - see download_started().
"""
import datetime as dt
import json
import os
import time
from pathlib import Path

import pandas as pd
import yfinance as yf  # noqa: batch + single fetch

from config import DATA_DIR, BACKTEST_YEARS
from universe import load_universe

PRICE_DIR = DATA_DIR / "prices"

# ISO-8601 UTC, whole seconds: the "generated_at" contract with the bot.
STAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"
STARTS_NAME = "_download_started.json"


def safe_name(ticker: str) -> str:
    return ticker.replace("^", "_IDX_").replace("=", "_EQ_").replace(".", "_")


def cache_path(ticker: str) -> Path:
    return PRICE_DIR / f"{safe_name(ticker)}.csv"


def utc_stamp(when: dt.datetime | None = None) -> str:
    when = when or dt.datetime.now(dt.timezone.utc)
    return when.astimezone(dt.timezone.utc).strftime(STAMP_FMT)


def _record_download_start(tickers, started: str) -> None:
    """Remember, per ticker, when the download that wrote its price file began.

    Why (review 2026-09-17, "The settle check uses the bot's own clock, not the
    time the card was built"): the bot needs to know how old a card's prices
    are, and the build cannot tell - CI downloads in one process (data_fetch.py)
    and builds in another (build_dashboard.py, whose own fetch_all mostly reads
    the files the first step wrote). A file beside the prices is what both steps
    share. Written after the price files, so a crash in between leaves a file
    with an older start or none - never one claiming to be newer than it is."""
    if not tickers:
        return
    PRICE_DIR.mkdir(parents=True, exist_ok=True)
    path = PRICE_DIR / STARTS_NAME
    try:
        starts = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(starts, dict):
            starts = {}
    except Exception:
        starts = {}
    for t in tickers:
        starts[t] = started
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(starts, indent=0, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def download_started(tickers) -> str | None:
    """The UTC time ("YYYY-MM-DDTHH:MM:SSZ") the price download behind these
    tickers STARTED: the earliest recorded start among them, so no card built
    from them is claimed newer than its oldest prices. On CI every ticker comes
    from the first data_fetch.py step, so this is that step's start; the later
    fetches in the same job only add tickers, with later starts.

    None when it cannot be known - no tickers, no record, or any ticker without
    a well-formed start (a price file written before starts were recorded). The
    caller must not guess a start for those."""
    tickers = list(tickers)
    if not tickers:
        return None
    try:
        starts = json.loads((PRICE_DIR / STARTS_NAME).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(starts, dict):
        return None
    seen = []
    for t in tickers:
        s = starts.get(t)
        try:
            dt.datetime.strptime(str(s), STAMP_FMT)
        except (TypeError, ValueError):
            return None
        seen.append(s)
    return min(seen)                     # fixed-width UTC text sorts as time


def fetch_one(ticker: str, force: bool = False, started: str | None = None) -> pd.DataFrame | None:
    """started: when the download this belongs to began (fetch_all passes its
    own); a direct call records its own start."""
    started = started or utc_stamp()
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
        # keepna=True, as in fetch_all: a bar Yahoo left all-null must reach
        # _fill_last_close, not be dropped inside yfinance (review 2026-09-17).
        df = yf.download(ticker, start=start, interval="1d", keepna=True,
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
    _record_download_start([ticker], started)
    return df


def _positive_float(v) -> float | None:
    """v as a float when it is a real number above zero, else None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f > 0 else None       # f == f: not NaN


def _live_quote(ticker: str) -> tuple[float | None, dt.date | None, dict]:
    """Last traded price from the quote endpoint (not the daily-bar series), the
    DATE of that trade on the exchange's own clock (None if Yahoo gives no
    time), and that session's range as far as Yahoo gives it: {"High": ...,
    "Low": ..., "Open": ...}, each a positive float or None. One Ticker object
    serves all three: fast_info's price lookup already loads the history
    metadata that carries regularMarketTime, regularMarketDayHigh and
    regularMarketDayLow, so none of them costs an extra request. Yahoo's chart
    metadata normally carries no day open; "regularMarketOpen" is read only in
    case it does."""
    session = {"High": None, "Low": None, "Open": None}
    try:
        tk = yf.Ticker(ticker)
        fi = tk.fast_info
    except Exception:
        return None, None, session
    px = None
    for k in ("last_price", "lastPrice", "regular_market_price", "regularMarketPrice"):
        try:
            v = fi[k]
        except Exception:
            continue
        v = _positive_float(v)
        if v is not None:
            px = v
            break
    if px is None:
        return None, None, session
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
        for field, key in (("High", "regularMarketDayHigh"), ("Low", "regularMarketDayLow"),
                           ("Open", "regularMarketOpen")):
            session[field] = _positive_float(md.get(key))
    except Exception:
        day = None
        session = {"High": None, "Low": None, "Open": None}
    return px, day, session


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
    with EVERY field null - a shape yfinance's default keepna=False drops
    outright, so it only reached here as a union row made by a batch peer.
    Calendar batching then left ABI.BR and NDA-FI.HE alone in their batches,
    with no peer to make one (review 2026-09-17, "Calendar chunking leaves
    single-name and tiny tail batches"). Both downloads now pass keepna=True,
    so a ticker's OWN all-null newest bar arrives here whatever its batch, and
    this test decides it; its quote was stamped 2026-08-31 06:30 UTC, 15:30
    JST, the same day. A row with neither keeps its NaN close and _clean
    removes it, exactly as before 8b98d34.

    A zero Open/High/Low is no evidence and is not kept: keepna=True also lets
    through rows yfinance used to drop for being all NaN-or-zero, and filling
    only the Close of O=H=L=0 would stamp a bar with a range down to zero.

    A filled bar keeps its session's range (final review 2026-09-17): when the
    quote is dated the bar's own day, a missing High is max(dayHigh, px), a
    missing Low min(dayLow, px), a missing Open the day open if Yahoo gives one.
    Otherwise - no day range, or a quote from another day - each is px.
    """
    if not ticker or df is None or df.empty or "Close" not in df.columns:
        return df
    try:
        if not pd.isna(df["Close"].iloc[-1]):
            return df                        # newest bar already has a close
        i = df.index[-1]

        def _positive(c):
            v = df.at[i, c] if c in df.columns else None
            try:
                return v is not None and not pd.isna(v) and float(v) > 0
            except (TypeError, ValueError):
                return False

        partial = any(_positive(c) for c in ("Open", "High", "Low", "Volume"))
        px, day, session = _live_quote(ticker)
        if not px:
            return df                        # no quote either - drop it as before
        bar_day = pd.Timestamp(i).date()
        if not partial and day != bar_day:
            print(f"  ~ {ticker}: newest row {bar_day} is empty and the last trade "
                  f"was {day} - no session that day, row dropped")
            return df                        # NaN close -> dropna removes it
        # The session's own range, not a flat bar. auto_adjust multiplies
        # Open/High/Low by AdjClose/Close, which is NaN when the Close is
        # missing, so the O/H/L Yahoo DID send arrive here as NaN. Filling all
        # three with px gave O=H=L=C and a true range of |px - prevClose|: the
        # EU bars still null-close at the 23:35 decision cut ATR by 3-6%, and
        # max(stop, hw - k*ATR) saved the tighter trail for good (final review
        # 2026-09-17). The quote's regularMarketDayHigh/Low are that range - but
        # only for the bar's OWN day; another session's range is not this bar's.
        # The newest bar's adjustment ratio is 1, so raw is the adjusted basis.
        # Anything missing or not positive falls back to px, as before.
        same_day = day == bar_day
        fill = {"High": px, "Low": px, "Open": px}
        if same_day:
            if session.get("High"):
                fill["High"] = max(session["High"], px)
            if session.get("Low"):
                fill["Low"] = min(session["Low"], px)
            if session.get("Open"):              # kept inside the filled range
                fill["Open"] = min(max(session["Open"], fill["Low"]), fill["High"])
        df.loc[i, "Close"] = px
        for c in ("Open", "High", "Low"):    # only what Yahoo left missing
            if c in df.columns and not _positive(c):
                df.loc[i, c] = fill[c]
        print(f"  ~ {ticker}: newest bar had no close - filled from live quote {px}"
              + (f" (day range {fill['Low']}-{fill['High']})"
                 if fill["High"] != fill["Low"] else ""))
    except Exception as e:
        print(f"  ! {ticker}: live-close fill skipped ({e})")
    return df


def _clean(df: pd.DataFrame, ticker: str | None = None) -> pd.DataFrame | None:
    if df is None or df.empty:
        return None
    df = _fill_last_close(df, ticker)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
    # keepna=True (review 2026-09-17) keeps rows yfinance used to drop for being
    # all NaN or zero. The NaN-close ones go above; a close of zero or below is
    # no price either, and must not reach an ATR or a trailing stop.
    df = df[df["Close"] > 0]
    return df if len(df) > 0 else None


# Venues that share ONE trading calendar. Euronext runs a single harmonised
# holiday calendar for its Paris, Amsterdam, Brussels and Lisbon cash markets,
# so their names can share a batch without a union row for a day one of them
# did not trade. Milan and Oslo are Euronext too but keep national holidays,
# so they - and every venue not listed here - stay apart.
_SHARED_CALENDAR = {"PA": "EURONEXT", "AS": "EURONEXT", "BR": "EURONEXT",
                    "LS": "EURONEXT"}


def _calendar_group(ticker: str) -> str:
    """Which trading calendar a ticker's daily bars follow, as a download-batch
    key. Yahoo suffixes name the exchange (.HK, .T, .DE, .L ...), so every EU
    venue keeps its own holidays (LSE shut on 31 Aug 2026 while Xetra traded) -
    except the venues in _SHARED_CALENDAR, pooled under one key.
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
        suffix = t.rsplit(".", 1)[1]
        return _SHARED_CALENDAR.get(suffix, suffix)
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
    kept within a group.

    Each group is split into ceil(n/size) EVEN batches (sizes differ by at most
    one), not size-then-remainder: slicing by 80 left tails such as the last 3
    HK names and 2 US names, and ABI.BR alone, with no batch peers (review
    2026-09-17, "Calendar chunking leaves single-name and tiny tail batches").
    The batch count is unchanged. Peers no longer decide whether a null newest
    bar is filled - keepna=True does - so this only stops names being left
    alone for no reason."""
    groups: dict[str, list[str]] = {}
    for t in tickers:
        groups.setdefault(_calendar_group(t), []).append(t)
    out = []
    for g in groups.values():
        k = -(-len(g) // size)               # ceil(n / size) batches
        base, extra = divmod(len(g), k)
        i = 0
        for j in range(k):
            n = base + (1 if j < extra else 0)
            out.append(g[i:i + n])
            i += n
    return out


def fetch_all(force: bool = False) -> dict[str, pd.DataFrame]:
    """Load fresh-cached tickers, then BATCH-download the misses in chunks of 80
    (one multi-ticker request instead of hundreds of singles) with a per-ticker
    fallback. Makes a ~1,000-product universe fetch in minutes. A chunk only
    ever holds tickers from ONE trading calendar - see _download_chunks.

    Every price file this call writes is recorded with the time the call
    STARTED (download_started), taken before anything is read or fetched: a
    name fetched minutes later is dated earlier than its prices, never later."""
    started = utc_stamp()
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
            # keepna=True: with the default, yfinance drops a ticker's own
            # all-null newest bar before _fill_last_close can test it, so the
            # 5301.T fix only fired when a batch peer rebuilt the row - never
            # for a name alone in its batch (review 2026-09-17). _clean drops
            # every other NaN-close row, and any close <= 0.
            raw = yf.download(chunk, start=start, interval="1d", auto_adjust=True,
                              progress=False, group_by="ticker", threads=True,
                              keepna=True)
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
        _record_download_start(sorted(got), started)
        for t in [x for x in chunk if x not in got]:   # per-ticker fallback
            df = fetch_one(t, force=force, started=started)
            if df is not None and len(df) > 260:
                out[t] = df
        print(f"  fetched {done}/{len(need)} new "
              f"(+{len([t for t in out if t in uni['tickers']]) - 0} total ready)")
    print(f"Data ready: {len(out)}/{len(uni['tickers'])} tickers usable")
    return out


if __name__ == "__main__":
    fetch_all()
