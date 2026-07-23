"""UK Capital Gains computation from the fills ledger.

Implements HMRC share identification for individuals (TCGA 1992 s105/s106A;
CG51550+; HS284): disposals match acquisitions in strict order
  1) same-day, 2) 30 days AFTER the disposal (earliest first, "bed & breakfast"),
  3) Section 104 pool at weighted-average cost — maintained in GBP, with each
acquisition entering at (cost + buy commission) converted at its OWN date's rate.
Gains are computed in GBP per-leg (never net-local-then-convert).

FX cash conversions (secType CASH) are outside CGT for individuals since
6 Apr 2012 (TCGA 1992 s252) — listed for reference only.

Ledger rows (data/fills_ledger.jsonl), one JSON object per line:
  {execId, ts:"YYYY-MM-DD HH:MM", date:"YYYY-MM-DD", symbol, con_id, sec_type,
   side:"BOT"|"SLD", qty, price, ccy, commission, commission_ccy,
   gbp_rate (GBP per 1 unit of ccy), gbp_rate_commission, source:"api"|"estimate",
   name, exchange, flags:[...]}
Rows with source=="estimate" or missing gbp_rate taint everything they touch:
those disposals are excluded from headline totals (basis_quality ESTIMATED).
"""
import json
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LEDGER = REPO / "data" / "fills_ledger.jsonl"
REPORT = REPO / "data" / "tax_report.json"
PROVISIONAL_DAYS = 31


def tax_year_of(d):
    y = d.year if (d.month, d.day) >= (4, 6) else d.year - 1
    return f"{y}/{str(y + 1)[2:]}"


def _load_ledger():
    rows = {}
    if LEDGER.exists():
        for line in LEDGER.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                rows[r["execId"]] = r          # later lines win (corrections)
            except Exception:
                continue
    return list(rows.values())


def _gbp(row, field="price"):
    """GBP value of qty*price plus nothing else; None if no usable rate."""
    rate = row.get("gbp_rate")
    if not rate or rate <= 0:
        return None
    return row["qty"] * row[field] * rate


def _fee_gbp(row):
    rate = row.get("gbp_rate_commission") or row.get("gbp_rate") or 0
    com = row.get("commission") or 0
    return abs(com) * rate if rate > 0 else None


