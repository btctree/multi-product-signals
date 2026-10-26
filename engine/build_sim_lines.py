#!/usr/bin/env python3
"""Two DISPLAY-ONLY comparison lines for the dashboard's Growth vs S&P card.

    python build_sim_lines.py      # CI, after "Build dashboard" -> docs/sim_lines.json

Nothing here trades. ib_bot and the Telegram digest read data.json and the
product cards, never this file, so a wrong number here can mislead a reader
but can never place, size or skip an order (owner's decision 2026-10-26: the
two lines are display only, clearly labelled as simulated).

The page draws both lines from THIS account's real daily returns; this file
only carries the two sleeves' own daily equity:

  * trend - the research crypto trend sleeve that makes up the 30% of the
    headline's 70/30 figure: research_r7_trend.run_trend(N=55, K=4.0, slots=2,
    entry_mode='regime', exit_sma='sma_fast') on BTC-USD and ETH-USD, at the
    engine's 0.20% a side. The sleeve is ON HOLD - the bot does not run it,
    and IBKR HK cannot run it as configured (PAXOS crypto is not offered
    there) - so this is the research blend drawn on real days, not something
    the account could have done.
    Rows: [date, equity], equity 1.0 on the first published day.

  * m1 - a long-only Bitcoin slot run by the M1 model, from M1's PUBLISHED
    trade record (engine_actions_live.json): Buy opens the slot, Sell, Short
    and Cover leave it flat. Its DATES only, never its booked prices: M1 books
    entries at the signal day's open, which it could not know at that open.
    The slot follows M1 one Hong Kong session late (a spot Bitcoin ETF bought
    on SEHK), using the ^HSI dates as the HK calendar - the rule the research
    measured (scratchpad live_blend.py): on calendar day d the slot holds
    Bitcoin when M1 was long at the close of the day before the latest HK
    session on or before d. 0.23% a side on every change plus 1%/yr while
    held (the HK ETF route's costs), on BTC-USD daily closes.
    Rows: [date, equity, in], in = 1 on a day the slot held Bitcoin or paid
    its exit cost, so the page knows which days 20% of the book sat in it.

The page, not this file, decides the blend and states it: 0.70 x account +
0.30 x trend each day, and (1 - 0.20 e) x account + 0.20 e x slot, where
e = 1 while the slot is in - the stock book is scaled down to make room,
never topped up with money the account did not have (it was 85-100% invested
from late July, so an M1 slot "from idle cash" would have been leverage).

COMPLETE DAYS ONLY. Crypto bars are UTC days, and the hourly build downloads
mid-day, so the newest bar in the price file is still trading. complete_through
is the UTC day before the download that wrote the files STARTED
(data_fetch.download_started); the page ends both lines there, and compares
them with the account on that same day - a few hours of Bitcoin must never be
counted against a closed account day.

FAIL SOFT. This runs in the hourly build that also deploys data.json, and a
failed build leaves data.json stale, which makes the bot defer every holding's
exit. So: the CI step is continue-on-error with a timeout, this script exits 0
whatever happens (a ::warning:: says what went wrong), it reads only the price
files the build already downloaded (never fetch_one, which can download), it
needs no package the build does not already install, and it writes ONE file,
docs/sim_lines.json, atomically. Each line fails on its own: M1's site down
gives "m1": null and the trend line still draws. No file at all and the page
simply draws no simulated lines.
"""
import bisect
import datetime as dt
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

import pandas as pd

import data_fetch as D

OUT = Path(__file__).resolve().parent.parent / "docs" / "sim_lines.json"

# The research 70/30's crypto sleeve, exactly as the headline study ran it
# (scratchpad live/research_runs.py) - the C1 sleeve of research_r7_trend.
TREND_TICKERS = ("BTC-USD", "ETH-USD")
TREND_RUN = dict(N=55, K=4.0, slots=2, entry_mode="regime", exit_sma="sma_fast")

# M1's public trade record, on the same GitHub Pages site as this dashboard.
M1_URL = "https://btctree.github.io/btc-dashboard/engine_actions_live.json"
M1_TIMEOUT_S = 20
M1_MAX_BYTES = 5_000_000
# Long-only: only a Buy holds Bitcoin. A Short is flat for this slot, and so is
# the Cover that ends it - the slot never borrows.
M1_LONG = {"Buy": 1, "Sell": 0, "Short": 0, "Cover": 0}
M1_COST = 0.0023        # a side: HK spot Bitcoin ETF (research route B)
M1_FEE_YR = 0.01        # the ETF's yearly fee, charged while held
HK_CALENDAR = "^HSI"    # a day with a Hang Seng bar is an HK session

