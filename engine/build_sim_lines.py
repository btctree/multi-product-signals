#!/usr/bin/env python3
"""Three DISPLAY-ONLY comparison lines for the dashboard's Growth vs S&P card.

    python build_sim_lines.py      # CI, after "Build dashboard" -> docs/sim_lines.json

Nothing here trades. ib_bot and the Telegram digest read data.json and the
product cards, never this file, so a wrong number here can mislead a reader
but can never place, size or skip an order (owner's decisions 2026-10-26 and
2026-10-27: the lines are display only, clearly labelled as simulated).

The page draws every line from THIS account's real daily figures; this file
only carries what each line changes. For the first two, the sleeves' own daily
equity:

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

  * filter - the account with the bot BUYs the 100-day market filter would
    have skipped taken out (the filter runs in SHADOW mode - log only - since
    the owner's decision of 2026-10-26; the line was asked for on 2026-10-27).
    The lots come from two records, once each: data/filter_backfill.json for
    every bot BUY before the bot's own shadow record began (judged once, by
    hand, by build_filter_backfill.py), and bot_state.json "market_filter"
    .would_have_skipped from then on. Their fills and sales come from
    data/fills_ledger.jsonl, their daily value from data/day_parts.json -
    filter_lots.py says how, and what is never guessed.
    Rows: [date, c], c = the skipped lots' summed value less cost in HKD that
    day (0 before the first fill; the realised profit once sold). The page
    subtracts c from NetLiq - (nl - c), with money in and out handled exactly
    as for the account - so with nothing skipped the line IS the account. It
    does not guess what a live filter would have bought with the free slot.
    Its days are the account's own (netliq_history.json), not the Bitcoin
    bars', so complete_through does not cut it.

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
files the build already downloaded (never fetch_one, which can download) and
the repo's own data files, it needs no package the build does not already
install, and it writes ONE file, docs/sim_lines.json, atomically. Each line
fails on its own: M1's site down gives "m1": null and the trend line still
draws; an unreadable record gives "filter": null and the other two draw. No
file at all and the page simply draws no simulated lines.
"""
import bisect
import datetime as dt
import json
import math
import os
import re
import sys
import urllib.request
from pathlib import Path

import pandas as pd

import data_fetch as D
import filter_lots as FL

OUT = Path(__file__).resolve().parent.parent / "docs" / "sim_lines.json"
# The repo's own data files the filter line reads - committed by the VM's
# publisher and the bot, and data/filter_backfill.json by hand.
DATA = Path(__file__).resolve().parent.parent / "data"
FILTER_MARKETS = ("US", "HK", "JP", "EU")

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


