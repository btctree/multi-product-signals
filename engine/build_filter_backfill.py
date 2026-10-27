#!/usr/bin/env python3
"""The market filter's verdict on every past bot BUY: data/filter_backfill.json.

    python build_filter_backfill.py --download DIR   # fetch the four index files into DIR, then build
    python build_filter_backfill.py --prices DIR     # build from index files already fetched into DIR
    ... --first-live "2026-10-28 14:00 UTC"          # at the deploy: judge every order to then

BY HAND, NEVER IN CI. It walks the git history of data/bot_state.json (CI
checks out one commit) and --download goes to Yahoo. The file it writes is
committed and reviewed like code; build_sim_lines.py only reads it.

WHY. The dashboard's "Sim: you with market filter (shadow)" line (owner's
decision 2026-10-27) is the account with the bot BUYs the 100-day market
filter would have skipped taken out. From the day the filter runs in shadow
mode the bot records those itself (bot_state.json "market_filter"
.would_have_skipped). Every earlier bot BUY - from the first, 20 Jul 2026 - is
judged here, once, by the same rule.

THE BOT'S BUYS. bot_state.json publishes only its newest 100 History rows, so
every version of the file in the history of --ref (default origin/main) is
read and their rows pooled: filter_lots.bot_orders keeps the bot's own entry
orders, once each, and filter_lots.match_lots pairs them with their fills in
data/fills_ledger.jsonl (read at the same ref). The rules for what is and is
not a bot lot - hand trades, rejected and unfilled orders - are in
filter_lots.py's docstring.

THE RULE, identical to the shadow filter and to the backtest: an order is
skipped when its market's home index - US ^GSPC, HK ^HSI, JP ^N225, EU
^STOXX50E - closed below its 100-day simple average on the last FINISHED bar
at the moment the order was placed. That is market_trend.trend_row(market,
closes, started=order time): a bar counts once its market's close + 90
minutes (execution/market_clock.py) had passed, and the average is the last
100 finished closes including that bar. Exits are never gated, so no SELL is
judged.

THE MARKET of an order comes from its currency, because a History row carries
the IB symbol and currency, not the engine's market: JPY is JP, HKD is HK,
and EUR, GBP, CHF, DKK, SEK and NOK are all EU - the engine's market_of puts
every European listing, Swiss and Nordic included, in EU. USD is US unless
engine/universe.market_of calls the symbol an ETF, a bond fund or the like,
which has no home index in the backtest and is never gated. Every bot BUY so
far was a stock in USD, JPY or EUR.

ONE DIFFERENCE FROM THE LIVE SHADOW. The bot judges on the data.json of the
newest hourly build, whose download may have begun before a bar settled; here
the order time itself is the cut-off. An order placed within about an hour of
a bar settling can see one bar more here than the bot would have.

INDEX CLOSES. Yahoo daily closes through engine/data_fetch.fetch_one, the
build's own download (--download), into a folder of their own - never
data/prices. The file states where they came from and when the download
started, and each lot carries the bar, close and average it was judged on, so
any verdict can be checked by hand.

AT THE DEPLOY: --first-live. The two records must meet with no gap: this file
for every order up to an instant, the bot's own shadow record for every order
after it - which holds only if every later order came from a bot that already
runs the shadow. So re-run this IMMEDIATELY BEFORE the push that deploys the
shadow filter, with --first-live set to the push time, and commit the file in
that push:

    git fetch origin
    python build_filter_backfill.py --download DIR --first-live "2026-10-28 14:00 UTC"

Every bot order up to that instant is judged here from the index history, and
"first_live" and "orders_through" both record the instant. The VM installs
the push at its next :25 and every trading run after that runs the shadow. So
push in a quiet hour - after the day's catch-up and its 12:00 London retry,
and before 23:25 UTC - so that no trading run falls between the History this
reads (--ref, origin/main: fetch first) and that :25. An order at or after
the instant that the History read here already holds is taken as the
shadow's (skipped only if a shadow row names it); the run lists any such
order, which should be none.

Without --first-live, a run after the shadow went live finds the first
version of bot_state.json carrying the bot's "market_filter" block in shadow
mode and takes that block's "at" as first_live. Orders from then on are not
judged here - the bot's own record decides, and every would_have_skipped row
the history ever published is kept, so none is lost when the bot's list of 50
rolls on. With neither, first_live is null and "orders_through" is the newest
History this run read: a bot BUY placed after it is in neither record, so
build_sim_lines lists it as "unjudged" (with a ::warning::) and the page says
later buys are not judged yet.
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import data_fetch as D
import filter_lots as FL
import market_trend as MT
from universe import market_of

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "filter_backfill.json"
STATE = "data/bot_state.json"
LEDGER = "data/fills_ledger.jsonl"
EU_CCY = ("EUR", "GBP", "CHF", "DKK", "SEK", "NOK")


def _git(*args, data=None) -> bytes:
    return subprocess.run(["git", "-C", str(ROOT)] + list(args), input=data,
                          capture_output=True, check=True).stdout


def state_history(ref: str) -> list:
    """Every version of data/bot_state.json in ref's history, oldest first:
    [(commit, parsed file)]. A version that does not parse is left out."""
    revs = _git("log", "--format=%H", ref, "--", STATE).decode().split()
    raw = _git("cat-file", "--batch",
               data="".join(f"{r}:{STATE}\n" for r in revs).encode())
    out, i = [], 0
    for r in revs:
        nl = raw.index(b"\n", i)
        head = raw[i:nl].split()
        if len(head) < 3 or head[1] == b"missing":
            i = nl + 1
            continue
        size = int(head[2])
        body = raw[nl + 1:nl + 1 + size]
        i = nl + 1 + size + 1
        try:
            snap = json.loads(body)
        except ValueError:
            continue
        if isinstance(snap, dict):
            out.append((r, snap))
    out.reverse()
    return out


def shadow_start(history):
    """(the first shadow run's time, its "at" text) - from the first version
    whose "market_filter" block says mode "shadow" - or (None, None)."""
    for _rev, snap in history:
        mf = snap.get("market_filter")
        if isinstance(mf, dict) and mf.get("mode") == "shadow":
            try:
                return FL.parse_time(mf.get("at")), str(mf.get("at"))
            except (TypeError, ValueError):
                continue
    return None, None


def shadow_rows(history) -> dict:
    """{order key: row} for every would_have_skipped row the history published."""
    out = {}
    for _rev, snap in history:
        mf = snap.get("market_filter")
        rows = mf.get("would_have_skipped") if isinstance(mf, dict) else None
        for r in rows if isinstance(rows, list) else []:
            try:
                FL.parse_time(r["time"])
                out.setdefault(FL.order_key(r["time"], FL.ib_symbol(r["symbol"])), {
                    "market": str(r["market"]), "pct": float(r["pct"])})
            except (TypeError, KeyError, ValueError):
                continue
    return out


def order_market(order):
    """US/HK/JP/EU, or None for an order with no home index (see the module
    docstring)."""
    ccy = order.get("ccy")
    if ccy == "USD":
        return "US" if market_of(order["symbol"]) == "US" else None
    return {"JPY": "JP", "HKD": "HK"}.get(ccy) or ("EU" if ccy in EU_CCY else None)


def download(folder: Path) -> str:
    """The four index files from Yahoo into `folder` (data_fetch.fetch_one,
    forced), returning when the download started."""
    D.PRICE_DIR = folder
    started = D.utc_stamp()
    for ticker, _name in MT.MARKETS.values():
        if D.fetch_one(ticker, force=True, started=started) is None:
            raise RuntimeError(f"{ticker}: Yahoo returned nothing")
    return started


def index_closes(folder: Path):
    D.PRICE_DIR = folder
    closes, files = {}, {}
    for market, (ticker, _name) in MT.MARKETS.items():
        s = MT._closes(ticker, None)
        closes[market] = s
        files[ticker] = {"bars": int(len(s)), "first": s.index[0].strftime("%Y-%m-%d"),
                         "last": s.index[-1].strftime("%Y-%m-%d")}
    return closes, files, D.download_started([t for t, _ in MT.MARKETS.values()])


def judge(order, closes) -> dict:
    """The rule's verdict on one order, with what it was judged on."""
    m = order_market(order)
    if m is None:
        return {"market": None, "skipped": False,
                "why": "no home index for this listing - never gated"}
    try:
        r = MT.trend_row(m, closes[m], FL.parse_time(order["time"]))
    except Exception as e:                 # the bot reads such a market as unknown
        return {"market": m, "skipped": False,
                "why": f"index row not usable ({type(e).__name__}: {str(e)[:120]}) - "
                       f"unknown, never gated"}
    return {"market": m, "index": r["index"], "bar": r["bar"], "close": r["close"],
            "sma100": r["sma100"], "pct": r["pct_vs_sma100"], "skipped": r["below"]}


def first_live_arg(text):
    """--first-live: (the instant, as History writes a time) - "2026-10-28
    14:00 UTC" - or ValueError."""
    t = FL.parse_time(str(text).strip())
    return t, t.strftime(FL.TIME_FMT)


def build(ref: str, prices: Path, now=None, first_live=None) -> dict:
    """first_live: None (take it from the first shadow block in the history,
    if any), or the --first-live text: every order before that instant is
    judged here, every one from it on is the shadow's."""
    history = state_history(ref)
    if not history:
        raise RuntimeError(f"no readable version of {STATE} in {ref}")
    rows = [r for _rev, snap in history for r in (snap.get("activity") or [])
            if isinstance(r, dict)]
    orders = FL.bot_orders(rows)
    ledger = FL.read_ledger(_git("show", f"{ref}:{LEDGER}").decode("utf-8"))
    lots, unclaimed = FL.match_lots(orders, ledger)
    FL.attach_sales(lots, ledger)
    closes, files, started = index_closes(prices)
    if first_live is not None:
        live_from, live_at = first_live_arg(first_live)
        source = "set by hand (--first-live), the deploy's push time"
    else:
        live_from, live_at = shadow_start(history)
        source = "the first shadow market_filter block in the history" if live_at else None
    shadow = shadow_rows(history)
    # the orders this file leaves to the shadow's record (at a deploy with
    # --first-live at the push time: none)
    after = [o for o in orders
             if live_from is not None and FL.parse_time(o["time"]) >= live_from]

    def verdict(o):
        if live_from is not None and FL.parse_time(o["time"]) >= live_from:
            s = shadow.get(FL.order_key(o["time"], o["symbol"]))
            return {"market": (s or {}).get("market") or order_market(o),
                    "pct": (s or {}).get("pct"), "skipped": s is not None,
                    "judged_by": "shadow"}
        return dict(judge(o, closes), judged_by="rule")

    out_lots = []
    for lot in lots:
        o = lot["order"]
        v = verdict(o)
        row = {"order": o["time"], "symbol": o["symbol"], "ccy": o["ccy"],
               "order_qty": o["qty"], "limit": o["limit"], "reason": o["reason"],
               "status": o["status"], "qty": round(sum(f[2] for f in lot["fills"]), 6),
               "fills": [[f[0], f[1], round(f[2], 6), f[3], round(f[4], 4), f[5], f[6]]
                         for f in lot["fills"]]}
        row.update(v)
        if v["skipped"]:
            row["sales"] = [[s[0], s[1], round(s[2], 6), s[3], round(s[4], 4), s[5], s[6]]
                            for s in lot["sales"]]
        out_lots.append(row)
    filled = {lot["key"] for lot in lots}
    unfilled = [{"order": o["time"], "symbol": o["symbol"], "ccy": o["ccy"], "qty": o["qty"],
                 "status": o["status"]}
                for o in orders if FL.order_key(o["time"], o["symbol"]) not in filled]
    newest = history[-1][1].get("updated")
    head = _git("rev-parse", "--short=12", ref).decode().strip()
    skipped = [r for r in out_lots if r["skipped"]]
    return {
        "schema": 1,
        "what": "every bot BUY lot with the 100-day market filter's verdict - display "
                "only, for the dashboard's 'Sim: you with market filter (shadow)' line; "
                "never read by the bot",
        "rule": "skipped when the market's home index (US ^GSPC, HK ^HSI, JP ^N225, EU "
                "^STOXX50E) closed below its 100-day simple average on its last finished "
                "bar (close + 90 min) when the order was placed - market_trend.trend_row; "
                "exits never gated; hand trades are not bot lots",
        "built_at": D.utc_stamp(now),
        "ref": f"{ref} @ {head}",
        "history": {"file": STATE, "versions": len(history),
                    "first": history[0][1].get("updated"), "last": newest},
        "ledger": f"{LEDGER} at {ref}: {len(ledger)} stock fills",
        "index": {"source": "Yahoo Finance daily closes, engine/data_fetch.fetch_one "
                            "(yfinance), downloaded for this file",
                  "download_started": started, "files": files},
        "orders_through": live_at or newest,
        "first_live": live_at,
        "first_live_from": source,
        "summary": {"bot_orders": len(orders), "lots": len(out_lots),
                    "skipped": len(skipped), "unfilled_orders": len(unfilled),
                    "fills_not_bot": len(unclaimed),
                    "orders_from_first_live": len(after)},
        "lots": out_lots,
        "unfilled": unfilled,
        "not_bot": unclaimed,
    }


def write(payload: dict, path: Path = OUT) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--download", metavar="DIR", help="fetch the index files into DIR first")
    g.add_argument("--prices", metavar="DIR", help="index files already fetched into DIR")
    ap.add_argument("--ref", default="origin/main")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--first-live", metavar='"YYYY-MM-DD HH:MM UTC"',
                    help="the deploy's push time: judge every bot order before it here, "
                         "and leave every later one to the shadow's own record")
    a = ap.parse_args(argv)
    if a.first_live is not None:
        try:
            first_live_arg(a.first_live)
        except ValueError:
            ap.error(f'--first-live must read like "2026-10-28 14:00 UTC", not {a.first_live!r}')
    folder = Path(a.download or a.prices).resolve()
    if folder == (ROOT / "data" / "prices").resolve():
        ap.error("use a folder of its own, never data/prices")
    if a.download:
        folder.mkdir(parents=True, exist_ok=True)
        print(f"index download started {download(folder)} -> {folder}")
    payload = build(a.ref, folder, first_live=a.first_live)
    write(payload, Path(a.out))
    s = payload["summary"]
    print(f"{s['bot_orders']} bot BUY orders, {s['lots']} lots, {s['skipped']} the filter "
          f"would have skipped, {s['unfilled_orders']} never filled, "
          f"{s['fills_not_bot']} BUY fills not the bot's -> {a.out}")
    print(f"judged to {payload['orders_through']}; first_live {payload['first_live']} "
          f"({payload['first_live_from'] or 'not set: later bot BUYs are unjudged'})")
    if s["orders_from_first_live"]:
        print(f"  NOTE: {s['orders_from_first_live']} bot BUY order(s) at or after first_live "
              f"are left to the shadow's record - at a deploy there should be none")
    for r in payload["lots"]:
        if r["skipped"]:
            print(f"  skipped: {r['symbol']} ({r['market']}) order {r['order']}, "
                  f"index {r.get('pct')}% vs its 100-day average")
    return 0


if __name__ == "__main__":
    sys.exit(main())
