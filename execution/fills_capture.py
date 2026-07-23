"""Append-only fills ledger: captures IB executions + commissions with GBP-rate
stamps, keyed by execId (later corrections overwrite on read). Called from the
bot's publish path so every scheduled run sweeps the day's executions.

On first run it seeds the ledger with the pre-capture go-live week's trades as
source="estimate" rows (basis from IB average cost / known order data) — the tax
engine excludes these from headline totals until proper statement backfill.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LEDGER = REPO / "data" / "fills_ledger.jsonl"

# Pre-capture era (before 23 Jul 2026): approximate records, clearly estimates.
SEED = [
    # entries 20 Jul (fills at/near limit; avg costs from IB positions)
    ("est-dal-b",  "2026-07-20", "DAL",  "STK", "BOT", 21, 84.1971, "USD", "Delta Air Lines"),
    ("est-ms-b",   "2026-07-20", "MS",   "STK", "BOT", 8, 211.2845, "USD", "Morgan Stanley"),
    ("est-crl-b",  "2026-07-20", "CRL",  "STK", "BOT", 8, 219.3350, "USD", "Charles River Labs"),
    ("est-ben-b",  "2026-07-20", "BEN",  "STK", "BOT", 54, 32.4235, "USD", "Franklin Resources"),
    ("est-uri-b",  "2026-07-20", "URI",  "STK", "BOT", 1, 1020.2895, "USD", "United Rentals"),
    ("est-wst-b",  "2026-07-21", "WST",  "STK", "BOT", 25, 350.93, "USD", "West Pharmaceutical"),
    ("est-crwd-b", "2026-07-22", "CRWD", "STK", "BOT", 9, 192.11, "USD", "CrowdStrike"),
    # legacy manual holdings (acquisition dates unknown -> estimates, old date)
    ("est-mc-b",   "2024-01-01", "MC",   "STK", "BOT", 2, 738.325, "EUR", "LVMH"),
    ("est-sqqq-b", "2024-01-01", "SQQQ", "STK", "BOT", 4, 272.9502, "USD", "ProShares UltraPro Short QQQ"),
    ("est-zroz-b", "2024-01-01", "ZROZ", "STK", "BOT", 30, 83.3117, "USD", "PIMCO 25+ Yr Zero"),
    ("est-wen-b",  "2024-01-01", "WEN",  "STK", "BOT", 100, 8.06, "USD", "Wendy's"),
    # exits / trims 22 Jul (fill prices approximate)
    ("est-sqqq-s", "2026-07-22", "SQQQ", "STK", "SLD", 4, 41.23, "USD", "ProShares UltraPro Short QQQ"),
    ("est-zroz-s", "2026-07-22", "ZROZ", "STK", "SLD", 30, 60.00, "USD", "PIMCO 25+ Yr Zero"),
    ("est-wst-s",  "2026-07-22", "WST",  "STK", "SLD", 20, 357.20, "USD", "West Pharmaceutical"),
    ("est-mc-s",   "2026-07-22", "MC",   "STK", "SLD", 2, 483.00, "EUR", "LVMH"),
    # FX conversions (reference only; exempt)
    ("est-fx-jpy", "2026-07-20", "HKD.JPY", "CASH", "SLD", 14208, 20.70, "JPY", "HKD->JPY conversion"),
    ("est-fx-eur", "2026-07-22", "EUR.USD", "CASH", "SLD", 7686, 1.1464, "USD", "EUR->USD auto-sweep"),
]
SEED_RATES = {"USD": 0.7419, "EUR": 0.8490, "HKD": 0.0946, "JPY": 0.00457, "GBP": 1.0}


def _seed_rows():
    out = []
    for execId, d, sym, st, side, qty, px, ccy, name in SEED:
        out.append({"execId": execId, "date": d, "ts": d + " 00:00", "symbol": sym,
                    "con_id": 0, "sec_type": st, "side": side, "qty": qty,
                    "price": px, "ccy": ccy, "commission": 1.0, "commission_ccy": ccy,
                    "gbp_rate": SEED_RATES.get(ccy, 0), "gbp_rate_commission":
                    SEED_RATES.get(ccy, 0), "source": "estimate", "name": name,
                    "exchange": "", "flags": ["seed"]})
    return out


def _append(rows):
    if not rows:
        return 0
    LEDGER.parent.mkdir(exist_ok=True)
    existing = set()
    if LEDGER.exists():
        for line in LEDGER.read_text(encoding="utf-8").splitlines():
            try:
                existing.add(json.loads(line)["execId"])
            except Exception:
                pass
    new = [r for r in rows if r["execId"] not in existing]
    if new:
        with LEDGER.open("a", encoding="utf-8") as f:
            for r in new:
                f.write(json.dumps(r) + "\n")
    return len(new)


def capture(ib, fx_rate_fn):
    """Sweep today's IB executions into the ledger. fx_rate_fn(ccy)->GBP per unit."""
    if not LEDGER.exists():
        n = _append(_seed_rows())
        print(f"[fills] seeded ledger with {n} pre-capture estimate rows")
    rows = []
    try:
        from ib_async import ExecutionFilter
        fills = ib.reqExecutions(ExecutionFilter())
        ib.sleep(2)
        for f in fills:
            ex, c = f.execution, f.contract
            com = getattr(f, "commissionReport", None)
            ccy = c.currency or "USD"
            rate = fx_rate_fn(ccy)
            side = "BOT" if ex.side in ("BOT", "BUY") else "SLD"
            ts = ex.time.strftime("%Y-%m-%d %H:%M") if getattr(ex, "time", None) else ""
            rows.append({
                "execId": ex.execId, "date": (ts or "")[:10] or
                    datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "ts": ts, "symbol": c.symbol, "con_id": getattr(c, "conId", 0),
                "sec_type": getattr(c, "secType", "STK"), "side": side,
                "qty": float(ex.shares), "price": float(ex.price), "ccy": ccy,
                "commission": float(getattr(com, "commission", 0) or 0) if com else 0.0,
                "commission_ccy": getattr(com, "currency", ccy) if com else ccy,
                "gbp_rate": rate or None, "gbp_rate_commission": rate or None,
                "source": "api", "name": "", "exchange": getattr(c, "exchange", ""),
                "flags": [] if rate else ["rate_missing"]})
    except Exception as e:
        print(f"[fills] capture skipped ({e})")
    n = _append(rows)
    if n:
        print(f"[fills] captured {n} new execution(s)")
    return n
