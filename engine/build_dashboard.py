"""Build the static dashboard data for GitHub Pages.

Writes:
  docs/data.json                 — actions, positions, history, product index
  docs/products/<safe>.json      — per-product price history + analysis card
Everything the mobile dashboard needs; refreshed daily by GitHub Actions.
"""
import json
import math
import datetime as dt
from pathlib import Path

import pandas as pd

from config import ROOT, DATA_DIR, SLEEVES, START_CAPITAL_HKD, MODEL
from data_fetch import download_started, fetch_all, safe_name, utc_stamp
from universe import load_universe, market_of, NAMES
from company_names import CURATED
from model_d3e1 import e1_asof, facts_for
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


def generated_at(tickers, now=None):
    """The build's "generated_at": when the price download behind these cards
    STARTED, as "YYYY-MM-DDTHH:MM:SSZ" (UTC).

    Why (review 2026-09-17, "The settle check uses the bot's own clock, not the
    time the card was built"): "generated" is a date only, so the bot cannot
    tell a card downloaded at 13:45 JST, mid-session, from one downloaded after
    the close, and the hourly build is often hours late or skipped. The DOWNLOAD
    start, not the build time, is what bounds how fresh the prices are.

    Falls back to the build time - `now` - only when the start is unknown
    (download_started gives None), or would be later than the build itself,
    and says so: a build time is later than the prices, the less safe way to
    be wrong, so it must never pass silently."""
    built = utc_stamp(now)
    started = download_started(tickers)
    if started is None or started > built:
        print(f"!! generated_at: download start "
              f"{'unavailable' if started is None else started + ' is after the build'}"
              f" - falling back to the BUILD time {built}, later than the prices")
        return built
    return started


def e1_resolver(sec_dir):
    """The E1 resolver analyze() and scan_actions() share: e1(ticker, bar date)
    -> model_d3e1.e1_decision dict, from the SEC files engine/sec_fetch.py
    saved under sec_dir in its own CI step - never the network.

    Memoised per (ticker, date), so the card loop and scan_actions see one
    answer per name, and each name's filings are parsed once. It never
    raises: a missing, unreadable or malformed file is "unscored", and an
    unscored dip is KEPT (plain E) - the research's rule, and what makes an
    SEC outage fall back to today's behaviour. `seen` holds every answer
    given, for the build's report (payload["model"])."""
    seen = {}

    def e1(ticker, asof):
        key = (ticker, str(asof))
        if key not in seen:
            try:
                df, concept = facts_for(sec_dir, ticker)
                r = e1_asof(df, asof)
                r["concept"] = concept
            except Exception as e:
                r = {"decision": "unscored", "growth": None, "qend": None,
                     "why": f"SEC file unreadable ({type(e).__name__})", "concept": None}
            seen[key] = r
        return seen[key]

    e1.seen = seen
    return e1


def sec_status(sec_dir):
    """What the SEC step (engine/sec_fetch.py) reported for this build, from
    its _status.json: {"state", "message", "ran_at"}. "missing" when the step
    left nothing - it failed, timed out, or did not run - so every US dip is
    unscored and kept. Never raises."""
    try:
        st = json.loads((Path(sec_dir) / "_status.json").read_text(encoding="utf-8"))
        if not isinstance(st, dict):
            raise ValueError("not an object")
        return {"state": str(st.get("state", "unknown")),
                "message": str(st.get("message", "")),
                "ran_at": st.get("ran_at")}
    except Exception as e:
        return {"state": "missing", "ran_at": None,
                "message": f"no usable report from the SEC step ({type(e).__name__}): "
                           f"every US dip is unscored and kept (plain E)"}


def model_report(model, e1, actions, status):
    """payload["model"]: which signals ran and what E1 decided (an extra key
    only - nothing the bot reads). Prints a ::warning:: when US dips went
    unscored for want of SEC data, so a broken SEC step shows in the log."""
    if model != "D3E1":
        return {"name": "E", "code": model}
    seen = getattr(e1, "seen", {})
    by = {"keep": [], "block": [], "unscored": []}
    for (t, _), r in sorted(seen.items()):
        by.setdefault(r.get("decision", "unscored"), []).append(t)
    no_data = sorted(t for (t, _), r in seen.items()
                     if r.get("decision") == "unscored"
                     and str(r.get("why", "")).startswith(("no SEC data", "SEC file")))
    if no_data:
        print(f"::warning::E1: {len(no_data)} US dip(s) unscored for want of SEC data, "
              f"kept as plain E: {', '.join(no_data)} (SEC step: {status['state']} - "
              f"{status['message']})")
    return {"name": "D3+E1", "code": model,
            "e1": {"checked": len(seen), "kept": len(by["keep"]),
                   "blocked": len(by["block"]), "unscored": len(by["unscored"]),
                   "blocked_symbols": by["block"], "unscored_symbols": by["unscored"],
                   "sec_status": status},
            "d3_actions": sum(1 for c in actions if c.get("signal") == "D3")}


