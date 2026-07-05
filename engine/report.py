"""Daily report generator (spec items 2, 6, 10).

Produces reports/daily_YYYY-MM-DD.md with:
- portfolio state: holdings with entry, live target & cut-loss (re-ratcheted daily)
- today's best new candidates with evidence
- watchlist / regime overview per market
- universe changes

State (open positions) persists in data/positions.json so targets/stops are
anchored to real entries, never re-priced (METHODOLOGY §6).
"""
import datetime as dt
import json

import pandas as pd

from config import DATA_DIR, REPORT_DIR, STRAT, CRY_STRAT, SLEEVES, \
    MAX_POSITIONS, START_CAPITAL_HKD
from indicators import add_features
from strategy import entry_signal, exit_signal, stop_price, target_price, \
    candidate_score, evidence
from universe import load_universe, market_of, NAMES

POS_FILE = DATA_DIR / "positions.json"


def sleeve_of(ticker: str) -> str:
    return "CRY" if market_of(ticker) == "CRYPTO" else "DIP"


def load_state() -> dict:
    """{"cash": {"DIP": x, "CRY": y}, "positions": {ticker: {...}}}."""
    if POS_FILE.exists():
        st = json.loads(POS_FILE.read_text())
        if "positions" not in st:  # migrate legacy flat format
            st = {"cash": START_CAPITAL_HKD, "positions": st}
        if not isinstance(st["cash"], dict):  # migrate single ledger -> sleeves
            st["cash"] = {s: st["cash"] * cfg["weight"]
                          for s, cfg in SLEEVES.items()}
        return st
    return {"cash": {s: START_CAPITAL_HKD * cfg["weight"]
                     for s, cfg in SLEEVES.items()}, "positions": {}}


def save_state(st: dict):
    POS_FILE.write_text(json.dumps(st, indent=2))


def load_positions() -> dict:
    return load_state()["positions"]


def label(t: str) -> str:
    return f"{NAMES.get(t, t)} ({t})" if t in NAMES else t