def compute(rows=None, today=None):
    """Pure computation: ledger rows -> report dict."""
    rows = _load_ledger() if rows is None else rows
    today = today or date.today()
    stocks = [r for r in rows if r.get("sec_type") != "CASH"]
    fx = [r for r in rows if r.get("sec_type") == "CASH"]

    by_sym = {}
    for r in stocks:
        by_sym.setdefault(r.get("symbol"), []).append(r)

    disposals_out, open_out = [], []

    for sym, rs in sorted(by_sym.items()):
        acqs = sorted([dict(r, remaining=r["qty"]) for r in rs if r["side"] == "BOT"],
                      key=lambda r: (r["date"], r.get("ts", "")))
        disps = sorted([dict(r, remaining=r["qty"]) for r in rs if r["side"] == "SLD"],
                       key=lambda r: (r["date"], r.get("ts", "")))
        matches = {id(d): [] for d in disps}

        def take(d, a, rule):
            q = min(d["remaining"], a["remaining"])
            if q <= 0:
                return
            d["remaining"] -= q
            a["remaining"] -= q
            rate = a.get("gbp_rate") or 0
            fee = _fee_gbp(a)
            cost = (q * a["price"] * rate + (fee or 0) * q / a["qty"]) if rate > 0 else None
            est = a.get("source") == "estimate" or cost is None
            matches[id(d)].append({"rule": rule, "qty": q, "acq_date": a["date"],
                                   "cost_gbp": cost, "estimated": est,
                                   "acq_price": a["price"], "acq_rate": rate or None})

        # 1) same-day  2) 30-day after (earliest first)
        for d in disps:
            dd = date.fromisoformat(d["date"])
            for a in acqs:
                if a["date"] == d["date"]:
                    take(d, a, "same_day")
            for a in acqs:
                ad = date.fromisoformat(a["date"])
                if dd < ad <= dd + timedelta(days=30):
                    take(d, a, "30_day")

        # 3) chronological S104 pool over remaining quantities
        events = ([("A", date.fromisoformat(a["date"]), a) for a in acqs] +
                  [("D", date.fromisoformat(d["date"]), d) for d in disps])
        events.sort(key=lambda e: (e[1], 0 if e[0] == "A" else 1))
        pool_qty, pool_cost, pool_est = 0.0, 0.0, False
        for kind, dt_, r in events:
            if kind == "A" and r["remaining"] > 0:
                rate = r.get("gbp_rate") or 0
                fee = _fee_gbp(r)
                if rate > 0 and fee is not None:
                    add_cost = r["remaining"] * r["price"] * rate + fee * r["remaining"] / r["qty"]
                else:
                    add_cost = 0.0
                    pool_est = True
                if r.get("source") == "estimate":
                    pool_est = True
                pool_qty += r["remaining"]
                pool_cost += add_cost
                r["remaining"] = 0
            elif kind == "D" and r["remaining"] > 0:
                q = min(r["remaining"], pool_qty)
                if q > 0:
                    avg = pool_cost / pool_qty if pool_qty else 0.0
                    matches[id(r)].append({"rule": "s104_pool", "qty": q,
                                           "acq_date": "S104 pool",
                                           "cost_gbp": q * avg,
                                           "estimated": pool_est,
                                           "acq_price": None, "acq_rate": None})
                    pool_qty -= q
                    pool_cost -= q * avg
                    r["remaining"] -= q
                if r["remaining"] > 0:          # nothing left to match against
                    matches[id(r)].append({"rule": "unmatched", "qty": r["remaining"],
                                           "acq_date": "unknown", "cost_gbp": None,
                                           "estimated": True,
                                           "acq_price": None, "acq_rate": None})
                    r["remaining"] = 0

        for d in disps:
            dd = date.fromisoformat(d["date"])
            slices = matches[id(d)]
            rate = d.get("gbp_rate") or 0
            proceeds = d["qty"] * d["price"] * rate if rate > 0 else None
            sell_fee = _fee_gbp(d)
            est = (d.get("source") == "estimate" or proceeds is None or sell_fee is None
                   or any(s["estimated"] for s in slices))
            cost = sum(s["cost_gbp"] or 0 for s in slices)
            gain = (proceeds - (sell_fee or 0) - cost) if (proceeds is not None) else None
            disposals_out.append({
                "symbol": sym, "name": d.get("name", ""), "ccy": d.get("ccy"),
                "date": d["date"], "tax_year": tax_year_of(dd), "qty": d["qty"],
                "price_local": d["price"], "proceeds_local": round(d["qty"] * d["price"], 2),
                "gbp_rate": rate or None,
                "proceeds_gbp": round(proceeds, 2) if proceeds is not None else None,
                "sell_fee_gbp": round(sell_fee, 2) if sell_fee is not None else None,
                "cost_gbp": round(cost, 2),
                "gain_gbp": round(gain, 2) if gain is not None else None,
                "rules": sorted({s["rule"] for s in slices}),
                "slices": [{k: (round(v, 2) if isinstance(v, float) else v)
                            for k, v in s.items()} for s in slices],
                "basis_quality": "ESTIMATED" if est else "captured",
                "provisional_until": (dd + timedelta(days=PROVISIONAL_DAYS)).isoformat()
                    if (today - dd).days < PROVISIONAL_DAYS else None,
            })

        if pool_qty > 0.0001:
            open_out.append({"symbol": sym, "qty": round(pool_qty, 4),
                             "cost_gbp": round(pool_cost, 2),
                             "estimated": pool_est})

    # ---- per-tax-year summaries (captured-basis only in headline) ----
    years = {}
    for d in disposals_out:
        y = years.setdefault(d["tax_year"], {"disposals": 0, "proceeds_gbp": 0.0,
                                             "gains_gbp": 0.0, "losses_gbp": 0.0,
                                             "excluded_estimated": 0,
                                             "excluded_est_net_gbp": 0.0})
        y["disposals"] += 1
        if d["basis_quality"] == "captured" and d["gain_gbp"] is not None:
            y["proceeds_gbp"] += d["proceeds_gbp"]
            if d["gain_gbp"] >= 0:
                y["gains_gbp"] += d["gain_gbp"]
            else:
                y["losses_gbp"] += -d["gain_gbp"]
        else:
            y["excluded_estimated"] += 1
            if d["gain_gbp"] is not None:
                y["excluded_est_net_gbp"] += d["gain_gbp"]
    for y in years.values():
        for k in ("proceeds_gbp", "gains_gbp", "losses_gbp", "excluded_est_net_gbp"):
            y[k] = round(y[k], 2)
        y["net_gain_gbp"] = round(y["gains_gbp"] - y["losses_gbp"], 2)

    fx_out = [{"date": r["date"], "symbol": r.get("symbol"), "qty": r["qty"],
               "price": r["price"], "ccy": r.get("ccy"),
               "gbp_value": round(_gbp(r), 2) if _gbp(r) else None,
               "side": r["side"]} for r in sorted(fx, key=lambda r: r["date"])]

    cov = min((r["date"] for r in rows if r.get("source") != "estimate"), default=None)
    return {"generated": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
            "coverage_start": cov, "years": years, "disposals": disposals_out,
            "open_positions": open_out, "fx_conversions": fx_out}


def build_report():
    rep = compute()
    REPORT.write_text(json.dumps(rep, indent=1), encoding="utf-8")
    return rep


if __name__ == "__main__":
    r = build_report()
    print("tax_report.json:", len(r["disposals"]), "disposals,",
          len(r["open_positions"]), "open,", len(r["fx_conversions"]), "fx rows")