def model_headline(h):
    """The header's figures from data/model_headline.json (MODEL "D3E1"), or
    None when the file is absent or malformed - the caller then falls back to
    the old arm-E reading. Never raises.

    The figure is the RESEARCH engine's 70% stocks / 30% crypto-trend blend for
    D3+E1 at 15 slots and the HK$50k cap. The owner chose it (2026-10-26) with
    the wording below: the crypto sleeve is on hold and the bot trades stocks
    only, so `basis` must say both. The same file's stocks_only block (22.5%
    CAGR) and the live-faithful simulation are lower."""
    try:
        win, cagr, dd = float(h["win"]), float(h["cagr"]), float(h["dd"])
        start, final, years = float(h["start_hkd"]), float(h["final_hkd"]), float(h["years"])
        slots, cap = int(h["slots"]), float(h["cap_hkd"])
        if not all(map(math.isfinite, (win, cagr, dd, start, final, years, cap))):
            return None
        out = {"win": f"{win*100:.1f}%", "cagr": f"{cagr*100:.1f}%",
                "maxdd": f"{dd*100:.1f}%",
                "grows": f"HK${start/1e3:.0f}k -> {final/1e6:.2f}M ({years:.1f}y backtest)",
                "years": f"{years:.1f}",
                # MAINTENANCE: the blend wording is fixed text - a study with
                # another blend must change it, or the header will misdescribe it
                "basis": f"research engine: 70% stocks + 30% crypto (crypto on hold)"
                         f" · {slots} slots · HK${cap/1e3:.0f}k cap"}
        # The same research run without the crypto sleeve - what the bot actually
        # trades. The page's note sets it beside the 70/30 header so the header
        # is never read as the bot's own expectation (review 2026-10-26). Optional:
        # a file without the block simply has no such sentence.
        so = h.get("stocks_only") or {}
        try:
            sw, sc, sd = float(so["win"]), float(so["cagr"]), float(so["dd"])
            if all(map(math.isfinite, (sw, sc, sd))):
                out["stocks_only"] = (f"win {sw*100:.1f}%, CAGR {sc*100:.1f}%, "
                                      f"maxDD {sd*100:.1f}%")
        except Exception:
            pass
        return out
    except Exception:
        return None


