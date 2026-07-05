"""Build the static dashboard data for GitHub Pages.

Writes:
  docs/data.json                 — actions, positions, history, product index
  docs/products/<safe>.json      — per-product price history + analysis card
Everything the mobile dashboard needs; refreshed daily by GitHub Actions.
"""
import json
import datetime as dt
from pathlib import Path

import pandas as pd

from config import ROOT, DATA_DIR, SLEEVES, START_CAPITAL_HKD
from data_fetch import fetch_all, safe_name
from universe import load_universe, market_of, NAMES
from company_names import CURATED
from production import analyze, scan_actions, TRADEABLE

DOCS = ROOT / "docs"
PROD_DIR = DOCS / "products"
POS_FILE = DATA_DIR / "positions.json"
LOG_FILE = DATA_DIR / "trade_log.json"
NAMES_CACHE = DATA_DIR / "company_names.json"


def load_json(p, default):
    return json.loads(p.read_text()) if p.exists() else default


def build_name_map(tickers, fetch=False):
    """Friendly names (NAMES) > curated > yfinance cache > ticker. Fetch fills
    the long tail once and caches it (CI runs with fetch=True the first time)."""
    cache = load_json(NAMES_CACHE, {})
    if fetch:
        import yfinance as yf
        changed = False
        for t in tickers:
            if t in NAMES or t in CURATED or t in cache:
                continue
            try:
                info = yf.Ticker(t).get_info()
                cache[t] = info.get("longName") or info.get("shortName") or t
            except Exception:
                cache[t] = t
            changed = True
        if changed:
            NAMES_CACHE.write_text(json.dumps(cache, indent=1))
    return lambda t: NAMES.get(t) or CURATED.get(t) or cache.get(t) or t


def main():
    import sys
    PROD_DIR.mkdir(parents=True, exist_ok=True)
    data = fetch_all()
    uni = load_universe()
    today = dt.date.today().isoformat()
    name_of = build_name_map(list(data.keys()), fetch="--names" in sys.argv)

    # ---- per-product cards + price files ----
    index, cards = [], {}
    for t, df in data.items():
        try:
            card = analyze(t, df)
        except Exception:
            continue
        card["name"] = name_of(t)
        cards[t] = card
        index.append({"sym": t, "name": name_of(t), "market": market_of(t),
                      "action": card["action"], "regime": card["regime"],
                      "price": card["price"]})
        # price history (last ~500 sessions) for the chart
        hist = df["Close"].dropna().tail(500)
        prices = [[d.strftime("%Y-%m-%d"), round(float(v), 4)] for d, v in hist.items()]
        sma200 = df["Close"].rolling(200).mean().dropna().tail(500)
        s200 = [[d.strftime("%Y-%m-%d"), round(float(v), 4)] for d, v in sma200.items()]
        (PROD_DIR / f"{safe_name(t)}.json").write_text(json.dumps(
            {"sym": t, "name": name_of(t), "market": market_of(t),
             "prices": prices, "sma200": s200, "card": card}))

    # ---- today's actions (tradeable BUYs) ----
    actions = scan_actions(data)[:20]
    for c in actions:
        c["name"] = name_of(c["symbol"])

    # ---- current positions (from live state) ----
    state = load_json(POS_FILE, {"cash": {}, "positions": {}})
    positions = []
    for sym, pos in state.get("positions", {}).items():
        last = cards.get(sym, {}).get("price")
        pnl = (last / pos["entry_px"] - 1) * 100 if last and pos.get("entry_px") else None
        positions.append({"sym": sym, "name": name_of(sym),
                          "entry": pos.get("entry_px"), "entry_date": pos.get("entry_date"),
                          "last": last, "pnl_pct": round(pnl, 1) if pnl is not None else None,
                          "target": pos.get("target"), "stop": pos.get("stop"),
                          "sleeve": "CRY" if market_of(sym) == "CRYPTO" else "DIP"})

    # ---- historical actions (trade log) ----
    history = load_json(LOG_FILE, [])

    payload = {
        "generated": today,
        "product": "P1 + SMA200  (70% equity dip + 30% crypto trend)",
        "headline": {"win": "51.7%", "cagr": "29.8%", "maxdd": "-24.3%",
                     "grows": "HK$150k -> 2.78M (11.2y backtest)"},
        "universe_count": len(uni["tickers"]),
        "actions": actions,
        "positions": positions,
        "history": history,
        "index": sorted(index, key=lambda x: (x["market"], x["sym"])),
    }
    (DOCS / "data.json").write_text(json.dumps(payload, indent=1))
    print(f"dashboard built: {len(index)} products, {len(actions)} actions, "
          f"{len(positions)} positions -> {DOCS/'data.json'}")


if __name__ == "__main__":
    main()