def _read(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def filter_orders(backfill: dict, state: dict):
    """(orders, verdicts) from the two records, each order once, by its History
    time and IB symbol. Every order either record knows goes into the fill
    matching - the unfilled and the not-skipped ones too - so a fill is paired
    with the same order the backfill paired it with; only the verdicts decide
    which lots are taken out. The bot's current History adds the orders since
    the backfill; a shadow row whose History row has rolled off is still an
    order, its size then read from its fills. Where both records list an
    order, the bot's own shadow verdict stands.

    Raises on a record whose shape is not as written - all or nothing, as for
    M1's record: a half-read record is not one to draw from."""
    if not isinstance(backfill, dict) or backfill.get("schema") != 1:
        raise ValueError("data/filter_backfill.json is not schema 1")
    lots, unfilled = backfill.get("lots"), backfill.get("unfilled")
    if not isinstance(lots, list) or not isinstance(unfilled, list):
        raise ValueError("data/filter_backfill.json: lots or unfilled is not a list")
    for k in ("orders_through", "first_live"):
        if not (backfill.get(k) is None or isinstance(backfill.get(k), str)):
            raise ValueError(f"data/filter_backfill.json: {k} is not text")
    orders, verdicts = {}, {}
    for r in lots + unfilled:
        FL.parse_time(r["order"])
        sym = str(r["symbol"]).strip().upper()
        qty = float(r["order_qty"] if "order_qty" in r else r["qty"])
        if not sym or not qty > 0:
            raise ValueError("a backfill order with no symbol or size: %.80r" % (r,))
        orders[FL.order_key(r["order"], sym)] = {
            "time": r["order"], "symbol": sym, "ccy": str(r["ccy"]).upper(), "qty": qty}
    for r in lots:
        if not isinstance(r.get("skipped"), bool):
            raise ValueError("a backfill lot with no verdict: %.80r" % (r,))
        verdicts[FL.order_key(r["order"], r["symbol"])] = {
            "skipped": r["skipped"], "market": r.get("market"), "pct": r.get("pct"),
            "by": r.get("judged_by") or "rule"}
    if not isinstance(state, dict):
        raise ValueError("data/bot_state.json is not an object")
    for o in FL.bot_orders(state.get("activity") or []):
        orders.setdefault(FL.order_key(o["time"], o["symbol"]), o)
    mf = state.get("market_filter")
    rows = mf.get("would_have_skipped", []) if isinstance(mf, dict) else []
    if not isinstance(rows, list):
        raise ValueError("market_filter.would_have_skipped is not a list")
    for s in rows:
        if not isinstance(s, dict):
            raise ValueError("a shadow row is not an object")
        FL.parse_time(s["time"])
        sym, pct = FL.ib_symbol(s["symbol"]), float(s["pct"])
        if not sym or s.get("market") not in FILTER_MARKETS or not math.isfinite(pct):
            raise ValueError("unexpected shadow row %.80r" % (s,))
        k = FL.order_key(s["time"], sym)
        orders.setdefault(k, {"time": s["time"], "symbol": sym,
                              "ccy": FL.ccy_of(s["symbol"]), "qty": None})
        verdicts[k] = {"skipped": True, "market": s["market"], "pct": pct, "by": "shadow"}
    ordered = sorted(orders.values(), key=lambda o: (FL.parse_time(o["time"]), o["symbol"]))
    return ordered, verdicts


def filter_line(first: str, data: Path | None = None) -> dict:
    """The "filter" entry: the skipped lots' daily value less cost, over the
    account's own days from `first`, and the lots themselves for the note.
    Reads only the repo's data files. Raises on anything it cannot read; a
    day it cannot price without guessing ends the series there instead
    (filter_lots.filter_series), and "stopped" says why."""
    data = Path(data or DATA)
    backfill = _read(data / "filter_backfill.json")
    state = _read(data / "bot_state.json")
    orders, verdicts = filter_orders(backfill, state)
    ledger = FL.read_ledger((data / "fills_ledger.jsonl").read_text(encoding="utf-8"))
    lots, _hand = FL.match_lots(orders, ledger)
    FL.attach_sales(lots, ledger)
    skipped = [lot for lot in lots if verdicts.get(lot["key"], {}).get("skipped") is True]
    parts = _read(data / "day_parts.json").get("days")
    if not isinstance(parts, dict):
        raise ValueError("data/day_parts.json has no days")
    hist = _read(data / "netliq_history.json").get("series")
    days = sorted({r["d"] for r in (hist if isinstance(hist, list) else [])
                   if isinstance(r, dict) and isinstance(r.get("d"), str)
                   and _DAY_RE.match(r["d"]) and r["d"] >= first})
    if not days:
        raise ValueError("data/netliq_history.json has no days")
    series, stop = FL.filter_series(days, skipped, parts)
    if stop:
        print(f"::warning::sim lines: the market filter line stops after "
              f"{series[-1][0] if series else 'no day'} - {stop}")
    last, newest = (series[-1][0] if series else None), max(parts) if parts else ""
    out_lots = []
    for lot in skipped:
        v, o = verdicts[lot["key"]], lot["order"]
        held = sum(f[2] for f in lot["fills"]) - sum(s[2] for s in lot["sales"])
        pl = FL.lot_value(lot, last, parts, newest)[0] if last else None
        out_lots.append({
            "sym": o["symbol"], "mkt": v["market"], "pct": v["pct"], "order": o["time"][:10],
            "fill": lot["fills"][0][1], "qty": round(sum(f[2] for f in lot["fills"]), 6),
            "sold": lot["sales"][-1][1] if lot["sales"] and held <= 1e-6 else None,
            "pl": None if pl is None else round(pl), "by": v["by"]})
    mf = state.get("market_filter")
    return {
        "rule": "bot BUYs taken out when the market's home index (US ^GSPC, HK ^HSI, "
                "JP ^N225, EU ^STOXX50E) closed below its 100-day average on its last "
                "finished bar; their money left as idle cash (0 interest); exits never "
                "gated; hand trades never skipped",
        "source": "data/filter_backfill.json (judged to its orders_through) + "
                  "bot_state.json market_filter.would_have_skipped; fills_ledger.jsonl; "
                  "day_parts.json",
        "judged_through": backfill.get("orders_through"),
        "first_live": backfill.get("first_live"),
        "shadow_mode": mf.get("mode") if isinstance(mf, dict) else None,
        "lots": out_lots, "stopped": stop, "series": series}


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
           "weights": dict(WEIGHTS), "trend": None, "m1": None, "filter": None}

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

    # The market filter line: the repo's own records only, on the account's
    # own days - nothing above feeds it, and its failure touches neither line.
    try:
        out["filter"] = filter_line(first)
    except Exception as e:
        out["filter"] = None
        out["filter_error"] = _err(e)
        print(f"::warning::sim lines: market filter line not built ({_err(e)}) - "
              f"filter is null")

    _write_atomic(out_path, out)
    return out


def main() -> int:
    try:
        out = build()
        t, m, f = out.get("trend"), out.get("m1"), out.get("filter")
        print(f"sim lines: complete through {out['complete_through']}; "
              f"trend {len(t['series']) if t else 'NOT BUILT'} days; "
              f"M1 {len(m['series']) if m else 'NOT BUILT'} days"
              + (f", last action {m['last_action']['action']} {m['last_action']['date']}"
                 if m else "")
              + (f"; market filter {len(f['series'])} days, {len(f['lots'])} lot(s) "
                 f"skipped" if f else "; market filter NOT BUILT") + f" -> {OUT}")
    except Exception as e:
        # display only: nothing here may fail the build that deploys data.json
        print(f"::warning::sim lines not built ({_err(e)}) - the page draws none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