def main():
    import sys
    PROD_DIR.mkdir(parents=True, exist_ok=True)
    data = fetch_all()
    uni = load_universe()
    today = dt.date.today().isoformat()
    # One value for data.json and every card, fixed before the first is written.
    stamp = generated_at(data.keys())
    name_of = build_name_map(list(data.keys()), fetch="--names" in sys.argv)
    # E1 reads only what the SEC step saved (data/sec/, gitignored, fresh on
    # every CI run); one memoised resolver for the cards and the actions alike
    sec_dir = DATA_DIR / "sec"
    e1 = e1_resolver(sec_dir) if MODEL == "D3E1" else None

    # ---- per-product cards + price files ----
    index, cards = [], {}
    for t, df in data.items():
        try:
            card = analyze(t, df, e1=e1)
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
             "generated_at": stamp,
             "prices": prices, "sma200": s200, "card": card}))

    # ---- self-heal the universe: strike & drop tickers with no analyzable
    # data (delisted / renamed on Yahoo / junk imports); held names protected
    from universe import prune_dead
    bot_state = load_json(DATA_DIR / "bot_state.json", {})
    held_bases = {str(p.get(k, "")) for p in bot_state.get("positions", [])
                  if isinstance(p, dict) for k in ("symbol", "ib_symbol")}
    uni = prune_dead(data, {i["sym"] for i in index}, held_bases)

    # ---- today's actions (tradeable BUYs) ----
    # written in their final order - tier (E, then D3), then score, then raw
    # 90-day momentum - because the digest lists them in file order and the
    # bot ranks them the same way. The cut to 20 comes AFTER that sort, so
    # when more than 20 tie at the top score the strongest momentum is kept.
    actions = scan_actions(data, e1=e1)[:20]
    for c in actions:
        c["name"] = name_of(c["symbol"])
    try:                                   # a report must never fail the build
        model = model_report(MODEL, e1, actions, sec_status(sec_dir))
    except Exception as e:
        print(f"::warning::model report failed ({type(e).__name__}) - cards unaffected")
        model = {"name": "D3+E1" if MODEL == "D3E1" else "E", "code": MODEL}
    # Each home index against its 100-day average, on its last FINISHED bar
    # (owner decision 2026-10-26: the per-country market filter, LOG ONLY -
    # ib_bot reads it in shadow mode and sends the same orders either way).
    # From the frames already loaded, never a download. An extra key only, and
    # never a reason to fail the build: market_trend() itself never raises, and
    # a market it cannot vouch for is simply absent (the bot: "unknown").
    # Imported here, inside the guard: even a broken module costs only the key.
    try:
        from market_trend import market_trend
        trend = market_trend(data)
        print("market trend (log only): " + (", ".join(
            f"{m} {'below' if r['below'] else 'above'} {r['pct_vs_sma100']:+.2f}% "
            f"(bar {r['bar']})" for m, r in trend.items()) or "no market judged"))
    except Exception as e:
        print(f"::warning::market trend not built ({type(e).__name__}) - "
              f"data.json goes out without it")
        trend = {}

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

    # ---- historical actions (trade log) + backtest trade history ----
    history = load_json(LOG_FILE, [])
    backtest_trades = load_json(DATA_DIR / "backtest_trades.json", [])

    # recent monitoring-list changes (daily add/remove audit trail)
    changes = []
    for log, verb in ((uni.get("added_log", []), "ADDED"),
                      (uni.get("removed_log", []), "REMOVED")):
        for entry in log[-3:]:
            ts = entry["tickers"]
            shown = ts[:12] + ([f"… +{len(ts) - 12} more"] if len(ts) > 12 else [])
            changes.append({"date": entry["date"], "verb": verb, "tickers": shown,
                            "via": entry.get("via", "")})
    changes.sort(key=lambda x: x["date"], reverse=True)

    # Headline: what the bot ACTUALLY does, not what the validated engine does.
    # The validated run (revalidation.json) takes a stop the moment it is hit,
    # intraday; the live bot tests the stop on the CLOSE and sells at the next
    # open, with a 60-bar time stop. The exit-timing study measured that
    # difference on one frozen database: 31.4% validated against 30.1% for the
    # bot's own rules, combined 13+2. The operator asked for the header to show
    # the truth about what runs (2026-09-21), so it reads the live-rules arm and
    # falls back to the validated run only if that arm is missing.
    #
    # Since 2026-10-26 (owner decision), with MODEL "D3E1" the header shows the
    # research engine's 70/30 figure for D3+E1 at 15 slots and the HK$50k cap,
    # from data/model_headline.json (computed 2026-10-26: win 51.6%, CAGR
    # 31.8%, maxDD -28.7%, 150k -> 3.55M over 11.5 years). It is a RESEARCH
    # number with a 30% crypto sleeve the bot does not trade (on hold), and
    # `basis` says so; stocks only, the same run gives 22.5% / -26.9%, and the
    # live-faithful simulation less again. The arm-E reading below is the
    # fallback - MODEL "E", or that file missing or malformed.
    # MAINTENANCE: both files are study results that no build refreshes.
    # model_headline.json comes from the 2026-10-26 research run (fresh data to
    # 2026-10-23); re-run that study when D3, E1, the slots or the cap change.
    # exit_timing_test.json: re-run engine/research_exit_timing.py whenever the
    # D config or revalidation changes. Otherwise the header describes an old run.
    headline = None
    if MODEL == "D3E1":
        try:
            headline = model_headline(load_json(DATA_DIR / "model_headline.json", None))
        except Exception:
            headline = None              # malformed JSON: fall back, never fail
        if headline is None:
            print("::warning::data/model_headline.json missing or malformed - "
                  "header falls back to the arm-E figures")
    if headline is None:
        et = load_json(DATA_DIR / "exit_timing_test.json", {})
        arm = next((v for k, v in (et.items() if isinstance(et, dict) else [])
                    if str(k).startswith("E ")), None)
        reval = load_json(DATA_DIR / "revalidation.json", [])
        live = next((r for r in reval if str(r.get("tag", "")).startswith("D ")), None)
        c = (arm or {}).get("combined") or ((live or {}).get("combined"))
        basis = "the bot's own exit rules" if (arm or {}).get("combined") else "validated engine"
        if c:
            headline = {"win": f"{c['win']*100:.1f}%", "cagr": f"{c['cagr']*100:.1f}%",
                        "maxdd": f"{c['dd']*100:.1f}%",
                        "grows": f"HK$150k -> {c['final']/1e6:.2f}M (11.2y backtest)",
                        "years": "11.2", "basis": basis}
        else:
            headline = {"win": "n/a", "cagr": "n/a", "maxdd": "n/a", "grows": "n/a",
                        "years": "n/a", "basis": "n/a"}

    payload = {
        "generated": today,
        # when this build's price download STARTED, UTC - see generated_at()
        "generated_at": stamp,
        "product": ("D3+E1: score>60 dips (US: EPS growing) + 0.618 holds · 15 positions"
                    if MODEL == "D3E1" else
                    "D: score>60 dips + crypto trend · 15 positions (13+2)"),
        "headline": headline,
        # which stock signals ran and what E1 decided - see model_report()
        "model": model,
        # monitored = products with analyzable data (matches the Search list);
        # listed = universe file size — any gap is shown on the dashboard and
        # self-heals via prune_dead (5 no-data strikes -> auto-removed)
        "universe_count": len(index),
        "universe_listed": len(uni["tickers"]),
        "universe_updated": uni.get("updated"),
        "universe_changes": changes[:6],
        "actions": actions,
        "positions": positions,
        "history": history,
        "backtest_trades": backtest_trades,
        "add_reasons": uni.get("add_reasons", {}),
        "index": sorted(index, key=lambda x: (x["market"], x["sym"])),
    }
    if trend:            # no market could be judged: no key, never an empty one
        payload["market_trend"] = trend
    (DOCS / "data.json").write_text(json.dumps(payload, indent=1))
    print(f"dashboard built: {len(index)} products, {len(actions)} actions, "
          f"{len(positions)} positions, generated_at {stamp} -> {DOCS/'data.json'}")


if __name__ == "__main__":
    main()