# What the page blends with. Stated here so the file says what it is for; the
# page names each line from these numbers.
WEIGHTS = {"trend": 0.30, "m1": 0.20}
# The SPY card the chart runs on holds ~500 sessions (about two years); a
# little more than that keeps the longest window's first day covered.
KEEP_DAYS = 800

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _day(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%d")


def _shift(day: str, days: int) -> str:
    return (dt.date.fromisoformat(day) + dt.timedelta(days=days)).isoformat()


def read_prices(ticker: str) -> pd.DataFrame:
    """The build's own cached daily bars for one ticker - read, never fetched."""
    df = pd.read_csv(D.cache_path(ticker), index_col=0, parse_dates=True)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[df["Close"] > 0]
    if len(df) < 2:
        raise ValueError(f"{ticker}: fewer than two usable bars")
    return df


def complete_through(tickers) -> str:
    """The last UTC day whose bars are final in every one of these files: the
    day before their download STARTED. Without a recorded start (a file written
    before starts were kept) the files' own write times stand in - a file is
    written after its download starts, so that day can only be later, which is
    why it is said in the log rather than passed silently."""
    s = D.download_started(tickers)
    if s:
        day = dt.datetime.strptime(s, D.STAMP_FMT).date()
    else:
        print("::warning::sim lines: no recorded download start for "
              f"{', '.join(tickers)} - using the price files' write times")
        day = min(dt.datetime.fromtimestamp(D.cache_path(t).stat().st_mtime,
                                            dt.timezone.utc).date() for t in tickers)
    return (day - dt.timedelta(days=1)).isoformat()


def trend_sleeve(frames: dict) -> pd.Series:
    """The research crypto sleeve's daily equity (any scale), by date."""
    from research_r7_trend import run_trend          # pulls in the backtester
    _, eq, _ = run_trend(frames, **TREND_RUN)
    eq = eq.astype(float)
    eq.index = [_day(d) for d in eq.index]
    return eq


def validate_m1(rows) -> list:
    """M1's records as [{date, action}], or ValueError for anything unexpected.
    All or nothing: a record whose shape changed is not one to draw from."""
    if not isinstance(rows, list) or not rows:
        raise ValueError("not a non-empty list")
    out = []
    for r in rows:
        if not isinstance(r, dict):
            raise ValueError("a record is not an object")
        d, a = r.get("date"), r.get("action")
        if not (isinstance(d, str) and _DAY_RE.match(d)) or a not in M1_LONG:
            raise ValueError("unexpected record %.80r" % (r,))
        dt.date.fromisoformat(d)                      # a real calendar day
        out.append({"date": d, "action": a})
    # stable: two actions on one day (a Cover then a Buy) keep their order,
    # and the later one is that day's closing state
    return sorted(out, key=lambda x: x["date"])


def fetch_m1(url: str = M1_URL, timeout: float = M1_TIMEOUT_S, opener=None) -> list:
    req = urllib.request.Request(url, headers={
        "User-Agent": "multi-product-signals/sim-lines (display only)",
        "Accept": "application/json"})
    with (opener or urllib.request.urlopen)(req, timeout=timeout) as r:
        status = getattr(r, "status", 200)
        if status != 200:
            raise ValueError(f"HTTP {status}")
        raw = r.read(M1_MAX_BYTES + 1)
    if len(raw) > M1_MAX_BYTES:
        raise ValueError("response larger than expected")
    return validate_m1(json.loads(raw.decode("utf-8")))


def m1_long_at(actions: list):
    """day -> 1 if M1 was long at that day's close, else 0 (the last action
    on or before the day decides; nothing before the first action = flat)."""
    days = [a["date"] for a in actions]
    vals = [M1_LONG[a["action"]] for a in actions]

    def at(day: str) -> int:
        i = bisect.bisect_right(days, day) - 1
        return vals[i] if i >= 0 else 0
    return at


def m1_slot(close: pd.Series, actions: list, hk_sessions: list) -> list:
    """[(day, equity, in)] for every day of `close` (BTC-USD daily closes).

    hk_sessions: sorted "YYYY-MM-DD" HK trading days. On day d the slot holds
    Bitcoin (h = 1) when M1 was long at the close of the calendar day before
    the latest HK session on or before d: a Saturday signal is first held on
    Monday's session, Monday's return being Sunday's close to Monday's. One
    cost per change of h, on the day it changes; the fee only while held."""
    long_at = m1_long_at(actions)
    days = [_day(d) for d in close.index]
    px = close.astype(float).tolist()
    out, g, prev = [], 1.0, None
    for i, day in enumerate(days):
        j = bisect.bisect_right(hk_sessions, day) - 1
        h = long_at(_shift(hk_sessions[j], -1)) if j >= 0 else 0
        if prev is not None:
            r = h * (px[i] / px[i - 1] - 1) - h * M1_FEE_YR / 365.0
            g *= (1 + r) * (1 - M1_COST * abs(h - prev))
        changed = prev is not None and h != prev
        out.append((day, g, 1 if (h or changed) else 0))
        prev = h
    return out


def _window(rows, first: str, last: str):
    """Rows dated first..last, equity rescaled to 1.0 on the first of them."""
    rows = [r for r in rows if first <= r[0] <= last]
    if len(rows) < 2:
        raise ValueError("fewer than two complete days to publish")
    base = rows[0][1]
    if not base > 0:
        raise ValueError("non-positive equity at the window start")
    return [[r[0], round(r[1] / base, 6)] + list(r[2:]) for r in rows]


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _err(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"[:200]


def build(out_path: Path | None = None, fetch=None, now: dt.datetime | None = None) -> dict:
    # resolved per call, not as defaults, so a test that points OUT at a temp
    # dir can never have this write the real docs/sim_lines.json
    out_path = Path(out_path or OUT)
    fetch = fetch or fetch_m1
    now = now or dt.datetime.now(dt.timezone.utc)
    frames = {t: read_prices(t) for t in TREND_TICKERS}
    # never past the last bar either file actually has
    through = min([complete_through(TREND_TICKERS)]
                  + [_day(f.index[-1]) for f in frames.values()])
    first = _shift(through, -KEEP_DAYS)
    out = {"schema": 1,
           "display_only": "simulated comparison lines - never traded, never read by the bot",
           "built_at": D.utc_stamp(now), "complete_through": through,
           "weights": dict(WEIGHTS), "trend": None, "m1": None}

    try:
        eq = trend_sleeve(frames)
        out["trend"] = {
            "rule": "research_r7_trend.run_trend(N=55, K=4.0, slots=2, "
                    "entry_mode='regime', exit_sma='sma_fast') on BTC-USD + ETH-USD",
            "source": "Yahoo daily closes, this build's own download",
            "cost_side": 0.0020,
            "series": _window(list(eq.items()), first, through)}
    except Exception as e:
        out["trend_error"] = _err(e)
        print(f"::warning::sim lines: crypto trend sleeve not built ({_err(e)})")

    try:
        actions = fetch(M1_URL)
        fetched_at = D.utc_stamp()
        hk = sorted(_day(d) for d in read_prices(HK_CALENDAR).index)
        # the HK calendar must be as complete as the Bitcoin days it decides
        m1_through = min(through, complete_through((HK_CALENDAR,)))
        rows = m1_slot(frames["BTC-USD"]["Close"], actions, hk)
        out["m1"] = {
            "source": M1_URL, "fetched_at": fetched_at,
            "last_action": actions[-1],
            "rule": "long only (Buy opens; Sell, Short and Cover are flat), one HK "
                    "session late (^HSI dates), on BTC-USD daily closes",
            "cost_side": M1_COST, "fee_yr": M1_FEE_YR,
            "series": _window(rows, first, m1_through)}
        out["complete_through"] = min(through, m1_through)
    except Exception as e:
        out["m1"] = None
        out["m1_error"] = _err(e)
        print(f"::warning::sim lines: M1 slot not built ({_err(e)}) - m1 is null")

    _write_atomic(out_path, out)
    return out


def main() -> int:
    try:
        out = build()
        t, m = out.get("trend"), out.get("m1")
        print(f"sim lines: complete through {out['complete_through']}; "
              f"trend {len(t['series']) if t else 'NOT BUILT'} days; "
              f"M1 {len(m['series']) if m else 'NOT BUILT'} days"
              + (f", last action {m['last_action']['action']} {m['last_action']['date']}"
                 if m else "") + f" -> {OUT}")
    except Exception as e:
        # display only: nothing here may fail the build that deploys data.json
        print(f"::warning::sim lines not built ({_err(e)}) - the page draws none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