def generate_report(data: dict[str, pd.DataFrame], p: dict = STRAT) -> str:
    REPORT_DIR.mkdir(exist_ok=True)
    today = dt.date.today().isoformat()
    feats = {t: add_features(df, p) for t, df in data.items()}
    state = load_state()
    positions, cash = state["positions"], state["cash"]
    held = {s: sum(1 for t in positions if sleeve_of(t) == s) for s in SLEEVES}
    uni = load_universe()

    sleeve_line = " · ".join(
        f"{s} {int(cfg['weight']*100)}%: cash HKD {cash[s]:,.0f}, "
        f"{held[s]}/{cfg['slots']} slots, next size HKD "
        f"{cash[s] / max(1, cfg['slots'] - held[s]):,.0f}"
        for s, cfg in SLEEVES.items())
    lines = [f"# Daily Strategy Report — {today}",
             "",
             f"Universe: {len(uni['tickers'])} products (last updated {uni['updated']}) · "
             f"long-only, no margin · two sleeves, monthly rebalance",
             "",
             sleeve_line,
             ""]
    if dt.date.today().day <= 3:
        total = sum(cash.values()) + sum(pos.get("notional", 0) for pos in positions.values())
        lines += [f"**REBALANCE (month start):** total ≈ HKD {total:,.0f} → targets: "
                  + ", ".join(f"{s} HKD {total*cfg['weight']:,.0f}"
                              for s, cfg in SLEEVES.items())
                  + ". Move cash between sleeves toward targets (do not force-sell positions).",
                  ""]

    # ---- holdings management ----
    lines.append("## Current holdings — daily target / cut-loss update")
    if not positions:
        lines.append("*(no open positions)*")
    else:
        lines.append("| Product | Entry date | Entry | Last | P&L % | Target | Cut-loss | Action |")
        lines.append("|---|---|--:|--:|--:|--:|--:|---|")
        for t, pos in list(positions.items()):
            if t not in feats:
                continue
            row = feats[t].iloc[-1]
            last = float(row["Close"])
            pnl = last / pos["entry_px"] - 1
            if sleeve_of(t) == "CRY":
                # trend position: ratchet the chandelier trail, no profit target
                pos["hw"] = max(pos.get("hw", pos["entry_px"]), last)
                trail = pos["hw"] - CRY_STRAT["trail_atr_mult"] * float(row["atr"])
                pos["stop"] = max(pos["stop"], trail)
                stp, tgt_txt = pos["stop"], "trend (ride)"
                closes = feats[t]["Close"].iloc[-CRY_STRAT["exit_consecutive"]:]
                smas = feats[t]["sma_fast"].iloc[-CRY_STRAT["exit_consecutive"]:]
                action = "HOLD (keep GTC stop resting; trail updates daily)"
                if last <= stp:
                    action = "**SELL — trail stop hit**"
                elif bool((closes < smas).all()):
                    action = "**SELL next open — trend exit (2 closes below SMA50)**"
            else:
                tgt = pos.get("target") or target_price(pos["entry_px"], pos["entry_atr"], p)
                tgt_txt = f"{tgt:.2f}"
                stp = pos["stop"]
                action = "HOLD (keep GTC target + stop orders resting)"
                if last >= tgt:
                    action = "**TARGET reached — sell if the resting order hasn't filled**"
                elif last <= stp:
                    action = "**SELL — stop hit**"
                elif exit_signal(row, p):
                    action = "**SELL next open — backup exit signal (RSI snap-back complete)**"
                elif (dt.date.today() - dt.date.fromisoformat(pos["entry_date"])).days > p["max_hold_days"] * 1.6:
                    action = "**SELL next open — time stop**"
            lines.append(f"| {label(t)} | {pos['entry_date']} | {pos['entry_px']:.2f} | "
                         f"{last:.2f} | {pnl * 100:+.1f}% | {tgt_txt} | {stp:.2f} | {action} |")
        save_state(state)  # persist ratcheted trail stops
    lines.append("")

    # ---- new candidates (tradeable) + monitor-only signals ----
    slots = sum(cfg["slots"] - held[s] for s, cfg in SLEEVES.items())
    excluded = set(p.get("exclude_markets", ())) - {"CRYPTO"}
    cands, monitor_only = [], []
    for t, df in feats.items():
        if t in positions:
            continue
        row = df.iloc[-1]
        if pd.isna(row["sma_trend"]) or pd.isna(row["atr"]):
            continue
        if market_of(t) == "CRYPTO":
            # CRY sleeve: trend entry (regime on)
            if row["Close"] > row["sma_trend"] and row["sma_fast"] > row["sma_trend"]:
                cands.append((1000.0, t, row))  # trend entries outrank dips (own sleeve)
        elif entry_signal(row, p):
            if market_of(t) in excluded:
                monitor_only.append(t)
            else:
                cands.append((candidate_score(row), t, row))
    cands.sort(key=lambda x: -x[0])

    lines.append(f"## Best new BUY candidates today ({len(cands)} signals, {slots} free slots)")
    if not cands:
        lines.append("*No entry signals today — standing aside is a position "
                     "(playbook §3: never force a trade without an edge).*")
    for score, t, row in cands[:max(slots, 5)]:
        px = float(row["Close"])
        a = float(row["atr"])
        s = sleeve_of(t)
        sleeve_free = SLEEVES[s]["slots"] - held[s]
        act = f"→ BUY at next open ({s} sleeve)" if sleeve_free > 0 \
            else f"(watch — {s} sleeve full)"
        lines.append("")
        lines.append(f"### {label(t)} — {act}")
        if s == "CRY":
            trail = px - CRY_STRAT["trail_atr_mult"] * a
            lines.append(f"- Reference price: {px:.2f} · Exit: ride trend, "
                         f"chandelier trail from {trail:.2f} "
                         f"({(trail / px - 1) * 100:.1f}%), ratchets up daily; "
                         f"or 2 closes below SMA50")
            lines.append("- Evidence:")
            lines.append(f"  - Crypto uptrend regime: price above 200-day average, "
                         f"50-day above 200-day (trend-sleeve entry)")
            if pd.notna(row.get("mom_90")):
                lines.append(f"  - 90-day momentum: {row['mom_90'] * 100:+.1f}%")
        else:
            lines.append(f"- Reference price: {px:.2f} · Target: {target_price(px, a, p):.2f} "
                         f"(+{(target_price(px, a, p) / px - 1) * 100:.1f}%) · Cut-loss: "
                         f"{stop_price(px, a, p):.2f} ({(stop_price(px, a, p) / px - 1) * 100:.1f}%)")
            lines.append("- Evidence:")
            for ev in evidence(row, t):
                lines.append(f"  - {ev}")

    if monitor_only:
        lines += ["", "### Monitor-only signals (not traded: futures-roll/index artifacts)",
                  ", ".join(label(t) for t in monitor_only)]

    # ---- market regime overview ----
    lines += ["", "## Market regime overview", "",
              "| Market | In uptrend (regime pass) | Oversold dips today |", "|---|--:|--:|"]
    by_m = {}
    for t, df in feats.items():
        row = df.iloc[-1]
        if pd.isna(row["sma_trend"]):
            continue
        m = market_of(t)
        up = row["Close"] > row["sma_trend"] and row["sma_fast"] > row["sma_trend"]
        sig = entry_signal(row, p)
        a, b, n = by_m.get(m, (0, 0, 0))
        by_m[m] = (a + up, b + sig, n + 1)
    for m, (up, sig, n) in sorted(by_m.items()):
        lines.append(f"| {m} | {up}/{n} | {sig} |")

    if uni["added_log"]:
        recent = uni["added_log"][-1]
        lines += ["", f"**Universe change ({recent['date']}):** added {', '.join(recent['tickers'])}"]

    lines += ["", "---",
              "*Workflow: BUY at next open, then immediately place two resting GTC orders — "
              "a SELL LIMIT at the target and a SELL STOP at the cut-loss. Record fills with "
              "`python position_cli.py buy/sell TICKER PRICE` so this report tracks them. "
              "Long only, no margin. Two-sleeve R2 product (70% equity dip + 30% crypto "
              "trend, monthly rebalance): blended win 62.8%, CAGR 23.7%, maxDD −26.2% "
              "over 11.2y net of costs; not financial advice.*"]

    out = "\n".join(lines)
    path = REPORT_DIR / f"daily_{today}.md"
    path.write_text(out, encoding="utf-8")
    print(f"Report written: {path}")
    return out


if __name__ == "__main__":
    from data_fetch import fetch_all
    generate_report(fetch_all())
